from __future__ import annotations

from .base import (
    Attachment,
    AttachmentRef,
    ConnectorStatus,
    MailConnector,
    MailFolder,
    MessageBody,
    MessageHeader,
    MessageQuery,
    parse_address,
)
from .gmail import GmailConnector
from .graph import GraphConnector
from .imap import ImapConnector
from .registry import PROVIDER_LABELS, build_connector, describe_provider

__all__ = [
    "Attachment",
    "AttachmentRef",
    "ConnectorStatus",
    "GmailConnector",
    "GraphConnector",
    "ImapConnector",
    "MailConnector",
    "MailFolder",
    "MessageBody",
    "MessageHeader",
    "MessageQuery",
    "PROVIDER_LABELS",
    "build_connector",
    "describe_provider",
    "parse_address",
]
