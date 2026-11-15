"""Infer a framed_binary protocol spec from a raw capture plus a few anchor readings.

Inputs (and nothing else):
  * the raw byte stream captured off the wire
  * anchors: a handful of known (message, field, ordinal, value) readings, e.g. what
    the device's own display showed at a given moment. `ordinal` is the frame's
    sequence-counter value, unwrapped (the capture has no clock). Without a sequence
    counter it is the index of the frame among the valid frames.
  * optionally a KnowledgeBase of what earlier validated runs learned.

Stage 1, structure from the bytes alone. Three ways in, tried in this order:
  1. a framing already in the knowledge base that explains the capture;
  2. a repeating sync pattern with variable gaps -> a length field is searched;
  3. constant-size frames (equal gaps, or a period found by autocorrelation) -> the
     frame phase is found by which alignment makes a checksum validate.
Then: trailer (constant bytes after the check value), checksum (CRC16 variants,
Fletcher-8, byte sum, XOR - learned parameters first), sequence counter, type byte.

Stage 2, field encodings from anchors: every (offset, width, endianness, signedness)
candidate is fitted with  value = raw * scale + bias;  the best exact fit wins, ties
are broken by prior evidence (knowledge base), then by roundness of the scale.
Ambiguity is reported instead of hidden.

The output is a dict per message that goes through ProtocolSpec.from_dict, so an
inferred spec is validated by exactly the same code as a hand-written one.
"""

from __future__ import annotations

import collections
import itertools
import math
import struct
from dataclasses import dataclass, field

from generator.spec import CHECKSUM_ALGOS, FRAMED_BINARY_FIELD_WIDTHS
from reverse.knowledge import KnowledgeBase

_STRUCT = {
    "uint8": "B", "int8": "b",
    "uint16_be": ">H", "uint16_le": "<H", "int16_be": ">h", "int16_le": "<h",
    "uint32_be": ">I", "uint32_le": "<I", "int32_be": ">i", "int32_le": "<i",
}
# Prefer unsigned, narrow, big-endian-first when several encodings fit equally well.
_TYPE_PREFERENCE = list(_STRUCT)

_BUILTIN_POLYS = [0x1021, 0x8005, 0x3D65, 0x0589]
_CRC8_POLYS = [0x07, 0x31, 0x1D, 0x9B, 0xD5, 0x39, 0x2F, 0xA7, 0x49, 0x81]   # 0x81: BACnet MS/TP header CRC
_CRC32_POLYS = [0x04C11DB7, 0x1EDC6F41, 0xA833982B]


class InferenceError(RuntimeError):
    pass


# --------------------------------------------------------------------------- checksums

def _crc_generic(buf: bytes, width: int, poly: int, init: int, reflected: bool, xorout: int) -> int:
    """Rocksoft-model CRC for any width (8/16/32); `poly` is given in the normal (non-reflected) form."""
    mask = (1 << width) - 1
    top = 1 << (width - 1)
    crc = init
    if reflected:
        rpoly = int(f"{poly:0{width}b}"[::-1], 2)
        for b in buf:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ rpoly if crc & 1 else crc >> 1
    else:
        for b in buf:
            crc ^= b << (width - 8)
            for _ in range(8):
                crc = ((crc << 1) ^ poly) & mask if crc & top else (crc << 1) & mask
    return (crc ^ xorout) & mask


def checksum(buf: bytes, algo: str, poly: int = 0, init: int = 0, reflected: bool = False,
             xorout: int = 0) -> int:
    if algo in ("crc8", "crc32"):
        return _crc_generic(buf, 8 if algo == "crc8" else 32, poly, init, reflected, xorout)
    if algo == "crc16":
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
    if algo == "fletcher8":
        s1 = s2 = 0
        for b in buf:
            s1 = (s1 + b) % 255
            s2 = (s2 + s1) % 255
        return (s2 << 8) | s1
    if algo == "fletcher8_256":
        s1 = s2 = 0
        for b in buf:
            s1 = (s1 + b) & 0xFF
            s2 = (s2 + s1) & 0xFF
        return (s2 << 8) | s1
    if algo == "sum16":
        return ((init + sum(buf)) & 0xFFFF) ^ xorout
    if algo == "negsum8":
        return (init - sum(buf)) & 0xFF
    if algo == "negsum16":
        return (init - sum(buf)) & 0xFFFF
    if algo == "sum8":
        return ((init + sum(buf)) & 0xFF) ^ xorout
    if algo == "xor8":
        acc = init
        for b in buf:
            acc ^= b
        return acc ^ xorout
    raise ValueError(algo)


