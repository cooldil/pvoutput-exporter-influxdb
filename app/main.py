"""Periodically export SolarEdge production/consumption from InfluxDB to PVOutput."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .check import run_check
from .exporter import Exporter
from .pvoutput import PVOutputClient, PVOutputError, RateLimited
from .sources import Signal, make_source

log = logging.getLogger("pvoutput_exporter")

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def load_dotenv(path: Path) -> bool:
    """Load KEY=VALUE lines into the environment. Variables already set are left alone,
    so real environment variables (e.g. from docker compose) win over the file."""
    if not path.is_file():
        return False
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True


def load_config(path: Path, need_pvoutput: bool = True) -> dict:
    """YAML config with ${VAR} / ${VAR:-default} substitution from the environment."""
    text = _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), path.read_text())
    cfg = yaml.safe_load(text) or {}
    cfg["pvoutput"] = pvo = cfg.get("pvoutput") or {}
    missing = [k for k in ("api_key", "system_id") if not pvo.get(k)]
    if missing and need_pvoutput:
        raise SystemExit(f"config: pvoutput.{' and pvoutput.'.join(missing)} must be set")
    if not cfg.get("signals"):
        raise SystemExit("config: no signals configured")
    return cfg


class State:
    """Remembers the last uploaded slot so restarts back-fill instead of leaving gaps."""

    def __init__(self, path: Path):
        self.path = path
        self.last_slot: datetime | None = None
        self.day: dict = {}
        if path.exists():
            data = json.loads(path.read_text())
            if data.get("last_slot"):
                self.last_slot = datetime.fromisoformat(data["last_slot"])
            self.day = data.get("day") or {}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "last_slot": self.last_slot.isoformat() if self.last_slot else None,
            "day": self.day,
        }, indent=2))
        tmp.replace(self.path)


def floor_to(t: datetime, step: timedelta) -> datetime:
    secs = int(step.total_seconds())
    return datetime.fromtimestamp(int(t.timestamp()) // secs * secs, timezone.utc)


def pending_slots(now: datetime, last: datetime | None, interval: timedelta, delay: timedelta,
                  max_backfill: timedelta, initial_backfill: timedelta) -> list[datetime]:
    latest = floor_to(now - delay, interval)
    if last is None:
        last = floor_to(latest - initial_backfill, interval) - interval
    first = max(last + interval, floor_to(now - max_backfill, interval) + interval)
    slots = []
    t = first
    while t <= latest:
        slots.append(t)
        t += interval
    return slots


def run_once(exporter: Exporter, client: PVOutputClient | None, state: State, cfg: dict,
             dry_run: bool) -> datetime | None:
    """Upload everything that is due. Returns a time to wait until if rate limited."""
    pvo = cfg["pvoutput"]
    interval = timedelta(minutes=int(pvo.get("interval_minutes", 5)))
    slots = pending_slots(
        datetime.now(timezone.utc), state.last_slot, interval,
        delay=timedelta(seconds=int(pvo.get("upload_delay_seconds", 60))),
        max_backfill=timedelta(days=float(pvo.get("max_backfill_days", 13))),
        initial_backfill=timedelta(hours=float(pvo.get("initial_backfill_hours", 0))),
    )
    if not slots:
        return None
    if len(slots) > 1:
        log.info("Uploading %d slots from %s", len(slots), slots[0].astimezone(exporter.tz))

    batch_size = int(pvo.get("batch_size", 30))
    for i in range(0, len(slots), batch_size):
        chunk = slots[i:i + batch_size]
        statuses = exporter.build_statuses(chunk, state.day)
        if dry_run:
            for s in statuses:
                log.info("[dry-run] %s", s.encode())
        elif statuses:
            try:
                added = client.add_batch(statuses)
                log.info("Sent %d status(es) up to %s, %d added/updated",
                         len(statuses), statuses[-1].when.strftime("%Y-%m-%d %H:%M"), added)
            except RateLimited as exc:
                log.warning("PVOutput rate limit hit (%s)", exc)
                return exc.reset_at or datetime.now(timezone.utc) + timedelta(minutes=15)
            except PVOutputError as exc:
                if exc.retryable:
                    raise
                log.error("PVOutput rejected batch, skipping it: %s", exc)
        state.last_slot = chunk[-1]
        if not dry_run:
            state.save()
        if client and client.blocked_until:
            return client.blocked_until
    return None


def main() -> None:
    # Load .env first so it can also supply LOG_LEVEL, CONFIG_PATH and STATE_PATH.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=".env",
                     help="file of KEY=VALUE settings to load if it exists (default: .env)")
    env_file = Path(pre.parse_known_args()[0].env_file)
    dotenv_loaded = load_dotenv(env_file)

    parser = argparse.ArgumentParser(description=__doc__, parents=[pre])
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH", "/config/config.yaml"))
    parser.add_argument("--state", default=os.environ.get("STATE_PATH", "/data/state.json"))
    parser.add_argument("--once", action="store_true", help="upload what is due, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="log the statuses that would be sent instead of uploading them")
    parser.add_argument("--check", action="store_true",
                        help="show what each signal finds in InfluxDB and what would be sent, then exit")
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)-7s %(message)s")
    if dotenv_loaded:
        log.info("Loaded settings from %s", env_file.resolve())

    cfg = load_config(Path(args.config), need_pvoutput=not args.check)
    pvo = cfg["pvoutput"]
    dry_run = args.dry_run or bool(pvo.get("dry_run", False))
    interval = timedelta(minutes=int(pvo.get("interval_minutes", 5)))
    delay = timedelta(seconds=int(pvo.get("upload_delay_seconds", 60)))
    tz = ZoneInfo(cfg.get("timezone") or "UTC")

    exporter = Exporter(
        source=make_source(cfg["influxdb"]),
        signals={name: Signal.from_config(s) for name, s in cfg["signals"].items() if s},
        tz=tz,
        interval=interval,
        counter_max_age=timedelta(minutes=int(cfg.get("counter_max_age_minutes", 60))),
    )
    if args.check:
        try:
            ok = run_check(exporter, floor_to(datetime.now(timezone.utc) - delay, interval))
        except Exception as exc:
            log.debug("check failed", exc_info=True)
            sys.exit(f"Could not query InfluxDB at {cfg['influxdb'].get('url')}: {exc}")
        sys.exit(0 if ok else 1)

    client = None if dry_run else PVOutputClient(pvo["api_key"], pvo["system_id"])
    state = State(Path(args.state))
    if dry_run:
        # Show the latest slot without touching the saved progress.
        state.last_slot = None

    log.info("Exporting to PVOutput system %s every %s (timezone %s, signals: %s)%s",
             pvo["system_id"], interval, tz, ", ".join(exporter.signals),
             " [DRY RUN]" if dry_run else "")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        wait_until = None
        try:
            wait_until = run_once(exporter, client, state, cfg, dry_run)
        except Exception:
            log.exception("Export failed; will retry next interval")
        if args.once:
            return
        now = datetime.now(timezone.utc)
        next_run = floor_to(now - delay, interval) + interval + delay
        if wait_until and wait_until > next_run:
            log.info("Waiting for rate limit reset at %s", wait_until.astimezone(tz))
            next_run = wait_until
        time.sleep(max(5.0, (next_run - now).total_seconds()))


if __name__ == "__main__":
    main()
