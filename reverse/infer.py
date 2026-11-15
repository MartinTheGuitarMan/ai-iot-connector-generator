"""Infer a framed_binary protocol spec from a raw capture plus a few anchor readings.

Inputs (and nothing else):
  * the raw byte stream captured off the wire
  * anchors: a handful of known (message, field, ordinal, value) readings, e.g. what
    the device's own display showed at a given moment. `ordinal` is the frame's
    sequence-counter value, unwrapped (the capture has no clock).

Two stages:
  1. Structure from the bytes alone: sync, length field, type/seq bytes, CRC16
     parameters (swept over common variants), message types and payload sizes.
  2. Field encodings from anchors: every (offset, width, endianness, signedness)
     candidate is fitted to the anchors with  value = raw * scale + bias;  the best
     exact fit wins, and ambiguity is reported instead of hidden.

The output is a dict per message type that goes through ProtocolSpec.from_dict,
so an inferred spec is validated by exactly the same code as a hand-written one.
"""

from __future__ import annotations

import collections
import itertools
import math
import struct
from dataclasses import dataclass, field

from generator.spec import FRAMED_BINARY_FIELD_WIDTHS

_STRUCT = {
    "uint8": "B", "int8": "b",
    "uint16_be": ">H", "uint16_le": "<H", "int16_be": ">h", "int16_le": "<h",
    "uint32_be": ">I", "uint32_le": "<I", "int32_be": ">i", "int32_le": "<i",
}
# Prefer unsigned, narrow, big-endian-first when several encodings fit equally well.
_TYPE_PREFERENCE = list(_STRUCT)

_CRC_POLYS = [0x1021, 0x8005, 0x3D65, 0x0589]


class InferenceError(RuntimeError):
    pass


@dataclass
class Frame:
    raw: bytes
    ordinal: int  # unwrapped sequence counter (or arrival index without one)


@dataclass
class Structure:
    sync: bytes
    length_offset: int
    length_overhead: int
    type_offset: int
    seq_offset: int | None
    payload_offset: int
    crc: dict | None  # CrcSpec kwargs
    frames: list[Frame]
    report: list[str] = field(default_factory=list)

    def framing_dict(self) -> dict:
        d = {
            "sync": self.sync.hex(),
            "length_offset": self.length_offset,
            "length_overhead": self.length_overhead,
            "type_offset": self.type_offset,
            "payload_offset": self.payload_offset,
        }
        if self.seq_offset is not None:
            d["seq_offset"] = self.seq_offset
        if self.crc:
            d["crc"] = dict(self.crc)
        return d


# --------------------------------------------------------------------------- stage 1

def _crc16(buf: bytes, poly: int, init: int, reflected: bool, xorout: int) -> int:
    crc = init
    if reflected:
        rpoly = int(f"{poly:016b}"[::-1], 2)
        for b in buf:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ rpoly if crc & 1 else crc >> 1
    else:
        for b in buf:
            crc ^= b << 8
            for _ in range(8):
                crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc ^ xorout


