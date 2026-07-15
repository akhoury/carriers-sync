# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.4.6] — 2026-07-10

### Added

- **Alfa AlfaNet plans.** AlfaNet postpaid lines expose a single data bundle
  named after the plan (e.g. "Alfanet 800GB"). The parser now selects the
  primary data bundle generically — the first non-"Free" data entry — instead
  of matching only U-share / Mobile Internet, so AlfaNet (and future plan
  names) work. Verified live. Plan size/unit are always read from
  `TotalAmount`/`UsedAmount` (never parsed from the name), and unit handling
  now covers KB/MB/GB/TB case-insensitively, so upgrading/downgrading to any
  plan size is reported correctly.

### Changed

- **`secondary_labels` is now optional** in the App config schema. Adding an
  account without secondaries through the Home Assistant UI no longer fails
  with "Missing option 'secondary_labels' in accounts".
- **Unsupported providers now reset their sensors.** When a provider can no
  longer be automated (Touch, after the OTP-only migration), the account's
  values are reset to zero, its secondary lines are zeroed, and it is dropped
  from persisted state — instead of freezing the last-known numbers in Home
  Assistant. Backed by a new `ProviderUnsupportedError` (a no-retry auth
  error that also triggers the reset).

## [0.4.5] — 2026-07-10

### Fixed

- **Alfa: every account broke with "missing or empty
  ServiceInformationValue".** Alfa migrated its consumption endpoint (now
  `getconsumptionasync`) off the old `ServiceInformationValue` schema.
  Rewrote `parse_response` for the new `FreeUnitsValue` shape
  (`UsedAmount`/`UsedUnit`, `TotalAmount`/`TotalUnit`, `ExtraUsage`).
  Verified live against all configured Alfa accounts.

### Changed

- **Alfa U-share is now reported as a single aggregate line.** Alfa no
  longer exposes per-secondary usage (`Secondaries` is always null); it
  provides a "U-share Total Bundle" aggregate (main + all secondaries) plus
  a "U-share Main" line. We report the aggregate against the shared quota, so
  the account's usage/quota sensors stay correct. The per-secondary HA
  devices no longer update, because Alfa stopped sending that data.
- **Touch temporarily unavailable — moved to OTP-only login.** Touch retired
  its username/password portal; `www.touch.com.lb/autoforms/*` now redirects
  to an OTP-only site (mobile/email verification code). The provider detects
  this and fails with a clear, actionable message instead of the cryptic
  "login did not produce a logged-in page (no logout link)". Automated sync
  will return once OTP verification is implemented (log in, prompt the user
  for the OTP, keep the session alive).

### Docs

- README, DOCS, and the App description now strike through Touch and note
  the OTP migration. DOCS also lists Ogero (previously omitted).

## [0.4.4] — 2026-04-30

### Fixed

