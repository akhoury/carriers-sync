"""Pure functions that turn ProviderResults into MQTT discovery + state messages.

No MQTT client work — just shape building. This file is fully unit-tested.

All identifiers, unique_ids, object_ids, and MQTT topics are namespaced as
`carriers_sync_<provider>_<line_id>[_<metric>]` (with `<provider>` being the
provider id with dashes replaced by underscores, e.g. `alfa_lb`, `touch_lb`).
This guards against cross-provider collisions if line_ids ever overlap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from carriers_sync.providers.base import LineUsage, ProviderResult

AVAILABILITY_TOPIC = "carriers_sync/availability"
APP_DEVICE_ID = "carriers_sync_app"


@dataclass(frozen=True)
class MqttMessage:
    topic: str
    payload: Any
    retain: bool = True


def build_app_device_messages() -> list[MqttMessage]:
    """Singleton App device with refresh-all button and status sensor."""
    device = {
        "identifiers": [APP_DEVICE_ID],
        "name": "Carriers Sync",
        "manufacturer": "carriers-sync",
        "model": "Home Assistant App",
    }
    # Note on entity_ids: HA's MQTT integration ignores `object_id` when an
    # entity has a device association (it derives entity_id from
    # device.name + entity.name). We keep object_id set anyway in case HA
    # ever changes that behavior, but don't rely on it for naming.
    msgs: list[MqttMessage] = [
        MqttMessage(
            topic="homeassistant/button/carriers_sync_refresh_all/config",
            payload={
                "name": "Refresh all",
                "unique_id": "carriers_sync_refresh_all",
                "object_id": "carriers_sync_refresh_all",
                "command_topic": "carriers_sync/refresh_all/cmd",
                "device": device,
                "availability_topic": AVAILABILITY_TOPIC,
            },
        ),
        MqttMessage(
            topic="homeassistant/sensor/carriers_sync_app_status/config",
            payload={
                "name": "App status",
                "unique_id": "carriers_sync_app_status",
                "object_id": "carriers_sync_app_status",
                "state_topic": "carriers_sync/app/state",
                "value_template": "{{ value_json.status }}",
                "device": device,
                "availability_topic": AVAILABILITY_TOPIC,
            },
        ),
    ]
    return msgs


def _pid(provider_id: str) -> str:
    """Provider id slug for use in unique_ids and topic paths.

    `alfa-lb` -> `alfa_lb`, `touch-lb` -> `touch_lb`.
    """
    return provider_id.replace("-", "_")


def build_account_messages(
    result: ProviderResult,
    *,
    danger_percent: int,
    provider_display: str,
    provider_id: str,
) -> list[MqttMessage]:
    """Discovery + state messages for one account and its secondaries."""
    messages: list[MqttMessage] = []
    pid = _pid(provider_id)

    main = next(line for line in result.lines if not line.is_secondary)
    secondaries = [line for line in result.lines if line.is_secondary]

    main_device = _device_dict(
        main.line_id, main.label, main.is_secondary, provider_display, pid, parent=None
    )
    messages.extend(
        _main_discovery_messages(
            main,
            main_device,
            account_id=result.account_id,
            pid=pid,
            has_main_own=result.main_own_consumed_gb is not None,
        )
    )
    messages.append(_main_state_message(result, main, secondaries, danger_percent, pid=pid))

    for sec in secondaries:
        sec_device = _device_dict(
            sec.line_id, sec.label, sec.is_secondary, provider_display, pid, parent=main.line_id
        )
        messages.extend(
            _secondary_discovery_messages(
                sec.line_id,
                sec_device,
                account_id=result.account_id,
                pid=pid,
                has_quota=sec.quota_gb is not None,
            )
        )
        messages.append(
            _secondary_state_message(
                sec,
                account_id=result.account_id,
                danger_percent=danger_percent,
                pid=pid,
                fetched_at=result.fetched_at,
            )
        )
        messages.append(
            _secondary_status_message(
                sec.line_id, account_id=result.account_id, pid=pid, stale=False, reason=""
            )
        )

    # Configured secondaries the carrier did not report. We deliberately do NOT
    # publish to their state topic: the retained last-known consumed_gb and
    # last_seen stay put, so Home Assistant keeps showing the last real reading
    # instead of blanking it. The status topic is what flips them to stale.
    for miss in result.missing_secondaries:
        miss_device = _device_dict(
            miss.line_id, miss.label, True, provider_display, pid, parent=main.line_id
        )
        messages.extend(
            _secondary_discovery_messages(
                miss.line_id,
                miss_device,
                account_id=result.account_id,
                pid=pid,
                has_quota=False,
            )
        )
        messages.append(
            _secondary_status_message(
                miss.line_id,
                account_id=result.account_id,
                pid=pid,
                stale=True,
                reason=miss.reason,
            )
        )

    return messages


def _device_dict(
    line_id: str,
    label: str,
    is_secondary: bool,
    manufacturer: str,
    pid: str,
    *,
    parent: str | None,
) -> dict[str, Any]:
    # Short prefix derived from the manufacturer string ("Alfa (Lebanon)" -> "Alfa",
    # "Touch (Lebanon)" -> "Touch"). Avoids hardcoding any one provider in
    # the device name.
    short = manufacturer.split(" (")[0] if " (" in manufacturer else manufacturer
    d: dict[str, Any] = {
        "identifiers": [f"carriers_sync_{pid}_{line_id}"],
        "name": f"{short}: {label}",
        "manufacturer": manufacturer,
        "model": "Secondary line" if is_secondary else "Account",
    }
    if parent:
        d["via_device"] = f"carriers_sync_{pid}_{parent}"
    return d


def _main_discovery_messages(
    line: LineUsage,
    device: dict[str, Any],
    *,
    account_id: str,
    pid: str,
    has_main_own: bool = False,
) -> list[MqttMessage]:
    state_topic = f"carriers_sync/{pid}/{account_id}/state"
    cmd_topic = f"carriers_sync/{pid}/{account_id}/refresh/cmd"
    base: dict[str, Any] = {
        "device": device,
        "availability_topic": AVAILABILITY_TOPIC,
        "state_topic": state_topic,
    }

    def sensor(
        metric: str,
        *,
        unit: str | None = None,
        device_class: str | None = None,
        state_class: str | None = None,
        name: str,
    ) -> MqttMessage:
        slug = f"carriers_sync_{pid}_{line.line_id}_{metric}"
        cfg: dict[str, Any] = {
            **base,
            "name": name,
            "unique_id": slug,
            "object_id": slug,
            "value_template": "{{ value_json." + metric + " }}",
        }
        if unit:
            cfg["unit_of_measurement"] = unit
        if device_class:
            cfg["device_class"] = device_class
        if state_class:
            cfg["state_class"] = state_class
        return MqttMessage(
            topic=f"homeassistant/sensor/{slug}/config",
            payload=cfg,
        )

    def binary(metric: str, *, device_class: str, name: str) -> MqttMessage:
        slug = f"carriers_sync_{pid}_{line.line_id}_{metric}"
        return MqttMessage(
            topic=f"homeassistant/binary_sensor/{slug}/config",
            payload={
                **base,
                "name": name,
                "unique_id": slug,
                "object_id": slug,
                "value_template": "{{ value_json." + metric + " }}",
                "device_class": device_class,
                "payload_on": "ON",
                "payload_off": "OFF",
            },
        )

    refresh_slug = f"carriers_sync_{pid}_{line.line_id}_refresh"
    extra: list[MqttMessage] = []
    if has_main_own:
        # Only emitted when the carrier reports a pooled aggregate, so this is
        # the main number's own share of the pool rather than the pool total.
        extra.append(
            sensor(
                "main_own_consumed_gb",
                unit="GB",
                device_class="data_size",
                state_class="total_increasing",
                name="Main line consumed",
            )
        )
    return extra + [
        sensor(
            "consumed_gb",
            unit="GB",
            device_class="data_size",
            state_class="total_increasing",
            name="Consumed",
        ),
        sensor(
            "total_consumed_gb",
            unit="GB",
            device_class="data_size",
            state_class="total_increasing",
            name="Total consumed",
        ),
        sensor(
            "quota_gb",
            unit="GB",
            device_class="data_size",
            state_class="measurement",
            name="Quota",
        ),
        sensor(
            "remaining_gb",
            unit="GB",
            device_class="data_size",
            state_class="measurement",
            name="Remaining",
        ),
        sensor("usage_percent", unit="%", state_class="measurement", name="Usage percent"),
        sensor(
            "extra_consumed_gb",
            unit="GB",
            device_class="data_size",
            state_class="measurement",
            name="Extra consumed",
        ),
        binary("danger", device_class="problem", name="Danger"),
        binary("sync_ok", device_class="connectivity", name="Sync OK"),
        binary(
            "secondaries_stale",
            device_class="problem",
            name="Secondary lines stale",
        ),
        sensor(
            "secondaries_missing",
            state_class="measurement",
            name="Secondary lines missing",
        ),
        sensor("secondaries_missing_detail", name="Secondary lines missing detail"),
        sensor("last_synced", device_class="timestamp", name="Last synced"),
        sensor("last_attempted", device_class="timestamp", name="Last attempted"),
        sensor("last_error", name="Last error"),
        MqttMessage(
            topic=f"homeassistant/button/{refresh_slug}/config",
            payload={
                "name": "Refresh",
                "unique_id": refresh_slug,
                "object_id": refresh_slug,
                "command_topic": cmd_topic,
                "device": device,
                "availability_topic": AVAILABILITY_TOPIC,
            },
        ),
    ]


def _secondary_discovery_messages(
    line_id: str,
    device: dict[str, Any],
    *,
    account_id: str,
    pid: str,
    has_quota: bool,
) -> list[MqttMessage]:
    """Discovery messages for a secondary/peer line.

    Always includes consumed_gb, a last_seen timestamp and a stale flag. When
    the secondary has its own quota (Touch lines, never Alfa U-share twins),
    also publishes quota/remaining/usage_percent/danger sensors.

    consumed_gb and last_seen read the line's state topic, which is only
    republished when the carrier actually reports the line — so they retain the
    last real reading. stale reads a separate status topic that is republished
    every cycle, which is what lets a card show "8.93 GB, as of Tuesday" rather
    than presenting a frozen number as current.
    """
    state_topic = f"carriers_sync/{pid}/{account_id}/{line_id}/state"
    status_topic = f"carriers_sync/{pid}/{account_id}/{line_id}/status"
    base_cfg: dict[str, Any] = {
        "device": device,
        "availability_topic": AVAILABILITY_TOPIC,
        "state_topic": state_topic,
    }
    status_cfg: dict[str, Any] = {
        "device": device,
        "availability_topic": AVAILABILITY_TOPIC,
        "state_topic": status_topic,
    }

    def _slug(metric: str) -> str:
        return f"carriers_sync_{pid}_{line_id}_{metric}"

    consumed_slug = _slug("consumed_gb")
    msgs: list[MqttMessage] = [
        MqttMessage(
            topic=f"homeassistant/sensor/{consumed_slug}/config",
            payload={
                **base_cfg,
                "name": "Consumed",
                "unique_id": consumed_slug,
                "object_id": consumed_slug,
                "value_template": "{{ value_json.consumed_gb }}",
                "unit_of_measurement": "GB",
                "device_class": "data_size",
                "state_class": "total_increasing",
            },
        ),
        MqttMessage(
            topic=f"homeassistant/sensor/{_slug('last_seen')}/config",
            payload={
                **base_cfg,
                "name": "Last seen",
                "unique_id": _slug("last_seen"),
                "object_id": _slug("last_seen"),
                "value_template": "{{ value_json.last_seen }}",
                "device_class": "timestamp",
            },
        ),
        MqttMessage(
            topic=f"homeassistant/binary_sensor/{_slug('stale')}/config",
            payload={
                **status_cfg,
                "name": "Stale",
                "unique_id": _slug("stale"),
                "object_id": _slug("stale"),
                "value_template": "{{ value_json.stale }}",
                "device_class": "problem",
                "payload_on": "ON",
                "payload_off": "OFF",
                "json_attributes_topic": status_topic,
            },
        ),
    ]

    if has_quota:
        for metric, name, unit, device_class, state_class in [
            ("quota_gb", "Quota", "GB", "data_size", "measurement"),
            ("remaining_gb", "Remaining", "GB", "data_size", "measurement"),
            ("usage_percent", "Usage percent", "%", None, "measurement"),
        ]:
            slug = _slug(metric)
            cfg: dict[str, Any] = {
                **base_cfg,
                "name": name,
                "unique_id": slug,
                "object_id": slug,
                "value_template": "{{ value_json." + metric + " }}",
                "unit_of_measurement": unit,
                "state_class": state_class,
            }
            if device_class:
                cfg["device_class"] = device_class
            msgs.append(MqttMessage(topic=f"homeassistant/sensor/{slug}/config", payload=cfg))

        danger_slug = _slug("danger")
        msgs.append(
            MqttMessage(
                topic=f"homeassistant/binary_sensor/{danger_slug}/config",
                payload={
                    **base_cfg,
                    "name": "Danger",
                    "unique_id": danger_slug,
                    "object_id": danger_slug,
                    "value_template": "{{ value_json.danger }}",
                    "device_class": "problem",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                },
            )
        )

    return msgs


def _main_state_message(
    result: ProviderResult,
    main: LineUsage,
    secondaries: list[LineUsage],
    danger_percent: int,
    *,
    pid: str,
) -> MqttMessage:
    if main.is_aggregate:
        # Adapter has already rolled up secondaries into main.consumed_gb.
        total = main.consumed_gb
    else:
        total = main.consumed_gb + sum(s.consumed_gb for s in secondaries)
    quota = main.quota_gb or 0.0
    remaining = max(0.0, quota - total) if quota else 0.0
    pct = round((total / quota) * 100, 1) if quota else 0.0
    danger = bool(quota and (total / quota) * 100 >= danger_percent) or main.extra_consumed_gb > 0
    iso = result.fetched_at.isoformat()
    missing = result.missing_secondaries
    detail = ", ".join(f"{m.label} ({m.line_id})" for m in missing)
    return MqttMessage(
        topic=f"carriers_sync/{pid}/{result.account_id}/state",
        payload={
            "consumed_gb": main.consumed_gb,
            "total_consumed_gb": round(total, 3),
            "quota_gb": main.quota_gb,
            "remaining_gb": round(remaining, 3),
            "usage_percent": pct,
            "extra_consumed_gb": main.extra_consumed_gb,
            "danger": "ON" if danger else "OFF",
            "sync_ok": "ON",
            "last_synced": iso,
            "last_attempted": iso,
            "last_error": "",
            "secondaries_expected": len(secondaries) + len(missing),
            "secondaries_reported": len(secondaries),
            "secondaries_missing": len(missing),
            "secondaries_missing_detail": detail,
            "secondaries_stale": "ON" if missing else "OFF",
            "main_own_consumed_gb": result.main_own_consumed_gb,
        },
    )


def _secondary_status_message(
    line_id: str,
    *,
    account_id: str,
    pid: str,
    stale: bool,
    reason: str,
) -> MqttMessage:
    """Per-secondary staleness, republished every cycle.

    Kept on its own topic so that marking a line stale never overwrites the
    retained consumed_gb/last_seen on the state topic.
    """
    return MqttMessage(
        topic=f"carriers_sync/{pid}/{account_id}/{line_id}/status",
        payload={"stale": "ON" if stale else "OFF", "reason": reason},
    )


def _secondary_state_message(
    sec: LineUsage,
    *,
    account_id: str,
    danger_percent: int,
    pid: str,
    fetched_at: datetime,
) -> MqttMessage:
    payload: dict[str, Any] = {
        "consumed_gb": sec.consumed_gb,
        "last_seen": fetched_at.isoformat(),
    }
    if sec.quota_gb is not None:
        quota = sec.quota_gb
        remaining = max(0.0, quota - sec.consumed_gb) if quota else 0.0
        pct = round((sec.consumed_gb / quota) * 100, 1) if quota else 0.0
        danger = bool(quota and (sec.consumed_gb / quota) * 100 >= danger_percent)
        payload.update(
            {
                "quota_gb": sec.quota_gb,
                "remaining_gb": round(remaining, 3),
                "usage_percent": pct,
                "danger": "ON" if danger else "OFF",
            }
        )
    return MqttMessage(
        topic=f"carriers_sync/{pid}/{account_id}/{sec.line_id}/state",
        payload=payload,
    )
