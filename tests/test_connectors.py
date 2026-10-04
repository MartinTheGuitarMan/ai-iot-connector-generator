import datetime as dt

from connectors import weather_station_modbus, weather_station_obs, weather_station_obs_full

# Self-authored weather station ASCII sentences (checksum verified by running
# them through the generated connector, not just computed by hand):
# site at 48.1173N, 11.5167E, reading at 08:15:30 UTC.
OBS_SENTENCE = "$STWXOBS,081530,4807.038,N,01131.000,E,21.4,63.0,1013.2*7D"
OBSF_SENTENCE = "$STWXOBSF,081530,A,4807.038,N,01131.000,E,14.5,230,150325*64"


def test_obs_parses_position_and_timestamp():
    reading = weather_station_obs.parse(
        OBS_SENTENCE, device_id="site-1", reference_date=dt.date(2026, 9, 21)
    )
    assert reading is not None
    assert reading.protocol == "wx_ascii"
    assert reading.message_type == "WXOBS"
    assert reading.timestamp == "2026-09-21T08:15:30Z"
    assert reading.location.lat == pytest_approx(48 + 7.038 / 60)
    assert reading.location.lon == pytest_approx(11 + 31.000 / 60)


def test_obs_parses_measurements():
    reading = weather_station_obs.parse(OBS_SENTENCE, device_id="site-1")
    values = {m.name: m.value for m in reading.measurements}
    assert values["temperature_c"] == 21.4
    assert values["humidity_pct"] == 63.0
    assert values["pressure_hpa"] == 1013.2


def test_obs_rejects_bad_checksum():
    corrupted = OBS_SENTENCE[:-2] + "00"
    assert weather_station_obs.parse(corrupted, device_id="site-1") is None


def test_obs_rejects_wrong_message_type():
    # An OBSF sentence handed to the OBS-only connector must be refused,
    # not silently misparsed against OBS's field positions. This also
    # exercises the match-suffix fix: WXOBS (5 chars) and WXOBSF (6 chars)
    # must not cross-match on a naive fixed-length suffix comparison.
    assert weather_station_obs.parse(OBSF_SENTENCE, device_id="site-1") is None


def test_obsf_derives_full_timestamp_from_its_own_date_field():
    # Unlike OBS, OBSF carries its own ddmmyy date, so it needs no
    # external reference_date and its parse() signature has no such param.
    reading = weather_station_obs_full.parse(OBSF_SENTENCE, device_id="site-1")
    assert reading is not None
    assert reading.protocol == "wx_ascii"
    assert reading.message_type == "WXOBSF"
    assert reading.timestamp == "2025-03-15T08:15:30Z"
    assert reading.location.lat == pytest_approx(48 + 7.038 / 60)


def test_obsf_parses_wind():
    reading = weather_station_obs_full.parse(OBSF_SENTENCE, device_id="site-1")
    values = {m.name: m.value for m in reading.measurements}
    assert values["wind_speed_kmh"] == pytest_approx(14.5)
    assert values["wind_dir_deg"] == 230


def test_obsf_rejects_bad_checksum():
    corrupted = OBSF_SENTENCE[:-2] + "00"
    assert weather_station_obs_full.parse(corrupted, device_id="site-1") is None


def test_modbus_weather_station_scales_values():
    registers = {
        0: 125,  # rain_mm, uint16, scale 0.1 -> 12.5mm
        1: 320,  # wind_gust_kmh, scale 0.1 -> 32.0 kmh
        2: 370,  # battery_v, scale 0.01 -> 3.70V
        3: 45,  # uv_index, scale 0.1 -> 4.5
    }
    reading = weather_station_modbus.parse(
        registers, device_id="station-2", timestamp="2026-09-21T09:00:00Z"
    )
    assert reading is not None
    values = {m.name: m.value for m in reading.measurements}
    assert values["rain_mm"] == pytest_approx(12.5)
    assert values["wind_gust_kmh"] == pytest_approx(32.0)
    assert values["battery_v"] == pytest_approx(3.70)
    assert values["uv_index"] == pytest_approx(4.5)


def test_modbus_weather_station_missing_register_returns_none():
    assert (
        weather_station_modbus.parse({0: 125}, device_id="station-2", timestamp="now")
        is None
    )


def pytest_approx(value):
    import pytest

    return pytest.approx(value, rel=1e-6)
