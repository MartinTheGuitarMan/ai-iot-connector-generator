"""Canonical IoT data model that every generated connector must produce.

This is the single target shape every generated protocol connector
translates into, regardless of source protocol (ASCII sensor telemetry
sentences, binary register maps, etc).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Measurement:
    name: str
    value: Any
    unit: str | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "value": self.value, "unit": self.unit}


@dataclass
class Location:
    lat: float
    lon: float

    def to_dict(self) -> dict:
        return {"lat": self.lat, "lon": self.lon}


@dataclass
class CanonicalReading:
    device_id: str
    protocol: str
    message_type: str
    timestamp: str  # ISO 8601 UTC, e.g. "2026-09-21T09:48:00Z"
    measurements: list[Measurement] = field(default_factory=list)
    location: Location | None = None
    raw: dict | None = None

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "protocol": self.protocol,
            "message_type": self.message_type,
            "timestamp": self.timestamp,
            "measurements": [m.to_dict() for m in self.measurements],
            "location": self.location.to_dict() if self.location else None,
            "raw": self.raw,
        }
