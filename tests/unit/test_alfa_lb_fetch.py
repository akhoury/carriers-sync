"""Light-touch tests for AlfaLbProvider.fetch using a mocked Playwright browser."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from carriers_sync.providers.alfa_lb import AlfaLbProvider
from carriers_sync.providers.base import (
    AccountConfig,
    AuthFetchError,
    TransientFetchError,
    UnknownFetchError,
)


def make_account():
    return AccountConfig(
        provider="alfa-lb",
        username="03333333",
        password="pw",
        label="John",
        secondary_labels={"03222222": "Wife"},
    )


def make_browser(*, page_text="ok", xhr_json=None, raise_on_wait=None):
    """Build a fake Playwright browser whose context.new_page returns a
    page that the adapter can drive."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.fill = AsyncMock()
    page.click = AsyncMock()
    page.text_content = AsyncMock(return_value=page_text)

    response = MagicMock()
    response.json = AsyncMock(return_value=xhr_json)

    # Playwright async API: info.value is an awaitable property — the adapter
    # does `await info.value`. Mock it as a coroutine instance.
    async def _value_coro():
        return response

    info_mock = MagicMock()
    info_mock.value = _value_coro()

    cm = MagicMock()
    if raise_on_wait is not None:
        cm.__aenter__ = AsyncMock(side_effect=raise_on_wait)
    else:
        cm.__aenter__ = AsyncMock(return_value=info_mock)
    cm.__aexit__ = AsyncMock(return_value=None)
    page.expect_response = MagicMock(return_value=cm)

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.set_default_navigation_timeout = MagicMock()
    context.set_default_timeout = MagicMock()
    context.close = AsyncMock()

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    return browser


async def test_happy_path_returns_provider_result():
    xhr = {
        "ResponseCode": 8090,
        "MobileNumberValue": "03333333",
        "CurrentBalanceValue": "$ 0.00",
        "FreeUnitsValue": [
            {
                "DisplayName": "Mobile Internet",
                "SubDisplayName": "Data",
                "UsageType": "data",
                "UsedAmount": "1",
                "UsedUnit": "GB",
                "ExtraUsage": "",
                "ExtraUnit": "",
                "TotalAmount": "20",
                "TotalUnit": "GB",
            }
        ],
    }
    browser = make_browser(xhr_json=xhr)
    result = await AlfaLbProvider().fetch(make_account(), browser)
    assert result.account_id == "03333333"
    assert len(result.lines) == 1
    assert result.lines[0].consumed_gb == 1.0


async def test_login_page_rejected_raises_transient():
    browser = make_browser(page_text="The requested URL was rejected.")
    with pytest.raises(TransientFetchError, match="rejected"):
        await AlfaLbProvider().fetch(make_account(), browser)


async def test_xhr_timeout_raises_transient():
    browser = make_browser(raise_on_wait=TimeoutError())
    with pytest.raises(TransientFetchError):
        await AlfaLbProvider().fetch(make_account(), browser)


async def test_missing_service_raises_unknown_then_classifier_keeps_unknown():
    browser = make_browser(xhr_json={"FreeUnitsValue": []})
    with pytest.raises(UnknownFetchError):
        await AlfaLbProvider().fetch(make_account(), browser)


async def test_login_form_error_text_classified_as_auth():
    page_text = "Invalid Username or Password"
    browser = make_browser(page_text=page_text)
    with pytest.raises(AuthFetchError):
        await AlfaLbProvider().fetch(make_account(), browser)