- **Ogero login crash after JS form submit.** `page.content()` was
  raising `Unable to retrieve content because the page is navigating
  and changing the content` because we read the DOM mid-redirect. The
  previous `wait_for_load_state("networkidle")` was wrapped in
  `contextlib.suppress(Exception)` so its timeouts were silently
  swallowed. Replaced with `_read_settled_content()` helper that uses
  `wait_for_load_state("load")` (stricter than domcontentloaded, but
  doesn't hang on long-poll connections like networkidle does) and
  retries on the rare race where another navigation kicks in between
  the wait and the content read. Applied to all 3 content-read sites
  in the Ogero adapter (post-submit, dashboard, per-line page).
- **Confirmed working end-to-end on a real Ogero account.** With
  stealth + timing, reCAPTCHA v3 passes; no cookie injection needed.

### Added

- **Dev-only account filter** (`__main__.py`). Two env vars,
  AND-combined, comma-separated lists supported:
  - `CARRIERS_SYNC_DEV_PROVIDER` keep accounts whose provider matches
  - `CARRIERS_SYNC_DEV_USERNAME` keep accounts whose username matches
  Lets you iterate on one provider without waiting for a full
  Alfa+Touch+Ogero cycle. Production behaviour unchanged (HA Supervisor
  doesn't set these vars).
- `docker-compose.yml` plumbs the same env vars from the host shell so
  `CARRIERS_SYNC_DEV_PROVIDER=ogero-lb docker compose up` filters to
  Ogero-only without editing config.

## [0.4.3] — 2026-04-30

### Other

- Internal repo cleanup; no functional code change. (Real account
  identifiers that had leaked into committed test fixtures and docs
  during early development were replaced with placeholders.)

## [0.4.2] — 2026-04-30

### Changed

- **Ogero login: stealth mode + realistic timing.** Added
  `playwright-stealth` to mask common automation fingerprints
  (`navigator.webdriver`, plugin enumeration, WebGL vendor strings, etc.)
  and added small human-like delays between page load / fill / submit so
  reCAPTCHA v3's invisible scoring is more likely to pass.
- **Robust submit fallback.** When `button[type="submit"]` isn't on the
  page (Ogero uses jQuery + styled anchors), the adapter falls back to
  `form.requestSubmit() / submit()` via JS, which still fires onsubmit
  handlers that inject the reCAPTCHA token.
- Realistic user-agent + 1440x900 viewport on the Ogero context.

### Notes

This is best-effort. If reCAPTCHA still scores us below threshold, the
fallback is manual cookie injection — planned for 0.5.0 alongside an
optional per-provider `keep_warm` background ping that refreshes
PHPSESSID before its ~24-min server-side TTL expires. The error message
on captcha-blocked logins now hints at this.

## [0.4.1] — 2026-04-30

### Reverted

- The `has_entity_name: false` change from 0.4.0 didn't actually make HA
  honor `object_id`, and the entity-name change (`f"{label} {metric}"`)
  caused the label to appear twice in entity_ids
  (`sensor.alfa_pauli_main_pauli_main_consumed`). Reverted both:
  `has_entity_name` removed (back to MQTT default of true), entity names
  back to plain metric names (`"Consumed"`, `"Quota"`, etc.).
- Net effect: existing entity_ids that got doubled stay doubled (HA
  preserves them). The fix here only affects fresh registrations —
  if/when you wipe the entity registry next, new entities will get the
  pre-0.4.0 entity_id format (`sensor.alfa_pauli_main_consumed`).

## [0.4.0] — 2026-04-30

### Added

- **Ogero (Lebanon) provider** — third provider, modeled like Touch (one
  login, multiple lines per account). Reads consumption from the
  `MyOgeroDashboardSection2Consumption` div on the post-login dashboard,
  iterating every `(phone, dsl)` pair from the `changnumber` select.
  **Known limitation**: Ogero's login form requires Google reCAPTCHA v2,
  which Playwright can't satisfy automatically. If the captcha
  challenges, the adapter raises `AuthFetchError("captcha")` so users
  can see what failed. Cookie-injection / 2captcha workarounds may land
  in a follow-up.

### Changed (breaking)

- **`has_entity_name: false` set on every discovery payload.** HA's MQTT
  integration silently sets `has_entity_name=true` for entities with a
  device association, which makes it ignore `object_id` and derive
  entity_id from `slugify(device.name) + slugify(entity.name)`. Setting
  the flag explicitly forces HA to honor `object_id`, so entity_ids are
  now actually `sensor.carriers_sync_<provider>_<line_id>_<metric>` as
  intended.
- **Entity names now include the line label.** With `has_entity_name=false`
  HA stops auto-prefixing the device name in the friendly name display.
  To compensate, each entity's `name` field now bakes in the label —
  `"Aziz quota"` instead of just `"Quota"` — so they remain readable in
  HA's entity list and on dashboards. Net display change is mild: was
  "Alfa: Aziz Alfa Main (esim) Quota", now "Aziz Alfa Main (esim) quota".

### Migration

If your 0.3.0 entity_ids ended up in the old `sensor.alfa_<label>_*`
format anyway (because HA preserved them from earlier registrations), do
a thorough wipe of the entity registry before deploying 0.4.0:

```bash
cp /config/.storage/core.entity_registry /config/.storage/core.entity_registry.bak

python3 -c "
import json
path = '/config/.storage/core.entity_registry'
d = json.load(open(path))
for key in ('entities', 'deleted_entities'):
    if key in d['data']:
        d['data'][key] = [
            e for e in d['data'][key]
            if 'carriers_sync' not in str(e.get('unique_id',''))
        ]
json.dump(d, open(path,'w'), indent=4)
"
```

Restart HA → install/update 0.4.0 → entities re-register fresh with
`carriers_sync_<provider>_*` entity_ids.

## [0.3.0] — 2026-04-29

### Changed (breaking)

- **Provider-namespaced unique_ids, device identifiers, and MQTT topics.**
  Every entity is now keyed `carriers_sync_<provider>_<line_id>_<metric>`
  (e.g. `carriers_sync_alfa_lb_03333333_consumed_gb`,
  `carriers_sync_touch_lb_familyacct_consumed_gb`). State and command topics
  also include the provider segment:
  `carriers_sync/<provider>/<account>/state`. This guards against
  cross-provider line_id collisions and makes IDs self-documenting.
- Touch's synthetic account `line_id` no longer carries a `touch_` prefix —
  the provider qualifier in unique_ids already provides namespacing, so
  the `line_id` is now just the username (e.g. `familyacct`).
- The MQTT command-topic subscription is now a single wildcard
  (`carriers_sync/+/+/refresh/cmd`) covering every provider/account combo.

### Migration

Same shape as 0.2.0 — old entities go orphan; new entities are created
fresh. If you used Path A in 0.2.0 (delete + re-add MQTT integration),
do the same here. Or edit `.storage/core.entity_registry` to remove
entries with `unique_id` starting with `carriers_sync_` (the previous
0.2.0 format) — the 0.3.0 unique_ids are different
(`carriers_sync_<provider>_…`) so they'll re-register fresh.

## [0.2.0] — 2026-04-29

### Changed (breaking)

- **Renamed** project from `carriers-lb-sync` / "Carriers LB Sync" to
  `carriers-sync` / "Carriers Sync". The Lebanon framing was specific to
  the included providers (Alfa and Touch); the wrapper itself is now
  country-agnostic so other countries' carriers can land alongside.
- All MQTT topics moved from `carriers_lb_sync/...` to `carriers_sync/...`.
- All entity `unique_id`s and discovery `node_id`s moved from
  `carriers_lb_*` to `carriers_sync_*`. Existing entities in HA will go
  orphan (visible under MQTT integration → "Unavailable") and new ones
  with the new IDs will appear. Delete the orphaned ones manually.
- Per-provider device prefixes — devices are now named `Alfa: <label>` /
  `Touch: <label>` based on the provider rather than always `Alfa: …`.
- Entity IDs are now namespaced via MQTT discovery `object_id`, e.g.
  `sensor.carriers_sync_03333333_consumed_gb`. Filter by `carriers_sync`
  in HA to see them all. (You can rename them in HA's UI.)

### Migration

After updating, expect to:
1. Delete orphaned `Alfa: …`/`Touch: …` devices from MQTT integration.
2. Update any dashboards that referenced the old `sensor.alfa_*` ids
   (now `sensor.carriers_sync_*`).

## [0.1.1] — 2026-04-29

### Fixed

- MQTT auth: query Supervisor's `http://supervisor/services/mqtt` API for
  broker credentials when running under HA. Previously relied on
  `MQTT_USERNAME`/`MQTT_PASSWORD` env vars (which Supervisor does NOT
  inject), causing `Not authorized` on first connect.

## [0.1.0] — Initial release

### Added

- Initial Home Assistant App release: rewrite of the standalone
  Google-Keep-syncing script as an App that publishes per-account and
  per-line Alfa usage data via MQTT discovery.
- Touch (Lebanon) provider — one login surfaces all numbers on the
  account, each with its own quota, modelled as a synthetic account
  device + per-number devices.
- Fallback path for Alfa accounts whose `getconsumption` response has
  no Mobile Internet (typically alarm SIMs): reads the active bundle
  from `/account/manage-services/getmyservices` instead.
- Provider-adapter abstraction so additional Lebanese carriers can be
  added as single new files.
- Per-account and global "Refresh now" buttons.
- `last_synced` / `last_attempted` / `last_error` / `sync_ok` sensors
  for staleness visibility.

[0.4.6]: https://github.com/akhoury/carriers-sync/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/akhoury/carriers-sync/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/akhoury/carriers-sync/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/akhoury/carriers-sync/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/akhoury/carriers-sync/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/akhoury/carriers-sync/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/akhoury/carriers-sync/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/akhoury/carriers-sync/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/akhoury/carriers-sync/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/akhoury/carriers-sync/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/akhoury/carriers-sync/releases/tag/v0.1.0