def infer_structure(data: bytes) -> Structure:
    rep: list[str] = []
    if len(data) < 64:
        raise InferenceError("capture too short to infer anything")

    # sync: most frequent 2-gram, extended while the following byte is constant
    c2 = collections.Counter(data[i:i + 2] for i in range(len(data) - 1))
    sync, n_sync = c2.most_common(1)[0]
    pos = [i for i in range(len(data) - len(sync) + 1) if data[i:i + len(sync)] == sync]
    while True:
        nxt = collections.Counter(data[i + len(sync)] for i in pos if i + len(sync) < len(data))
        byte, cnt = nxt.most_common(1)[0]
        if cnt >= 0.95 * len(pos):
            sync += bytes([byte])
            continue
        break
    rep.append(f"sync {sync.hex(' ')} (seen {len(pos)}x)")

    bounds = pos + [len(data)]  # include the final frame (it has no following sync)
    chunks = [data[a:b] for a, b in zip(bounds, bounds[1:]) if b - a >= len(sync) + 3]
    if len(chunks) < 20:
        raise InferenceError("too few frames to infer structure")

    # length field: a header byte k such that chunk_len - byte is constant
    hdr = range(len(sync), len(sync) + 6)
    length_offset = overhead = None
    for k in hdr:
        c = collections.Counter(len(ch) - ch[k] for ch in chunks if len(ch) > k)
        val, cnt = c.most_common(1)[0]
        if cnt >= 0.9 * len(chunks):
            length_offset, overhead = k, val
            break
    if length_offset is None:
        raise InferenceError("no length field found")
    rep.append(f"length field at byte {length_offset}; frame length = byte + {overhead}")

    good = [ch[:ch[length_offset] + overhead] for ch in chunks
            if len(ch) >= ch[length_offset] + overhead and len(ch) > length_offset]

    # seq: increments by one (mod 256); type: low-cardinality header byte
    seq_offset = type_offset = None
    for k in hdr:
        if k == length_offset:
            continue
        seqs = [g[k] for g in good if len(g) > k]
        if sum((b - a) % 256 == 1 for a, b in zip(seqs, seqs[1:])) >= 0.9 * (len(seqs) - 1):
            seq_offset = k
            break
    for k in hdr:
        if k in (length_offset, seq_offset):
            continue
        if 2 <= len({g[k] for g in good if len(g) > k}) <= 16:
            type_offset = k
            break
    if type_offset is None:
        raise InferenceError("no message-type byte found")
    payload_offset = max(length_offset, type_offset, seq_offset or 0) + 1
    rep.append(f"type byte at {type_offset}, "
               f"{'seq counter at ' + str(seq_offset) if seq_offset is not None else 'no seq counter'}, "
               f"payload starts at byte {payload_offset}")

    # CRC16: sweep common parameter sets over plausible covered spans / byte orders
    crc = None
    sample = [g for g in good if len(g) >= payload_offset + 2][:300]
    combos = itertools.product(_CRC_POLYS, [0, 0xFFFF], [True, False], [0, 0xFFFF],
                               sorted({0, len(sync), *range(len(sync), payload_offset + 1)}), ["little", "big"])
    for poly, init, refl, xo, start, endian in combos:
        def ok(g):
            return _crc16(g[start:-2], poly, init, refl, xo) == int.from_bytes(g[-2:], endian)
        if all(ok(g) for g in sample[:12]) and sum(map(ok, sample)) >= 0.95 * len(sample):
            crc = dict(poly=poly, init=init, reflected=refl, xorout=xo, start=start, endian=endian)
            rep.append(f"CRC16 poly={poly:#06x} init={init:#06x} reflected={refl} xorout={xo:#06x} "
                       f"over bytes[{start}:-2], stored {endian}-endian")
            break
    if crc is None:
        rep.append("WARNING: no CRC16 variant matched; frames are validated by length only")

    def valid(g: bytes) -> bool:
        if not crc:
            return True
        return _crc16(g[crc["start"]:-2], crc["poly"], crc["init"], crc["reflected"], crc["xorout"]) \
            == int.from_bytes(g[-2:], crc["endian"])

    frames, base, prev = [], 0, None
    for g in good:
        if not valid(g):
            continue
        if seq_offset is not None:
            s = g[seq_offset]
            if prev is not None and s < prev - 128:
                base += 256
            prev = s
            ordinal = base + s
        else:
            ordinal = len(frames)
        frames.append(Frame(g, ordinal))
    rep.append(f"{len(frames)} valid frames; {len(chunks) - len(frames)} rejected (corrupt / noise)")
    return Structure(sync, length_offset, overhead, type_offset, seq_offset, payload_offset, crc, frames, rep)


# --------------------------------------------------------------------------- stage 2

def _roundness(a: float) -> float:
    m = abs(a) / 10 ** math.floor(math.log10(abs(a)))
    return min(abs(m - c) / c for c in (1, 2, 2.5, 5, 10))


def _fit(xs: list[float], ys: list[float]):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return a, my - a * mx


@dataclass
class FieldFit:
    name: str
    type: str
    offset: int
    scale: float
    bias: float
    unit: str | None
    residual: float
    alternatives: int


def fit_field(payloads: list[bytes], ys: list[float], plen: int, name: str, unit: str | None) -> FieldFit:
    rng = (max(ys) - min(ys)) or 1.0
    tol = max(1e-6, 0.002 * rng)
    found = []
    for ftype in _TYPE_PREFERENCE:
        w = FRAMED_BINARY_FIELD_WIDTHS[ftype]
        for off in range(plen - w + 1):
            xs = [struct.unpack_from(_STRUCT[ftype], p, off)[0] for p in payloads]
            ab = _fit(xs, ys)
            if not ab or abs(ab[0]) < 1e-12:
                continue
            a = float(f"{ab[0]:.8g}")
            b = ab[1]
            if all(abs(a * x - y) <= tol for x, y in zip(xs, ys)):
                b = 0.0  # fits without a bias term: prefer that
            else:
                b = float(f"{b:.8g}")
            res = max(abs(a * x + b - y) for x, y in zip(xs, ys))
            if res <= tol:
                found.append((b != 0.0, round(_roundness(a), 6), w, _TYPE_PREFERENCE.index(ftype), off, ftype, a, b, res))
    if not found:
        raise InferenceError(f"no payload encoding fits the anchors for field {name!r}")
    found.sort()
    _, _, _, _, off, ftype, a, b, res = found[0]
    return FieldFit(name, ftype, off, a, b, unit, res, len(found) - 1)


