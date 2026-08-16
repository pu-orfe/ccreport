from __future__ import annotations

import inspect
import re

from ccreport.connectors import GmailConnector, GraphConnector, ImapConnector, MailConnector

_MUTATING = re.compile(r"(delete|update|send|move|mark|write|create|patch|post_message|expunge|store|append)", re.IGNORECASE)


def public_methods(cls: type) -> set[str]:
    return {name for name, member in inspect.getmembers(cls) if callable(member) and not name.startswith("_")}


def test_mail_connector_protocol_has_no_mutating_methods() -> None:
    assert not [name for name in public_methods(MailConnector) if _MUTATING.search(name)]


def test_concrete_connectors_have_no_public_mutating_methods() -> None:
    for cls in (GraphConnector, GmailConnector, ImapConnector):
        assert not [name for name in public_methods(cls) if _MUTATING.search(name)]
