"""Touch (Lebanon) provider adapter.

Differs from Alfa structurally: one Touch login can view many phone numbers
on a single account. Each number has its OWN data quota and usage. We model
this as a synthetic "account aggregate" line plus one peer-secondary per
real number — see LineUsage.is_aggregate.

The Touch portal exposes login at POST /autoforms/auth?redir= with form-encoded
{redir, currentPath, username, password} (Set-Cookie sets the session).

After login, GET /autoforms/portal/touch/mytouch/myusage returns HTML with a
<select id="select_id"> listing every number on the account. POST same URL
with form {number=<n>} returns the per-number usage HTML, where the Mobile
Internet bundle's usage appears as e.g.
    <span class="price">2.77 GB / 7 GB</span>
inside a <div class="unbilledInfo"> following <h5>Mobile Internet</h5>.

Numbers without a Mobile Internet bundle are skipped (logged at INFO).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

from carriers_sync.providers.base import (
    AccountConfig,
    AuthFetchError,
    LineUsage,
    ProviderResult,
    ProviderUnsupportedError,
    TransientFetchError,
    UnknownFetchError,
)

logger = logging.getLogger("carriers_sync.providers.touch_lb")

_HOME_URL = "https://www.touch.com.lb/autoforms/portal/touch"
_LOGIN_URL = "https://www.touch.com.lb/autoforms/auth?redir="
_USAGE_URL = "https://www.touch.com.lb/autoforms/portal/touch/mytouch/myusage"
_DEFAULT_TIMEOUT_MS = 90_000

# Detect Touch's bot-protection / outage page.
_REJECTED_MARKERS = (
    "The requested URL was rejected",
    "Service Unavailable",
)
# Login is considered successful when the response contains a logout link
# (the homepage shows a "Logout" link only for authenticated sessions).
_LOGIN_OK_MARKERS = ("Logout", "logout")


_SELECT_RE = re.compile(
    r'<select[^>]*\bid="select_id"[^>]*>(.*?)</select>',
    re.DOTALL | re.IGNORECASE,
)
_OPTION_RE = re.compile(r"<option[^>]*>\s*(\d{6,12})\s*</option>", re.IGNORECASE)

_INTERNET_BLOCK_RE = re.compile(r"<h5>\s*Mobile Internet\s*</h5>(.*)", re.DOTALL | re.IGNORECASE)
_PRICE_RE = re.compile(
    r'<span\s+class="price">\s*'
    r"([\d.]+)\s*([KMGT]?B)\s*/\s*([\d.]+)\s*([KMGT]?B)\s*"
    r"</span>",
    re.IGNORECASE,
)


def parse_number_list(html: str) -> list[str]:
    """Extract phone numbers from the <select id="select_id"> dropdown."""
    select_match = _SELECT_RE.search(html)
    if not select_match:
        return []
    return _OPTION_RE.findall(select_match.group(1))


def parse_internet_usage(html: str) -> tuple[float, float]:
    """Return (consumed_gb, quota_gb) for the Mobile Internet bundle.

    Raises UnknownFetchError if the section is missing or the value can't
    be parsed (e.g. number has no internet plan).
    """
    block = _INTERNET_BLOCK_RE.search(html)
    if not block:
        raise UnknownFetchError("Mobile Internet section not found")
    price = _PRICE_RE.search(block.group(1))
    if not price:
        raise UnknownFetchError("Mobile Internet usage value not found")
    consumed_val, consumed_unit, quota_val, quota_unit = price.groups()
    consumed_gb = _to_gb(float(consumed_val), consumed_unit.upper())
    quota_gb = _to_gb(float(quota_val), quota_unit.upper())
    return consumed_gb, quota_gb


def _to_gb(val: float, unit: str) -> float:
    if unit == "GB":
        return round(val, 3)
    if unit == "MB":
        return round(val / 1024, 3)
    if unit == "KB":
        return round(val / (1024 * 1024), 3)
    if unit == "TB":
        return round(val * 1024, 3)
    raise UnknownFetchError(f"unknown data unit: {unit}")


class TouchLbProvider:
    id: ClassVar[str] = "touch-lb"
    display_name: ClassVar[str] = "Touch (Lebanon)"

    async def fetch(
        self,
        account: AccountConfig,
        browser: Any,
    ) -> ProviderResult:
        context = await browser.new_context()
        context.set_default_navigation_timeout(_DEFAULT_TIMEOUT_MS)
        context.set_default_timeout(_DEFAULT_TIMEOUT_MS)
        try:
            # Warm up: load the homepage so the F5 BIG-IP edge sets its TS*
            # cookies on our context. Without this the login POST is rejected.
            page = await context.new_page()
            try:
                await page.goto(_HOME_URL, wait_until="domcontentloaded")
            except Exception as e:
                raise TransientFetchError(f"goto home failed: {e}") from e
            await self._guard_rejected(await page.content())

            # Touch retired its username/password portal in mid-2026: the
            # /autoforms/* URLs now redirect to an OTP-only site
            # (touch.com.lb/en) where login requires a mobile/email
            # verification code — impossible for an unattended poller. Detect
            # the redirect off /autoforms and fail with an actionable message
            # rather than the generic "no logout link" from the dead POST.
            if "/autoforms" not in page.url:
                raise ProviderUnsupportedError(
                    "Touch retired its password portal; login is now OTP-only "
                    "(mobile/email verification code), so automated sync is no "
                    "longer possible. Remove this Touch account until Touch "
                    "exposes an API or app password."
                )

            # Login.
            try:
                login_resp = await context.request.post(
                    _LOGIN_URL,
                    form={
                        "redir": "",
                        "currentPath": "/autoforms/portal/touch",
                        "username": account.username,
                        "password": account.password,
                    },
                )
            except Exception as e:
                raise TransientFetchError(f"login request failed: {e}") from e
            if not login_resp.ok:
                raise TransientFetchError(f"login HTTP {login_resp.status}")
            login_html = await login_resp.text()
            await self._guard_rejected(login_html)
            if not any(m in login_html for m in _LOGIN_OK_MARKERS):
                raise AuthFetchError("login did not produce a logged-in page (no logout link)")

            # Discover numbers.
            try:
                list_resp = await context.request.get(_USAGE_URL)
            except Exception as e:
                raise TransientFetchError(f"usage list request failed: {e}") from e
            if not list_resp.ok:
                raise TransientFetchError(f"usage list HTTP {list_resp.status}")
            list_html = await list_resp.text()
            await self._guard_rejected(list_html)
            numbers = parse_number_list(list_html)
            if not numbers:
                raise UnknownFetchError("no numbers found in usage page select")

            # Per-number internet usage. The synthetic "account" line uses the
            # username verbatim as its line_id — the provider qualifier in
            # unique_ids (carriers_sync_touch_lb_…) keeps it from colliding
            # with phone numbers from other providers.
            secondaries: list[LineUsage] = []
            parent_id = account.username
            for number in numbers:
                try:
                    resp = await context.request.post(_USAGE_URL, form={"number": number})
                except Exception as e:
                    raise TransientFetchError(f"per-number request failed for {number}: {e}") from e
                if not resp.ok:
                    raise TransientFetchError(f"per-number HTTP {resp.status} for {number}")
                html = await resp.text()
                await self._guard_rejected(html)
                try:
                    consumed_gb, quota_gb = parse_internet_usage(html)
                except UnknownFetchError as e:
                    logger.info("number %s has no Mobile Internet bundle, skipping (%s)", number, e)
                    continue
                secondaries.append(
                    LineUsage(
                        line_id=number,
                        label=account.secondary_labels.get(number, number),
                        consumed_gb=consumed_gb,
                        quota_gb=quota_gb,
                        extra_consumed_gb=0.0,
                        is_secondary=True,
                        parent_line_id=parent_id,
                        is_aggregate=False,
                    )
                )

            if not secondaries:
                raise UnknownFetchError("no numbers with a Mobile Internet bundle on this account")

            total_consumed = round(sum(s.consumed_gb for s in secondaries), 3)
            total_quota = round(sum((s.quota_gb or 0.0) for s in secondaries), 3)
            account_main = LineUsage(
                line_id=parent_id,
                label=account.label or account.username,
                consumed_gb=total_consumed,
                quota_gb=total_quota or None,
                extra_consumed_gb=0.0,
                is_secondary=False,
                parent_line_id=None,
                is_aggregate=True,
            )

            return ProviderResult(
                account_id=account.username,
                lines=[account_main, *secondaries],
                fetched_at=datetime.now(UTC),
            )
        finally:
            await context.close()

    @staticmethod
    async def _guard_rejected(body: str) -> None:
        for marker in _REJECTED_MARKERS:
            if marker in body:
                raise TransientFetchError(f"Touch edge rejected: {marker!r}")