def _check_ok(frame: bytes, p: dict, trailer_len: int) -> bool:
    size = CHECKSUM_ALGOS[p["algo"]]
    end = len(frame) - trailer_len - size
    if end < p["start"]:
        return False
    stored = int.from_bytes(frame[end:end + size], p.get("endian", "little") if size > 1 else "big")
    return checksum(frame[p["start"]:end], p["algo"], p.get("poly", 0), p.get("init", 0),
                    p.get("reflected", False), p.get("xorout", 0)) == stored


def _checksum_candidates(sync_len: int, max_start: int, kb: KnowledgeBase):
    for p in kb.learned_checksums():  # what earlier validated runs established
        yield dict(p)
    polys = list(dict.fromkeys(_BUILTIN_POLYS + kb.learned_polys()))
    starts = sorted({s for s in range(0, sync_len + 8) if s <= max_start})  # every offset up to just past the sync
    for start in starts:
        for poly, init, refl, xo, endian in itertools.product(
                polys, [0, 0xFFFF], [True, False], [0, 0xFFFF], ["little", "big"]):
            yield dict(algo="crc16", poly=poly, init=init, reflected=refl, xorout=xo, start=start, endian=endian)
        for poly, init, refl, xo in itertools.product(_CRC8_POLYS, [0, 0xFF], [True, False], [0, 0xFF]):
            yield dict(algo="crc8", poly=poly, init=init, reflected=refl, xorout=xo, start=start)
        for poly, init, refl, xo, endian in itertools.product(
                _CRC32_POLYS, [0, 0xFFFFFFFF], [True, False], [0, 0xFFFFFFFF], ["little", "big"]):
            yield dict(algo="crc32", poly=poly, init=init, reflected=refl, xorout=xo, start=start, endian=endian)
        for algo in ("fletcher8", "fletcher8_256", "sum16", "negsum16"):
            for endian in ("little", "big"):
                p = dict(algo=algo, start=start, endian=endian)
                if algo == "sum16":
                    p.update(init=0, xorout=0)
                yield p
        yield dict(algo="negsum8", start=start)
        for xo in (0, 0xFF):
            yield dict(algo="sum8", init=0, xorout=xo, start=start)
        for init in (0, 0xFF):
            yield dict(algo="xor8", init=init, xorout=0, start=start)


def _find_checksum(frames: list[bytes], trailer_len: int, sync_len: int, kb: KnowledgeBase) -> dict | None:
    sample = frames[:300]
    if len(sample) < 12:
        return None
    max_start = min(len(f) for f in sample) // 2
    for p in _checksum_candidates(sync_len, max_start, kb):
        if all(_check_ok(f, p, trailer_len) for f in sample[:12]) and \
                sum(_check_ok(f, p, trailer_len) for f in sample) >= 0.95 * len(sample):
            return p
    return None


def _trailer_len(frames: list[bytes]) -> int:
    """Number of constant bytes at the very end of (nearly) every frame."""
    t = 0
    while t < 4:
        col = collections.Counter(f[-(t + 1)] for f in frames if len(f) > t + 1)
        if not col or col.most_common(1)[0][1] < 0.99 * len(frames):
            break
        t += 1
    return t


# --------------------------------------------------------------------------- structure

@dataclass
class Frame:
    raw: bytes
    ordinal: int  # unwrapped sequence counter (or index among valid frames)


