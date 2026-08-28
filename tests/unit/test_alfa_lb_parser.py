import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from carriers_sync.providers.alfa_lb import parse_response, parse_services
from carriers_sync.providers.base import (
    AccountConfig,
    NoConsumptionDataError,
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
    """Captured before Alfa reintroduced per-twin entries: this payload has a
    'U-share Total Bundle' aggregate and a 'U-share Main' line but no
    'Twin-Data Secondary' entries at all. The main line must still be the
    aggregate, and the two configured secondaries must be reported as missing
    rather than silently dropped."""
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

    # No twin entries in this payload, so both configured secondaries are
    # unaccounted for. The bundle itself is live, so this is the per-line
    # failure mode, not a deactivated bundle.
    assert [(m.line_id, m.reason) for m in result.missing_secondaries] == [
        ("03111111", "not_in_response"),
        ("03222222", "not_in_response"),
    ]
    # Nothing to subtract, so main-only usage equals the aggregate.
    assert result.main_own_consumed_gb == pytest.approx(14.20)


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


# --------------------------------------------------------------------------
# Twin-Data Secondary entries (Alfa restored per-twin usage in a new shape:
# one FreeUnitsValue entry per active twin, MSISDN in PrimaryNumber).
# --------------------------------------------------------------------------


def twin_account(secondary_labels=None):
    return AccountConfig(
        provider="alfa-lb",
        username="70200001",
        password="x",
        label="Laure",
        secondary_labels=(
            secondary_labels
            if secondary_labels is not None
            else {"70300001": "Josephine", "70300002": "S22"}
        ),
    )


def test_parse_ushare_reports_twin_secondaries():
    """Each 'Twin-Data Secondary' entry becomes a secondary line, keyed by
    PrimaryNumber and labelled from secondary_labels."""
    result = parse_response(
        load("alfa_ushare_twins.json"),
        account=twin_account(),
        fetched_at=datetime.now(UTC),
    )

    assert len(result.lines) == 3
    main, *twins = result.lines

    assert main.is_secondary is False
    assert main.is_aggregate is True
    assert main.consumed_gb == pytest.approx(25.65)
    assert main.quota_gb == pytest.approx(85.0)

    assert [(t.line_id, t.label, t.consumed_gb) for t in twins] == [
        ("70300001", "Josephine", pytest.approx(8.93)),
        ("70300002", "S22", pytest.approx(15.64)),
    ]
    for t in twins:
        assert t.is_secondary is True
        assert t.parent_line_id == "70200001"
        # TotalAmount on a twin repeats the shared pool quota (85), which would
        # render a meaningless per-line percentage. Must stay None.
        assert t.quota_gb is None

    assert result.missing_secondaries == []


def test_main_own_usage_derived_from_aggregate_minus_twins():
    """'U-share Main' now mirrors the pooled total (25.65 == 25.65), so the
    main number's own usage has to be derived: 25.65 - (8.93 + 15.64)."""
    result = parse_response(
        load("alfa_ushare_twins.json"),
        account=twin_account(),
        fetched_at=datetime.now(UTC),
    )
    assert result.main_own_consumed_gb == pytest.approx(1.08)


def test_twin_label_falls_back_to_number_when_unlabelled():
    result = parse_response(
        load("alfa_ushare_twins.json"),
        account=twin_account(secondary_labels={"70300001": "Josephine"}),
        fetched_at=datetime.now(UTC),
    )
    twins = [line for line in result.lines if line.is_secondary]
    assert [t.label for t in twins] == ["Josephine", "70300002"]
    # Unlabelled but present: reported, not missing.
    assert result.missing_secondaries == []


def test_deactivated_ushare_reports_bundle_inactive():
    """When Alfa deactivates the twins the whole U-share block disappears and
    the account reverts to a plain bundle. Every configured secondary is then
    missing for the same reason, which is the case that needs a call to Alfa."""
    result = parse_response(
        load("alfa_ushare_deactivated.json"),
        account=twin_account(),
        fetched_at=datetime.now(UTC),
    )

    assert len(result.lines) == 1
    main = result.lines[0]
    assert main.is_aggregate is False
    assert main.consumed_gb == pytest.approx(0.0)
    assert main.quota_gb == pytest.approx(7.0)

    assert [(m.line_id, m.label, m.reason) for m in result.missing_secondaries] == [
        ("70300001", "Josephine", "bundle_inactive"),
        ("70300002", "S22", "bundle_inactive"),
    ]
    # No aggregate to subtract from, so main-only usage is not derivable.
    assert result.main_own_consumed_gb is None


def test_partial_twin_absence_reports_not_in_response():
    """Bundle live but one configured number absent: only that line is stale."""
    result = parse_response(
        load("alfa_ushare_twins.json"),
        account=twin_account(
            secondary_labels={
                "70300001": "Josephine",
                "70300002": "S22",
                "70300003": "Third",
            }
        ),
        fetched_at=datetime.now(UTC),
    )
    assert len(result.lines) == 3
    assert [(m.line_id, m.reason) for m in result.missing_secondaries] == [
        ("70300003", "not_in_response")
    ]


def test_twin_without_primary_number_is_skipped():
    payload = load("alfa_ushare_twins.json")
    for e in payload["FreeUnitsValue"]:
        if e["DisplayName"] == "Twin-Data Secondary" and e["PrimaryNumber"] == "70300002":
            e["PrimaryNumber"] = None
    result = parse_response(payload, account=twin_account(), fetched_at=datetime.now(UTC))

    twins = [line for line in result.lines if line.is_secondary]
    assert [t.line_id for t in twins] == ["70300001"]
    # Unidentifiable, so it counts as missing rather than vanishing.
    assert [m.line_id for m in result.missing_secondaries] == ["70300002"]


def test_unreadable_twin_usage_does_not_break_the_account():
    payload = load("alfa_ushare_twins.json")
    for e in payload["FreeUnitsValue"]:
        if e["DisplayName"] == "Twin-Data Secondary" and e["PrimaryNumber"] == "70300001":
            e["UsedAmount"] = "not-a-number"
    result = parse_response(payload, account=twin_account(), fetched_at=datetime.now(UTC))

    # The good twin and the main line survive.
    assert [line.line_id for line in result.lines] == ["70200001", "70300002"]
    assert [m.line_id for m in result.missing_secondaries] == ["70300001"]


def test_duplicate_twin_entries_are_deduplicated():
    payload = load("alfa_ushare_twins.json")
    dup = next(
        dict(e)
        for e in payload["FreeUnitsValue"]
        if e["DisplayName"] == "Twin-Data Secondary" and e["PrimaryNumber"] == "70300001"
    )
    dup["UsedAmount"] = "99.99"
    payload["FreeUnitsValue"].append(dup)

    result = parse_response(payload, account=twin_account(), fetched_at=datetime.now(UTC))
    twins = [line for line in result.lines if line.is_secondary]
    assert [t.line_id for t in twins] == ["70300001", "70300002"]
    # First entry wins; the 99.99 duplicate is ignored.
    assert twins[0].consumed_gb == pytest.approx(8.93)


def test_twin_entry_never_selected_as_the_accounts_own_bundle():
    """A payload with twins but no 'U-share Total Bundle'/'U-share Main' must
    not fall through and pick a twin as the main line."""
    payload = load("alfa_ushare_twins.json")
    payload["FreeUnitsValue"] = [
        e
        for e in payload["FreeUnitsValue"]
        if e["DisplayName"] not in ("U-share Total Bundle", "U-share Main")
    ]
    with pytest.raises(NoConsumptionDataError):
        parse_response(payload, account=twin_account(), fetched_at=datetime.now(UTC))
