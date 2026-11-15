"""Find where a known reading lives in a frame, without knowing the frame layout.

Given a frame and a value read off the device's display (or a reference decode), list every
(offset, width, encoding) under which the bytes decode to that value, optionally times a power of ten.
Encodings: unsigned little/big endian integer, signed little endian, packed BCD (low digit pair first, as
M-Bus). With a single frame this cannot say WHICH hit is the field; it narrows the search and, across several
frames of the same layout, the true location is the one that keeps matching."""

from __future__ import annotations

import math
import struct


def _bcd_le(b: bytes) -> int | None:
    v = 0
    for byte in reversed(b):
        hi, lo = byte >> 4, byte & 0x0F
        if hi > 9 or lo > 9:
            return None
        v = v * 100 + hi * 10 + lo
    return v


def decode_candidates(b: bytes):
    yield "uint_le", int.from_bytes(b, "little")
    yield "uint_be", int.from_bytes(b, "big")
    yield "int_le", int.from_bytes(b, "little", signed=True)
    bcd = _bcd_le(b)
    if bcd is not None:
        yield "bcd_le", bcd
    if len(b) == 4:
        for name, fmt in (("float32_le", "<f"), ("float32_be", ">f")):
            f = struct.unpack(fmt, b)[0]
            if math.isfinite(f):
                yield name, f


def locate(frame: bytes, value: float, scales=(1, 10, 100, 1000, 0.1, 0.01, 0.001), widths=(1, 2, 3, 4, 6, 8),
           start: int = 0, end: int | None = None) -> list[dict]:
    end = len(frame) if end is None else end
    hits = []
    for off in range(start, end):
        for w in widths:
            if off + w > end:
                continue
            for enc, raw in decode_candidates(frame[off:off + w]):
                for s in scales:
                    tol = 1e-6 if enc.startswith("float") else 1e-9
                    if raw != 0 and abs(raw * s - value) <= tol * max(1.0, abs(value)):
                        hits.append({"offset": off, "width": w, "encoding": enc, "scale": s})
    return hits
