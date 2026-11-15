"""Simulate a legacy device with a *hidden* wire protocol, for the reverse demo.

This is the "unknown device". It writes three files:

    capture.bin   raw byte stream as a tap on the serial line would see it
                  (frames, ~1% corrupted frames, optionally line noise)
    anchors.yaml  what a human operator would note down: a few display readings
                  per field, tagged with the frame's ordinal
    truth.csv     the full ground truth, used ONLY to score the result

Nothing in `reverse/` may read this file or truth.csv; the whole point is that
inference sees capture.bin + anchors.yaml and nothing else. Deterministic.

Profiles (each mirrors a real-world shape that broke the first version of the tool):

    vessel         sync 7E C3, length byte, type byte, seq counter, CRC-16/MODBUS (LE)
    vessel_oddcrc  the same layout, but with an obscure CRC polynomial (not in any catalogue)
    cressi         3-byte sync AA AA AA, length, command, payload, CRC-16/XMODEM (LE) and
                   a constant end byte 55 AFTER the CRC; no sequence counter
                   (modelled on the Cressi Nepto dive-computer frames)
    apc            constant 19-byte frames, NO sync bytes, NO length byte, NO type byte:
                   id counter, 16 data bytes, 2-byte Fletcher-8 check (not a CRC)
                   (modelled on the APC "Microlink" UPS replies)

Usage:
    python3 scripts/gen_legacy_demo.py --out /tmp/legacy_demo [--profile vessel] [--seed 1337]
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

ODD_POLY = 0x2F15  # not in any catalogue the inference ships with


def crc16(data: bytes, poly=0xA001, init=0xFFFF, reflected=True, xorout=0) -> int:
    crc = init
    if reflected:  # poly given already reflected (0xA001 == reflected 0x8005)
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ poly if crc & 1 else crc >> 1
    else:
        for b in data:
            crc ^= b << 8
            for _ in range(8):
                crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc ^ xorout


def fletcher8(data: bytes) -> int:
    s1 = s2 = 0
    for b in data:
        s1 = (s1 + b) % 255
        s2 = (s2 + s1) % 255
    return (s2 << 8) | s1


# --------------------------------------------------------------------------- vessel family

VESSEL_UNITS = {
    ("engine", "rpm"): "rpm", ("engine", "coolant_c"): "celsius", ("engine", "oil_bar"): "bar",
    ("tank", "tank_id"): "none", ("tank", "level_pm"): "permille", ("tank", "temp_c"): "celsius",
    ("status", "flags"): "none", ("status", "hours"): "hours",
}


class Vessel:
    units = VESSEL_UNITS
    has_seq = True
    noise = True

    def __init__(self, rnd: random.Random, odd_crc: bool = False):
        self.rnd, self.odd = rnd, odd_crc
        self.rpm, self.cool, self.oil = 1480.0, 78.0, 4.1
        self.tanks = [620.0, 340.0, 880.0]
        self.hours = 12345  # raw units of 0.1 h; rolls over a low-byte boundary during the run
        self.flags = 0b00101000

    def step(self, i):
        r = self.rnd
        self.rpm = min(1900, max(700, self.rpm + r.gauss(0, 12)))
        self.cool = min(95, max(55, self.cool + r.gauss(0, 0.15)))
        self.oil = min(5.5, max(2.5, self.oil + r.gauss(0, 0.02)))
        for t in range(3):
            self.tanks[t] = min(1000, max(0, self.tanks[t] + r.gauss(-0.05, 0.6)))
        if i % 97 == 0:
            self.flags ^= 1 << r.choice([0, 3, 5])
        if i % 10 == 0:
            self.hours += 1
        slot = i % 10
        row = {}
        if slot == 0:
            typ, payload = 0x00, b""
            row["message"] = "heartbeat"
        elif slot in (1, 2, 3, 6, 7):
            typ = 0x10
            rp, c, o = int(round(self.rpm)), int(round(self.cool * 10)), int(round(self.oil / 0.05))
            payload = struct.pack(">Hh", rp, c) + bytes([o])
            row.update(message="engine", rpm=rp, coolant_c=c / 10, oil_bar=round(o * 0.05, 2))
        elif slot in (4, 8):
            typ = 0x21
            tid = r.randrange(3)
            lv = int(round(self.tanks[tid]))
            tc = r.randint(-4, 22)  # sub-zero values exist, so signedness is observable
            payload = bytes([tid]) + struct.pack("<H", lv) + struct.pack("b", tc)
            row.update(message="tank", tank_id=tid, level_pm=lv, temp_c=tc)
        else:
            typ = 0x30
            payload = bytes([self.flags]) + struct.pack(">H", self.hours)
            row.update(message="status", flags=self.flags, hours=self.hours / 10)
        return row, typ, payload

    def frame(self, i, typ, payload):
        body = bytes([len(payload), typ, i & 0xFF]) + payload
        if self.odd:
            c = crc16(body, poly=ODD_POLY, init=0xFFFF, reflected=False, xorout=0x0001)
            return b"\x7e\xc3" + body + struct.pack(">H", c)
        return b"\x7e\xc3" + body + struct.pack("<H", crc16(body))

    def corrupt(self, frame):
        fb = bytearray(frame)
        fb[self.rnd.randrange(5, len(fb) - 2)] ^= 0x40
        return bytes(fb) if len(frame) > 7 else frame


# --------------------------------------------------------------------------- cressi-like

CRESSI_UNITS = {
    ("dive", "depth_m"): "m", ("dive", "temp_c"): "celsius", ("dive", "pressure_bar"): "bar",
    ("gas", "o2_pct"): "percent", ("gas", "remaining_min"): "min",
}


class Cressi:
    units = CRESSI_UNITS
    has_seq = False
    noise = True

    def __init__(self, rnd):
        self.rnd = rnd
        self.depth, self.temp, self.bar, self.o2, self.rem = 12.0, 18.0, 190.0, 32, 60

    def step(self, i):
        r = self.rnd
        self.depth = min(40, max(0, self.depth + r.gauss(0, 0.2)))
        self.temp = min(26, max(-2, self.temp + r.gauss(0, 0.05)))
        self.bar = max(30, self.bar - abs(r.gauss(0.04, 0.02)))
        self.rem = max(1, self.rem - (1 if i % 40 == 0 else 0))
        self.o2 = (21, 32, 36, 50)[(i // 1000) % 4]  # the diver switches gas mix during the run
        if i % 3 == 2:
            row = {"message": "gas", "o2_pct": self.o2, "remaining_min": self.rem}
            return row, 0x32, bytes([self.o2]) + struct.pack("<H", self.rem)
        d, t, p = int(round(self.depth * 100)), int(round(self.temp)), int(round(self.bar * 10))
        row = {"message": "dive", "depth_m": d / 100, "temp_c": t, "pressure_bar": p / 10}
        return row, 0x31, struct.pack("<Hb", d, t) + struct.pack("<H", p)

    def frame(self, i, typ, payload):
        body = bytes([len(payload), typ]) + payload
        return b"\xaa\xaa\xaa" + body + struct.pack("<H", crc16(body, poly=0x1021, init=0, reflected=False)) + b"\x55"

    def corrupt(self, frame):
        fb = bytearray(frame)
        fb[self.rnd.randrange(4, len(fb) - 3)] ^= 0x40
        return bytes(fb)


# --------------------------------------------------------------------------- apc-like

APC_UNITS = {
    ("status", "battery_pct"): "percent", ("status", "input_v"): "volts", ("status", "output_v"): "volts",
    ("status", "load_pct"): "percent", ("status", "temp_c"): "celsius", ("status", "runtime_min"): "min",
}


class Apc:
    units = APC_UNITS
    has_seq = True
    noise = False  # point-to-point request/response: no line noise between replies

    def __init__(self, rnd):
        self.rnd = rnd
        self.batt, self.vin, self.vout, self.load, self.temp = 96.0, 230.0, 229.5, 38.0, 22.0

    def step(self, i):
        r = self.rnd
        self.batt = min(100, max(40, self.batt + r.gauss(0, 0.05)))
        self.vin = min(250, max(190, self.vin + r.gauss(0, 0.6)))
        self.vout = min(240, max(215, self.vout + r.gauss(0, 0.4)))
        self.load = min(90, max(5, self.load + r.gauss(0, 0.4)))
        self.temp = min(40, max(-5, self.temp + r.gauss(0, 0.1)))
        b, vi, vo, ld, tp = (int(round(self.batt)), int(round(self.vin * 10)), int(round(self.vout * 10)),
                             int(round(self.load)), int(round(self.temp)))
        rt = max(1, int(round(self.batt * 1.6 - self.load * 0.5)))
        row = {"message": "status", "battery_pct": b, "input_v": vi / 10, "output_v": vo / 10,
               "load_pct": ld, "temp_c": tp, "runtime_min": rt}
        data = b"SUA1" + bytes([b]) + struct.pack(">HH", vi, vo) + bytes([ld]) + struct.pack("b", tp) \
            + struct.pack("<H", rt) + b"\x00\x00\x00"
        assert len(data) == 16
        return row, 0, data

    def frame(self, i, typ, payload):
        body = bytes([i % 127]) + payload  # id counter wraps after 0x7E
        return body + struct.pack("<H", fletcher8(body))

    def corrupt(self, frame):
        fb = bytearray(frame)
        fb[self.rnd.randrange(1, 17)] ^= 0x40
        return bytes(fb)


PROFILES = {
    "vessel": lambda rnd: Vessel(rnd),
    "vessel_oddcrc": lambda rnd: Vessel(rnd, odd_crc=True),
    "cressi": lambda rnd: Cressi(rnd),
    "apc": lambda rnd: Apc(rnd),
}


def build(out: Path, profile: str = "vessel", seed: int = 1337) -> None:
    rnd = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    dev = PROFILES[profile](rnd)

    stream = bytearray()
    truth: list[dict] = []
    vidx = 0
    for i in range(N_FRAMES):
        row, typ, payload = dev.step(i)
        frame = dev.frame(i, typ, payload)
        row.update(ord=i, corrupt=0)
        if rnd.random() < 0.01 and len(frame) > 7:
            frame = dev.corrupt(frame)
            row["corrupt"] = 1
        else:
            row["vidx"] = vidx
            vidx += 1
        stream += frame
        truth.append(row)
        if dev.noise and rnd.random() < 0.02:  # line noise (never contains a sync byte)
            stream += bytes(rnd.choice([b for b in range(256) if b not in (0x7E, 0xAA)])
                            for _ in range(rnd.randint(1, 4)))

    (out / "capture.bin").write_bytes(bytes(stream))

    fields = sorted({k for r in truth for k in r} - {"ord", "vidx", "message", "corrupt"})
    with open(out / "truth.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ord", "vidx", "message", "corrupt"] + fields, restval="")
        w.writeheader()
        w.writerows(truth)

    # Simulated operator: for each field, read the display at ANCHORS_PER_FIELD moments
    # spread across the range of values seen (min .. max), like jotting down readings.
    # Ordinal = the frame's sequence counter if the protocol has one, else its index
    # among the valid frames.
    anchors = []
    for (message, field), unit in dev.units.items():
        rows = [r for r in truth if r.get("message") == message and not r["corrupt"]]
        rows.sort(key=lambda r: float(r[field]))
        picks = [rows[round(k * (len(rows) - 1) / (ANCHORS_PER_FIELD - 1))] for k in range(ANCHORS_PER_FIELD)]
        for r in picks:
            anchors.append({"message": message, "field": field, "unit": unit,
                            "ordinal": r["ord"] if dev.has_seq else r["vidx"], "value": float(r[field])})
    (out / "anchors.yaml").write_text(yaml.safe_dump({"anchors": anchors}, sort_keys=False))
    print(f"[{profile}] wrote {out}/capture.bin ({len(stream)} bytes, {len(truth)} frames), anchors.yaml, truth.csv")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--profile", choices=sorted(PROFILES), default="vessel")
    ap.add_argument("--seed", type=int, default=1337)
    a = ap.parse_args()
    build(a.out, a.profile, a.seed)