@dataclass
class Structure:
    sync: bytes
    payload_offset: int
    length_offset: int | None = None
    length_overhead: int | None = None
    fixed_length: int | None = None
    type_offset: int | None = None
    seq_offset: int | None = None
    trailer: bytes = b""
    crc: dict | None = None  # CrcSpec kwargs
    frames: list[Frame] = field(default_factory=list)
    report: list[str] = field(default_factory=list)

    @property
    def chk_size(self) -> int:
        return CHECKSUM_ALGOS[self.crc["algo"]] if self.crc else 0

    def payload(self, raw: bytes) -> bytes:
        return raw[self.payload_offset:len(raw) - len(self.trailer) - self.chk_size]

    def framing_dict(self) -> dict:
        d: dict = {"sync": self.sync.hex(), "payload_offset": self.payload_offset}
        if self.length_offset is not None:
            d["length_offset"], d["length_overhead"] = self.length_offset, self.length_overhead
        else:
            d["fixed_length"] = self.fixed_length
        if self.type_offset is not None:
            d["type_offset"] = self.type_offset
        if self.seq_offset is not None:
            d["seq_offset"] = self.seq_offset
        if self.trailer:
            d["trailer"] = self.trailer.hex()
        if self.crc:
            c = {"algo": self.crc["algo"], "start": self.crc["start"]}
            if self.crc["algo"] in ("crc8", "crc16", "crc32"):
                c.update(poly=self.crc["poly"], init=self.crc["init"],
                         reflected=self.crc["reflected"], xorout=self.crc["xorout"])
            elif self.crc["algo"] in ("sum8", "sum16", "negsum8", "negsum16", "xor8"):
                c.update(init=self.crc.get("init", 0), xorout=self.crc.get("xorout", 0))
            if self.chk_size > 1:
                c["endian"] = self.crc["endian"]
            d["crc"] = c
        return d


def _frame_ok(frame: bytes, st: Structure) -> bool:
    if len(frame) < st.payload_offset + st.chk_size + len(st.trailer):
        return False
    if st.trailer and not frame.endswith(st.trailer):
        return False
    return _check_ok(frame, st.crc, len(st.trailer)) if st.crc else True


def scan_frames(data: bytes, st: Structure) -> list[tuple[int, bytes]]:
    """Mirror of the generated connectors' iter_frames(): (offset, frame) pairs."""
    out, i, n = [], 0, len(data)
    while i < n:
        if st.sync:
            i = data.find(st.sync, i)
            if i < 0:
                break
        if st.length_offset is not None:
            if i + st.length_offset >= n:
                break
            end = i + data[i + st.length_offset] + st.length_overhead
        else:
            end = i + st.fixed_length
        if end <= n and _frame_ok(data[i:end], st):
            out.append((i, data[i:end]))
            i = end
        else:
            i += 1
    return out


def _with_frames(st: Structure, data: bytes) -> Structure:
    found = scan_frames(data, st)
    if st.seq_offset is not None and found:
        seqs = [f[st.seq_offset] for _, f in found]
        modulus = max(seqs) + 1
        base, prev, frames = 0, None, []
        for (_, raw), s in zip(found, seqs):
            if prev is not None and s < prev - modulus // 2:
                base += modulus
            prev = s
            frames.append(Frame(raw, base + s))
    else:
        frames = [Frame(raw, k) for k, (_, raw) in enumerate(found)]
    st.frames = frames
    return st


def _find_sync(data: bytes):
    """Most frequent 2-gram, extended while the following byte is constant.

    Overlapping hits (a run like AA AA AA matches AA AA twice) count once, at the start of the run."""
    c2 = collections.Counter(data[i:i + 2] for i in range(len(data) - 1))
    sync = c2.most_common(1)[0][0]

    def starts(positions: list[int]) -> list[int]:
        have = set(positions)
        return [i for i in positions if i - 1 not in have]  # first hit of each overlapping chain

    pos = starts([i for i in range(len(data) - len(sync) + 1) if data[i:i + len(sync)] == sync])
    while len(sync) < 8:
        nxt = collections.Counter(data[i + len(sync)] for i in pos if i + len(sync) < len(data))
        byte, cnt = nxt.most_common(1)[0]
        if cnt < 0.95 * len(pos):
            break
        sync += bytes([byte])
        pos = starts([i for i in pos if data[i:i + len(sync)] == sync])
    return sync, pos


