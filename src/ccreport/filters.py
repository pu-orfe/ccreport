"""Deterministic receipt detection for mailbox browse results."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from .connectors.base import MessageHeader, parse_address

RECEIPT_MEDIA_WEIGHT = 3
KEYWORD_WEIGHT = 2
KEYWORD_SCORE_CAP = 4
AMOUNT_WEIGHT = 2
VENDOR_WEIGHT = 2
MARKETING_WEIGHT = -2
CALENDAR_WEIGHT = -2
RECEIPT_THRESHOLD = 3

RECEIPT_KEYWORDS = (
    "receipt",
    "invoice",
    "order confirmation",
    "order",
    "confirmation",
    "payment",
    "purchase",
    "transaction",
    "billed",
    "billing",
    "renewal",
    "subscription",
    "statement",
    "paid",
    "tax invoice",
)

KNOWN_VENDORS: dict[str, str] = {
    "amazon": "Amazon",
    "apple": "Apple",
    "uber": "Uber",
    "lyft": "Lyft",
    "delta": "Delta",
    "united": "United Airlines",
    "american airlines": "American Airlines",
    "aa.com": "American Airlines",
    "marriott": "Marriott",
    "hilton": "Hilton",
    "hyatt": "Hyatt",
    "airbnb": "Airbnb",
    "staples": "Staples",
    "w.b. mason": "W.B. Mason",
    "wbmason": "W.B. Mason",
    "bhphotovideo": "B&H",
    "b&h": "B&H",
    "dell": "Dell",
    "adobe": "Adobe",
    "github": "GitHub",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "microsoft": "Microsoft",
    "zoom": "Zoom",
    "slack": "Slack",
    "dropbox": "Dropbox",
    "overleaf": "Overleaf",
    "springer": "Springer",
    "elsevier": "Elsevier",
    "wiley": "Wiley",
    "jstor": "JSTOR",
    "acm": "ACM",
    "ieee": "IEEE",
    "siam": "SIAM",
    "ams": "AMS",
    "eventbrite": "Eventbrite",
    "doordash": "DoorDash",
    "grubhub": "Grubhub",
    "fedex": "FedEx",
    "ups": "UPS",
    "usps": "USPS",
    "home depot": "Home Depot",
    "homedepot": "Home Depot",
    "best buy": "Best Buy",
    "bestbuy": "Best Buy",
    "newegg": "Newegg",
    "cdw": "CDW",
    "expedia": "Expedia",
    "labyrinth books": "Labyrinth Books",
    "labyrinthbooks": "Labyrinth Books",
    "jammin crepes": "Jammin Crepes",
    "jammincrepes": "Jammin Crepes",
    "princeton": "Princeton University",
}

_CURRENCY_CODES = {"USD", "EUR", "GBP", "JPY"}
_SYMBOL_TO_CODE = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_ZERO_MINOR = {"JPY"}
_AMOUNT_RE = re.compile(
    r"(?<![\w])(?P<open>\()?\s*(?P<neg>-)?\s*"
    r"(?:(?P<prefix_code>USD|EUR|GBP|JPY)\s*)?"
    r"(?P<prefix_sym>[$€£¥])?\s*"
    r"(?P<number>(?:\d{1,3}(?:[,.]\d{3})+|\d+)(?:[,.]\d{2})?)"
    r"\s*(?P<suffix_sym>[$€£¥])?\s*"
    r"(?P<suffix_code>USD|EUR|GBP|JPY)?\s*(?P<close>\))?"
    r"(?![\w%])",
    re.IGNORECASE,
)
_MARKETING_RE = re.compile(
    r"\b(unsubscribe|view in browser|manage preferences)\b|"
    r"\b(no-reply@[^\s<>@]*news|marketing@|newsletter@)",
    re.IGNORECASE,
)
_CALENDAR_RE = re.compile(r"\b(invitation:|accepted:|declined:|canceled event)", re.IGNORECASE)
_NOISE_WORD_RE = re.compile(r"\b(via|no-?reply|billing|receipts?|team)\b", re.IGNORECASE)
_MAIL_SUBDOMAIN_RE = re.compile(r"^(email|mail|e|mailer|notifications|t)\.", re.IGNORECASE)


def load_vendors(path: str | Path) -> dict[str, str]:
    """Load vendor aliases from JSON or plain lines."""
    raw = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() == ".json":
        data = json.loads(raw)
        if isinstance(data, Mapping):
            return {str(k).strip().lower(): str(v).strip() for k, v in data.items() if str(k).strip()}
        if isinstance(data, list):
            return _vendors_from_lines(str(item) for item in data)
        raise ValueError("vendor JSON must be an object or list")
    return _vendors_from_lines(raw.splitlines())


def _vendors_from_lines(lines: Iterable[str]) -> dict[str, str]:
    vendors: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            key, sep, value = line.partition(",")
        key = key.strip().lower()
        value = value.strip() if sep else _title_domain(key)
        if key:
            vendors[key] = value or _title_domain(key)
    return vendors


def score_message(
    header: MessageHeader,
    *,
    vendors: Mapping[str, str] | None = None,
    body_text: str | None = None,
) -> MessageHeader:
    """Populate receipt score, flag, hints, and matched signals on ``header``."""
    vendor_map = dict(KNOWN_VENDORS)
    if vendors:
        vendor_map.update({k.lower(): v for k, v in vendors.items()})

    score = 0
    signals: list[str] = []
    haystack = " ".join(filter(None, [header.subject, header.snippet, body_text]))

    if any(ref.is_probably_receipt_media for ref in header.attachment_refs):
        score += RECEIPT_MEDIA_WEIGHT
        signals.append("receipt-media")

    keyword_hits = _keyword_hits(f"{header.subject} {header.snippet}")
    if keyword_hits:
        keyword_score = min(len(keyword_hits) * KEYWORD_WEIGHT, KEYWORD_SCORE_CAP)
        score += keyword_score
        signals.extend(f"keyword:{hit}" for hit in keyword_hits)

    amount = extract_amount(haystack)
    if amount:
        header.amount_hint_cents, header.currency_hint = amount
        score += AMOUNT_WEIGHT
        signals.append(f"amount:{header.currency_hint}")
    else:
        header.amount_hint_cents = None
        header.currency_hint = None

    vendor = _match_known_vendor(header, vendor_map)
    header.vendor_hint = vendor or extract_vendor(header)
    if vendor:
        score += VENDOR_WEIGHT
        signals.append(f"vendor:{vendor}")

    sender_text = " ".join(filter(None, [header.from_name, header.from_address]))
    if _MARKETING_RE.search(f"{sender_text} {header.subject} {header.snippet} {body_text or ''}"):
        score += MARKETING_WEIGHT
        signals.append("marketing")

    if _CALENDAR_RE.search(f"{header.subject} {header.snippet}"):
        score += CALENDAR_WEIGHT
        signals.append("calendar")

    header.receipt_score = score
    header.likely_receipt = score >= RECEIPT_THRESHOLD
    header.matched_signals = signals
    return header


def extract_amount(text: str | None) -> tuple[int, str] | None:
    """Return the largest currency amount in cents and its ISO currency code."""
    if not text:
        return None
    candidates: list[tuple[int, str]] = []
    for match in _AMOUNT_RE.finditer(text):
        code = _currency_code(match)
        if not code:
            continue
        number = match.group("number")
        if _reject_number(text, match.start(), match.end(), number):
            continue
        cents = _parse_number(number, code)
        if cents is None:
            continue
        if match.group("neg") or (match.group("open") and match.group("close")):
            cents = -cents
        candidates.append((cents, code))
    if not candidates:
        return None
    return max(candidates, key=lambda item: abs(item[0]))


def extract_vendor(header: MessageHeader) -> str | None:
    """Infer a display vendor from known aliases, sender name, or sender domain."""
    known = _match_known_vendor(header, KNOWN_VENDORS)
    if known:
        return known
    cleaned = _clean_sender_name(header.from_name)
    if cleaned:
        return cleaned
    domain = _registrable_domain(header.from_address)
    return _title_domain(domain) if domain else None


def header_matches(
    header: MessageHeader,
    *,
    subject_contains: str | None = None,
    from_contains: str | None = None,
) -> bool:
    """Case-insensitive exact substring match for provider-side search fallback."""
    if subject_contains and subject_contains.casefold() not in header.subject.casefold():
        return False
    if from_contains:
        sender = " ".join(filter(None, [header.from_name, header.from_address]))
        if from_contains.casefold() not in sender.casefold():
            return False
    return True


def _keyword_hits(text: str) -> list[str]:
    lowered = text.casefold()
    hits: list[str] = []
    for keyword in RECEIPT_KEYWORDS:
        if re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", lowered):
            hits.append(keyword)
    return hits


def _currency_code(match: re.Match[str]) -> str | None:
    code = (match.group("prefix_code") or match.group("suffix_code") or "").upper()
    if code in _CURRENCY_CODES:
        return code
    symbol = match.group("prefix_sym") or match.group("suffix_sym")
    return _SYMBOL_TO_CODE.get(symbol or "")


def _reject_number(text: str, start: int, end: int, number: str) -> bool:
    after = text[end : min(len(text), end + 20)].lower()
    digits = re.sub(r"\D", "", number)
    if "%" in after[:2]:
        return True
    if not any(ch in text[start:end] for ch in "$€£¥") and not re.search(
        r"\b(usd|eur|gbp|jpy)\b", text[start:end], re.IGNORECASE
    ):
        return True
    if len(digits) == 4 and 1900 <= int(digits) <= 2100 and "." not in number and "," not in number:
        return True
    if len(digits) > 9:
        return True
    if re.match(r"^\s*(%|percent|tracking|order)", after):
        return True
    return False


def _parse_number(raw: str, code: str) -> int | None:
    if "," in raw and "." in raw:
        decimal_sep = "," if raw.rfind(",") > raw.rfind(".") else "."
    elif "," in raw:
        parts = raw.split(",")
        decimal_sep = "," if len(parts[-1]) == 2 else ""
    elif "." in raw:
        parts = raw.split(".")
        decimal_sep = "." if len(parts[-1]) == 2 else ""
    else:
        decimal_sep = ""

    if decimal_sep:
        whole, _, frac = raw.rpartition(decimal_sep)
        whole_digits = re.sub(r"\D", "", whole)
        frac_digits = re.sub(r"\D", "", frac)[:2].ljust(2, "0")
    else:
        whole_digits = re.sub(r"\D", "", raw)
        frac_digits = "00"
    if not whole_digits:
        return None
    if code in _ZERO_MINOR:
        return int(whole_digits)
    return int(whole_digits) * 100 + int(frac_digits)


def _match_known_vendor(header: MessageHeader, vendors: Mapping[str, str]) -> str | None:
    sender = " ".join(filter(None, [header.from_name, header.from_address])).casefold()
    subject = header.subject.casefold()
    for needle, name in vendors.items():
        key = needle.casefold().strip()
        if key and (_contains_vendor_key(sender, key) or _contains_vendor_key(subject, key)):
            return name
    return None


def _contains_vendor_key(text: str, key: str) -> bool:
    if "." in key or "@" in key or "&" in key:
        return key in text
    return re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text) is not None


def _clean_sender_name(name: str | None) -> str | None:
    if not name:
        return None
    name = re.sub(r"\s+via\s+.+$", "", name, flags=re.IGNORECASE)
    name = _NOISE_WORD_RE.sub("", name)
    name = re.sub(r"[|\-–—_:]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" '\"")
    if not name or name.casefold() in {"support", "hello", "info"}:
        return None
    return name


def _registrable_domain(address: str | None) -> str | None:
    _, parsed = parse_address(address)
    address = parsed or address
    if not address or "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1].lower().strip(" >")
    domain = _MAIL_SUBDOMAIN_RE.sub("", domain)
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "ac"}:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return domain or None


def _title_domain(domain: str) -> str:
    base = domain.split("@")[-1].split(".")[0]
    base = re.sub(r"[-_]", " ", base).strip()
    special = {"github": "GitHub", "openai": "OpenAI", "jstor": "JSTOR", "ieee": "IEEE"}
    return special.get(base.lower(), base.title())


__all__ = [
    "AMOUNT_WEIGHT",
    "CALENDAR_WEIGHT",
    "KEYWORD_SCORE_CAP",
    "KEYWORD_WEIGHT",
    "KNOWN_VENDORS",
    "MARKETING_WEIGHT",
    "RECEIPT_MEDIA_WEIGHT",
    "RECEIPT_THRESHOLD",
    "VENDOR_WEIGHT",
    "extract_amount",
    "extract_vendor",
    "header_matches",
    "load_vendors",
    "score_message",
]
