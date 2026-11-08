"""Turn raw inverter/meter series into PVOutput statuses.

Power (W), averaged over each interval:
  generation  = inverter AC power + external production meter power
  consumption = generation - grid power          (grid power: + export, - import)

Energy (Wh since local midnight), from lifetime counters:
  generation  = d(inverter energy) + d(production meter energy)
  consumption = generation + d(grid import) - d(grid export)
"""
from __future__ import annotations

import bisect
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .pvoutput import Status
from .sources import Signal

log = logging.getLogger(__name__)

GENERATION_POWER = ("inverter_power", "production_meter_power")
GENERATION_ENERGY = ("inverter_energy", "production_meter_energy")
COUNTERS = GENERATION_ENERGY + ("grid_import_energy", "grid_export_energy")
AVERAGED = GENERATION_POWER + ("grid_power", "temperature", "voltage")
KNOWN_SIGNALS = COUNTERS + AVERAGED


def _total(values: list[float | None]) -> float | None:
    """Sum of all parts, or None if there are no parts or any part is missing."""
    if not values or any(v is None for v in values):
        return None
    return sum(values)


class _CounterSeries:
    """Last known counter value at or before a time, ignoring readings older than max_age."""

    def __init__(self, points: dict[datetime, float], max_age: timedelta):
        self.times = sorted(points)
        self.values = [points[t] for t in self.times]
        self.max_age = max_age

    def at(self, t: datetime) -> float | None:
        i = bisect.bisect_right(self.times, t) - 1
        if i >= 0 and t - self.times[i] < self.max_age:
            return self.values[i]
        return None


class Exporter:
    def __init__(self, source, signals: dict[str, Signal], tz: ZoneInfo,
                 interval: timedelta, counter_max_age: timedelta):
        unknown = set(signals) - set(KNOWN_SIGNALS)
        if unknown:
            raise ValueError(f"unknown signal(s) in config: {', '.join(sorted(unknown))}")
        self.source = source
        self.signals = signals
        self.tz = tz
        self.interval = interval
        self.counter_max_age = counter_max_age

    def _configured(self, names: tuple[str, ...]) -> list[str]:
        return [n for n in names if n in self.signals]

    def baseline(self, name: str, midnight: datetime) -> float | None:
        """Counter value at local midnight: last reading before it, else the first one after it."""
        sig = self.signals[name]
        point = self.source.point(sig, midnight - timedelta(days=1), midnight, "last", positive_only=True)
        if point is None:
            point = self.source.point(sig, midnight, midnight + timedelta(days=1), "first", positive_only=True)
        return point[1] if point else None

    def build_statuses(self, slots: list[datetime], day_state: dict) -> list[Status]:
        """Build one status per slot (UTC interval end times, ascending).

        day_state carries the highest energy totals sent so far today, so values never go
        backwards (PVOutput dislikes that, and it happens when meters are read a few seconds apart).
        It is updated in place.
        """
        start, stop = slots[0] - self.interval, slots[-1]

        averaged = {
            n: self.source.series(self.signals[n], start, stop, self.interval, "mean")
            for n in self._configured(AVERAGED)
        }
        counters = {
            n: _CounterSeries(
                self.source.series(self.signals[n], start - self.counter_max_age, stop,
                                   self.interval, "last", positive_only=True),
                self.counter_max_age)
            for n in self._configured(COUNTERS)
        }
        baselines: dict[tuple[str, datetime], float | None] = {}

        statuses = []
        for slot in slots:
            local = slot.astimezone(self.tz)
            midnight = datetime.combine(local.date(), time(), self.tz).astimezone(timezone.utc)

            power = {n: series.get(slot) for n, series in averaged.items()}
            energy: dict[str, float | None] = {}
            for n, series in counters.items():
                if (n, midnight) not in baselines:
                    baselines[(n, midnight)] = self.baseline(n, midnight)
                now, base = series.at(slot), baselines[(n, midnight)]
                # A negative delta means a counter reset or a bad reading; don't guess.
                energy[n] = now - base if now is not None and base is not None and now >= base else None

            gen_power = _total([None if power[n] is None else max(0.0, power[n])
                                for n in self._configured(GENERATION_POWER)])
            cons_power = None
            if gen_power is not None and power.get("grid_power") is not None:
                cons_power = max(0.0, gen_power - power["grid_power"])

            gen_energy = _total([energy[n] for n in self._configured(GENERATION_ENERGY)])
            cons_energy = None
            if gen_energy is not None and None not in (energy.get("grid_import_energy"),
                                                       energy.get("grid_export_energy")):
                cons_energy = max(0.0, gen_energy + energy["grid_import_energy"]
                                  - energy["grid_export_energy"])

            day = local.strftime("%Y%m%d")
            if day_state.get("day") != day:
                day_state.clear()
                day_state["day"] = day
            if gen_energy is not None:
                gen_energy = day_state["generation"] = max(gen_energy, day_state.get("generation", 0.0))
            if cons_energy is not None:
                cons_energy = day_state["consumption"] = max(cons_energy, day_state.get("consumption", 0.0))

            status = Status(
                when=local,
                energy_generation=gen_energy,
                power_generation=gen_power,
                energy_consumption=cons_energy,
                power_consumption=cons_power,
                temperature=power.get("temperature"),
                voltage=power.get("voltage"),
            )
            if status.has_data():
                statuses.append(status)
            else:
                log.warning("No usable data for %s, skipping", local.strftime("%Y-%m-%d %H:%M"))
        return statuses
