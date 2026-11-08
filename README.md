# SolarEdge → PVOutput exporter

A small Docker service that reads SolarEdge inverter and meter data from InfluxDB and uploads
production and consumption to [PVOutput](https://pvoutput.org) every status interval.

## What gets sent

| PVOutput   | Calculated as |
|------------|---------------|
| v2 Power generation (W) | inverter AC power + external production meter power (averaged over the interval) |
| v4 Power consumption (W) | generation − grid power (grid power is + when exporting) |
| v1 Energy generation (Wh today) | Δ inverter lifetime energy + Δ production meter lifetime energy since local midnight |
| v3 Energy consumption (Wh today) | generation + Δ grid import − Δ grid export since local midnight |
| v5 Temperature / v6 Voltage | optional, averaged over the interval |

Uploads use PVOutput's batch API. The last uploaded interval is saved in `/data/state.json`, so after
a restart or an outage the exporter fills the gap (up to 13 days back, PVOutput's limit is 14).

## Setup

1. Copy the example files:
   ```bash
   cp env.example .env
   cp config.example.yaml config.yaml
   ```
2. In `.env`, set your PVOutput API key and system ID (PVOutput → Settings), your timezone,
   and the InfluxDB connection details. `INFLUX_VERSION=1` uses InfluxQL with a database;
   `INFLUX_VERSION=2` uses Flux with an org, bucket and token.
3. In `config.yaml`, edit `signals` so each one points at the measurement, field and tags your
   Modbus logger writes. You can find them with:
   - 1.x: `SHOW MEASUREMENTS`, `SHOW FIELD KEYS FROM <m>`, `SHOW TAG VALUES FROM <m> WITH KEY = <tag>`
   - 2.x: the Data Explorer in the InfluxDB UI
4. Make sure `interval_minutes` matches the Status Interval on your PVOutput system.

### Things to check in your data

- **Scale factors.** Values must be stored with the SunSpec scale factors already applied.
  SolarEdge changes the power scale factor as output changes, so a fixed `scale` can't correct raw
  register values.
- **Grid meter sign.** The exporter expects `grid_power` to be positive when exporting and negative
  when importing. At night it should be negative; if it's positive, add `scale: -1`.
- **Production meter direction.** `production_meter_power` must be positive while producing, and
  `production_meter_energy` must be the counter that goes up while producing (`M_Exported` or
  `M_Imported` depending on how the CT is fitted).
- **One series per signal.** The tags must narrow each signal to one series (for example one meter).
- **Batteries.** If you add a StorEdge battery later, inverter AC power includes battery discharge,
  so "generation" would include it.

## Run

First check that every signal finds its data:

```bash
docker compose run --rm pvoutput-exporter --check
```

This lists the latest reading of each signal and how old it is. For energy counters it also shows
today's total. Then it shows what would be sent for the latest interval. Any value that's missing
lists the signals it's calculated from. A "Problems" list at the end says what to fix in
`config.yaml`, such as a signal with no data, an old reading, a counter that went down, or
production power that's negative. It doesn't need the PVOutput API key and never uploads anything.

Then try it without uploading. This prints the status that would be sent for the latest interval:

```bash
docker compose run --rm pvoutput-exporter --once --dry-run
```

Each line is `date,time,v1,v2,v3,v4,v5,v6`. `-1` means the value was missing. Check that the numbers
look right, then start the service:

```bash
docker compose up -d --build
```

```bash
docker compose logs -f pvoutput-exporter
```

To also upload earlier today's data on the very first run, set `initial_backfill_hours` (for example
`24`) before the first start. Once `state.json` exists, the setting is ignored.

## Options

| Setting | Default | Meaning |
|---------|---------|---------|
| `pvoutput.interval_minutes` | 5 | Status interval; must match PVOutput |
| `pvoutput.upload_delay_seconds` | 60 | Delay after an interval ends before it's uploaded |
| `pvoutput.batch_size` | 30 | Statuses per request (donors: up to 100) |
| `pvoutput.max_backfill_days` | 13 | How far back gaps are filled |
| `pvoutput.initial_backfill_hours` | 0 | History to upload on the first ever run |
| `pvoutput.dry_run` | false | Log instead of upload |
| `counter_max_age_minutes` | 60 | Energy counter readings older than this count as missing |
| `LOG_LEVEL` (env) | INFO | `DEBUG` also shows PVOutput's responses |

PVOutput allows 60 requests per hour (300 for donors). A 5-minute interval uses 12. If the limit
is hit while back-filling, the exporter waits for the reset time PVOutput sends back.

## Running without Docker

Needs Python 3.9 or newer. Do the same setup steps as above, then run these from the project folder:

```bash
pip install -r requirements.txt
```

```bash
python -m app.main --config config.yaml --check
```

```bash
python -m app.main --config config.yaml --state state.json --once --dry-run
```

Drop `--once --dry-run` to run it continuously. On Windows, use `py` instead of `python`.

Settings are read from `.env` in the current folder (or the file given with `--env-file`). Real
environment variables take priority over the file. Two things are different from Docker:

- `INFLUX_URL` must be an address this machine can reach, e.g. `http://localhost:8086` or the
  server's IP address. A container name like `http://influxdb:8086` only works inside Docker.
- Always pass `--state`, or progress is saved to `/data/state.json` (`C:\data\state.json` on Windows).
