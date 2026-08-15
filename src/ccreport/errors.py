"""Exception types shared across ccreport.

Configuration problems raise at construction time rather than at first use. A
misconfiguration that only surfaces on the unlucky request is a misconfiguration
that ships.
"""

from __future__ import annotations


class CCReportError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(CCReportError):
    """Settings are inconsistent or unsafe. Refuse to boot."""


class NotAuthorized(CCReportError):
    """The caller authenticated but is not permitted to do this."""


class ConnectorError(CCReportError):
    """A mail provider refused, failed, or answered in a shape we cannot use."""


class ReauthRequired(ConnectorError):
    """The stored credential is dead; the user must reconnect this account.

    Distinct from :class:`ConnectorError` because it is not a fault to log and
    retry — it is a state to surface in the UI with a "Reconnect" button.
    """


class RenderError(CCReportError):
    """A message could not be turned into a PDF by any available renderer."""
