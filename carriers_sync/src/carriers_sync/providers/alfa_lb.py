"""Alfa Lebanon provider adapter.

Split into:
  - parse_response(): pure function, fully unit-tested with fixtures.
  - parse_services(): pure fallback for /account/manage-services/getmyservices.
  - AlfaLbProvider.fetch(): Playwright-driven scrape that calls both.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any, ClassVar

from carriers_sync.providers.base import (
    AccountConfig,
    AuthFetchError,
    LineUsage,
    MissingLine,
    NoConsumptionDataError,
    ProviderResult,
    TransientFetchError,
    UnknownFetchError,
)

logger = logging.getLogger("carriers_sync.providers.alfa_lb")

# getconsumptionasync entries are matched by DisplayName. A U-share plan
# exposes an aggregate line (main + all secondaries rolled up) and a main-only
# line; single-bundle plans expose one data entry named after the plan
# ("Mobile Internet 7GB", "Alfanet 800GB", ...). Free add-ons are named
# "Free <something>" and must never be picked as the primary line.
_USHARE_TOTAL = "U-share Total Bundle"
_USHARE_MAIN = "U-share Main"
# One entry per active U-share twin. The twin's MSISDN is in PrimaryNumber
# (SubDisplayName is null); TotalAmount repeats the shared pool quota.
_TWIN_SECONDARY = "Twin-Data Secondary"
_FREE_ADDON_PREFIX = "free"
_LOGIN_URL = "https://www.alfa.com.lb/en/account/login"
_CONSUMPTION_URL_PATTERN = "**/en/account/getconsumption*"
_GETMYSERVICES_URL = "https://www.alfa.com.lb/en/account/manage-services/getmyservices"
_REJECTED_MARKER = "The requested URL was rejected"
_AUTH_ERROR_MARKERS = (
    "Invalid Username or Password",
    "Account is locked",
    "verification code",
)
_DEFAULT_TIMEOUT_MS = 90_000


def parse_response(
    payload: dict[str, Any],
    *,
    account: AccountConfig,
    fetched_at: datetime,
) -> ProviderResult:
    """Convert Alfa's getconsumptionasync JSON into a ProviderResult.

    Alfa's response lists every free unit (data + voice bundles) under
    ``FreeUnitsValue``. For a U-share plan it exposes a "U-share Total Bundle"
    aggregate (main plus every twin, against the shared quota), one
    "Twin-Data Secondary" entry per *active* twin, and a "U-share Main" entry.

    Two Alfa quirks drive the shape of this parser:

    * ``Secondaries`` is always null. The per-twin breakdown lives in the
      separate "Twin-Data Secondary" entries instead, keyed by
      ``PrimaryNumber``.
    * "U-share Main" now mirrors the pooled total rather than the main
      number's own usage, so main-only usage is derived as
      ``aggregate - sum(twins)`` and returned as ``main_own_consumed_gb``.

    A twin that Alfa has deactivated simply disappears from the response (and
    the account reverts to a plain data bundle). Configured secondaries that
    are absent are returned in ``missing_secondaries`` so the consumer can
    mark them stale rather than silently reporting the last known number.

    Raises UnknownFetchError on any shape mismatch, NoConsumptionDataError when
    there is no data bundle at all (voice-only / alarm SIMs).
    """
    free_units = payload.get("FreeUnitsValue")
    if not isinstance(free_units, list) or not free_units:
        raise UnknownFetchError("missing or empty FreeUnitsValue")

    entry, is_aggregate = _select_data_entry(free_units)
    if entry is None:
        # Some accounts (alarm SIMs, low-use lines) have a bundle assigned
        # but the consumption endpoint doesn't expose it. The fetcher
        # should fall back to /account/manage-services/getmyservices which
        # always lists the active bundle.
        raise NoConsumptionDataError(
            f"no supported service in getconsumptionasync response (have: "
            f"{[e.get('DisplayName') for e in free_units]})"
        )

    consumed_gb = _to_gb(_require_num(entry, "UsedAmount"), _unit(entry, "UsedUnit"))
    quota_gb = _to_gb(_require_num(entry, "TotalAmount"), _unit(entry, "TotalUnit"))
    extra_gb = _extra_gb(entry)

    main_line = LineUsage(
        line_id=account.username,
        label=account.label or account.username,
        consumed_gb=consumed_gb,
        quota_gb=quota_gb,
        extra_consumed_gb=extra_gb,
        is_secondary=False,
        parent_line_id=None,
        is_aggregate=is_aggregate,
    )

    secondaries = _parse_twin_secondaries(free_units, account=account)

    # Only meaningful against a pooled aggregate: without one there is no
    # total to subtract the twins from.
    main_own = None
    if is_aggregate:
        main_own = round(
            max(0.0, consumed_gb - sum(s.consumed_gb for s in secondaries)), 3
        )

    missing = _missing_secondaries(
        account=account,
        reported={s.line_id for s in secondaries},
        ushare_active=is_aggregate,
    )
    if missing:
        logger.warning(
            "%s: %d configured secondary line(s) not reported by Alfa (%s): %s",
            account.username,
            len(missing),
            missing[0].reason,
            ", ".join(f"{m.line_id} ({m.label})" for m in missing),
        )

    return ProviderResult(
        account_id=account.username,
        lines=[main_line, *secondaries],
        fetched_at=fetched_at,
        missing_secondaries=missing,
        main_own_consumed_gb=main_own,
    )


def _parse_twin_secondaries(
    free_units: list[dict[str, Any]],
    *,
    account: AccountConfig,
) -> list[LineUsage]:
    """Every active "Twin-Data Secondary" entry, as secondary LineUsages.

    quota_gb is left None on purpose: TotalAmount on a twin entry repeats the
    shared pool quota, so treating it as a per-line allowance would render a
    meaningless percentage (two twins each showing "15.64 / 85 GB").
    """
    out: list[LineUsage] = []
    seen: set[str] = set()
    for e in free_units:
        if e.get("DisplayName") != _TWIN_SECONDARY:
            continue
        raw = e.get("PrimaryNumber")
        num = raw.strip() if isinstance(raw, str) else ""
        if not num:
            logger.warning(
                "%s: %s entry without PrimaryNumber — skipping",
                account.username,
                _TWIN_SECONDARY,
            )
            continue
        if num in seen:
            logger.warning(
                "%s: duplicate %s entry for %s — keeping the first",
                account.username,
                _TWIN_SECONDARY,
                num,
            )
            continue
        try:
            consumed = _to_gb(_require_num(e, "UsedAmount"), _unit(e, "UsedUnit"))
        except UnknownFetchError as exc:
            # One unreadable twin must not cost us the rest of the account.
            logger.warning(
                "%s: unreadable usage for twin %s (%s) — skipping",
                account.username,
                num,
                exc,
            )
            continue
        seen.add(num)
        out.append(
            LineUsage(
                line_id=num,
                label=account.secondary_labels.get(num) or num,
                consumed_gb=consumed,
                quota_gb=None,
                extra_consumed_gb=_extra_gb(e),
                is_secondary=True,
                parent_line_id=account.username,
            )
        )
    return out


def _missing_secondaries(
    *,
    account: AccountConfig,
    reported: set[str],
    ushare_active: bool,
) -> list[MissingLine]:
    """Configured secondaries absent from this response.

    ushare_active distinguishes the two failure modes: when the pooled bundle
    itself is gone every twin is deactivated ("bundle_inactive", the case that
    needs a call to Alfa); when the bundle is live but one number is missing,
    only that line is affected ("not_in_response").
    """
    reason = "not_in_response" if ushare_active else "bundle_inactive"
    return [
        MissingLine(line_id=num, label=label or num, reason=reason)
        for num, label in sorted(account.secondary_labels.items())
        if num not in reported
    ]


def _select_data_entry(
    free_units: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool]:
    """Pick the FreeUnitsValue entry to report and whether it's an aggregate.

    Priority: U-share aggregate > U-share main > first non-"Free" data bundle
    (Mobile Internet, Alfanet, or any future plan — matched generically, never
    by the size in the plan name). Returns (None, False) when there is no data
    bundle to report.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for e in free_units:
        name = e.get("DisplayName")
        if isinstance(name, str):
            by_name.setdefault(name, e)

    if _USHARE_TOTAL in by_name:
        return by_name[_USHARE_TOTAL], True
    if _USHARE_MAIN in by_name:
        return by_name[_USHARE_MAIN], False
    # Single-bundle accounts (Mobile Internet, Alfanet, ...): the primary data
    # bundle is the first data entry that isn't a free add-on ("Free
    # e-learning", "Free Minutes", ...).
    for e in free_units:
        name = e.get("DisplayName")
        if (
            isinstance(name, str)
            and name != _TWIN_SECONDARY
            and e.get("UsageType") == "data"
            and not name.strip().lower().startswith(_FREE_ADDON_PREFIX)
        ):
            return e, False
    return None, False


