import datetime as dt

from connectors import plc_line_telemetry, process_sensor_reading, process_sensor_reading_full

# Self-authored industrial ASCII telemetry sentences (checksum verified by
# running them through the generated connector, not just computed by hand):
# site at 48.1173N, 11.5167E, reading at 08:15:30 UTC.
PROC_SENTENCE = "$INPROC,081530,4807.038,N,01131.000,E,1,23.5,2.1,145.6*3D"
PROCF_SENTENCE = "$INPROCF,081530,A,4807.038,N,01131.000,E,1.8,1780,150325*3D"


def test_proc_parses_position_and_timestamp():
    reading = process_sensor_reading.parse(
        PROC_SENTENCE, device_id="site-1", reference_date=dt.date(2026, 9, 21)
    )
    assert reading is not None
    assert reading.protocol == "industrial_ascii"
    assert reading.message_type == "PROC"
    assert reading.timestamp == "2026-09-21T08:15:30Z"
    assert reading.location.lat == pytest_approx(48 + 7.038 / 60)
    assert reading.location.lon == pytest_approx(11 + 31.000 / 60)


def test_proc_parses_measurements():
    reading = process_sensor_reading.parse(PROC_SENTENCE, device_id="site-1")
    values = {m.name: m.value for m in reading.measurements}
    assert values["sensor_status"] == 1
    assert values["temperature_c"] == 23.5
    assert values["pressure_bar"] == 2.1
    assert values["flow_rate_m3h"] == 145.6


def test_proc_rejects_bad_checksum():
    corrupted = PROC_SENTENCE[:-2] + "00"
    assert process_sensor_reading.parse(corrupted, device_id="site-1") is None


def test_proc_rejects_wrong_message_type():
    # A PROCF sentence handed to the PROC-only connector must be refused,
    # not silently misparsed against PROC's field positions. This also
    # exercises the match-suffix fix: PROC (4 chars) and PROCF (5 chars)
    # must not cross-match on a naive fixed-length suffix comparison.
    assert process_sensor_reading.parse(PROCF_SENTENCE, device_id="site-1") is None


def test_procf_derives_full_timestamp_from_its_own_date_field():
    # Unlike PROC, PROCF carries its own ddmmyy date, so it needs no
    # external reference_date and its parse() signature has no such param.
    reading = process_sensor_reading_full.parse(PROCF_SENTENCE, device_id="site-1")
    assert reading is not None
    assert reading.protocol == "industrial_ascii"
    assert reading.message_type == "PROCF"
    assert reading.timestamp == "2025-03-15T08:15:30Z"
    assert reading.location.lat == pytest_approx(48 + 7.038 / 60)


def test_procf_parses_vibration_and_rpm():
    reading = process_sensor_reading_full.parse(PROCF_SENTENCE, device_id="site-1")
    values = {m.name: m.value for m in reading.measurements}
    assert values["vibration_mm_s"] == pytest_approx(1.8)
    assert values["motor_rpm"] == 1780


def test_procf_rejects_bad_checksum():
    corrupted = PROCF_SENTENCE[:-2] + "00"
    assert process_sensor_reading_full.parse(corrupted, device_id="site-1") is None


def test_plc_line_telemetry_scales_and_signs_values():
    registers = {
        0: 1200,  # line_speed_upm, uint16, scale 1.0
        1: 125,  # motor_current_a raw -> 12.5A after *0.1 scale
        2: 0xFE0C,  # motor_temp_c raw -500 -> -50.0C after *0.1 scale (two's complement)
        3: 22069,  # uptime_hours low word
        4: 1,  # uptime_hours high word -> (1<<16 | 22069) = 87605 -> *0.1 = 8760.5h
    }
    reading = plc_line_telemetry.parse(
        registers, device_id="line-1", timestamp="2026-09-21T09:00:00Z"
    )
    assert reading is not None
    values = {m.name: m.value for m in reading.measurements}
    assert values["line_speed_upm"] == 1200
    assert values["motor_current_a"] == pytest_approx(12.5)
    assert values["motor_temp_c"] == pytest_approx(-50.0)
    assert values["uptime_hours"] == pytest_approx(8760.5)


def test_plc_missing_register_returns_none():
    assert (
        plc_line_telemetry.parse({0: 1200}, device_id="line-1", timestamp="now") is None
    )


def pytest_approx(value):
    import pytest

    return pytest.approx(value, rel=1e-6)
