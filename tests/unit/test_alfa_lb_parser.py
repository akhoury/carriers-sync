import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from carriers_sync.providers.alfa_lb import parse_response, parse_services
from carriers_sync.providers.base import (
    AccountConfig,
    UnknownFetchError,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def make_account(secondary_labels=None):
    return AccountConfig(
        provider="alfa-lb",
        username="03333333",
        password="x",
        label="John",
        secondary_labels=secondary_labels or {"03222222": "Wife", "03111111": "Alarm eSIM"},
    )


def load(name):
    return json.loads((FIXTURES / name).read_text())


def test_parse_ushare_reports_aggregate_total():
    """U-share accounts: Alfa's getconsumptionasync exposes a 'U-share Total
    Bundle' aggregate (main + all secondaries) and a 'U-share Main' line, but
    no per-secondary breakdown. We report a single aggregate main line so the
    quota/usage sensors track the whole plan."""
    payload = load("alfa_ushare_new.json")
    fetched_at = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    result = parse_response(payload, account=make_account(), fetched_at=fetched_at)

    assert result.account_id == "03333333"
    assert result.fetched_at == fetched_at
    assert len(result.lines) == 1

    main = result.lines[0]
    assert main.line_id == "03333333"
    assert main.label == "John"
    assert main.is_secondary is False
    assert main.is_aggregate is True
    # Aggregate total (14.20), NOT the main-only value (2.85).
    assert main.consumed_gb == pytest.approx(14.20)
    assert main.quota_gb == pytest.approx(25.0)
    assert main.extra_consumed_gb == 0.0
    assert main.parent_line_id is None


def test_parse_alfanet_single_bundle():
    """AlfaNet postpaid lines expose a single non-U-share data bundle named
    after the plan (e.g. 'Alfanet 800GB'). Report it as a plain main line."""
    payload = load("alfa_alfanet_new.json")
    result = parse_response(
        payload,
        account=make_account(secondary_labels={}),
        fetched_at=datetime.now(UTC),
    )
    assert len(result.lines) == 1
    main = result.lines[0]
    assert main.consumed_gb == pytest.approx(10.51)
    assert main.quota_gb == pytest.approx(53.33)
    assert main.is_secondary is False
    assert main.is_aggregate is False


def test_free_addons_not_picked_as_main_line():
    """Free add-ons ('Free e-learning') must never be chosen as the data
    line when a real bundle is present."""
    payload = {
        "FreeUnitsValue": [
            {
                "DisplayName": "Free e-learning",
                "SubDisplayName": "Data",
                "UsageType": "data",
                "UsedAmount": "0",
                "UsedUnit": "GB",
                "TotalAmount": "20",
                "TotalUnit": "GB",
            },
            {
                "DisplayName": "Alfanet 800GB",
                "SubDisplayName": "Data",
                "UsageType": "data",
                "UsedAmount": "3",
                "UsedUnit": "GB",
                "TotalAmount": "53.33",
                "TotalUnit": "GB",
            },
        ]
    }
    result = parse_response(
        payload,
        account=make_account(secondary_labels={}),
        fetched_at=datetime.now(UTC),
    )
    assert result.lines[0].consumed_gb == pytest.approx(3.0)
    assert result.lines[0].quota_gb == pytest.approx(53.33)


def test_parse_mobile_internet_suffixed_name():
    """Standalone data lines can be named 'Mobile Internet 7GB' etc. — match
    by prefix, not exact string. Not a U-share plan, so not an aggregate."""
    payload = load("alfa_mobile_internet_new.json")
    result = parse_response(
        payload,
        account=make_account(secondary_labels={}),
        fetched_at=datetime.now(UTC),
    )
    assert len(result.lines) == 1
    main = result.lines[0]
    assert main.consumed_gb == pytest.approx(5.5)
    assert main.quota_gb == pytest.approx(10.0)
    assert main.is_secondary is False
    assert main.is_aggregate is False


def test_extra_consumption_passed_through():
    payload = {
        "FreeUnitsValue": [
            {
                "DisplayName": "Mobile Internet",
                "SubDisplayName": "Data",
                "UsageType": "data",
                "UsedAmount": "9",
                "UsedUnit": "GB",
                "ExtraUsage": "1.5",
                "ExtraUnit": "GB",
                "TotalAmount": "10",
                "TotalUnit": "GB",
            }
        ]
    }
    result = parse_response(
        payload,
        account=make_account(secondary_labels={}),
        fetched_at=datetime.now(UTC),
    )
    assert result.lines[0].extra_consumed_gb == pytest.approx(1.5)


def test_mb_units_converted():
    payload = {
        "FreeUnitsValue": [
            {
                "DisplayName": "Mobile Internet",
                "SubDisplayName": "Data",
                "UsageType": "data",
                "UsedAmount": "512",
                "UsedUnit": "MB",
                "ExtraUsage": "",
                "ExtraUnit": "",
                "TotalAmount": "1",
                "TotalUnit": "GB",
            }
        ]
    }
    result = parse_response(
        payload,
        account=make_account(secondary_labels={}),
        fetched_at=datetime.now(UTC),
    )
    assert result.lines[0].consumed_gb == pytest.approx(0.5, abs=0.001)
    assert result.lines[0].quota_gb == pytest.approx(1.0)


def test_plan_size_read_from_fields_not_name_any_unit():
    """Plan size is never parsed from the DisplayName — quota/usage come from
    TotalAmount/UsedAmount + their units. An upgrade/downgrade to any size or
    unit (incl. TB) is reported straight from the live values."""
    payload = {
        "FreeUnitsValue": [
            {
                "DisplayName": "Alfanet 2TB",
                "SubDisplayName": "Data",
                "UsageType": "data",
                "UsedAmount": "0.5",
                "UsedUnit": "TB",
                "TotalAmount": "2",
                "TotalUnit": "TB",
                "ExtraUsage": "",
                "ExtraUnit": "",
            }
        ]
    }
    result = parse_response(
        payload,
        account=make_account(secondary_labels={}),
        fetched_at=datetime.now(UTC),
    )
    assert result.lines[0].consumed_gb == pytest.approx(512.0)  # 0.5 TB
    assert result.lines[0].quota_gb == pytest.approx(2048.0)  # 2 TB


def test_lowercase_unit_normalized():
    payload = {
        "FreeUnitsValue": [
            {
                "DisplayName": "Alfanet 500GB",
                "SubDisplayName": "Data",
                "UsageType": "data",
                "UsedAmount": "12.5",
                "UsedUnit": "gb",
                "TotalAmount": "500",
                "TotalUnit": "Gb",
            }
        ]
    }
    result = parse_response(
        payload,
        account=make_account(secondary_labels={}),
        fetched_at=datetime.now(UTC),
    )
    assert result.lines[0].consumed_gb == pytest.approx(12.5)
    assert result.lines[0].quota_gb == pytest.approx(500.0)


def test_missing_free_units_raises_unknown():
    with pytest.raises(UnknownFetchError, match="FreeUnitsValue"):
        parse_response(
            {},
            account=make_account(secondary_labels={}),
            fetched_at=datetime.now(UTC),
        )


def test_empty_free_units_raises_unknown():
    with pytest.raises(UnknownFetchError, match="FreeUnitsValue"):
        parse_response(
            {"FreeUnitsValue": []},
            account=make_account(secondary_labels={}),
            fetched_at=datetime.now(UTC),
        )


def test_voice_only_raises_no_consumption_data_error():
    """Voice-only / alarm SIMs have no data bundle in getconsumptionasync.
    We raise NoConsumptionDataError so the fetcher can fall back to
    getmyservices for the assigned bundle."""
    from carriers_sync.providers.base import NoConsumptionDataError

    payload = {
        "FreeUnitsValue": [
            {
                "DisplayName": "Free Minutes",
                "SubDisplayName": "Voice",
                "UsageType": "voice",
                "UsedAmount": "0",
                "UsedUnit": "MIN",
                "TotalAmount": "60",
                "TotalUnit": "MIN",
            }
        ]
    }
    with pytest.raises(NoConsumptionDataError, match="no supported service"):
        parse_response(
            payload,
            account=make_account(secondary_labels={}),
            fetched_at=datetime.now(UTC),
        )


def test_parse_services_finds_active_mobile_internet_bundle():
    payload = json.loads((FIXTURES / "alfa_getmyservices_response.json").read_text())
    result = parse_services(
        payload,
        account=make_account(secondary_labels={}),
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    assert len(result.lines) == 1
    main = result.lines[0]
    assert main.line_id == "03333333"
    assert main.consumed_gb == 0.0  # endpoint doesn't expose usage
    assert main.quota_gb == pytest.approx(7.0)
    assert main.is_secondary is False


def test_parse_services_no_mobile_internet_returns_no_plan():
    payload = [
        {"Name": "CLIP", "ActiveBundle": None},
        {"Name": "Detailed Bill", "ActiveBundle": None},
    ]
    result = parse_services(
        payload,
        account=make_account(secondary_labels={}),
        fetched_at=datetime.now(UTC),
    )
    main = result.lines[0]
    assert main.consumed_gb == 0.0
    assert main.quota_gb is None  # signals no plan


def test_parse_services_active_but_payg_returns_no_plan():
    payload = [
        {
            "Name": "Mobile Internet",
            "ActiveBundle": {
                "Text": "PAYG",
                "TextEn": "PAYG",
                "Selected": True,
            },
        }
    ]
    result = parse_services(
        payload,
        account=make_account(secondary_labels={}),
        fetched_at=datetime.now(UTC),
    )
    assert result.lines[0].quota_gb is None


def test_parse_services_active_but_unselected_returns_no_plan():
    payload = [
        {
            "Name": "Mobile Internet",
            "ActiveBundle": {"Text": "7GB", "TextEn": "7GB", "Selected": False},
        }
    ]
    result = parse_services(
        payload,
        account=make_account(secondary_labels={}),
        fetched_at=datetime.now(UTC),
    )
    assert result.lines[0].quota_gb is None


def test_parse_services_handles_mb_units():
    payload = [
        {
            "Name": "Mobile Internet",
            "ActiveBundle": {"Text": "500MB", "TextEn": "500MB", "Selected": True},
        }
    ]
    result = parse_services(
        payload,
        account=make_account(secondary_labels={}),
        fetched_at=datetime.now(UTC),
    )
    assert result.lines[0].quota_gb == pytest.approx(500 / 1024, abs=0.001)


def test_parse_services_invalid_payload_raises_unknown():
    with pytest.raises(UnknownFetchError, match="not a JSON array"):
        parse_services(
            {"not": "a list"},
            account=make_account(secondary_labels={}),
            fetched_at=datetime.now(UTC),
        )