def _unit(entry: dict[str, Any], key: str) -> str:
    u = entry.get(key)
    if not isinstance(u, str) or not u.strip():
        return "GB"
    return u.strip()


def _extra_gb(entry: dict[str, Any]) -> float:
    """Extra (over-quota) consumption in GB, or 0.0 when none/blank."""
    raw = entry.get("ExtraUsage")
    val = _optional_float(raw)
    if val is None or val <= 0:
        return 0.0
    unit = entry.get("ExtraUnit")
    if not isinstance(unit, str) or not unit.strip():
        unit = _unit(entry, "UsedUnit")
    return _to_gb(val, unit.strip())


def _optional_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


_BUNDLE_TEXT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)\s*$", re.IGNORECASE)


def parse_services(
    payload: list[dict[str, Any]] | dict[str, Any],
    *,
    account: AccountConfig,
    fetched_at: datetime,
) -> ProviderResult:
    """Parse /account/manage-services/getmyservices and return a placeholder
    ProviderResult based on the active Mobile Internet bundle.

    consumed_gb is 0.0 (this endpoint doesn't expose actual usage). quota_gb
    comes from ActiveBundle.Text — for example '7GB' -> 7.0. If no active
    bundle (or PAYG / unparseable), returns a no-plan placeholder.
    """
    if not isinstance(payload, list):
        raise UnknownFetchError("getmyservices response is not a JSON array")

    mi = next(
        (s for s in payload if isinstance(s, dict) and s.get("Name") == "Mobile Internet"), None
    )
    if mi is None:
        return _no_plan_result(account, fetched_at)

    active = mi.get("ActiveBundle")
    if not isinstance(active, dict) or not active.get("Selected"):
        return _no_plan_result(account, fetched_at)

    text = active.get("TextEn") or active.get("Text") or ""
    quota_gb = _parse_bundle_text(str(text))
    if quota_gb is None:
        # PAYG or non-volumetric bundle — treat as no-plan
        logger.info(
            "%s active Mobile Internet bundle is non-volumetric (%r); reporting as no-plan",
            account.username,
            text,
        )
        return _no_plan_result(account, fetched_at)

    return ProviderResult(
        account_id=account.username,
        lines=[
            LineUsage(
                line_id=account.username,
                label=account.label or account.username,
                consumed_gb=0.0,
                quota_gb=quota_gb,
                extra_consumed_gb=0.0,
                is_secondary=False,
                parent_line_id=None,
            )
        ],
        fetched_at=fetched_at,
    )


