"""Minimal PVOutput client for the Add Batch Status service.

API reference: https://pvoutput.org/help/api_specification.html#add-batch-status-service
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

BATCH_URL = "https://pvoutput.org/service/r2/addbatchstatus.jsp"


class PVOutputError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class RateLimited(PVOutputError):
    def __init__(self, message: str, reset_at: datetime | None):
        super().__init__(message, retryable=True)
        self.reset_at = reset_at


@dataclass
class Status:
    when: datetime                      # local time of the end of the interval
    energy_generation: float | None = None   # v1, Wh since local midnight
    power_generation: float | None = None    # v2, W
    energy_consumption: float | None = None  # v3, Wh since local midnight
    power_consumption: float | None = None   # v4, W
    temperature: float | None = None         # v5, deg C
    voltage: float | None = None             # v6, V

    def has_data(self) -> bool:
        return any(v is not None for v in (self.energy_generation, self.power_generation,
                                           self.energy_consumption, self.power_consumption))

    def encode(self) -> str:
        values = [
            None if self.energy_generation is None else f"{self.energy_generation:.0f}",
            None if self.power_generation is None else f"{self.power_generation:.0f}",
            None if self.energy_consumption is None else f"{self.energy_consumption:.0f}",
            None if self.power_consumption is None else f"{self.power_consumption:.0f}",
            None if self.temperature is None else f"{self.temperature:.1f}",
            None if self.voltage is None else f"{self.voltage:.1f}",
        ]
        # Energy generation is a mandatory column; anything after the last known value can be dropped.
        while len(values) > 1 and values[-1] is None:
            values.pop()
        fields = [self.when.strftime("%Y%m%d"), self.when.strftime("%H:%M")]
        fields += ["-1" if v is None else v for v in values]
        return ",".join(fields)


class PVOutputClient:
    def __init__(self, api_key: str, system_id: str, timeout: int = 30):
        self.timeout = timeout
        self.blocked_until: datetime | None = None
        self.session = requests.Session()
        self.session.headers.update({
            "X-Pvoutput-Apikey": api_key,
            "X-Pvoutput-SystemId": str(system_id),
            "X-Rate-Limit": "1",
        })

    def add_batch(self, statuses: list[Status]) -> int:
        """Upload statuses; returns how many PVOutput reported as added/updated."""
        payload = ";".join(s.encode() for s in statuses)
        try:
            resp = self.session.post(BATCH_URL, data={"data": payload}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise PVOutputError(f"request failed: {exc}", retryable=True) from exc

        body = resp.text.strip()
        remaining = resp.headers.get("X-Rate-Limit-Remaining")
        reset = resp.headers.get("X-Rate-Limit-Reset")
        reset_at = datetime.fromtimestamp(int(reset), timezone.utc) if reset and reset.isdigit() else None

        if resp.status_code == 403 and "exceeded" in body.lower():
            raise RateLimited(body, reset_at)
        if resp.status_code >= 500:
            raise PVOutputError(f"HTTP {resp.status_code}: {body}", retryable=True)
        if resp.status_code != 200:
            # 400s (bad data, date too old, ...) won't succeed on retry; 401/403 need a config fix.
            raise PVOutputError(f"HTTP {resp.status_code}: {body}",
                                retryable=resp.status_code in (401, 403))

        log.debug("PVOutput response: %s (rate limit remaining: %s)", body, remaining)
        # This upload worked, but the next one won't; let the caller wait for the reset.
        self.blocked_until = reset_at if remaining == "0" else None
        return sum(1 for item in body.split(";") if item.strip().endswith(",1"))
