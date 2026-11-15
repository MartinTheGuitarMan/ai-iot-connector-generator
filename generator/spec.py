"""Declarative protocol spec model.

A ProtocolSpec describes one message type of one IoT device protocol: how
to read its fields off the wire, and how those fields map onto the
canonical IoT reading (canonical.CanonicalReading). generator.codegen
renders this spec into a standalone Python connector module.

Three transports are supported for now:

- "delimited_text": comma-delimited sentences addressed by field index,
  e.g. a weather station's ASCII telemetry ($WXOBS,123519,4807.038,N,...).
- "register_map": a dict of {register_address: raw_int_value}, e.g. Modbus
  holding registers read from a facility/HVAC controller.
- "framed_binary": a byte stream of self-delimiting frames (sync bytes, a
  length field, a message-type byte, a payload, a trailing CRC16), with
  fields addressed by byte offset inside the payload. This is the shape of
  many legacy serial/RS-485 device protocols.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_TRANSPORTS = {"delimited_text", "register_map", "framed_binary"}

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


# Field "type" values understood by the framed_binary codegen backend
# (struct-style: <width><sign>_<endianness>; single bytes have no endianness).
FRAMED_BINARY_FIELD_TYPES = {
    "uint8", "int8",
    "uint16_be", "uint16_le", "int16_be", "int16_le",
    "uint32_be", "uint32_le", "int32_be", "int32_le",
}
FRAMED_BINARY_FIELD_WIDTHS = {
    "uint8": 1, "int8": 1,
    "uint16_be": 2, "uint16_le": 2, "int16_be": 2, "int16_le": 2,
    "uint32_be": 4, "uint32_le": 4, "int32_be": 4, "int32_le": 4,
}


class SpecError(ValueError):
    pass


@dataclass
class FieldSpec:
    name: str
    type: str
    index: int | None = None  # delimited_text: position in the split sentence
    address: int | None = None  # register_map: register address
    scale: float = 1.0  # register_map / framed_binary: raw_value * scale (+ bias)
    unit: str | None = None
    offset: int | None = None  # framed_binary: byte offset inside the payload
    bias: float = 0.0  # framed_binary: added after scaling (value = raw * scale + bias)


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
    type_value: int | None = None  # framed_binary: value of the frame's type byte
    payload_length: int | None = None  # framed_binary: expected payload size (optional)


CHECKSUM_ALGOS = {"crc16": 2, "fletcher8": 2, "fletcher8_256": 2, "sum8": 1, "sum16": 2,
                  "negsum8": 1, "negsum16": 2, "xor8": 1, "crc8": 1, "crc32": 4}  # algo -> stored size in bytes


@dataclass
class CrcSpec:
    """Frame check trailer of a framed_binary frame.

    algo: crc8 / crc16 / crc32 (Rocksoft-style poly/init/reflected/xorout), fletcher8 (two mod-255
    sums, stored as (sum2 << 8) | sum1), fletcher8_256 (same with mod 256, as u-blox UBX), sum8 / sum16
    (byte sum, init/xorout applied), negsum8 / negsum16 (two's complement of the sum) or xor8.
    The checked bytes run from `start` up to just before the check value.
    """

    poly: int = 0  # crc16 only
    algo: str = "crc16"
    init: int = 0
    reflected: bool = False  # crc16 only: reflect input bytes and output
    xorout: int = 0
    start: int = 0  # first frame byte covered by the check
    endian: str = "little"  # byte order of a 2-byte check value


@dataclass
class FramingSpec:
    """Frame layout shared by every message of a framed_binary protocol.

    Frames are delimited in one of two ways: a length byte (total length =
    frame[length_offset] + length_overhead, the overhead counting sync, header,
    check and trailer bytes) or a constant `fixed_length`. A frame may start with
    `sync` bytes (empty = none; then only fixed_length works and the stream is
    re-synchronised on the check value), may end with constant `trailer` bytes
    after the check value, and may have a message-type byte (`type_offset`) or not
    (single-message protocols). The payload runs from payload_offset up to the check.
    """

    sync: str = ""  # hex string, e.g. "7ec3"; "" = no sync bytes
    payload_offset: int = 0
    length_offset: int | None = None
    length_overhead: int | None = None
    fixed_length: int | None = None
    type_offset: int | None = None
    seq_offset: int | None = None  # informational: rolling sequence counter
    trailer: str = ""  # hex string of constant bytes after the check value
    crc: CrcSpec | None = None


@dataclass
class ProtocolSpec:
    protocol: str
    transport: str
    message: MessageSpec
    fields: list[FieldSpec]
    canonical: CanonicalMapping
    delimiter: str = ","
    framing: FramingSpec | None = None  # framed_binary only

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

        allowed_types = {
            "delimited_text": DELIMITED_TEXT_FIELD_TYPES,
            "register_map": REGISTER_MAP_FIELD_TYPES,
            "framed_binary": FRAMED_BINARY_FIELD_TYPES,
        }[self.transport]
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
            if self.transport == "framed_binary" and f.offset is None:
                raise SpecError(f"field {f.name!r} needs 'offset' for framed_binary transport")

        if self.transport == "framed_binary":
            self._validate_framed_binary()

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

    def _validate_framed_binary(self) -> None:
        fr = self.framing
        if fr is None:
            raise SpecError("framed_binary spec needs a 'framing' section")
        try:
            sync = bytes.fromhex(fr.sync)
            trailer = bytes.fromhex(fr.trailer)
        except ValueError:
            raise SpecError("framing.sync / framing.trailer must be hex strings") from None

        length_based = fr.length_offset is not None or fr.length_overhead is not None
        if length_based and fr.fixed_length is not None:
            raise SpecError("framing: use either length_offset/length_overhead or fixed_length, not both")
        if length_based and (fr.length_offset is None or fr.length_overhead is None):
            raise SpecError("framing: length_offset and length_overhead go together")
        if not length_based and fr.fixed_length is None:
            raise SpecError("framing needs length_offset+length_overhead or fixed_length")
        if not sync and not fr.fixed_length:
            raise SpecError("a framed_binary protocol without sync bytes needs fixed_length")

        if (fr.type_offset is None) != (self.message.type_value is None):
            raise SpecError("framing.type_offset and message.type_value must be given together")

        chk_size = 0
        if fr.crc is not None:
            if fr.crc.algo not in CHECKSUM_ALGOS:
                raise SpecError(f"unsupported checksum algo {fr.crc.algo!r} (one of {sorted(CHECKSUM_ALGOS)})")
            if fr.crc.endian not in ("little", "big"):
                raise SpecError("crc.endian must be 'little' or 'big'")
            if fr.crc.algo in ("crc8", "crc16", "crc32") and not fr.crc.poly:
                raise SpecError(f"{fr.crc.algo} needs a poly")
            chk_size = CHECKSUM_ALGOS[fr.crc.algo]
            if fr.crc.start < 0 or fr.crc.start > fr.payload_offset:
                raise SpecError("crc.start must lie between the frame start and the payload start")

        headers = [o for o in (fr.length_offset, fr.type_offset, fr.seq_offset) if o is not None]
        if any(o < len(sync) for o in headers):
            raise SpecError("header offsets must lie after the sync bytes")
        if fr.payload_offset < len(sync) or (headers and fr.payload_offset <= max(headers)):
            raise SpecError("framing.payload_offset must come after the sync and header bytes")
        min_frame = fr.payload_offset + chk_size + len(trailer)
        total = fr.length_overhead if length_based else fr.fixed_length
        if total < min_frame:
            raise SpecError(
                f"framing too short: sync/header/check/trailer need at least {min_frame} bytes, got {total}"
            )

        payload_max = self.message.payload_length
        if payload_max is None and not length_based:
            payload_max = fr.fixed_length - fr.payload_offset - chk_size - len(trailer)
        for f in self.fields:
            end = f.offset + FRAMED_BINARY_FIELD_WIDTHS[f.type]
            if f.offset < 0:
                raise SpecError(f"field {f.name!r} has a negative offset")
            if payload_max is not None and end > payload_max:
                raise SpecError(
                    f"field {f.name!r} ends at payload byte {end}, beyond payload length {payload_max}"
                )

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

        framing = None
        if data.get("framing"):
            fdata = dict(data["framing"])
            crc_data = fdata.pop("crc", None)
            framing = FramingSpec(crc=CrcSpec(**crc_data) if crc_data else None, **fdata)

        spec = ProtocolSpec(
            protocol=data["protocol"],
            transport=data["transport"],
            message=message,
            fields=fields,
            canonical=canonical,
            delimiter=data.get("delimiter", ","),
            framing=framing,
        )
        spec.validate()
        return spec

    @staticmethod
    def from_yaml(path: str | Path) -> "ProtocolSpec":
        with open(path) as fh:
            data = yaml.safe_load(fh)
        return ProtocolSpec.from_dict(data)
