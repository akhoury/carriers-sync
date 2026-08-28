"""Ogero (Lebanon) provider adapter.

Ogero is the Lebanese landline + ADSL/VDSL provider. Like Touch, one login
exposes multiple numbers/lines on the dashboard. Each line has its own
consumption (FUP, "fair use policy" volume cap), shown on the dashboard at
/myogero/index.php with `?nbr=<phone>&dsl=<dsl-id>` query params for
non-default lines.

Login flow:
  GET  /myogero/login.php       — form + Google reCAPTCHA v3 (invisible scoring)
  POST /myogero/login.p.php     — credentials + g-recaptcha-response
  GET  /myogero/index.php       — dashboard (success: MyOgeroMenuContainer
                                  / Logout link in HTML)

Captcha handling: the login form uses Google reCAPTCHA v3 (invisible /
score-based, NOT the v2 image grid). v3 grades the session and accepts a
token above a server-side threshold. We use playwright-stealth to mask
common automation fingerprints (navigator.webdriver, plugin enum, WebGL
vendor strings, etc.) and add small human-like delays so the page lifecycle
doesn't look obviously bot-driven. **This is best-effort** — Google may
still flag us. If reCAPTCHA blocks, login surfaces as
AuthFetchError("captcha") and the user can fall back to manual cookie
injection (planned 0.5.0 feature: a `session_cookie` field on the account
config + a `keep_warm` background ping to refresh the session before
PHPSESSID's ~24-min server-side TTL expires).

Per-line consumption is parsed from the dashboard's HTML
`MyOgeroDashboardSection2Consumption` div: `<b>Consumption</b>100 / 400 GB FUP`.
"""

from __future__ import annotations

import contextlib
import logging
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

from playwright_stealth import Stealth  # type: ignore[import-untyped]

from carriers_sync.providers.base import (
    AccountConfig,
    AuthFetchError,
    LineUsage,
    ProviderResult,
    TransientFetchError,
    UnknownFetchError,
    sanitize_account_id,
)

logger = logging.getLogger("carriers_sync.providers.ogero_lb")

_LOGIN_URL = "https://ogero.gov.lb/myogero/login.php"
_DASHBOARD_URL = "https://ogero.gov.lb/myogero/index.php"
_DEFAULT_TIMEOUT_MS = 90_000

_LOGIN_OK_MARKERS = ("MyOgeroMenuContainer", "Logout")
_CAPTCHA_MARKERS = ("g-recaptcha", "recaptcha")
_LOGIN_ERROR_MARKERS = (
    "incorrect",
    "invalid",
    "wrong",
)

_SELECT_RE = re.compile(
    r'<select[^>]*\bid="changnumber"[^>]*>(.*?)</select>',
    re.DOTALL | re.IGNORECASE,
)
_OPTION_RE = re.compile(
    r'<option\b[^>]*\bvalue="(\d+)"[^>]*\bvalue2="([^"]+)"',
    re.IGNORECASE,
)
_CONSUMPTION_RE = re.compile(
    r'<div\s+class="MyOgeroDashboardSection2Consumption">'
    r".*?<b>Consumption</b>\s*"
    r"([0-9.]+)\s*/\s*([0-9.]+)\s*([KMGT]?B)",
    re.DOTALL | re.IGNORECASE,
)


def parse_number_list(html: str) -> list[tuple[str, str]]:
    """Return list of (phone_number, dsl_id) tuples from the changnumber select."""
    sel = _SELECT_RE.search(html)
    if not sel:
        return []
    return _OPTION_RE.findall(sel.group(1))


def parse_consumption(html: str) -> tuple[float, float]:
    """Return (consumed_gb, quota_gb) from the dashboard consumption section.

    Raises UnknownFetchError if the section is missing or unparseable.
    """
    m = _CONSUMPTION_RE.search(html)
    if not m:
        raise UnknownFetchError("Ogero consumption section not found")
    consumed_val, quota_val, unit = m.groups()
    return _to_gb(float(consumed_val), unit.upper()), _to_gb(float(quota_val), unit.upper())


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


def _sanitize_username(username: str) -> str:  # noqa: D401  (thin alias)
    """Convert email/special-char usernames into a slug safe for use as a
    line_id (which feeds into entity_id slugs and MQTT topic paths)."""
    return sanitize_account_id(username)


