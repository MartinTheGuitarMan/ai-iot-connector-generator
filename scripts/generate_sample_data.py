"""Generate a synthetic but realistic industrial telemetry capture.

Simulates one fixed production site emitting paired PROC/PROCF process
sensor sentences and periodic PLC line-telemetry register snapshots once
per simulated minute. Deterministic (seeded RNG) so re-runs are
reproducible — this is fixture data for demoing/testing the connector
pipeline, not a substitute for real device captures (see README).

Writes:
    sample_data/process_sensor_log.txt one industrial_ascii sentence per line
    sample_data/plc_line_telemetry.jsonl one JSON register snapshot per line

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
SITE_LAT = 51.4556  # fixed plant location — sensors don't move
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


def proc_sentence(
    ts: dt.datetime, lat: float, lon: float, status: int, temp_c: float, pressure_bar: float, flow_m3h: float
) -> str:
    lat_raw, lat_dir = to_dm_lat(lat)
    lon_raw, lon_dir = to_dm_lon(lon)
    body = (
        f"INPROC,{ts.strftime('%H%M%S')},{lat_raw},{lat_dir},{lon_raw},{lon_dir},"
        f"{status},{temp_c:.1f},{pressure_bar:.1f},{flow_m3h:.1f}"
    )
    return f"${body}*{checksum(body)}"


def procf_sentence(
    ts: dt.datetime, lat: float, lon: float, vibration_mm_s: float, motor_rpm: int
) -> str:
    lat_raw, lat_dir = to_dm_lat(lat)
    lon_raw, lon_dir = to_dm_lon(lon)
    body = (
        f"INPROCF,{ts.strftime('%H%M%S')},A,{lat_raw},{lat_dir},{lon_raw},{lon_dir},"
        f"{vibration_mm_s:.1f},{motor_rpm},{ts.strftime('%d%m%y')}"
    )
    return f"${body}*{checksum(body)}"


def main() -> None:
    rng = random.Random(SEED)
    OUT_DIR.mkdir(exist_ok=True)

    ts = START_TIME
    uptime_hours = 1234.5

    sentence_lines: list[str] = []
    plc_records: list[dict] = []

    for _ in range(STEPS):
        status = 1  # nominal
        temp_c = 23.0 + rng.uniform(-1.0, 1.0)
        pressure_bar = 2.1 + rng.uniform(-0.1, 0.1)
        flow_m3h = 145.0 + rng.uniform(-3.0, 3.0)
        vibration_mm_s = 1.8 + rng.uniform(-0.3, 0.3)
        motor_rpm = 1780 + rng.randint(-20, 20)

        sentence_lines.append(proc_sentence(ts, SITE_LAT, SITE_LON, status, temp_c, pressure_bar, flow_m3h))
        sentence_lines.append(procf_sentence(ts, SITE_LAT, SITE_LON, vibration_mm_s, motor_rpm))

        line_speed_raw = 1200 + rng.randint(-30, 30)
        current_raw = int(round((12.5 + rng.uniform(-0.5, 0.5)) * 10))
        motor_temp_raw = int(round((65.0 + rng.uniform(-2.0, 2.0)) * 10))
        uptime_hours += STEP_MINUTES / 60.0
        uptime_raw = int(round(uptime_hours * 10))

        plc_records.append(
            {
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "device_id": "line-1",
                "registers": {
                    "0": line_speed_raw & 0xFFFF,
                    "1": current_raw & 0xFFFF,
                    "2": motor_temp_raw & 0xFFFF,
                    "3": uptime_raw & 0xFFFF,
                    "4": (uptime_raw >> 16) & 0xFFFF,
                },
            }
        )

        ts += dt.timedelta(minutes=STEP_MINUTES)

    (OUT_DIR / "process_sensor_log.txt").write_text("\n".join(sentence_lines) + "\n")
    with (OUT_DIR / "plc_line_telemetry.jsonl").open("w") as fh:
        for rec in plc_records:
            fh.write(json.dumps(rec) + "\n")

    print(
        f"wrote {len(sentence_lines)} sentences and {len(plc_records)} PLC records to {OUT_DIR}"
    )


if __name__ == "__main__":
    main()