def _parse_bundle_text(text: str) -> float | None:
    """Parse Alfa's bundle Text field. '7GB' -> 7.0, '475GB' -> 475.0,
    '10MB' -> 0.01. Returns None for 'PAYG' or unrecognised formats."""
    m = _BUNDLE_TEXT_RE.match(text)
    if not m:
        return None
    try:
        return _to_gb(float(m.group(1)), m.group(2).upper())
    except UnknownFetchError:
        return None


def _no_plan_result(account: AccountConfig, fetched_at: datetime) -> ProviderResult:
    return ProviderResult(
        account_id=account.username,
        lines=[
            LineUsage(
                line_id=account.username,
                label=account.label or account.username,
                consumed_gb=0.0,
                quota_gb=None,
                extra_consumed_gb=0.0,
                is_secondary=False,
                parent_line_id=None,
            )
        ],
        fetched_at=fetched_at,
    )


def _require_num(d: dict[str, Any], key: str) -> float:
    if key not in d:
        raise UnknownFetchError(f"missing field: {key}")
    try:
        return float(d[key])
    except (TypeError, ValueError) as e:
        raise UnknownFetchError(f"{key} is not numeric: {d[key]!r}") from e


def _to_gb(value: float, unit: str) -> float:
    # Normalise case/whitespace so plan changes that report a different unit
    # (or a lowercase one) still parse. Quota/usage never come from the plan
    # name — only from these fields — so any plan size is handled.
    u = unit.strip().upper()
    if u == "KB":
        return round(value / (1024 * 1024), 3)
    if u == "MB":
        return round(value / 1024, 3)
    if u == "GB":
        return round(value, 3)
    if u == "TB":
        return round(value * 1024, 3)
    raise UnknownFetchError(f"unknown unit: {unit}")


