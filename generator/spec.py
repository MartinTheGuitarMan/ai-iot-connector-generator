"""Declarative protocol spec model.

A ProtocolSpec describes one message type of one IoT device protocol: how
to read its fields off the wire, and how those fields map onto the
canonical IoT reading (canonical.CanonicalReading). generator.codegen
renders this spec into a standalone Python connector module.

Two transports are supported for now:

- "delimited_text": comma-delimited sentences addressed by field index,
  e.g. a weather station's ASCII telemetry ($WXOBS,123519,4807.038,N,...).
- "register_map": a dict of {register_address: raw_int_value}, e.g. Modbus
  holding registers read from a facility/HVAC controller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_TRANSPORTS = {"delimited_text", "register_map"}

# Field "type" values understood by the delimited_text codegen backend.
# hms_time: "HHMMSS[.ss]" time-of-day. ddmmyy_date: "DDMMYY" date, pairable
# with an hms_time field via canonical.date_field for a self-contained
# timestamp. dm_lat/dm_lon: degrees+decimal-minutes coordinates (ddmm.mmmm /
# dddmm.mmmm) with a separate N/S or E/W direction field.
DELIMITED_TEXT_FIELD_TYPES = {
    "str", "int", "float", "hms_time", "ddmmyy_date", "dm_lat", "dm_lon",
}

# Field "type" values understood by the register_map codegen backend.
REGISTER_MAP_FIELD_TYPES = {"uint16", "int16", "uint32", "int32"}


class SpecError(ValueError):
    pass


@dataclass
class FieldSpec:
    name: str
    type: str
    index: int | None = None  # delimited_text: position in the split sentence
    address: int | None = None  # register_map: register address
    scale: float = 1.0  # register_map: raw_value * scale
    unit: str | None = None


@dataclass
class MeasurementMapping:
    name: str
    field: str
    unit: str | None = None


@dataclass
class LocationMapping:
    lat_field: str
    lat_dir_field: str
    lon_field: str
    lon_dir_field: str


@dataclass
class CanonicalMapping:
    measurements: list[MeasurementMapping] = field(default_factory=list)
    timestamp_field: str | None = None
    date_field: str | None = None  # optional ddmmyy_date field paired with timestamp_field
    location: LocationMapping | None = None


@dataclass
class MessageSpec:
    id: str
    description: str = ""
    match_suffix: str | None = None  # delimited_text: e.g. "OBS" matches $WXOBS/$FWOBS


@dataclass
class ProtocolSpec:
    protocol: str
    transport: str
    message: MessageSpec
    fields: list[FieldSpec]
    canonical: CanonicalMapping
    delimiter: str = ","

    def field_by_name(self, name: str) -> FieldSpec:
        for f in self.fields:
            if f.name == name:
                return f
        raise SpecError(f"canonical mapping references unknown field {name!r}")

    def validate(self) -> None:
        if self.transport not in VALID_TRANSPORTS:
            raise SpecError(
                f"unknown transport {self.transport!r}, expected one of {sorted(VALID_TRANSPORTS)}"
            )
        if not self.fields:
            raise SpecError("spec defines no fields")

        allowed_types = (
            DELIMITED_TEXT_FIELD_TYPES
            if self.transport == "delimited_text"
            else REGISTER_MAP_FIELD_TYPES
        )
        for f in self.fields:
            if f.type not in allowed_types:
                raise SpecError(
                    f"field {f.name!r} has type {f.type!r}, not valid for "
                    f"transport {self.transport!r} (allowed: {sorted(allowed_types)})"
                )
            if self.transport == "delimited_text" and f.index is None:
                raise SpecError(f"field {f.name!r} needs 'index' for delimited_text transport")
            if self.transport == "register_map" and f.address is None:
                raise SpecError(f"field {f.name!r} needs 'address' for register_map transport")

        if self.transport == "delimited_text" and not self.message.match_suffix:
            raise SpecError("delimited_text message needs 'match_suffix'")

        for m in self.canonical.measurements:
            self.field_by_name(m.field)
        if self.canonical.timestamp_field:
            self.field_by_name(self.canonical.timestamp_field)
        if self.canonical.date_field:
            if not self.canonical.timestamp_field:
                raise SpecError("canonical.date_field requires canonical.timestamp_field")
            date_field = self.field_by_name(self.canonical.date_field)
            if date_field.type != "ddmmyy_date":
                raise SpecError(
                    f"canonical.date_field {date_field.name!r} must have type 'ddmmyy_date'"
                )
            ts_field = self.field_by_name(self.canonical.timestamp_field)
            if ts_field.type != "hms_time":
                raise SpecError(
                    f"canonical.timestamp_field {ts_field.name!r} must have type 'hms_time' "
                    "when canonical.date_field is set"
                )
        if self.canonical.location:
            loc = self.canonical.location
            for fname in (loc.lat_field, loc.lat_dir_field, loc.lon_field, loc.lon_dir_field):
                self.field_by_name(fname)

    @staticmethod
    def from_dict(data: dict) -> "ProtocolSpec":
        fields = [FieldSpec(**f) for f in data["fields"]]

        canonical_data = data.get("canonical", {})
        measurements = [
            MeasurementMapping(**m) for m in canonical_data.get("measurements", [])
        ]
        location_data = canonical_data.get("location")
        location = LocationMapping(**location_data) if location_data else None
        canonical = CanonicalMapping(
            measurements=measurements,
            timestamp_field=canonical_data.get("timestamp_field"),
            date_field=canonical_data.get("date_field"),
            location=location,
        )

        message = MessageSpec(**data["message"])

        spec = ProtocolSpec(
            protocol=data["protocol"],
            transport=data["transport"],
            message=message,
            fields=fields,
            canonical=canonical,
            delimiter=data.get("delimiter", ","),
        )
        spec.validate()
        return spec

    @staticmethod
    def from_yaml(path: str | Path) -> "ProtocolSpec":
        with open(path) as fh:
            data = yaml.safe_load(fh)
        return ProtocolSpec.from_dict(data)
