"""Reads sample_data/, runs every message through its generated connector,
and writes the resulting canonical readings to InfluxDB.

Usage:
    # Print line-protocol output without connecting to anything (works
    # anywhere, no InfluxDB required — this is how the pipeline is verified
    # in environments that can't run a live database):
    python -m pipeline.run_pipeline --dry-run

    # Write to a real InfluxDB (see docker-compose.yml to run one locally):
    INFLUX_TOKEN=devtoken-not-a-secret python -m pipeline.run_pipeline

Configuration (env vars, all optional except INFLUX_TOKEN for a real write):
    INFLUX_URL     default http://localhost:8086
    INFLUX_ORG     default iot
    INFLUX_BUCKET  default telemetry
    INFLUX_TOKEN   no default — required unless --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from canonical import CanonicalReading
from connectors import weather_station_modbus, weather_station_obs, weather_station_obs_full
from pipeline.influx_sink import to_point

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


def read_weather_obs_readings(device_id: str = "site-1") -> list[CanonicalReading]:
    readings = []
    path = SAMPLE_DIR / "weather_obs_log.txt"
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        sentence_id = line.split(",")[0]
        if sentence_id.endswith("WXOBSF"):
            reading = weather_station_obs_full.parse(line, device_id=device_id)
        elif sentence_id.endswith("WXOBS"):
            reading = weather_station_obs.parse(line, device_id=device_id)
        else:
            raise ValueError(f"unrecognized sentence in {path}: {line!r}")
        if reading is None:
            raise ValueError(f"connector rejected a sentence in {path}: {line!r}")
        readings.append(reading)
    return readings


def read_weather_station_modbus_readings() -> list[CanonicalReading]:
    readings = []
    path = SAMPLE_DIR / "weather_station_modbus.jsonl"
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        registers = {int(k): v for k, v in rec["registers"].items()}
        reading = weather_station_modbus.parse(
            registers, device_id=rec["device_id"], timestamp=rec["timestamp"]
        )
        if reading is None:
            raise ValueError(f"connector rejected a register snapshot in {path}: {rec!r}")
        readings.append(reading)
    return readings


def load_all_readings() -> list[CanonicalReading]:
    return read_weather_obs_readings() + read_weather_station_modbus_readings()


def write_to_influx(readings: list[CanonicalReading]) -> None:
    from influxdb_client import InfluxDBClient
    from influxdb_client.client.write_api import SYNCHRONOUS

    url = os.environ.get("INFLUX_URL", "http://localhost:8086")
    org = os.environ.get("INFLUX_ORG", "iot")
    bucket = os.environ.get("INFLUX_BUCKET", "telemetry")
    token = os.environ["INFLUX_TOKEN"]  # no default: a real write needs a real token

    points = [to_point(r) for r in readings]
    with InfluxDBClient(url=url, token=token, org=org) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=bucket, org=org, record=points)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pipeline.run_pipeline")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse sample_data and print line-protocol output; do not connect to InfluxDB",
    )
    args = parser.parse_args(argv)

    readings = load_all_readings()

    if args.dry_run:
        for reading in readings:
            print(to_point(reading).to_line_protocol())
        print(f"# {len(readings)} readings (dry run, nothing written)")
        return 0

    write_to_influx(readings)
    print(f"wrote {len(readings)} readings to InfluxDB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
