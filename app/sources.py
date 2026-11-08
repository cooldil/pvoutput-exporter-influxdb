"""Read time series out of InfluxDB (1.x via InfluxQL, 2.x via Flux).

Both sources expose the same two calls:
  series(): one aggregated value per interval window, keyed by the window END time (UTC)
  point():  a single first/last (time, value) inside a time range
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse


@dataclass(frozen=True)
class Signal:
    measurement: str
    field: str
    tags: dict[str, str] = field(default_factory=dict, hash=False)
    scale: float = 1.0

    @classmethod
    def from_config(cls, cfg: dict) -> "Signal":
        return cls(
            measurement=cfg["measurement"],
            field=cfg["field"],
            tags={str(k): str(v) for k, v in (cfg.get("tags") or {}).items()},
            scale=float(cfg.get("scale", 1.0)),
        )


def _rfc3339(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class InfluxV1Source:
    """InfluxDB 1.x (also works against 2.x's v1-compatibility API)."""

    def __init__(self, url: str, database: str, username: str | None = None,
                 password: str | None = None, verify_ssl: bool = True, timeout: int = 30):
        from influxdb import InfluxDBClient

        u = urlparse(url)
        self.client = InfluxDBClient(
            host=u.hostname, port=u.port or 8086, path=u.path.strip("/"),
            ssl=u.scheme == "https", verify_ssl=verify_ssl,
            username=username or None, password=password or None,
            database=database, timeout=timeout,
        )

    @staticmethod
    def _ident(name: str) -> str:
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'

    @staticmethod
    def _literal(value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    def _query(self, fn: str, sig: Signal, start: datetime, stop: datetime,
               positive_only: bool, group_by: str = "") -> list[dict]:
        where = [f"{self._ident(k)} = {self._literal(v)}" for k, v in sig.tags.items()]
        if positive_only:
            where.append(f"{self._ident(sig.field)} > 0")
        # [start, stop), the same as a GROUP BY time() bucket, so points and series agree.
        where.append(f"time >= '{_rfc3339(start)}' AND time < '{_rfc3339(stop)}'")
        q = (f"SELECT {fn}({self._ident(sig.field)}) AS \"v\" FROM {self._ident(sig.measurement)} "
             f"WHERE {' AND '.join(where)} {group_by}")
        return list(self.client.query(q, epoch="s").get_points())

    def series(self, sig: Signal, start: datetime, stop: datetime, interval: timedelta,
               fn: str, positive_only: bool = False) -> dict[datetime, float]:
        secs = int(interval.total_seconds())
        points = self._query(fn, sig, start, stop, positive_only, f"GROUP BY time({secs}s) fill(none)")
        # InfluxQL labels a GROUP BY bucket with its start time; we want the end.
        return {
            datetime.fromtimestamp(p["time"], timezone.utc) + interval: p["v"] * sig.scale
            for p in points if p.get("v") is not None
        }

    def point(self, sig: Signal, start: datetime, stop: datetime, fn: str,
              positive_only: bool = False) -> tuple[datetime, float] | None:
        points = self._query(fn, sig, start, stop, positive_only)
        if points and points[0].get("v") is not None:
            return datetime.fromtimestamp(points[0]["time"], timezone.utc), points[0]["v"] * sig.scale
        return None


class InfluxV2Source:
    """InfluxDB 2.x (and 3.x/Cloud endpoints that speak Flux)."""

    def __init__(self, url: str, token: str, org: str, bucket: str,
                 verify_ssl: bool = True, timeout: int = 30):
        from influxdb_client import InfluxDBClient

        self.client = InfluxDBClient(url=url, token=token, org=org,
                                     verify_ssl=verify_ssl, timeout=timeout * 1000)
        self.query_api = self.client.query_api()
        self.bucket = bucket

    @staticmethod
    def _str(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _base(self, sig: Signal, start: datetime, stop: datetime, positive_only: bool) -> str:
        preds = [f"r._measurement == {self._str(sig.measurement)}", f"r._field == {self._str(sig.field)}"]
        preds += [f"r[{self._str(k)}] == {self._str(v)}" for k, v in sig.tags.items()]
        flux = (f"from(bucket: {self._str(self.bucket)})\n"
                f"  |> range(start: {_rfc3339(start)}, stop: {_rfc3339(stop)})\n"
                f"  |> filter(fn: (r) => {' and '.join(preds)})\n"
                f"  |> toFloat()\n")
        if positive_only:
            flux += "  |> filter(fn: (r) => r._value > 0.0)\n"
        return flux

    def series(self, sig: Signal, start: datetime, stop: datetime, interval: timedelta,
               fn: str, positive_only: bool = False) -> dict[datetime, float]:
        secs = int(interval.total_seconds())
        # aggregateWindow stamps each window with its stop time, which is what we want.
        flux = self._base(sig, start, stop, positive_only) + \
            f"  |> aggregateWindow(every: {secs}s, fn: {fn}, createEmpty: false)\n"
        out: dict[datetime, float] = {}
        for table in self.query_api.query(flux):
            for rec in table.records:
                if rec.get_value() is not None:
                    out[rec.get_time().astimezone(timezone.utc)] = rec.get_value() * sig.scale
        return out

    def point(self, sig: Signal, start: datetime, stop: datetime, fn: str,
              positive_only: bool = False) -> tuple[datetime, float] | None:
        flux = self._base(sig, start, stop, positive_only) + f"  |> {fn}()\n"
        for table in self.query_api.query(flux):
            for rec in table.records:
                if rec.get_value() is not None:
                    return rec.get_time().astimezone(timezone.utc), rec.get_value() * sig.scale
        return None


def make_source(cfg: dict):
    version = int(cfg.get("version", 2))
    verify_ssl = bool(cfg.get("verify_ssl", True))
    if version == 1:
        return InfluxV1Source(cfg["url"], cfg["database"], cfg.get("username"),
                              cfg.get("password"), verify_ssl)
    if version == 2:
        return InfluxV2Source(cfg["url"], cfg["token"], cfg["org"], cfg["bucket"], verify_ssl)
    raise ValueError(f"influxdb.version must be 1 or 2, got {version}")
