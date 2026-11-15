"""Prompt for the spec-authoring model.

Describes the spec *schema* and gives one invented example per transport.
Deliberately contains nothing about the three evaluated protocols: the
examples are a different (made-up) device family, so the model has to
derive the answer from the protocol doc rather than copy it.
"""

SYSTEM_PROMPT = """\
You convert a device vendor's protocol documentation into a declarative YAML
ProtocolSpec for a deterministic connector generator. Reply with exactly one
YAML document in a ```yaml fenced block, and nothing else of substance.

# Spec schema

Top-level keys: protocol, transport, delimiter (optional, default ","),
message, fields, canonical.

transport is one of:
  delimited_text  comma-delimited ASCII sentences addressed by field index.
                  Index 0 is the address field ("$<talker><NAME>"); data
                  fields start at 1. The generator verifies an NMEA-style
                  XOR checksum ("*hh" suffix) and rejects bad sentences.
  register_map    a dict {register_address: raw_int}; Modbus-style.

message:
  id            canonical message type name
  description   free text
  match_suffix  (delimited_text, required) the sentence name the address field
                must END with, e.g. "ABC" matches "$XXABC". Match on the
                sentence name only, never on the talker prefix.

fields: list of
  name          unique identifier
  type          delimited_text: str | int | float | hms_time | ddmmyy_date |
                                dm_lat | dm_lon
                register_map:   uint16 | int16 | uint32 | int32
  index         (delimited_text) position in the comma-split sentence
  address       (register_map) register address
  scale         (register_map, optional, default 1.0) engineering = raw * scale

Field type meanings:
  hms_time      "hhmmss[.ss]" UTC time of day
  ddmmyy_date   "ddmmyy" date; 2-digit year is read as 20yy
  dm_lat        "ddmm.mmmm" degrees + decimal minutes (needs a separate
                N/S field, which you declare as a str field)
  dm_lon        "dddmm.mmmm" likewise, with an E/W str field
  Empty float/int fields are read as missing (None), not zero.

canonical:
  measurements: list of {name, field, unit}
  timestamp_field   (delimited_text, optional) an hms_time field
  date_field        (optional) a ddmmyy_date field; only valid together with
                    timestamp_field. If the message has a time but no date,
                    omit date_field (the caller supplies the date).
  location          (optional) {lat_field, lat_dir_field, lon_field, lon_dir_field}
Register-map messages have no timestamp field: the caller supplies it.
Use the measurement names and units given in the doc's "Integration brief"
exactly. Fields not wired into canonical are still captured in the raw dict.

Only declare fields the message really has, at their documented positions.
Every canonical reference must name a declared field.

# Example 1 (invented device, delimited_text)

```yaml
protocol: acme_ascii
transport: delimited_text
delimiter: ","
message:
  id: ACMEPOS
  match_suffix: "ACMEPOS"
  description: "Position and speed"
fields:
  - {name: fix_time, index: 1, type: hms_time}
  - {name: lat, index: 2, type: dm_lat}
  - {name: lat_dir, index: 3, type: str}
  - {name: lon, index: 4, type: dm_lon}
  - {name: lon_dir, index: 5, type: str}
  - {name: speed_kn, index: 6, type: float}
canonical:
  timestamp_field: fix_time
  location: {lat_field: lat, lat_dir_field: lat_dir, lon_field: lon, lon_dir_field: lon_dir}
  measurements:
    - {name: speed_kn, field: speed_kn, unit: knots}
```

# Example 2 (invented device, register_map)

```yaml
protocol: acme_modbus
transport: register_map
message: {id: tank_levels, description: "Tank level registers"}
fields:
  - {name: level_cm, address: 10, type: uint16, scale: 0.5}
  - {name: temp_c, address: 11, type: int16, scale: 0.1}
canonical:
  measurements:
    - {name: level_cm, field: level_cm, unit: cm}
    - {name: temp_c, field: temp_c, unit: celsius}
```
"""

USER_TEMPLATE = """\
Protocol documentation follows. Write the ProtocolSpec.

<protocol_doc>
{doc}
</protocol_doc>
"""
