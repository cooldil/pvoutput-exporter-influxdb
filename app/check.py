"""--check: show what each configured signal finds in InfluxDB, to help fix config.yaml."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .exporter import COUNTERS, GENERATION_ENERGY, GENERATION_POWER, Exporter

LOOKBACK = timedelta(hours=24)

# PVOutput value -> (label, unit, attribute on Status, signals it is calculated from)
OUTPUTS = [
    ("v1 energy generation today", "Wh", "energy_generation", GENERATION_ENERGY),
    ("v2 power generation", "W", "power_generation", GENERATION_POWER),
    ("v3 energy consumption today", "Wh", "energy_consumption",
     GENERATION_ENERGY + ("grid_import_energy", "grid_export_energy")),
    ("v4 power consumption", "W", "power_consumption", GENERATION_POWER + ("grid_power",)),
    ("v5 temperature", "C", "temperature", ("temperature",)),
    ("v6 voltage", "V", "voltage", ("voltage",)),
]


def _age(delta: timedelta) -> str:
    secs = max(0, int(delta.total_seconds()))
    if secs < 120:
        return f"{secs}s"
    if secs < 7200:
        return f"{secs // 60}m"
    return f"{secs // 3600}h {secs % 3600 // 60}m"


def _location(sig) -> str:
    tags = "".join(f", {k}={v}" for k, v in sig.tags.items())
    return f"measurement={sig.measurement}, field={sig.field}{tags}"


def run_check(exporter: Exporter, slot: datetime) -> bool:
    """Print a report; returns True if no problems were found."""
    now = datetime.now(timezone.utc)
    tz = exporter.tz
    local_now = now.astimezone(tz)
    midnight = datetime.combine(local_now.date(), datetime.min.time(), tz).astimezone(timezone.utc)
    problems = []

    print(f"\nLatest reading of each signal (searching the last {LOOKBACK.total_seconds() / 3600:.0f}h, "
          f"times in {tz}):\n")
    for name, sig in exporter.signals.items():
        point = exporter.source.point(sig, now - LOOKBACK, now + timedelta(minutes=1), "last")
        if point is None:
            print(f"  {name:<24} {'NO DATA':>14}   {_location(sig)}")
            problems.append(f"{name}: nothing found for {_location(sig)}. "
                            f"Check the measurement, field and tag names/values in InfluxDB.")
            continue

        t, value = point
        notes = []
        max_age = exporter.counter_max_age if name in COUNTERS else exporter.interval * 2
        if now - t > max_age:
            notes.append("STALE")
            problems.append(f"{name}: last reading was {_age(now - t)} ago "
                            f"({t.astimezone(tz):%Y-%m-%d %H:%M}); is the logger still writing it?")

        if name in COUNTERS:
            base = exporter.baseline(name, midnight)
            if base is None:
                notes.append("no reading near midnight")
                problems.append(f"{name}: no reading around midnight, so today's total can't be worked out.")
            elif value < base:
                notes.append(f"LOWER than at midnight ({base:.0f})")
                problems.append(f"{name}: value went down since midnight. Wrong field, "
                                f"or a counter that resets daily rather than a lifetime total?")
            else:
                notes.append(f"today {value - base:+.0f} Wh")
        elif name in GENERATION_POWER and value < -50:
            notes.append("NEGATIVE")
            problems.append(f"{name}: production power is negative; add 'scale: -1' to this signal.")
        elif name == "grid_power":
            notes.append("exporting" if value > 0 else "importing" if value < 0 else "")

        print(f"  {name:<24} {value:>14.1f}   {_age(now - t):>6} ago   {'  '.join(n for n in notes if n)}")

    statuses = exporter.build_statuses([slot], {})
    print(f"\nWhat would be sent for {slot.astimezone(tz):%Y-%m-%d %H:%M}:\n")
    status = statuses[0] if statuses else None
    for label, unit, attr, needs in OUTPUTS:
        value = getattr(status, attr) if status else None
        if value is not None:
            print(f"  {label:<30} {value:>10.1f} {unit}")
            continue
        configured = [n for n in needs if n in exporter.signals]
        # Production parts are optional, but consumption can't be worked out without the grid meter.
        unconfigured_grid = [n for n in needs if n.startswith("grid_") and n not in exporter.signals]
        if unconfigured_grid or not configured:
            reason = "not configured: " + ", ".join(unconfigured_grid or needs)
        else:
            reason = "calculated from " + ", ".join(configured)
        print(f"  {label:<30} {'missing':>10}    {reason}")

    if "grid_power" in exporter.signals:
        print("\nTip: grid_power should show 'importing' at night. If it says 'exporting', "
              "add 'scale: -1' to it.")

    if problems:
        print("\nProblems:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("\nNo problems found.")
    print()
    return not problems
