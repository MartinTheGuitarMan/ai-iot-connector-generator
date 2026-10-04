"""Every record in sample_data/ must parse cleanly through its connector.

This is the regression test for scripts/generate_sample_data.py: if the
generator ever produces a bad checksum, a malformed register value, or an
out-of-range field, this is what catches it — not eyeballing the output.
"""

import json
from pathlib import Path

from connectors import plc_line_telemetry, process_sensor_reading, process_sensor_reading_full

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


def test_sample_process_log_parses_completely():
    lines = (SAMPLE_DIR / "process_sensor_log.txt").read_text().splitlines()
    assert lines, "sample_data/process_sensor_log.txt is empty — run scripts/generate_sample_data.py"

    parsed = 0
    for line in lines:
        sentence_id = line.split(",")[0]
        if sentence_id.endswith("PROCF"):
            reading = process_sensor_reading_full.parse(line, device_id="site-1")
        elif sentence_id.endswith("PROC"):
            reading = process_sensor_reading.parse(line, device_id="site-1")
        else:
            raise AssertionError(f"unrecognized sentence in sample data: {line!r}")
        assert reading is not None, f"failed to parse: {line!r}"
        assert reading.location is not None
        parsed += 1

    assert parsed == len(lines)


def test_sample_plc_telemetry_parses_completely():
    lines = (SAMPLE_DIR / "plc_line_telemetry.jsonl").read_text().splitlines()
    assert lines, "sample_data/plc_line_telemetry.jsonl is empty — run scripts/generate_sample_data.py"

    for line in lines:
        rec = json.loads(line)
        registers = {int(k): v for k, v in rec["registers"].items()}
        reading = plc_line_telemetry.parse(
            registers, device_id=rec["device_id"], timestamp=rec["timestamp"]
        )
        assert reading is not None, f"failed to parse: {rec!r}"
        values = {m.name: m.value for m in reading.measurements}
        assert values["line_speed_upm"] > 0
        assert values["uptime_hours"] > 0


def test_sample_process_log_location_is_stable():
    # This site is fixed, not a moving vehicle — every PROC fix should
    # report (near enough) the same coordinates. Catches a broken lat/lon
    # formatter producing wildly wrong coordinates that still happen to
    # parse, same as a moving-track plausibility check would elsewhere.
    fixes = []
    for line in (SAMPLE_DIR / "process_sensor_log.txt").read_text().splitlines():
        if line.split(",")[0].endswith("PROC") and not line.split(",")[0].endswith("PROCF"):
            reading = process_sensor_reading.parse(line, device_id="site-1")
            fixes.append((reading.location.lat, reading.location.lon))

    assert len(fixes) >= 2
    first = fixes[0]
    for lat, lon in fixes[1:]:
        assert abs(lat - first[0]) < 1e-6
        assert abs(lon - first[1]) < 1e-6
