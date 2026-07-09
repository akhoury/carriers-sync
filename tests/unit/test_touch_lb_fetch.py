"""Fetch-level tests for TouchLbProvider using a mocked Playwright browser.

Touch retired its username/password portal in mid-2026; the /autoforms URLs
now redirect to an OTP-only site. The provider must detect this and fail with
a clear, actionable message rather than the old cryptic "no logout link".
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from carriers_sync.providers.base import AuthFetchError
from carriers_sync.providers.touch_lb import TouchLbProvider


def make_account():
    from carriers_sync.providers.base import AccountConfig

    return AccountConfig(
        provider="touch-lb",
        username="safouny87",
        password="pw",
        label="Stephanie Touch",
        secondary_labels={},
    )


def make_browser(*, landing_url: str, login_html: str = "<html>welcome</html>"):
    """Fake browser whose home page lands on `landing_url`."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.content = AsyncMock(return_value="<html>ok</html>")
    page.url = landing_url

    login_resp = MagicMock()
    login_resp.ok = True
    login_resp.status = 200
    login_resp.text = AsyncMock(return_value=login_html)

    request = MagicMock()
    request.post = AsyncMock(return_value=login_resp)
    request.get = AsyncMock(return_value=login_resp)

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.request = request
    context.set_default_navigation_timeout = MagicMock()
    context.set_default_timeout = MagicMock()
    context.close = AsyncMock()

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    return browser


async def test_migrated_site_raises_clear_auth_error():
    """When the /autoforms home URL redirects away (to the new OTP-only site),
    fail fast with a message that names the real cause."""
    browser = make_browser(landing_url="https://touch.com.lb/en")
    with pytest.raises(AuthFetchError, match="OTP"):
        await TouchLbProvider().fetch(make_account(), browser)


async def test_migrated_site_does_not_attempt_login_post():
    """No point POSTing credentials to a portal that no longer exists."""
    browser = make_browser(landing_url="https://touch.com.lb/en")
    context = await browser.new_context()
    with pytest.raises(AuthFetchError):
        await TouchLbProvider().fetch(make_account(), browser)
    context.request.post.assert_not_called()
