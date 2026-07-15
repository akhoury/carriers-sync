import json

import pytest
from carriers_sync.config import AppConfig, ConfigError, load_config
from carriers_sync.providers.base import AccountConfig


def write_options(tmp_path, payload):
    p = tmp_path / "options.json"
    p.write_text(json.dumps(payload))
    return p


def test_load_minimal_valid_config(tmp_path):
    path = write_options(
        tmp_path,
        {
            "poll_interval_minutes": 60,
            "danger_percent": 80,
            "log_level": "info",
            "accounts": [
                {
                    "provider": "alfa-lb",
                    "username": "03333333",
                    "password": "secret",
                    "label": "John",
                    "secondary_labels": [],
                }
            ],
        },
    )
    cfg = load_config(path)
    assert isinstance(cfg, AppConfig)
    assert cfg.poll_interval_minutes == 60
    assert cfg.danger_percent == 80
    assert cfg.log_level == "info"
    assert len(cfg.accounts) == 1
    acct = cfg.accounts[0]
    assert isinstance(acct, AccountConfig)
    assert acct.username == "03333333"
    assert acct.label == "John"
    assert acct.secondary_labels == {}


def test_secondary_labels_strings_to_dict_conversion(tmp_path):
    path = write_options(
        tmp_path,
        {
            "poll_interval_minutes": 60,
            "danger_percent": 80,
            "log_level": "info",
            "accounts": [
                {
                    "provider": "alfa-lb",
                    "username": "03333333",
                    "password": "p",
                    "label": "John",
                    "secondary_labels": [
                        "03222222:Wife",
                        "03111111: Alarm eSIM",
                    ],
                }
            ],
        },
    )
    cfg = load_config(path)
    assert cfg.accounts[0].secondary_labels == {
        "03222222": "Wife",
        "03111111": "Alarm eSIM",  # surrounding whitespace trimmed
    }


def test_secondary_labels_label_may_contain_colon(tmp_path):
    path = write_options(
        tmp_path,
        {
            "poll_interval_minutes": 60,
            "danger_percent": 80,
            "log_level": "info",
            "accounts": [
                {
                    "provider": "alfa-lb",
                    "username": "03333333",
                    "password": "p",
                    "label": "John",
                    "secondary_labels": ["03222222:Home: kitchen"],
                }
            ],
        },
    )
    cfg = load_config(path)
    # Split on the FIRST colon only.
    assert cfg.accounts[0].secondary_labels == {"03222222": "Home: kitchen"}


def test_secondary_labels_missing_colon_raises(tmp_path):
    path = write_options(
        tmp_path,
        {
            "poll_interval_minutes": 60,
            "danger_percent": 80,
            "log_level": "info",
            "accounts": [
                {
                    "provider": "alfa-lb",
                    "username": "03333333",
                    "password": "p",
                    "label": "John",
                    "secondary_labels": ["03222222 Wife"],
                }
            ],
        },
    )
    with pytest.raises(ConfigError, match="number:label"):
        load_config(path)


def test_secondary_labels_old_dict_format_rejected(tmp_path):
    """The pre-0.5.0 dict format must fail with a clear message pointing at
    the new string format."""
    path = write_options(
        tmp_path,
        {
            "poll_interval_minutes": 60,
            "danger_percent": 80,
            "log_level": "info",
            "accounts": [
                {
                    "provider": "alfa-lb",
                    "username": "03333333",
                    "password": "p",
                    "label": "John",
                    "secondary_labels": [{"number": "03222222", "label": "Wife"}],
                }
            ],
        },
    )
    with pytest.raises(ConfigError, match="number:label"):
        load_config(path)


def test_duplicate_usernames_dedup_first_wins(tmp_path):
    path = write_options(
        tmp_path,
        {
            "poll_interval_minutes": 60,
            "danger_percent": 80,
            "log_level": "info",
            "accounts": [
                {
                    "provider": "alfa-lb",
                    "username": "03333333",
                    "password": "a",
                    "label": "First",
                    "secondary_labels": [],
                },
                {
                    "provider": "alfa-lb",
                    "username": "03333333",
                    "password": "b",
                    "label": "Dup",
                    "secondary_labels": [],
                },
            ],
        },
    )
    cfg = load_config(path)
    assert len(cfg.accounts) == 1
    assert cfg.accounts[0].label == "First"


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.json")


def test_malformed_json_raises_config_error(tmp_path):
    path = tmp_path / "options.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="JSON"):
        load_config(path)


def test_missing_required_field_raises(tmp_path):
    path = write_options(
        tmp_path,
        {
            "poll_interval_minutes": 60,
            "danger_percent": 80,
            "accounts": [],
        },
    )
    with pytest.raises(ConfigError, match="log_level"):
        load_config(path)


def test_empty_accounts_list_is_allowed(tmp_path):
    path = write_options(
        tmp_path,
        {
            "poll_interval_minutes": 60,
            "danger_percent": 80,
            "log_level": "info",
            "accounts": [],
        },
    )
    cfg = load_config(path)
    assert cfg.accounts == []


def test_invalid_provider_rejected(tmp_path):
    path = write_options(
        tmp_path,
        {
            "poll_interval_minutes": 60,
            "danger_percent": 80,
            "log_level": "info",
            "accounts": [
                {
                    "provider": "unknown-co",
                    "username": "x",
                    "password": "y",
                    "label": "z",
                    "secondary_labels": [],
                }
            ],
        },
    )
    with pytest.raises(ConfigError, match="provider"):
        load_config(path)


def test_invalid_poll_interval_rejected(tmp_path):
    path = write_options(
        tmp_path,
        {
            "poll_interval_minutes": 1,
            "danger_percent": 80,
            "log_level": "info",
            "accounts": [],
        },
    )
    with pytest.raises(ConfigError, match="poll_interval_minutes"):
        load_config(path)
