"""Provider adapter contract.

Every carrier adapter (Alfa, Touch, etc.) implements the Provider protocol
defined here. The scheduler is provider-agnostic; it sees only ProviderResult
and LineUsage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Protocol


@dataclass(frozen=True, slots=True)
class AccountConfig:
    """One configured account from /data/options.json."""

    provider: str
    username: str
    password: str
    label: str
    secondary_labels: dict[str, str]


@dataclass(frozen=True, slots=True)
class LineUsage:
    """One billable line — either a main account number, a U-share secondary,
    or a synthetic provider-account aggregate line.

    is_aggregate=True signals that consumed_gb on the main line ALREADY rolls
    up the secondaries' usage, so discovery should not sum again. Used for
    providers like Touch where one login surfaces multiple peer numbers; we
    surface a synthetic "account" device whose totals are the sum of the
    individual numbers' totals.
    """

    line_id: str
    label: str
    consumed_gb: float
    quota_gb: float | None
    extra_consumed_gb: float
    is_secondary: bool
    parent_line_id: str | None
    is_aggregate: bool = False


@dataclass(frozen=True, slots=True)
class MissingLine:
    """A secondary configured in options.json that the carrier did not report.

    Alfa auto-deactivates U-share twins once the shared bundle is exhausted:
    the twin entries vanish from getconsumptionasync and the account reverts
    to a plain data bundle until the subscriber calls Alfa to re-enable them.
    Reporting the gap explicitly lets Home Assistant keep showing the last
    known value while flagging it as stale, instead of presenting a frozen
    number as if it were current.

    reason is machine-readable:
      "bundle_inactive" — the shared/U-share bundle is gone altogether
      "not_in_response" — the bundle is active but this number was absent
    """

    line_id: str
    label: str
    reason: str


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Result of one fetch for one account.

    missing_secondaries lists configured secondaries the carrier did not
    report this cycle. main_own_consumed_gb is the main number's own usage
    when the carrier only exposes a pooled aggregate, or None when it is not
    derivable.
    """

    account_id: str
    lines: list[LineUsage]
    fetched_at: datetime
    missing_secondaries: list[MissingLine] = field(default_factory=list)
    main_own_consumed_gb: float | None = None


class TransientFetchError(Exception):
    """Network timeout, URL rejected, browser crash. Worth retrying with backoff."""


class AuthFetchError(Exception):
    """Invalid credentials, account locked, captcha. Do NOT retry this cycle."""


class ProviderUnsupportedError(AuthFetchError):
    """The carrier can no longer be automated at all — e.g. it removed
    password login in favour of OTP. Treated like an auth error (no retry),
    but the scheduler also RESETS the account's published values so stale
    numbers don't linger in Home Assistant forever."""


class UnknownFetchError(Exception):
    """Unexpected page structure or JSON shape. One retry only."""


class NoConsumptionDataError(UnknownFetchError):
    """The provider's primary consumption response is structurally valid but
    has no data plan we recognise. Caller may try a fallback (e.g. fetching
    a different "active services" endpoint to read the assigned quota even
    when current usage is not reported)."""


class Provider(Protocol):
    """Protocol every provider adapter must satisfy."""

    id: ClassVar[str]
    display_name: ClassVar[str]

    async def fetch(
        self,
        account: AccountConfig,
        browser: object,
    ) -> ProviderResult: ...
