from __future__ import annotations

from typing import Any

from ccreport.errors import ConfigError
from ccreport.settings import get_settings

from .base import PROVIDERS, MailConnector
from .gmail import GmailConnector
from .graph import GraphConnector
from .imap import ImapConnector

PROVIDER_LABELS = {
    "graph": "Microsoft Outlook",
    "gmail": "Google Gmail",
    "imap": "IMAP app password",
}


def build_connector(provider: str, **credentials: Any) -> MailConnector:
    if provider == "graph":
        return GraphConnector(**credentials)
    if provider == "gmail":
        return GmailConnector(**credentials)
    if provider == "imap":
        return ImapConnector(**credentials)
    raise ConfigError(f"Unknown mail provider {provider!r}")


def describe_provider(provider: str, settings: Any = None) -> dict[str, Any]:
    settings = settings or get_settings()
    if provider not in PROVIDERS:
        raise ConfigError(f"Unknown mail provider {provider!r}")
    if provider == "graph":
        scopes = settings.ms_scope_list
        configured = settings.graph_configured
        detail = "Microsoft Graph delegated Mail.Read"
    elif provider == "gmail":
        scopes = settings.google_scope_list
        configured = settings.google_configured
        detail = "Gmail delegated gmail.readonly"
    else:
        scopes = []
        configured = settings.enable_imap
        detail = f"Read-only IMAP EXAMINE on {settings.imap_default_host}:{settings.imap_default_port}"
    return {"provider": provider, "label": PROVIDER_LABELS[provider], "configured": configured, "scopes": scopes, "detail": detail}


__all__ = ["PROVIDER_LABELS", "build_connector", "describe_provider"]
