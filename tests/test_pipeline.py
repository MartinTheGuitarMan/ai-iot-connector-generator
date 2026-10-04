"""End-to-end pipeline test: sample_data -> connectors -> canonical -> Point.

Runs entirely offline (no InfluxDB connection): load_all_readings() and
to_point() are pure functions, so this exercises the exact same code path
`pipeline.run_pipeline --dry-run` does, without needing a live database.
"""

from pipeline.influx_sink import to_point
from pipeline.run_pipeline import load_all_readings


def test_loads_every_sample_record():
    readings = load_all_readings()
    assert len(readings) == 90  # 60 sentences (30 PROC + 30 PROCF) + 30 PLC snapshots

    message_types = {r.message_type for r in readings}
    assert message_types == {"PROC", "PROCF", "line_telemetry"}


def test_every_reading_converts_to_a_writable_point():
    readings = load_all_readings()
    for reading in readings:
        line = to_point(reading).to_line_protocol()
        assert line, f"reading produced an empty (unwritable) line: {reading!r}"
        assert line.startswith("iot_reading,")


def test_readings_are_in_chronological_order():
    readings = load_all_readings()
    proc = [r for r in readings if r.message_type == "PROC"]
    timestamps = [r.timestamp for r in proc]
    assert timestamps == sorted(timestamps)
