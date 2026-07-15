"""Reads, validates, and shapes /data/options.json into typed dataclasses.

The on-disk format is what HA's Supervisor writes from the user's options form;
shape mismatches between disk and runtime (e.g. secondary_labels list -> dict)
are reconciled here so the rest of the codebase sees only typed dataclasses.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from carriers_sync.providers.base import AccountConfig

logger = logging.getLogger("carriers_sync.config")

KNOWN_PROVIDERS = {"alfa-lb", "touch-lb", "ogero-lb"}
VALID_LOG_LEVELS = {"trace", "debug", "info", "notice", "warning", "error", "fatal"}


class ConfigError(Exception):
    """Raised when /data/options.json is missing, malformed, or invalid."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    poll_interval_minutes: int
    danger_percent: int
    log_level: str
    accounts: list[AccountConfig]


def load_config(path: Path) -> AppConfig:
    """Read and validate options.json. Raise ConfigError on any problem."""
    if not path.exists():
        raise ConfigError(f"Options file not found: {path}")

    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"options.json is not valid JSON: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError("options.json root must be an object")

    poll = _require(raw, "poll_interval_minutes", int)
    danger = _require(raw, "danger_percent", int)
    log_level = _require(raw, "log_level", str)
    accounts_raw = _require(raw, "accounts", list)

    if log_level not in VALID_LOG_LEVELS:
        raise ConfigError(f"log_level must be one of {sorted(VALID_LOG_LEVELS)}, got {log_level!r}")
    if not (5 <= poll <= 1440):
        raise ConfigError(f"poll_interval_minutes must be in [5, 1440], got {poll}")
    if not (1 <= danger <= 100):
        raise ConfigError(f"danger_percent must be in [1, 100], got {danger}")

    accounts: list[AccountConfig] = []
    seen_usernames: set[str] = set()
    for i, item in enumerate(accounts_raw):
        if not isinstance(item, dict):
            raise ConfigError(f"accounts[{i}] must be an object")
        acct = _parse_account(item, index=i)
        if acct.username in seen_usernames:
            logger.warning(
                "Duplicate account username %s — keeping first, dropping later entry",
                acct.username,
            )
            continue
        seen_usernames.add(acct.username)
        accounts.append(acct)

    return AppConfig(
        poll_interval_minutes=poll,
        danger_percent=danger,
        log_level=log_level,
        accounts=accounts,
    )


def _parse_account(item: dict[str, Any], *, index: int) -> AccountConfig:
    provider = _require(item, "provider", str, ctx=f"accounts[{index}]")
    if provider not in KNOWN_PROVIDERS:
        raise ConfigError(
            f"accounts[{index}].provider must be one of {sorted(KNOWN_PROVIDERS)}, got {provider!r}"
        )
    username = _require(item, "username", str, ctx=f"accounts[{index}]")
    password = _require(item, "password", str, ctx=f"accounts[{index}]")
    label = _require(item, "label", str, ctx=f"accounts[{index}]")
    raw_secondaries = item.get("secondary_labels", [])
    if not isinstance(raw_secondaries, list):
        raise ConfigError(f"accounts[{index}].secondary_labels must be a list")

    # Each entry is a "number:label" string (e.g. "70301234:Wife"). This flat
    # format lets Home Assistant mark the whole list optional; a nested
    # list-of-dicts (the pre-0.5.0 format) cannot be omitted in the App schema.
    secondary_labels: dict[str, str] = {}
    for j, entry in enumerate(raw_secondaries):
        ctx = f"accounts[{index}].secondary_labels[{j}]"
        if not isinstance(entry, str):
            raise ConfigError(
                f"{ctx} must be a 'number:label' string (e.g. '70301234:Wife'); "
                f"the pre-0.5.0 {{number, label}} object format is no longer supported"
            )
        number, sep, slabel = entry.partition(":")
        number = number.strip()
        slabel = slabel.strip()
        if not sep or not number or not slabel:
            raise ConfigError(f"{ctx} must be 'number:label' (e.g. '70301234:Wife'), got {entry!r}")
        if not number.isdigit():
            raise ConfigError(f"{ctx} number part must be digits, got {number!r}")
        secondary_labels[number] = slabel

    return AccountConfig(
        provider=provider,
        username=username,
        password=password,
        label=label,
        secondary_labels=secondary_labels,
    )


def _require(d: dict[str, Any], key: str, expected_type: type, *, ctx: str = "") -> Any:
    if key not in d:
        prefix = f"{ctx}." if ctx else ""
        raise ConfigError(f"missing required field: {prefix}{key}")
    value = d[key]
    if not isinstance(value, expected_type):
        prefix = f"{ctx}." if ctx else ""
        raise ConfigError(
            f"{prefix}{key} must be {expected_type.__name__}, got {type(value).__name__}"
        )
    return value
