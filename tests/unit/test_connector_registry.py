from __future__ import annotations

import pytest

from ccreport.connectors import GmailConnector, GraphConnector, ImapConnector
from ccreport.connectors.registry import PROVIDER_LABELS, build_connector, describe_provider
from ccreport.errors import ConfigError


def test_build_connector_dispatches() -> None:
    assert isinstance(build_connector("graph", access_token="t"), GraphConnector)
    assert isinstance(build_connector("gmail", access_token="t"), GmailConnector)
    assert isinstance(build_connector("imap", host="h", port=993, username="u", app_password="p"), ImapConnector)


def test_provider_labels_and_descriptions() -> None:
    assert PROVIDER_LABELS["graph"] == "Microsoft Outlook"
    assert describe_provider("graph")["scopes"] == ["offline_access", "User.Read", "Mail.Read"]
    assert describe_provider("gmail")["scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert describe_provider("imap")["configured"] is True


def test_unknown_provider_fails() -> None:
    with pytest.raises(ConfigError):
        build_connector("bad")
    with pytest.raises(ConfigError):
        describe_provider("bad")
