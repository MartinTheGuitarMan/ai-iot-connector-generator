"""Simulate a legacy vessel device with a *hidden* wire protocol, for the reverse demo.

This is the "unknown device". It writes three files:

    capture.bin   raw byte stream as a tap on the serial line would see it
                  (frames, ~1% corrupted frames, occasional line noise)
    anchors.yaml  what a human operator would note down: a few display readings
                  per field, tagged with the frame's sequence-number ordinal
    truth.csv     the full ground truth, used ONLY to score the result

Nothing in `reverse/` may read this file or truth.csv; the whole point is that
inference sees capture.bin + anchors.yaml and nothing else. Deterministic.

Usage:
    python3 scripts/gen_legacy_demo.py --out /tmp/legacy_demo
"""

from __future__ import annotations

import argparse
import csv
import random
import struct
from pathlib import Path

import yaml

N_FRAMES = 4000
ANCHORS_PER_FIELD = 6

# message, field -> unit shown on the (imaginary) display
UNITS = {
    ("engine", "rpm"): "rpm", ("engine", "coolant_c"): "celsius", ("engine", "oil_bar"): "bar",
    ("tank", "tank_id"): "none", ("tank", "level_pm"): "permille", ("tank", "temp_c"): "celsius",
    ("status", "flags"): "none", ("status", "hours"): "hours",
}


def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build(out: Path, seed: int = 1337) -> None:
    rnd = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)

    rpm, cool, oil = 1480.0, 78.0, 4.1
    tanks = [620.0, 340.0, 880.0]
    hours = 12345  # raw units of 0.1 h; rolls over a low-byte boundary during the run
    flags = 0b00101000
    stream = bytearray()
    truth: list[dict] = []

    for i in range(N_FRAMES):
        rpm = min(1900, max(700, rpm + rnd.gauss(0, 12)))
        cool = min(95, max(55, cool + rnd.gauss(0, 0.15)))
        oil = min(5.5, max(2.5, oil + rnd.gauss(0, 0.02)))
        for t in range(3):
            tanks[t] = min(1000, max(0, tanks[t] + rnd.gauss(-0.05, 0.6)))
        if i % 97 == 0:
            flags ^= 1 << rnd.choice([0, 3, 5])
        if i % 10 == 0:
            hours += 1

        slot = i % 10
        row = {"ord": i, "corrupt": 0}
        if slot == 0:
            typ, payload = 0x00, b""
            row["message"] = "heartbeat"
        elif slot in (1, 2, 3, 6, 7):
            typ = 0x10
            r, c, o = int(round(rpm)), int(round(cool * 10)), int(round(oil / 0.05))
            payload = struct.pack(">Hh", r, c) + bytes([o])
            row.update(message="engine", rpm=r, coolant_c=c / 10, oil_bar=round(o * 0.05, 2))
        elif slot in (4, 8):
            typ = 0x21
            tid = rnd.randrange(3)
            lv = int(round(tanks[tid]))
            tc = rnd.randint(-4, 22)  # sub-zero values exist, so signedness is observable
            payload = bytes([tid]) + struct.pack("<H", lv) + struct.pack("b", tc)
            row.update(message="tank", tank_id=tid, level_pm=lv, temp_c=tc)
        else:
            typ = 0x30
            payload = bytes([flags]) + struct.pack(">H", hours)
            row.update(message="status", flags=flags, hours=hours / 10)

        body = bytes([len(payload), typ, i & 0xFF]) + payload
        frame = b"\x7e\xc3" + body + struct.pack("<H", _crc16_modbus(body))
        if rnd.random() < 0.01 and len(frame) > 7:
            fb = bytearray(frame)
            fb[rnd.randrange(5, len(fb) - 2)] ^= 0x40
            frame = bytes(fb)
            row["corrupt"] = 1
        stream += frame
        truth.append(row)
        if rnd.random() < 0.02:  # line noise (never contains the sync byte)
            stream += bytes(rnd.choice([b for b in range(256) if b != 0x7E])
                            for _ in range(rnd.randint(1, 4)))

    (out / "capture.bin").write_bytes(bytes(stream))

    keys = ["ord", "message", "corrupt", "rpm", "coolant_c", "oil_bar",
            "tank_id", "level_pm", "temp_c", "flags", "hours"]
    with open(out / "truth.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(truth)

    # Simulated operator: for each field, read the display at ANCHORS_PER_FIELD moments
    # spread across the range of values seen (min .. max), like jotting down readings.
    anchors = []
    for (message, field), unit in UNITS.items():
        rows = [r for r in truth if r.get("message") == message and not r["corrupt"]]
        rows.sort(key=lambda r: float(r[field]))
        picks = [rows[round(k * (len(rows) - 1) / (ANCHORS_PER_FIELD - 1))] for k in range(ANCHORS_PER_FIELD)]
        for r in picks:
            anchors.append({"message": message, "field": field, "unit": unit,
                            "ordinal": r["ord"], "value": float(r[field])})
    (out / "anchors.yaml").write_text(yaml.safe_dump({"anchors": anchors}, sort_keys=False))
    print(f"wrote {out}/capture.bin ({len(stream)} bytes, {len(truth)} frames), anchors.yaml, truth.csv")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    build(ap.parse_args().out)
