# Carriers Sync — Home Assistant App

Sync mobile and fixed-line carrier data-usage to Home Assistant as proper
devices and sensors, via MQTT discovery. Currently supports **Alfa**,
~~**Touch**~~, and **Ogero** (Lebanon).

> **Note:** ~~Touch~~ moved to **OTP-only login** (SMS/email verification
> code), which disables automated sync for now. Touch support is temporarily
> unavailable until we add OTP verification — logging in, prompting the user
> for the OTP, and keeping that session alive.

## Install

1. In Home Assistant, go to **Settings → Apps → Apps → ⋮ menu → Repositories**.
2. Add this repository's URL: `https://github.com/akhoury/carriers-sync`.
3. Find **Carriers Sync** in the App store, click **Install**.
4. Open the App's **Configuration** tab and add your Alfa and Touch account(s).
5. Start the App.

The App requires the **Mosquitto broker** App and the **MQTT** integration to
be installed and configured (most HA installs already have this).

## What you get

For each Alfa or Touch account, an HA device with:

- `sensor.alfa_<label>_consumed_gb`, `total_consumed_gb`, `quota_gb`,
  `remaining_gb`, `usage_percent`, `extra_consumed_gb`
- `binary_sensor.alfa_<label>_danger` (over threshold or extra used)
- `binary_sensor.alfa_<label>_sync_ok` (last fetch succeeded)
- `sensor.alfa_<label>_last_synced` / `_last_attempted` / `_last_error`
- `button.alfa_<label>_refresh`

For U-share accounts, each secondary line becomes its own HA device
(`via_device` linked to the main account) with its own `consumed_gb`
sensor. Plus a singleton "Carriers Sync" device with a global
`refresh_all` button and an `app_status` sensor.

<img width="1118" height="716" alt="Screenshot 2026-04-30 at 13 31 51" src="https://github.com/user-attachments/assets/1ea4e4b3-1b0c-491e-9179-bbb83f41a57e" />


See [`carriers_sync/DOCS.md`](carriers_sync/DOCS.md) for the full
configuration reference, troubleshooting, and bug-report instructions.

## Local development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
mypy carriers_sync/src
pytest -q
```

### Running the container locally with docker compose

A development stack with a Mosquitto broker is included for testing the
container outside Home Assistant:

```bash
cp dev/options.json.example dev/data/options.json
# edit dev/data/options.json and add your Alfa/Touch credentials

docker compose up --build

# in another terminal, watch the published MQTT messages
mosquitto_sub -h localhost -t 'carriers_sync/#' -v
```

## License

MIT — see `LICENSE`.
