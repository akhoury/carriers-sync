"""Persists last successful ProviderResult per account and the set of
currently-published discovery entities. Used to repopulate sensors after
container restart and to clean up entities for removed accounts.

Storage is a single JSON file at /data/state.json. Writes are atomic
(temp file + os.replace). On corruption, the file is moved aside and
fresh state is returned.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from carriers_sync.providers.base import LineUsage, MissingLine, ProviderResult

logger = logging.getLogger("carriers_sync.state")

# Bumped whenever a field the scheduler *republishes from* is added, so that
# results written by an older build are discarded rather than republished with
# the new field silently defaulted.
#
# 2: adds LineUsage.is_aggregate and ProviderResult.missing_secondaries. A v1
#    file has neither, and defaulting is_aggregate to False makes
#    _main_state_message re-add the secondaries to an already-pooled total —
#    republishing a confidently wrong number (e.g. 172 + 172 = 344 GB) on the
#    first start after upgrading. Dropping the cache costs one poll interval of
#    "unknown", which is honest; the wrong total is not.
STATE_VERSION = 2


@dataclass
class State:
    last_results: dict[str, ProviderResult] = field(default_factory=dict)
    last_published_entities: set[str] = field(default_factory=set)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> State:
        if not self.path.exists():
            return State()
        try:
            raw = json.loads(self.path.read_text())
        except json.JSONDecodeError as e:
            logger.warning("state.json is corrupt (%s); starting fresh", e)
            self._move_aside_corrupt()
            return State()

        version = raw.get("version", 1)
        if version < STATE_VERSION:
            logger.warning(
                "state.json is v%s (expected v%s): discarding cached results rather "
                "than republishing them with missing fields defaulted. Values will "
                "repopulate on the next sync.",
                version,
                STATE_VERSION,
            )
            return State(
                last_published_entities=set(raw.get("last_published_entities", []))
            )

        results: dict[str, ProviderResult] = {}
        for acct_id, payload in raw.get("last_results", {}).items():
            try:
                results[acct_id] = _result_from_dict(payload)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("dropping corrupt account %s from state: %s", acct_id, e)

        return State(
            last_results=results,
            last_published_entities=set(raw.get("last_published_entities", [])),
        )

    def save(self, state: State) -> None:
        payload = {
            "version": STATE_VERSION,
            "last_results": {
                acct: _result_to_dict(res) for acct, res in state.last_results.items()
            },
            "last_published_entities": sorted(state.last_published_entities),
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp, self.path)

    def _move_aside_corrupt(self) -> None:
        if not self.path.exists():
            return
        backup = self.path.with_suffix(self.path.suffix + ".corrupt")
        try:
            os.replace(self.path, backup)
        except OSError:
            self.path.unlink(missing_ok=True)


def _result_to_dict(r: ProviderResult) -> dict[str, Any]:
    """Serialise a result for disk.

    is_aggregate must round-trip: the scheduler rebuilds MQTT messages from
    restored results, and losing the flag makes _main_state_message re-add the
    secondaries to an already-pooled total (double counting).

    missing_secondaries must round-trip for the same reason — dropping it would
    republish stale lines as "current" on every restart, which is exactly the
    failure this data exists to prevent.
    """
    return {
        "account_id": r.account_id,
        "fetched_at": r.fetched_at.isoformat(),
        "main_own_consumed_gb": r.main_own_consumed_gb,
        "lines": [
            {
                "line_id": line.line_id,
                "label": line.label,
                "consumed_gb": line.consumed_gb,
                "quota_gb": line.quota_gb,
                "extra_consumed_gb": line.extra_consumed_gb,
                "is_secondary": line.is_secondary,
                "parent_line_id": line.parent_line_id,
                "is_aggregate": line.is_aggregate,
            }
            for line in r.lines
        ],
        "missing_secondaries": [
            {"line_id": m.line_id, "label": m.label, "reason": m.reason}
            for m in r.missing_secondaries
        ],
    }


def _result_from_dict(d: dict[str, Any]) -> ProviderResult:
    """Rehydrate a result. Fields added after 0.5.0 are read defensively so a
    state.json written by an older build still loads."""
    raw_own = d.get("main_own_consumed_gb")
    return ProviderResult(
        account_id=d["account_id"],
        fetched_at=datetime.fromisoformat(d["fetched_at"]),
        lines=[
            LineUsage(
                line_id=line["line_id"],
                label=line["label"],
                consumed_gb=float(line["consumed_gb"]),
                quota_gb=None if line["quota_gb"] is None else float(line["quota_gb"]),
                extra_consumed_gb=float(line["extra_consumed_gb"]),
                is_secondary=bool(line["is_secondary"]),
                parent_line_id=line["parent_line_id"],
                is_aggregate=bool(line.get("is_aggregate", False)),
            )
            for line in d["lines"]
        ],
        missing_secondaries=[
            MissingLine(
                line_id=m["line_id"],
                label=m.get("label") or m["line_id"],
                reason=m.get("reason", "not_in_response"),
            )
            for m in d.get("missing_secondaries", [])
        ],
        main_own_consumed_gb=None if raw_own is None else float(raw_own),
    )