class AlfaLbProvider:
    id: ClassVar[str] = "alfa-lb"
    display_name: ClassVar[str] = "Alfa (Lebanon)"

    async def fetch(
        self,
        account: AccountConfig,
        browser: Any,
    ) -> ProviderResult:
        context = await browser.new_context()
        context.set_default_navigation_timeout(_DEFAULT_TIMEOUT_MS)
        context.set_default_timeout(_DEFAULT_TIMEOUT_MS)
        try:
            page = await context.new_page()
            try:
                await page.goto(_LOGIN_URL)
            except Exception as e:
                raise TransientFetchError(f"goto failed: {e}") from e

            await self._guard_rejected(page)

            await page.fill("#loginForm #Username", account.username)
            await page.fill("#loginForm #Password", account.password)
            await page.click('#loginForm button[type="submit"]')

            await self._guard_rejected(page)
            await self._guard_auth_error(page)

            try:
                async with page.expect_response(
                    _CONSUMPTION_URL_PATTERN, timeout=_DEFAULT_TIMEOUT_MS
                ) as info:
                    await self._guard_rejected(page)
                # Async API: info.value is awaitable; sync API would expose it
                # as a plain attribute. We're async — await it.
                response = await info.value
            except (AuthFetchError, TransientFetchError, UnknownFetchError):
                raise
            except TimeoutError as e:
                raise TransientFetchError("getconsumption XHR did not arrive") from e
            except Exception as e:
                raise TransientFetchError(f"waiting for getconsumption failed: {e}") from e

            payload = await response.json()
            now = datetime.now(UTC)
            try:
                return parse_response(payload, account=account, fetched_at=now)
            except NoConsumptionDataError as e:
                logger.info(
                    "%s: %s — falling back to getmyservices for active bundle",
                    account.username,
                    e,
                )
                return await self._fetch_via_services(context, account, fetched_at=now)
        finally:
            await context.close()

    @staticmethod
    async def _fetch_via_services(
        context: Any, account: AccountConfig, *, fetched_at: datetime
    ) -> ProviderResult:
        """Hit /account/manage-services/getmyservices using the post-login
        cookies in `context`. Used as a fallback when getconsumption has no
        Mobile Internet for the account.
        """
        url = f"{_GETMYSERVICES_URL}?_={int(time.time() * 1000)}"
        try:
            resp = await context.request.get(
                url,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
            )
        except Exception as e:
            raise TransientFetchError(f"getmyservices request failed: {e}") from e
        if not resp.ok:
            raise TransientFetchError(f"getmyservices HTTP {resp.status}")
        payload = await resp.json()
        return parse_services(payload, account=account, fetched_at=fetched_at)

    @staticmethod
    async def _guard_rejected(page: Any) -> None:
        body = await page.text_content("body")
        if body and _REJECTED_MARKER in body:
            raise TransientFetchError("login URL rejected by Alfa edge")

    @staticmethod
    async def _guard_auth_error(page: Any) -> None:
        body = await page.text_content("body")
        if not body:
            return
        for marker in _AUTH_ERROR_MARKERS:
            if marker in body:
                raise AuthFetchError(f"login error: {marker!r}")