def infer_specs(data: bytes, anchors: list[dict], protocol: str) -> tuple[Structure, dict[str, dict], list[str]]:
    st = infer_structure(data)
    rep = list(st.report)
    by_ord = {f.ordinal: f for f in st.frames}

    by_msg: dict[str, list[dict]] = collections.defaultdict(list)
    for a in anchors:
        by_msg[a["message"]].append(a)

    specs: dict[str, dict] = {}
    for msg, items in by_msg.items():
        types = set()
        for a in items:
            fr = by_ord.get(int(a["ordinal"]))
            if fr is None:
                raise InferenceError(f"anchor ordinal {a['ordinal']} for {msg}.{a['field']} is not a valid frame")
            types.add(fr.raw[st.type_offset])
        if len(types) != 1:
            raise InferenceError(f"anchors for message {msg!r} point at several frame types: {sorted(types)}")
        type_value = types.pop()

        crc_len = 2 if st.crc else 0
        same = [f for f in st.frames if f.raw[st.type_offset] == type_value]
        lens = collections.Counter(len(f.raw) - st.payload_offset - crc_len for f in same)
        plen = lens.most_common(1)[0][0]
        rep.append(f"message {msg!r}: type {type_value:#04x}, {len(same)} frames, payload {plen} bytes"
                   + (f" (also seen: {dict(lens)})" if len(lens) > 1 else ""))

        fields: list[FieldFit] = []
        names = list(dict.fromkeys(a["field"] for a in items))
        for fname in names:
            rows = [a for a in items if a["field"] == fname]
            payloads = [by_ord[int(a["ordinal"])].raw[st.payload_offset:len(by_ord[int(a["ordinal"])].raw) - crc_len]
                        for a in rows]
            ys = [float(a["value"]) for a in rows]
            ff = fit_field(payloads, ys, plen, fname, rows[0].get("unit"))
            fields.append(ff)
            amb = f"  [{ff.alternatives} lower-ranked encodings also within tolerance]" if ff.alternatives else ""
            bias = f" bias={ff.bias:g}" if ff.bias else ""
            rep.append(f"  {fname}: payload[{ff.offset}] {ff.type} scale={ff.scale:g}{bias}"
                       f"  anchor residual {ff.residual:.2g}{amb}")

        spans = sorted((f.offset, f.offset + FRAMED_BINARY_FIELD_WIDTHS[f.type], f.name) for f in fields)
        for (s1, e1, n1), (s2, e2, n2) in zip(spans, spans[1:]):
            if s2 < e1:
                rep.append(f"  WARNING: fields {n1!r} and {n2!r} overlap in the payload")
        covered = {i for s, e, _ in spans for i in range(s, e)}
        unmapped = [i for i in range(plen) if i not in covered]
        if unmapped:
            rep.append(f"  unmapped payload bytes: {unmapped} (no anchors for them)")

        fdicts = []
        for f in fields:
            d = {"name": f.name, "type": f.type, "offset": f.offset, "scale": f.scale}
            if f.bias:
                d["bias"] = f.bias
            if f.unit:
                d["unit"] = f.unit
            fdicts.append(d)
        specs[msg] = {
            "protocol": protocol,
            "transport": "framed_binary",
            "framing": st.framing_dict(),
            "message": {
                "id": msg,
                "description": f"Inferred from a capture ({len(same)} frames, type {type_value:#04x})",
                "type_value": type_value,
                "payload_length": plen,
            },
            "fields": fdicts,
            "canonical": {"measurements": [
                {"name": f.name, "field": f.name, **({"unit": f.unit} if f.unit else {})} for f in fields
            ]},
        }

    seen_types = {f.raw[st.type_offset] for f in st.frames}
    anchored = {s["message"]["type_value"] for s in specs.values()}
    for t in sorted(seen_types - anchored):
        n = sum(1 for f in st.frames if f.raw[st.type_offset] == t)
        rep.append(f"type {t:#04x}: {n} frames, no anchors given -> no spec emitted")
    return st, specs, rep
