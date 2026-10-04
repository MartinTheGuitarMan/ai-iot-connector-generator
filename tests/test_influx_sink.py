"""Verifies CanonicalReading -> InfluxDB Point mapping without a live server.

influxdb_client.Point.to_line_protocol() is pure serialization — it needs no
network connection, so this is fully offline-testable even though we can't
run an actual InfluxDB instance in this environment.
"""

import pytest

from canonical import CanonicalReading, Location, Measurement
from pipeline.influx_sink import MEASUREMENT, to_point


def _line(reading: CanonicalReading) -> str:
    return to_point(reading).to_line_protocol()


def test_tags_and_fields_are_present():
    reading = CanonicalReading(
        device_id="site-1",
        protocol="wx_ascii",
        message_type="WXOBS",
        timestamp="2026-09-21T12:35:19Z",
        measurements=[
            Measurement(name="temperature_c", value=23.5, unit="celsius"),
            Measurement(name="sensor_status", value=1, unit="none"),
        ],
        location=Location(lat=48.1173, lon=11.5167),
    )
    line = _line(reading)

    assert line.startswith(f"{MEASUREMENT},")
    assert "device_id=site-1" in line
    assert "protocol=wx_ascii" in line
    assert "message_type=WXOBS" in line
    assert "temperature_c=23.5" in line
    assert "sensor_status=1i" in line  # int fields get an "i" suffix
    assert "lat=48.1173" in line
    assert "lon=11.5167" in line
    # ISO 8601 UTC "2026-09-21T12:35:19Z" as nanosecond epoch:
    assert line.endswith(" 1789994119000000000")


def test_timestamp_round_trips_exactly():
    import datetime as dt

    ts = "2026-09-21T12:35:19Z"
    reading = CanonicalReading(
        device_id="d", protocol="p", message_type="m", timestamp=ts,
        measurements=[Measurement(name="x", value=1.0)],
    )
    line = _line(reading)
    epoch_ns = int(line.rsplit(" ", 1)[-1])
    expected = int(dt.datetime(2026, 9, 21, 12, 35, 19, tzinfo=dt.timezone.utc).timestamp()) * 10**9
    assert epoch_ns == expected


def test_reading_with_no_fields_raises_instead_of_silently_vanishing():
    # A Point with zero fields serializes to "" and would silently be
    # dropped on write rather than erroring — to_point() must refuse it.
    reading = CanonicalReading(
        device_id="d", protocol="p", message_type="m",
        timestamp="2026-09-21T12:35:19Z",
        measurements=[Measurement(name="hdop", value=None)],
    )
    with pytest.raises(ValueError):
        to_point(reading)


def test_none_valued_measurements_are_dropped_not_written_broken():
    reading = CanonicalReading(
        device_id="d",
        protocol="p",
        message_type="m",
        timestamp="2026-09-21T12:35:19Z",
        measurements=[
            Measurement(name="hdop", value=None, unit="none"),
            Measurement(name="altitude", value=1.0, unit="m"),
        ],
    )
    line = _line(reading)
    assert "hdop" not in line
    assert "altitude=1" in line


def test_reading_without_location_has_no_lat_lon_fields():
    reading = CanonicalReading(
        device_id="station-2",
        protocol="modbus_weather",
        message_type="station_telemetry",
        timestamp="2026-09-21T08:00:00Z",
        measurements=[Measurement(name="rain_mm", value=1.2, unit="mm")],
    )
    line = _line(reading)
    assert "lat=" not in line
    assert "lon=" not in line


def test_different_readings_use_the_same_measurement_name():
    # message_type is a tag, not the measurement, so one query can span
    # every protocol/message type for a device.
    obs = CanonicalReading(
        device_id="d", protocol="wx_ascii", message_type="WXOBS",
        timestamp="2026-09-21T08:00:00Z",
        measurements=[Measurement(name="temperature_c", value=1.0)],
    )
    modbus = CanonicalReading(
        device_id="d", protocol="modbus_weather", message_type="station_telemetry",
        timestamp="2026-09-21T08:00:00Z",
        measurements=[Measurement(name="rain_mm", value=1.2)],
    )
    assert _line(obs).split(",")[0] == _line(modbus).split(",")[0] == MEASUREMENT