class OgeroLbProvider:
    id: ClassVar[str] = "ogero-lb"
    display_name: ClassVar[str] = "Ogero (Lebanon)"

    async def fetch(
        self,
        account: AccountConfig,
        browser: Any,
    ) -> ProviderResult:
        # Use a realistic user-agent + viewport so we don't look like a
        # default headless Chromium. reCAPTCHA v3's classifier weighs these.
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        context.set_default_navigation_timeout(_DEFAULT_TIMEOUT_MS)
        context.set_default_timeout(_DEFAULT_TIMEOUT_MS)
        try:
            page = await context.new_page()

            # Apply playwright-stealth patches (hide webdriver, plugin enum,
            # WebGL vendor, etc.) before navigating anywhere.
            stealth = Stealth()
            try:
                await stealth.apply_stealth_async(page)
            except Exception as e:
                logger.warning("stealth setup failed: %s (continuing without)", e)

            try:
                await page.goto(_LOGIN_URL, wait_until="domcontentloaded")
            except Exception as e:
                raise TransientFetchError(f"goto login failed: {e}") from e

            login_html = await page.content()

            # Give reCAPTCHA v3 a moment of "human" idle time before
            # interacting with the form — its scoring observes the session
            # lifecycle, and instant-fill-and-submit is a classic bot tell.
            await page.wait_for_timeout(2000)

            try:
                await page.fill('input[name="username"]', account.username)
                await page.wait_for_timeout(700)
                await page.fill('input[name="password"]', account.password)
                await page.wait_for_timeout(900)
            except Exception as e:
                raise TransientFetchError(f"could not fill Ogero login form: {e}") from e

            # Submit. Try common selectors, then fall back to JS form.submit().
            try:
                await self._submit_form(page)
            except Exception as e:
                raise TransientFetchError(f"could not submit Ogero login form: {e}") from e

            # Wait for the post-submit navigation to settle. The previous
            # implementation suppressed errors here and then immediately
            # tried page.content(), which raised
            # "Unable to retrieve content because the page is navigating".
            # Now we explicitly wait for the new document to load and retry
            # content() a few times if it races with another navigation.
            content = await self._read_settled_content(page)
            if not any(m in content for m in _LOGIN_OK_MARKERS):
                # If captcha was on the page AND login didn't succeed, it's
                # almost certainly a reCAPTCHA score below threshold.
                if "g-recaptcha" in login_html:
                    raise AuthFetchError(
                        "Ogero login likely blocked by reCAPTCHA v3 score — "
                        "stealth + human-like timing wasn't enough. Fall back "
                        "to cookie injection (manual login, paste PHPSESSID)."
                    )
                if any(m in content.lower() for m in _LOGIN_ERROR_MARKERS):
                    raise AuthFetchError("Ogero login: invalid credentials")
                raise AuthFetchError("Ogero login did not produce a logged-in page")

            # Make sure we're on the dashboard. Direct nav resets to default line.
            try:
                await page.goto(_DASHBOARD_URL, wait_until="domcontentloaded")
            except Exception as e:
                raise TransientFetchError(f"goto dashboard failed: {e}") from e

            dashboard_html = await self._read_settled_content(page)
            numbers = parse_number_list(dashboard_html)
            if not numbers:
                raise UnknownFetchError("no numbers found in Ogero dashboard select")

            parent_id = _sanitize_username(account.username)
            secondaries: list[LineUsage] = []
            for phone, dsl in numbers:
                url = f"{_DASHBOARD_URL}?nbr={phone}&dsl={dsl}"
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                except Exception as e:
                    raise TransientFetchError(f"goto {phone} failed: {e}") from e
                html = await self._read_settled_content(page)
                try:
                    consumed_gb, quota_gb = parse_consumption(html)
                except UnknownFetchError as e:
                    logger.info(
                        "Ogero number %s has no consumption data, skipping (%s)",
                        phone,
                        e,
                    )
                    continue
                secondaries.append(
                    LineUsage(
                        line_id=phone,
                        label=account.secondary_labels.get(phone, f"{phone} ({dsl})"),
                        consumed_gb=consumed_gb,
                        quota_gb=quota_gb,
                        extra_consumed_gb=0.0,
                        is_secondary=True,
                        parent_line_id=parent_id,
                        is_aggregate=False,
                    )
                )

            if not secondaries:
                raise UnknownFetchError("no Ogero numbers with consumption data on this account")

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
                account_id=parent_id,
                lines=[account_main, *secondaries],
                fetched_at=datetime.now(UTC),
            )
        finally:
            await context.close()

    @staticmethod
    async def _read_settled_content(page: Any) -> str:
        """Return page.content() after waiting for any in-flight navigation.

        Playwright's `page.content()` raises
            "Unable to retrieve content because the page is navigating
             and changing the content"
        when called mid-redirect. We wait for the document to settle, then
        retry a small number of times if another navigation kicks in
        between the wait and the content read.
        """
        last_exc: Exception | None = None
        for _attempt in range(5):
            with contextlib.suppress(Exception):
                # `load` is stricter than `domcontentloaded` (waits for
                # subresources) but doesn't require networkidle, which can
                # hang on long-poll connections (analytics beacons etc.).
                await page.wait_for_load_state("load", timeout=15_000)
            try:
                content: str = await page.content()
                return content
            except Exception as e:  # noqa: BLE001
                last_exc = e
                # If the page is still navigating, give it a moment and
                # try again on the next iteration.
                await page.wait_for_timeout(1500)
        raise TransientFetchError(
            f"could not read page content after navigation settled: {last_exc}"
        )

    @staticmethod
    async def _submit_form(page: Any) -> None:
        """Submit the Ogero login form.

        Tries several common submit-element selectors. If none match (Ogero's
        UI uses jQuery + custom CSS classes; the actual submit element might
        be a styled anchor or a div with onclick), falls back to triggering
        the form's submit() method directly via JS — which still fires any
        onsubmit handlers that captcha hooks attach to.
        """
        selectors = [
            'button[type="submit"]',
            'input[type="submit"]',
            "button.LoginFormSubmit, a.LoginFormSubmit",
            'button:has-text("Login")',
            'a:has-text("Login")',
        ]
        for sel in selectors:
            try:
                await page.click(sel, timeout=2000)
                return
            except Exception:  # noqa: BLE001
                continue

        # Fallback: programmatically submit the form via JS. This triggers
        # any onsubmit handler the page registered (which is typically what
        # invokes grecaptcha.execute() to inject the v3 token before POSTing).
        logger.info("Ogero submit: no clickable submit element found, using JS form.submit()")
        await page.evaluate(
            """
            () => {
                const form = document.querySelector('form');
                if (form && typeof form.requestSubmit === 'function') {
                    form.requestSubmit();
                } else if (form) {
                    form.submit();
                } else {
                    throw new Error('no form on page');
                }
            }
            """
        )
