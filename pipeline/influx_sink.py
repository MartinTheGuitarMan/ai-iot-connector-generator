"""Maps a canonical.CanonicalReading onto an InfluxDB Point.

Kept separate from generator/ and connectors/ deliberately: this is
transport to one specific time-series store, not part of the
protocol-to-canonical translation the generator is responsible for.
Swapping InfluxDB for another sink means replacing this module, nothing
upstream of it changes.

All readings share one measurement ("iot_reading"); message_type is a
tag rather than the measurement name so a single query can span protocols
(e.g. "every reading from device_id=site-1 in the last hour").
"""

from __future__ import annotations

from influxdb_client import Point

from canonical import CanonicalReading

MEASUREMENT = "iot_reading"


def to_point(reading: CanonicalReading) -> Point:
    point = (
        Point(MEASUREMENT)
        .tag("device_id", reading.device_id)
        .tag("protocol", reading.protocol)
        .tag("message_type", reading.message_type)
        .time(reading.timestamp)
    )
    field_count = 0
    for m in reading.measurements:
        if m.value is None:
            continue
        point = point.field(m.name, m.value)
        field_count += 1
    if reading.location is not None:
        point = point.field("lat", reading.location.lat).field("lon", reading.location.lon)
        field_count += 2

    if field_count == 0:
        # A Point with no fields serializes to "" (InfluxDB line protocol
        # requires at least one field) and would silently vanish on write.
        raise ValueError(
            f"reading {reading.device_id}/{reading.message_type} at {reading.timestamp} "
            "has no non-null measurements and no location — nothing to write"
        )
    return point
