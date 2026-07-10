# Carriers Sync — Documentation

Sync mobile-carrier data usage to Home Assistant as proper devices and
sensors via MQTT discovery. Currently supports **Alfa** and **Ogero** in
Lebanon (~~**Touch**~~ temporarily unavailable — see note); the architecture
is country-agnostic so other providers can be added as drop-in adapters.

> **Note:** ~~Touch~~ moved to **OTP-only login** (SMS/email verification
> code), which disables automated sync for now. Touch support is temporarily
> unavailable until we add OTP verification — logging in, prompting the user
> for the OTP, and keeping that session alive.

## Prerequisites

- The **Mosquitto broker** App, installed and running.
- The **MQTT** integration in Home Assistant, configured against Mosquitto.

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `poll_interval_minutes` | int (5–1440) | 60 | How often to fetch from each account. |
| `danger_percent` | int (1–100) | 80 | Threshold above which a line's `danger` binary sensor turns ON. |
| `log_level` | enum | `info` | One of trace/debug/info/notice/warning/error/fatal. |
| `accounts` | list | `[]` | One entry per Alfa account you want to monitor. |

### Account fields

| Field | Type | Description |
|---|---|---|
| `provider` | enum | `alfa-lb`, `touch-lb`, or `ogero-lb`. |
| `username` | str | Carrier portal username. Alfa: the phone number (`03333333`). Touch: the portal account login. Ogero: typically the email address you registered with. |
| `password` | str | Portal password (stored in `/data/options.json`). |
| `label` | str | Friendly name shown in Home Assistant. |
| `secondary_labels` | list | Per-line labels keyed by phone number: `[{number: 03222222, label: Wife}, ...]`. Alfa: names U-share twin lines under the main number. Touch / Ogero: names individual numbers under one account. |

### Provider differences

**Alfa.** Each Alfa portal account = 1 main phone number with one login of its own. U-share accounts may have additional "twin" lines that share the main quota. Each Alfa account in your config = one HA device for the main number + one per twin.

**Touch.** A single Touch portal login can view many numbers on the account, each with its own data quota. The App models this as one synthetic "Touch account" device (carrying the sync-status sensors) plus one device per number, each with its own consumed/quota/danger sensors. The synthetic account device's `total_consumed_gb` and `quota_gb` are the sum across all numbers on the account.

**Ogero.** Lebanese landline + ADSL/VDSL provider. One login surfaces multiple `(phone, DSL)` lines on the dashboard, each with its own FUP volume. Modeled like Touch — synthetic "Ogero account" device for sync sensors + one per line for usage. **Note**: Ogero's login form requires a Google reCAPTCHA v2 token, which Playwright can't always satisfy. If login is challenged, the App reports `last_error: auth` and skips fetches until the captcha is bypassed (cookie injection or a 2captcha-style service — not yet built in).

## Entities

For each account, this App creates an HA *device* identified by the main phone
number (Alfa) or by the username (Touch synthetic account), plus child devices
(linked via `via_device`) for each secondary line.

All entity IDs are namespaced as `carriers_sync_<provider>_<line_id>_<metric>`
where `<provider>` is the provider id with dashes replaced by underscores
(e.g. `alfa_lb`, `touch_lb`). Filter by `carriers_sync` in Developer Tools →
States to find them all; filter by `carriers_sync_alfa_lb` or
`carriers_sync_touch_lb` to narrow per-provider.

Per-account device entities:
- `sensor.carriers_sync_<provider>_<line_id>_consumed_gb`,
  `_total_consumed_gb`, `_quota_gb`, `_remaining_gb`, `_usage_percent`,
  `_extra_consumed_gb`
- `binary_sensor.carriers_sync_<provider>_<line_id>_danger`, `_sync_ok`
- `sensor.carriers_sync_<provider>_<line_id>_last_synced`, `_last_attempted`,
  `_last_error`
- `button.carriers_sync_<provider>_<line_id>_refresh`

Per secondary/peer line:
- `sensor.carriers_sync_<provider>_<secondary_line_id>_consumed_gb` (always)
- For Touch numbers (which have their own quota): also `_quota_gb`,
  `_remaining_gb`, `_usage_percent`, `binary_sensor.…_danger`. Alfa U-share
  twins share the main's quota, so they only get `_consumed_gb`.

Device names in HA are prefixed by provider — `Alfa: <label>` for Alfa
accounts and twins, `Touch: <label>` for Touch accounts and numbers.

A singleton "Carriers Sync" device exposes:
- `button.carriers_sync_refresh_all`
- `sensor.carriers_sync_app_status`

### Example

For an Alfa account with `username: 03333333, label: Aziz`, you'll get
entity IDs like:

```
sensor.carriers_sync_alfa_lb_03333333_total_consumed_gb
sensor.carriers_sync_alfa_lb_03333333_quota_gb
sensor.carriers_sync_alfa_lb_03333333_usage_percent
binary_sensor.carriers_sync_alfa_lb_03333333_sync_ok
button.carriers_sync_alfa_lb_03333333_refresh
```

For a Touch account with `username: familyacct, label: Stephanie Touch`:

```
sensor.carriers_sync_touch_lb_familyacct_total_consumed_gb
sensor.carriers_sync_touch_lb_familyacct_quota_gb
sensor.carriers_sync_touch_lb_familyacct_usage_percent
binary_sensor.carriers_sync_touch_lb_familyacct_sync_ok
button.carriers_sync_touch_lb_familyacct_refresh
```

Per Touch number under that account:

```
sensor.carriers_sync_touch_lb_81111111_consumed_gb
sensor.carriers_sync_touch_lb_81111111_quota_gb
...
```

<img width="1118" height="716" alt="Screenshot 2026-04-30 at 13 31 51" src="https://github.com/user-attachments/assets/1ea4e4b3-1b0c-491e-9179-bbb83f41a57e" />


You can rename them in HA's UI (right-click an entity → Customize) without
breaking anything — the durable identity is the `unique_id`, not the entity_id.

## Tips

- **Use unique labels.** Friendly names should be unique per device for clean
  display, even though entity IDs are line-id-based.
- **The `total_consumed_gb` sensor** is your real "data used this month" for
  U-share accounts (main + all twins) and for Touch accounts (sum across all
  numbers).
- **`last_synced` lags `last_attempted`** when fetches fail — that's the
  signal that data is stale.

## Troubleshooting

- **All `sync_ok` are OFF**: Mosquitto is probably down or the MQTT integration
  is misconfigured. Check the App's Log tab.
- **`last_error: auth`**: stored password is wrong, or Alfa locked the account.
  The App will not retry until you fix the credentials.
- **`last_error: transient`**: network issue or Alfa edge rejected us. The App
  retries 3 times per cycle; if all fail it waits for the next cycle.

To file a useful bug report, set `log_level: trace` and reproduce the issue —
the App writes Playwright network and HTML dumps to `/data/debug/` that you
can attach to a GitHub issue (after redacting your credentials).
