"""Generate a synthetic but realistic multi-vendor weather telemetry capture.

Simulates one fixed weather station site emitting paired OBS/OBSF ASCII
sentences, plus a second vendor's station at the same site reporting over
Modbus holding registers instead — the exact kind of per-vendor wire-format
fragmentation this generator exists to paper over. Deterministic (seeded
RNG) so re-runs are reproducible — this is fixture data for demoing/testing
the connector pipeline, not a substitute for real device captures (see
README).

Writes:
    sample_data/weather_obs_log.txt       one wx_ascii sentence per line
    sample_data/weather_station_modbus.jsonl  one JSON register snapshot per line

Usage:
    python3 scripts/generate_sample_data.py
"""

from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path

SEED = 20260921
START_TIME = dt.datetime(2026, 9, 21, 8, 0, 0, tzinfo=dt.timezone.utc)
SITE_LAT = 51.4556  # fixed weather station location — it doesn't move
SITE_LON = 7.0116
STEP_MINUTES = 1
STEPS = 30

OUT_DIR = Path(__file__).resolve().parent.parent / "sample_data"


def checksum(body: str) -> str:
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    return f"{calc:02X}"


def to_dm_lat(decimal: float) -> tuple[str, str]:
    direction = "N" if decimal >= 0 else "S"
    decimal = abs(decimal)
    degrees = int(decimal)
    minutes = (decimal - degrees) * 60
    return f"{degrees:02d}{minutes:07.4f}", direction


def to_dm_lon(decimal: float) -> tuple[str, str]:
    direction = "E" if decimal >= 0 else "W"
    decimal = abs(decimal)
    degrees = int(decimal)
    minutes = (decimal - degrees) * 60
    return f"{degrees:03d}{minutes:07.4f}", direction


def obs_sentence(
    ts: dt.datetime, lat: float, lon: float, temp_c: float, humidity_pct: float, pressure_hpa: float
) -> str:
    lat_raw, lat_dir = to_dm_lat(lat)
    lon_raw, lon_dir = to_dm_lon(lon)
    body = (
        f"STWXOBS,{ts.strftime('%H%M%S')},{lat_raw},{lat_dir},{lon_raw},{lon_dir},"
        f"{temp_c:.1f},{humidity_pct:.1f},{pressure_hpa:.1f}"
    )
    return f"${body}*{checksum(body)}"


def obsf_sentence(
    ts: dt.datetime, lat: float, lon: float, wind_speed_kmh: float, wind_dir_deg: int
) -> str:
    lat_raw, lat_dir = to_dm_lat(lat)
    lon_raw, lon_dir = to_dm_lon(lon)
    body = (
        f"STWXOBSF,{ts.strftime('%H%M%S')},A,{lat_raw},{lat_dir},{lon_raw},{lon_dir},"
        f"{wind_speed_kmh:.1f},{wind_dir_deg},{ts.strftime('%d%m%y')}"
    )
    return f"${body}*{checksum(body)}"


def main() -> None:
    rng = random.Random(SEED)
    OUT_DIR.mkdir(exist_ok=True)

    ts = START_TIME
    rain_accum_mm = 0.0

    sentence_lines: list[str] = []
    modbus_records: list[dict] = []

    for _ in range(STEPS):
        temp_c = 18.0 + rng.uniform(-1.5, 1.5)
        humidity_pct = 62.0 + rng.uniform(-4.0, 4.0)
        pressure_hpa = 1013.0 + rng.uniform(-1.5, 1.5)
        wind_speed_kmh = 12.0 + rng.uniform(-3.0, 3.0)
        wind_dir_deg = 230 + rng.randint(-15, 15)

        sentence_lines.append(obs_sentence(ts, SITE_LAT, SITE_LON, temp_c, humidity_pct, pressure_hpa))
        sentence_lines.append(obsf_sentence(ts, SITE_LAT, SITE_LON, wind_speed_kmh, wind_dir_deg))

        rain_accum_mm += max(0.0, rng.uniform(-0.05, 0.08))
        rain_raw = int(round(rain_accum_mm * 10))
        wind_gust_raw = int(round((wind_speed_kmh + rng.uniform(2.0, 6.0)) * 10))
        battery_raw = int(round((3.7 + rng.uniform(-0.05, 0.05)) * 100))
        uv_index_raw = int(round(max(0.0, 3.0 + rng.uniform(-0.5, 0.5)) * 10))

        modbus_records.append(
            {
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "device_id": "station-2",
                "registers": {
                    "0": rain_raw & 0xFFFF,
                    "1": wind_gust_raw & 0xFFFF,
                    "2": battery_raw & 0xFFFF,
                    "3": uv_index_raw & 0xFFFF,
                },
            }
        )

        ts += dt.timedelta(minutes=STEP_MINUTES)

    (OUT_DIR / "weather_obs_log.txt").write_text("\n".join(sentence_lines) + "\n")
    with (OUT_DIR / "weather_station_modbus.jsonl").open("w") as fh:
        for rec in modbus_records:
            fh.write(json.dumps(rec) + "\n")

    print(
        f"wrote {len(sentence_lines)} sentences and {len(modbus_records)} Modbus records to {OUT_DIR}"
    )


if __name__ == "__main__":
    main()
