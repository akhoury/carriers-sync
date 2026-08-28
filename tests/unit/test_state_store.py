from datetime import UTC, datetime

from carriers_sync.providers.base import LineUsage, MissingLine, ProviderResult
from carriers_sync.state_store import State, StateStore


def make_result(account_id="03333333"):
    return ProviderResult(
        account_id=account_id,
        lines=[
            LineUsage(
                line_id=account_id,
                label="John",
                consumed_gb=1.5,
                quota_gb=20.0,
                extra_consumed_gb=0.0,
                is_secondary=False,
                parent_line_id=None,
            )
        ],
        fetched_at=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
    )


def test_empty_store_returns_default_state(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = store.load()
    assert state.last_results == {}
    assert state.last_published_entities == set()


def test_round_trip(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = State(
        last_results={"03333333": make_result()},
        last_published_entities={"sensor.alfa_john_consumed_gb"},
    )
    store.save(state)
    loaded = store.load()
    assert "03333333" in loaded.last_results
    assert loaded.last_results["03333333"].lines[0].consumed_gb == 1.5
    assert "sensor.alfa_john_consumed_gb" in loaded.last_published_entities


def test_atomic_write_no_leftover_tmp(tmp_path):
    target = tmp_path / "state.json"
    store = StateStore(target)
    store.save(State(last_results={"03333333": make_result()}, last_published_entities=set()))
    assert not (tmp_path / "state.json.tmp").exists()
    assert target.exists()


def test_corrupt_file_recovers_with_warning(tmp_path, caplog):
    import logging

    target = tmp_path / "state.json"
    target.write_text("{this is not json")
    store = StateStore(target)
    with caplog.at_level(logging.WARNING):
        state = store.load()
    assert state.last_results == {}
    assert any("corrupt" in r.message.lower() for r in caplog.records)


def test_save_then_load_preserves_unicode_labels(tmp_path):
    store = StateStore(tmp_path / "state.json")
    res = ProviderResult(
        account_id="03333333",
        lines=[
            LineUsage(
                line_id="03333333",
                label="جون",
                consumed_gb=1.0,
                quota_gb=20.0,
                extra_consumed_gb=0.0,
                is_secondary=False,
                parent_line_id=None,
            )
        ],
        fetched_at=datetime(2026, 4, 28, tzinfo=UTC),
    )
    store.save(State(last_results={"03333333": res}, last_published_entities=set()))
    loaded = store.load()
    assert loaded.last_results["03333333"].lines[0].label == "جون"


def make_aggregate_result():
    """An Alfa U-share result: pooled main line, two twins, one deactivated."""
    return ProviderResult(
        account_id="70200001",
        lines=[
            LineUsage(
                line_id="70200001",
                label="Laure",
                consumed_gb=25.65,
                quota_gb=85.0,
                extra_consumed_gb=0.0,
                is_secondary=False,
                parent_line_id=None,
                is_aggregate=True,
            ),
            LineUsage(
                line_id="70300001",
                label="Josephine",
                consumed_gb=8.93,
                quota_gb=None,
                extra_consumed_gb=0.0,
                is_secondary=True,
                parent_line_id="70200001",
            ),
        ],
        fetched_at=datetime(2026, 8, 28, 16, 1, tzinfo=UTC),
        missing_secondaries=[
            MissingLine(line_id="70300002", label="S22", reason="bundle_inactive")
        ],
        main_own_consumed_gb=1.08,
    )


def test_is_aggregate_survives_round_trip(tmp_path):
    """The scheduler rebuilds MQTT messages from restored results. If
    is_aggregate is lost, _main_state_message re-adds the twins to an already
    pooled total and the account double-counts after every restart."""
    store = StateStore(tmp_path / "state.json")
    store.save(State(last_results={"70200001": make_aggregate_result()}))
    main = store.load().last_results["70200001"].lines[0]
    assert main.is_aggregate is True


def test_missing_secondaries_survive_round_trip(tmp_path):
    """Otherwise a restart republishes deactivated lines as stale=OFF, i.e. as
    current — the exact failure this data exists to prevent."""
    store = StateStore(tmp_path / "state.json")
    store.save(State(last_results={"70200001": make_aggregate_result()}))
    restored = store.load().last_results["70200001"]
    assert [(m.line_id, m.label, m.reason) for m in restored.missing_secondaries] == [
        ("70300002", "S22", "bundle_inactive")
    ]
    assert restored.main_own_consumed_gb == 1.08


def test_v1_state_is_discarded_rather_than_republished(tmp_path):
    """A v1 file has no is_aggregate. Defaulting it to False makes the scheduler
    republish a pooled total with the secondaries added on top — a confidently
    wrong number. Dropping the cache costs one poll interval of 'unknown'."""
    path = tmp_path / "state.json"
    path.write_text(
        """{
          "last_results": {
            "03333333": {
              "account_id": "03333333",
              "fetched_at": "2026-04-28T12:00:00+00:00",
              "lines": [{
                "line_id": "03333333", "label": "John", "consumed_gb": 172.0,
                "quota_gb": 2000.0, "extra_consumed_gb": 0.0,
                "is_secondary": false, "parent_line_id": null
              }]
            }
          },
          "last_published_entities": ["sensor.keep_me"]
        }"""
    )
    state = StateStore(path).load()
    assert state.last_results == {}
    # Published-entity tracking must survive, or cleanup would orphan entities.
    assert state.last_published_entities == {"sensor.keep_me"}


def test_saved_state_declares_its_version(tmp_path):
    import json as _json

    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save(State(last_results={"70200001": make_aggregate_result()}))
    assert _json.loads(path.read_text())["version"] == 2
    # ...and a file we just wrote is accepted on load.
    assert "70200001" in store.load().last_results


def test_state_written_by_an_older_build_still_loads(tmp_path):
    """A v2 file may still omit the optional per-result fields."""
    path = tmp_path / "state.json"
    path.write_text(
        """{
          "version": 2,
          "last_results": {
            "03333333": {
              "account_id": "03333333",
              "fetched_at": "2026-04-28T12:00:00+00:00",
              "lines": [{
                "line_id": "03333333", "label": "John", "consumed_gb": 1.5,
                "quota_gb": 20.0, "extra_consumed_gb": 0.0,
                "is_secondary": false, "parent_line_id": null
              }]
            }
          },
          "last_published_entities": []
        }"""
    )
    restored = StateStore(path).load().last_results["03333333"]
    assert restored.lines[0].is_aggregate is False
    assert restored.missing_secondaries == []
    assert restored.main_own_consumed_gb is None