def _autocorr_period(data: bytes) -> int:
    """Frame size of a constant-size stream with no sync bytes: where bytes repeat."""
    sample = data[:20000]
    scores = {}
    for L in range(4, min(129, len(sample) // 3)):
        n = len(sample) - L
        scores[L] = sum(sample[i] == sample[i + L] for i in range(n)) / n
    if not scores:
        raise InferenceError("capture too short to find a frame period; try `identify` with separate frames")
    best = max(scores.values())
    for L in sorted(scores):  # smallest period within 90% of the best (not a multiple of it)
        if scores[L] >= 0.9 * best:
            return L
    raise InferenceError("no periodic structure found")


def _detect_header(frames: list[bytes], sync_len: int, skip: set[int],
                   interleaved: bool = False) -> tuple[int | None, int | None]:
    """(seq_offset, type_offset) among the first few bytes after the sync.

    With constant-size frames nothing but the bytes themselves distinguishes a message-type
    byte from a slowly drifting sensor value, so there (`interleaved`) a type byte must switch
    value between consecutive frames often, as interleaved message types do."""
    hdr = [k for k in range(sync_len, sync_len + 6) if k not in skip and all(len(f) > k for f in frames)]
    seq = typ = None
    for k in hdr:
        vals = [f[k] for f in frames]
        if len(set(vals)) >= 8 and sum(b == a + 1 for a, b in zip(vals, vals[1:])) >= 0.85 * (len(vals) - 1):
            seq = k
            break
    for k in hdr:
        vals = [f[k] for f in frames]
        if k != seq and 2 <= len(set(vals)) <= 16:
            if interleaved and sum(a != b for a, b in zip(vals, vals[1:])) < 0.2 * (len(vals) - 1):
                continue
            typ = k
            break
    return seq, typ


def _finish_header(st: Structure, good: list[bytes], skip: set[int], rep: list[str],
                   interleaved: bool = False) -> None:
    st.seq_offset, st.type_offset = _detect_header(good, len(st.sync), skip, interleaved)
    offs = [o for o in (st.length_offset, st.type_offset, st.seq_offset) if o is not None]
    st.payload_offset = max(offs) + 1 if offs else len(st.sync)
    rep.append(
        f"type byte: {'at ' + str(st.type_offset) if st.type_offset is not None else 'none (single message type)'}; "
        f"seq counter: {'at ' + str(st.seq_offset) if st.seq_offset is not None else 'none'}; "
        f"payload starts at byte {st.payload_offset}")


def _report_checksum(rep: list[str], p: dict | None, trailer_len: int, origin: str = "") -> None:
    if trailer_len:
        rep.append(f"constant trailer of {trailer_len} byte(s) after the check value")
    if p is None:
        rep.append("WARNING: no checksum matched; frames are validated by length only")
        return
    detail = ""
    if p["algo"] in ("crc8", "crc16", "crc32"):
        w = CHECKSUM_ALGOS[p["algo"]] * 2 + 2
        detail = (f" poly={p['poly']:#0{w}x} init={p['init']:#0{w}x} reflected={p['reflected']}"
                  f" xorout={p['xorout']:#0{w}x}")
    endian = f", stored {p['endian']}-endian" if CHECKSUM_ALGOS[p["algo"]] > 1 else ""
    rep.append(f"checksum {p['algo']}{detail} over bytes[{p['start']}:check]{endian}{origin}")


def _try_known_framing(data: bytes, fd: dict, rep: list[str], n_protocols: int) -> Structure | None:
    c = fd.get("crc")
    st = Structure(
        sync=bytes.fromhex(fd.get("sync", "")), payload_offset=fd["payload_offset"],
        length_offset=fd.get("length_offset"), length_overhead=fd.get("length_overhead"),
        fixed_length=fd.get("fixed_length"), type_offset=fd.get("type_offset"),
        seq_offset=fd.get("seq_offset"), trailer=bytes.fromhex(fd.get("trailer", "")),
        crc=dict(c, endian=c.get("endian", "little")) if c else None)
    found = scan_frames(data, st)
    if len(found) >= 20 and sum(len(f) for _, f in found) >= 0.9 * len(data):
        rep.append(f"capture matches a framing from the knowledge base (known from {n_protocols} protocol(s)); "
                   "heuristics skipped")
        _report_checksum(rep, st.crc, len(st.trailer), " [learned]")
        return _with_frames(st, data)
    return None


def infer_structure(data: bytes, kb: KnowledgeBase | None = None) -> Structure:
    kb = kb or KnowledgeBase()
    rep: list[str] = []
    if len(data) < 64:
        raise InferenceError("capture too short to infer anything")

    for n_protocols, fd in kb.learned_framings():
        st = _try_known_framing(data, fd, rep, n_protocols)
        if st:
            st.report = rep + [f"{len(st.frames)} valid frames"]
            return st

    sync, pos = _find_sync(data)
    gaps = collections.Counter(b - a for a, b in zip(pos, pos[1:]))
    n_gaps = sum(gaps.values())
    regular = len(pos) >= 20 and sum(c for _, c in gaps.most_common(12)) >= 0.85 * n_gaps
    top_gap, top_cnt = gaps.most_common(1)[0] if gaps else (0, 0)

    if regular and top_cnt < 0.9 * n_gaps:
        st = _variable_length(data, sync, pos, kb, rep)
    else:
        L = top_gap if regular else _autocorr_period(data)
        rep.append(f"constant frame size {L} bytes" + ("" if regular else " (found by autocorrelation; no sync bytes)"))
        st = _constant_length(data, L, kb, rep)
    st = _with_frames(st, data)
    rep.append(f"{len(st.frames)} valid frames")
    st.report = rep
    return st


def _variable_length(data: bytes, sync: bytes, pos: list[int], kb: KnowledgeBase, rep: list[str]) -> Structure:
    rep.append(f"sync {sync.hex(' ')} (seen {len(pos)}x)")
    bounds = pos + [len(data)]  # include the final frame (it has no following sync)
    chunks = [data[a:b] for a, b in zip(bounds, bounds[1:]) if b - a >= len(sync) + 3]
    if len(chunks) < 20:
        raise InferenceError("too few frames to infer structure")

    length_offset = overhead = None
    for k in range(len(sync), len(sync) + 6):
        c = collections.Counter(len(ch) - ch[k] for ch in chunks if len(ch) > k)
        val, cnt = c.most_common(1)[0]
        if cnt >= 0.9 * len(chunks):
            length_offset, overhead = k, val
            break
    if length_offset is None:
        raise InferenceError("no length field found")
    rep.append(f"length field at byte {length_offset}; frame length = byte + {overhead}")

    good = [ch[:ch[length_offset] + overhead] for ch in chunks
            if len(ch) > length_offset and len(ch) >= ch[length_offset] + overhead]
    t = _trailer_len(good)
    crc = _find_checksum([g for g in good], t, len(sync), kb)
    st = Structure(sync=sync, payload_offset=0, length_offset=length_offset, length_overhead=overhead,
                   trailer=good[0][len(good[0]) - t:] if t else b"", crc=crc)
    _report_checksum(rep, crc, t)
    valid = [g for g in good if crc is None or (g.endswith(st.trailer) and _check_ok(g, crc, t))]
    _finish_header(st, valid or good, {length_offset}, rep)
    rep.append(f"{len(chunks) - len(valid)} candidate frame(s) rejected (corrupt / noise)")
    return st


def _constant_length(data: bytes, L: int, kb: KnowledgeBase, rep: list[str]) -> Structure:
    best = None
    for r in range(L):
        chunks = [data[i:i + L] for i in range(r, len(data) - L + 1, L)]
        if len(chunks) < 20:
            continue
        t = _trailer_len(chunks)
        crc = _find_checksum(chunks, t, 0, kb)
        if crc:
            best = (r, chunks, t, crc)
            break
    if not best:
        raise InferenceError(
            f"constant-size frames of {L} bytes found, but no checksum variant validates them at any alignment "
            "(teach the tool this device with `reverse learn`)")
    r, chunks, t, crc = best
    rep.append(f"frame alignment: offset {r} in the capture")
    ok = [c for c in chunks if _check_ok(c, crc, t)]
    # sync = leading bytes that are constant in (nearly) every valid frame
    n_sync = 0
    while n_sync < 4 and collections.Counter(c[n_sync] for c in ok).most_common(1)[0][1] >= 0.99 * len(ok):
        n_sync += 1
    sync = ok[0][:n_sync]
    rep.append(f"sync bytes: {sync.hex(' ') if sync else 'none'}")
    st = Structure(sync=sync, payload_offset=0, fixed_length=L,
                   trailer=ok[0][L - t:] if t else b"", crc=crc)
    _report_checksum(rep, crc, t)
    _finish_header(st, ok, set(), rep, interleaved=True)
    return st


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
    prior: int = 0
    prior_decided: bool = False
    confirmed: bool = True  # False when another encoding at the same offset/scale fits just as well


def fit_field(payloads: list[bytes], ys: list[float], plen: int, name: str, unit: str | None,
              kb: KnowledgeBase | None = None) -> FieldFit:
    kb = kb or KnowledgeBase()
    if len(set(ys)) < 2:
        raise InferenceError(
            f"the anchors for field {name!r} all have the same value ({ys[0]:g}); "
            "at least two different readings are needed to determine scale")
    rng = max(ys) - min(ys)
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
                found.append(dict(nb=b != 0.0, rnd=round(_roundness(a), 6), w=w,
                                  pref=_TYPE_PREFERENCE.index(ftype), off=off, ftype=ftype, a=a, b=b, res=res,
                                  prior=kb.prior(unit, ftype, a, b)))
    if not found:
        raise InferenceError(f"no payload encoding fits the anchors for field {name!r}")
    plain = sorted(found, key=lambda c: (c["nb"], c["rnd"], c["w"], c["pref"], c["off"]))
    p0 = plain[0]
    # Representation ambiguity: the same bytes, offset and scale read as another width / signedness.
    # Only that group may be reordered by prior evidence - never a different offset or scale.
    group = [c for c in plain if c["off"] == p0["off"] and c["a"] == p0["a"] and c["b"] == p0["b"]]
    best = sorted(group, key=lambda c: -c["prior"])[0]  # stable: ties keep the plain order
    return FieldFit(name, best["ftype"], best["off"], best["a"], best["b"], unit, best["res"],
                    len(found) - 1, best["prior"], best is not p0, confirmed=len(group) == 1)


def infer_specs(data: bytes, anchors: list[dict], protocol: str,
                kb: KnowledgeBase | None = None, learn: bool = True) -> tuple[Structure, dict[str, dict], list[str]]:
    """Infer one spec per anchored message. With `learn`, a validated run is recorded in `kb`
    (in memory; the caller decides whether to save it)."""
    kb = kb or KnowledgeBase()
    st = infer_structure(data, kb)
    rep = list(st.report)
    by_ord = {f.ordinal: f for f in st.frames}

    by_msg: dict[str, list[dict]] = collections.defaultdict(list)
    for a in anchors:
        by_msg[a["message"]].append(a)
    if st.type_offset is None and len(by_msg) > 1:
        raise InferenceError("the frames carry no message-type byte, but anchors name several messages: "
                             f"{sorted(by_msg)}")

    specs: dict[str, dict] = {}
    all_fits: list[FieldFit] = []
    for msg, items in by_msg.items():
        type_value = None
        if st.type_offset is not None:
            types = set()
            for a in items:
                fr = by_ord.get(int(a["ordinal"]))
                if fr is None:
                    raise InferenceError(f"anchor ordinal {a['ordinal']} for {msg}.{a['field']} is not a valid frame")
                types.add(fr.raw[st.type_offset])
            if len(types) != 1:
                raise InferenceError(f"anchors for message {msg!r} point at several frame types: {sorted(types)}")
            type_value = types.pop()
            same = [f for f in st.frames if f.raw[st.type_offset] == type_value]
        else:
            same = st.frames
        lens = collections.Counter(len(st.payload(f.raw)) for f in same)
        plen = lens.most_common(1)[0][0]
        rep.append(f"message {msg!r}: " + (f"type {type_value:#04x}, " if type_value is not None else "")
                   + f"{len(same)} frames, payload {plen} bytes"
                   + (f" (also seen: {dict(lens)})" if len(lens) > 1 else ""))

        fields: list[FieldFit] = []
        for fname in dict.fromkeys(a["field"] for a in items):
            rows = [a for a in items if a["field"] == fname]
            payloads = []
            for a in rows:
                fr = by_ord.get(int(a["ordinal"]))
                if fr is None:
                    raise InferenceError(f"anchor ordinal {a['ordinal']} for {msg}.{fname} is not a valid frame")
                payloads.append(st.payload(fr.raw))
            ff = fit_field(payloads, [float(a["value"]) for a in rows], plen, fname, rows[0].get("unit"), kb)
            fields.append(ff)
            all_fits.append(ff)
            amb = f"  [{ff.alternatives} lower-ranked encodings also within tolerance]" if ff.alternatives else ""
            prior = (f"  [prior from {ff.prior} protocol(s) decided between equally good fits]"
                     if ff.prior_decided else "")
            bias = f" bias={ff.bias:g}" if ff.bias else ""
            rep.append(f"  {fname}: payload[{ff.offset}] {ff.type} scale={ff.scale:g}{bias}"
                       f"  anchor residual {ff.residual:.2g}{amb}{prior}")

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
        message = {"id": msg, "description": f"Inferred from a capture ({len(same)} frames)",
                   "payload_length": plen}
        if type_value is not None:
            message["type_value"] = type_value
        specs[msg] = {
            "protocol": protocol, "transport": "framed_binary", "framing": st.framing_dict(),
            "message": message, "fields": fdicts,
            "canonical": {"measurements": [
                {"name": f.name, "field": f.name, **({"unit": f.unit} if f.unit else {})} for f in fields]},
        }

    if st.type_offset is not None:
        seen = {f.raw[st.type_offset] for f in st.frames}
        anchored = {s["message"]["type_value"] for s in specs.values()}
        for t in sorted(seen - anchored):
            n = sum(1 for f in st.frames if f.raw[st.type_offset] == t)
            rep.append(f"type {t:#04x}: {n} frames, no anchors given -> no spec emitted")

    # Learn only from validated runs: a checksum matched the real frames, and every anchor fitted.
    if learn and st.crc is not None and all_fits:
        # Only confirmed encodings are recorded: a guess (ambiguous fit, tie broken by preference or
        # by an earlier prior) must never come back later as "evidence".
        kb.record_run(st.framing_dict(),
                      [(f.unit, f.type, f.scale, f.bias) for f in all_fits if f.confirmed], protocol)
        rep.append(f"knowledge base updated: {kb.summary()}")
    elif learn:
        rep.append("knowledge base not updated: run was not fully validated (no checksum matched)")
    return st, specs, rep


def learn_from_spec(kb: KnowledgeBase, spec, data: bytes) -> str:
    """Teach the knowledge base from a verified spec: it must actually explain the capture.

    The spec's generated connector has to find well-formed frames covering >= 90% of the
    capture; only then are its framing, checksum and field encodings recorded.
    """
    from generator.codegen import generate

    ns: dict = {}
    exec(compile(generate(spec), f"<{spec.protocol}>", "exec"), ns)  # noqa: S102 - our own generated code
    frames = list(ns["iter_frames"](data))
    covered = sum(len(f) for f in frames) / max(1, len(data))
    if len(frames) < 20 or covered < 0.9:
        raise InferenceError(
            f"spec does not explain the capture: {len(frames)} frames, {covered:.0%} of bytes covered "
            "(need >= 20 frames and >= 90%); nothing was learned")
    fr = spec.framing
    fd: dict = {"sync": fr.sync, "payload_offset": fr.payload_offset}
    if fr.length_offset is not None:
        fd["length_offset"], fd["length_overhead"] = fr.length_offset, fr.length_overhead
    else:
        fd["fixed_length"] = fr.fixed_length
    for k in ("type_offset", "seq_offset"):
        if getattr(fr, k) is not None:
            fd[k] = getattr(fr, k)
    if fr.trailer:
        fd["trailer"] = fr.trailer
    if fr.crc:
        c = fr.crc
        cd: dict = {"algo": c.algo, "start": c.start}
        if c.algo in ("crc8", "crc16", "crc32"):
            cd.update(poly=c.poly, init=c.init, reflected=c.reflected, xorout=c.xorout)
        elif c.algo in ("sum8", "sum16", "negsum8", "negsum16", "xor8"):
            cd.update(init=c.init, xorout=c.xorout)
        if CHECKSUM_ALGOS[c.algo] > 1:
            cd["endian"] = c.endian
        fd["crc"] = cd
    kb.record_run(fd, [(f.unit, f.type, f.scale, f.bias) for f in spec.fields], spec.protocol)
    return (f"learned {spec.protocol}/{spec.message.id}: {len(frames)} frames explained "
            f"({covered:.0%} of the capture)")
