"""Few-frame identification: find sync, trailer and check-value rule from a handful of frames.

Datasheets and forum threads publish 1-20 frames, not a 10 000-frame capture. This is the
entry point for that case. It does not need constant frame sizes: frames are given separately
(one per line) or cut from a stream at a sync byte.

Every candidate that validates ALL frames is returned, simplest first, together with how
many independent frames back it. One frame validates an 8-bit rule by luck about 1 time in
256 per candidate tried, so `confidence` says so instead of pretending.
"""

from __future__ import annotations

import collections
import itertools
import math

from reverse.infer import _check_ok, _checksum_candidates
from reverse.knowledge import KnowledgeBase
from generator.spec import CHECKSUM_ALGOS


def split_stream(data: bytes, max_sync: int = 4) -> list[bytes]:
    """Cut a stream into frames at the most common leading byte pattern (mixed lengths allowed)."""
    first = collections.Counter(data[i] for i in range(len(data)))
    for byte, _ in first.most_common(6):
        pos = [i for i in range(len(data)) if data[i] == byte]
        if len(pos) >= 3 and pos[0] == 0:
            frames = [data[a:b] for a, b in zip(pos, pos[1:] + [len(data)])]
            if sum(len(f) >= 4 for f in frames) >= 0.9 * len(frames):
                return [f for f in frames if len(f) >= 4]
    return [data]


def _common_prefix(frames: list[bytes], cap: int) -> int:
    n = 0
    while n < cap and all(len(f) > n for f in frames) and len({f[n] for f in frames}) == 1:
        n += 1
    return n


def identify(frames: list[bytes], kb: KnowledgeBase | None = None, max_results: int = 5, _depth: int = 0) -> dict:
    kb = kb or KnowledgeBase()
    frames = [f for f in frames if f]
    if not frames:
        raise ValueError("no frames")
    from reverse.stuffing import detect as _detect_stuffing, unstuff as _unstuff
    stuffing = _detect_stuffing(frames)
    if stuffing:
        frames = [f for f in _unstuff(frames, stuffing) if f]
        if not frames:
            raise ValueError("no frames after unstuffing")
    sync_len = _common_prefix(frames, 4) if len(frames) > 1 else 1
    min_len = min(len(f) for f in frames)
    hits = []
    seen = set()
    for trailer_len in range(0, 3):
        # a trailer must be constant across frames (needs >1 frame to claim; with one we only try 0/1)
        if trailer_len and len(frames) > 1 and len({f[-trailer_len:] for f in frames}) != 1:
            continue
        if trailer_len and len(frames) == 1:
            continue  # one frame cannot show a byte is constant
        for p in _checksum_candidates(sync_len, max(0, min_len // 2), kb):
            if CHECKSUM_ALGOS[p["algo"]] + p["start"] + trailer_len >= min_len:
                continue
            if all(_check_ok(f, p, trailer_len) for f in frames):
                key = (trailer_len, tuple(sorted(p.items())))
                if key not in seen:
                    seen.add(key)
                    hits.append((trailer_len, p))
    # "constant minus sum" rules (Victron VE.Direct HEX: 0x55 - sum): the constant is not enumerated,
    # it is solved from the first frame and then has to hold on every other one. It costs one
    # free parameter, so confidence below is computed from frames - 1.
    solved = False
    if not hits and len(frames) > 1:
        for trailer_len in (0, 1):
            for start in range(0, min(min_len // 2, 8)):
                for algo, size in (("negsum8", 1), ("negsum16", 2)):
                    f0 = frames[0]; end = len(f0) - trailer_len - size
                    if end <= start:
                        continue
                    mask = (1 << (8 * size)) - 1
                    init = (int.from_bytes(f0[end:end + size], "big") + sum(f0[start:end])) & mask
                    p = dict(algo=algo, start=start, init=init, xorout=0)
                    if size == 2:
                        p["endian"] = "big"
                    if all(_check_ok(f, p, trailer_len) for f in frames):
                        hits.append((trailer_len, p)); solved = True
    # Full CRC-8 polynomial sweep: only when nothing else fits and there are enough frames to pay for the
    # extra search (255 polys x 8 parameter sets x starts of chance matches, about 11 bits).
    swept = False
    if not hits and len(frames) >= 3:
        for start in range(0, min(sync_len + 3, min_len // 2)):
            for poly, init, refl, xo in itertools.product(range(1, 256), (0, 0xFF), (True, False), (0, 0xFF)):
                p = dict(algo="crc8", poly=poly, init=init, reflected=refl, xorout=xo, start=start)
                if min_len > start + 1 and all(_check_ok(f, p, 0) for f in frames):
                    hits.append((0, p)); swept = True
    # Tolerant pass: real captures hold corrupted frames. Only when nothing fits all of them, keep the
    # rules that fit the most frames, if that is at least 70% and at least 4 frames. Confidence is capped
    # at medium and the outliers are reported so a person can look at them.
    partial = None
    if not hits and len(frames) >= 5:
        need = max(4, -(-7 * len(frames) // 10))
        best, found = 0, []
        for trailer_len in range(0, 3):
            if trailer_len and len({f[-trailer_len:] for f in frames}) != 1:
                continue
            for p in _checksum_candidates(sync_len, max(0, min_len // 2), kb):
                if CHECKSUM_ALGOS[p["algo"]] + p["start"] + trailer_len >= min_len:
                    continue
                good = [i for i, f in enumerate(frames) if _check_ok(f, p, trailer_len)]
                if len(good) >= need and len(good) >= best:
                    if len(good) > best:
                        best, found = len(good), []
                    found.append((trailer_len, p, good))
        if found:
            for t, p, good in found:
                hits.append((t, p))
            partial = {"validated": best, "of": len(frames),
                       "outliers": [i for i in range(len(frames)) if i not in found[0][2]]}
    # Seeded CRC-16 (a hidden constant byte, MAVLink style). Only worth trying when the plain search found
    # nothing, or so little evidence that its hits could be chance.
    seeded = []
    if (not hits or len(frames) < 3) and len(frames) >= 1:
        seeded = find_seeded_crc16(frames, sync_len, kb)
    # simplest first: fewer trailer bytes, then plain families before CRC parameter soup
    order = {"sum8": 0, "negsum8": 1, "xor8": 2, "sum16": 3, "negsum16": 4,
             "fletcher8": 5, "fletcher8_256": 6, "crc8": 7, "crc16": 8, "crc32": 9}
    hits.sort(key=lambda h: (order[h[1]["algo"]], h[0], h[1]["start"]))
    # collapse equivalents: same algo+trailer, different start only because bytes before are constant
    uniq, sigs = [], set()
    for t, p in hits:
        sig = (p["algo"], t, p.get("xorout", 0), p.get("endian"), p.get("poly"), p.get("init"), p.get("reflected"))
        if sig in sigs:
            continue
        sigs.add(sig)
        uniq.append((t, p))
    if len(uniq) > 1:   # several rules fit: ones validated on other protocols first (stable, so no reshuffle otherwise)
        uniq.sort(key=lambda h: -kb.support({k: v for k, v in h[1].items()}))
    # Nested check (CRSF extended frames: a CRC-8 sits inside the data a second CRC-8 covers): when the
    # winning rule is a 1-byte check with no trailer, take it off and look again. One level only.
    nested = None
    if (_depth == 0 and uniq and uniq[0][0] == 0 and CHECKSUM_ALGOS[uniq[0][1]["algo"]] == 1
            and not partial and all(len(f) > 4 for f in frames)):
        inner = identify([f[:-1] for f in frames], kb, max_results=3, _depth=1)
        if inner["matches"] and not inner.get("seeded_crc16"):
            nested = {"matches": inner["matches"], "confidence": inner["confidence"],
                      "frames": inner["frames"], "poly_sweep": inner["poly_sweep"]}
    # Mid-frame header checks, only if the end of the frame did not give a solid answer.
    header_checks = []
    if _depth == 0 and not (uniq and len(frames) >= 3 and not partial):
        header_checks = find_header_checks(frames, sync_len, kb)
    bits = 8 * (CHECKSUM_ALGOS[uniq[0][1]["algo"]] if uniq else 1)
    if partial:
        bits = min(bits, 8 * 2)
    return {
        "frames": len(frames),
        "sync": frames[0][:sync_len].hex(" ") if sync_len and len(frames) > 1 else "",
        "matches": [{"trailer_len": t, **p} for t, p in uniq[:max_results]],
        "n_candidates_validating": len(uniq),
        # chance that a random rule validates all frames ~ candidates_tried * 2^(-bits*frames)
        "confidence": (lambda c: "medium" if partial and c == "high" else c)(
            (lambda n: "high" if n * bits - (11 if swept else 0) >= 40 else
                       "medium" if n * bits - (11 if swept else 0) >= 20 else "low")(
                partial["validated"] if partial else len(frames) - (1 if solved else 0))),
        "constant_solved_from_data": solved,
        "partial": partial,
        "nested": nested,
        "poly_sweep": swept,
        "header_checks": header_checks,
        "padded_check": _padded_check(frames, kb, _depth),
        "ascii_hex_checks": find_ascii_hex_checks(frames) if _depth == 0 else [],
        "seeded_crc16": seeded[:max_results],
        "seeded_confidence": (lambda b: "high" if b >= 40 else "medium" if b >= 20 else "low")(
            16 * len(frames) - 8 * max((s["groups"] for s in seeded), default=1)) if seeded else None,
        "stuffing": stuffing["scheme"] if stuffing else None,
    }


def find_seeded_crc16(frames: list[bytes], sync_len: int, kb: KnowledgeBase) -> list[dict]:
    """CRC-16 with a hidden constant byte appended to the data before the check (MAVLink's CRC_EXTRA).

    The byte is not enumerated: for every CRC-16 parameter set it is solved per frame (one extra CRC step
    for each of the 256 values) and has to agree across frames. If frames of several message types share
    the check, they are grouped by one header byte and each group gets its own solved byte; a group only
    counts when it holds at least two frames, otherwise the byte would be fitted to a single frame."""
    from reverse.infer import _BUILTIN_POLYS, _crc_generic
    min_len = min(len(f) for f in frames)
    polys = list(dict.fromkeys(_BUILTIN_POLYS + kb.learned_polys()))
    out = []
    for start in range(0, min(sync_len + 3, min_len // 2)):
        for poly, init, refl, xo, endian in itertools.product(polys, [0, 0xFFFF], [True, False], [0, 0xFFFF], ["little", "big"]):
            per_frame = []
            for f in frames:
                end = len(f) - 2
                if end <= start:
                    break
                stored = int.from_bytes(f[end:], endian)
                state = _crc_generic(f[start:end], 16, poly, init, refl, 0)
                per_frame.append({c for c in range(256)
                                  if _crc_generic(bytes([c]), 16, poly, state, refl, xo) == stored})
            else:
                if not all(per_frame):
                    continue
                base = dict(algo="crc16", poly=poly, init=init, reflected=refl, xorout=xo, start=start, endian=endian)
                common = set.intersection(*per_frame)
                if common:
                    out.append({**base, "append": sorted(common)[:4], "append_by": None, "groups": 1})
                    continue
                for g in range(0, min_len - 2):
                    groups: dict[int, list[int]] = {}
                    for i, f in enumerate(frames):
                        groups.setdefault(f[g], []).append(i)
                    if len(groups) < 2 or any(len(ix) < 2 for ix in groups.values()):
                        continue
                    solved = {k: set.intersection(*(per_frame[i] for i in ix)) for k, ix in groups.items()}
                    if all(solved.values()):
                        out.append({**base, "append": {f"{k:02x}": sorted(v)[0] for k, v in sorted(solved.items())},
                                    "append_by": g, "groups": len(groups)})
                        break
    return out


def find_header_checks(frames: list[bytes], sync_len: int, kb: KnowledgeBase, max_len: int = 14) -> list[dict]:
    """A check that sits mid-frame, after a fixed-size header and before the payload (BACnet MS/TP: header CRC-8,
    DNP3 link header CRC). Each prefix length L is searched as if the first L bytes were a frame of their own
    with a trailing check. The many prefix lengths tried are paid for in the confidence: bits = frames x check
    bits minus log2(tries)."""
    out = []
    min_len = min(len(f) for f in frames)
    if len(frames) < 2:
        return out
    tries = 0
    for L in range(3, min(max_len, min_len) + 1):
        sub = [f[:L] for f in frames]
        for p in _checksum_candidates(sync_len, max(0, L // 2), kb):
            size = CHECKSUM_ALGOS[p["algo"]]
            if size + p["start"] >= L:
                continue
            tries += 1
            if all(_check_ok(f, p, 0) for f in sub):
                out.append({**p, "check_offset": L - size, "header_len": L})
    for h in out:
        bits = len(frames) * 8 * CHECKSUM_ALGOS[h["algo"]] - math.log2(max(tries, 2))
        h["confidence"] = "high" if bits >= 40 else "medium" if bits >= 20 else "low"
    out.sort(key=lambda h: ({"high": 0, "medium": 1, "low": 2}[h["confidence"]], h["header_len"]))
    return out[:8]


_ASCII_OPS = {"sum": lambda s, m: s & m, "negsum": lambda s, m: (-s) & m, "invsum": lambda s, m: (~s) & m}


def find_ascii_hex_checks(frames: list[bytes]) -> list[dict]:
    """Text frames whose checksum is written as 2 or 4 hex ASCII characters (before an optional CR/LF) and is
    computed over the ASCII characters themselves, not over the decoded bytes (Pylontech, Seplos, PACE
    YD/T 1363 style: '~' + hex text + 4-digit negated 16-bit sum + CR). Every start offset 0-2, width, and
    operation (sum, negated sum, inverted sum) is tried; all frames must agree. Confidence prices the tries."""
    if not frames or not all(len(f) > 6 and all(32 <= c < 127 or c in (10, 13) for c in f) for f in frames):
        return []
    out, tries = [], 0
    for w in (2, 4):
        for start in range(3):
            for op, fn in _ASCII_OPS.items():
                tries += 1
                ok = True
                for f in frames:
                    t = f.rstrip(b"\r\n")
                    tail = t[-w:]
                    if len(t) - w <= start or not all(chr(c) in "0123456789ABCDEFabcdef" for c in tail):
                        ok = False
                        break
                    mask = (1 << (4 * w)) - 1
                    if fn(sum(t[start:-w]), mask) != int(tail, 16):
                        ok = False
                        break
                if ok:
                    out.append({"algo": "ascii_hex_" + op, "width_chars": w, "start": start})
    for h in out:
        bits = len(frames) * 4 * h["width_chars"] - math.log2(max(tries, 2))
        h["confidence"] = "high" if bits >= 40 else "medium" if bits >= 20 else "low"
    return out


def _padded_check(frames: list[bytes], kb, depth: int) -> dict | None:
    """A check stored zero-extended in a wider field (Teltonika: CRC-16 in a 4-byte slot, 00 00 + CRC). When every
    frame has 2 zero bytes just before its last 2 bytes, those pad bytes are removed and the search is run again."""
    if depth or len(frames) < 2 or any(len(f) < 8 or f[-4:-2] != b"\x00\x00" for f in frames):
        return None
    stripped = [f[:-4] + f[-2:] for f in frames]
    r = identify(stripped, kb, max_results=3, _depth=1)
    if not r["matches"]:
        return None
    return {"pad_bytes": 2, "pad_at": "before last 2 bytes", "matches": r["matches"], "confidence": r["confidence"]}


def detect_block_crc(frames: list[bytes], header_len: int, rule: dict, sizes=(8, 16, 32, 64, 128, 255)) -> int | None:
    """Does the header's check rule repeat over the body in fixed-size blocks (DNP3: 16 data bytes + 2 CRC)?

    The body of each frame is cut into blocks of `size` data bytes followed by the 2-byte check; the last block
    may be shorter. Returns the data-block size that makes every block of every frame validate, else None.
    Needs at least one frame long enough to span two blocks, otherwise any size would 'fit'."""
    from reverse.infer import checksum
    key = {k: v for k, v in rule.items() if k in ("algo", "poly", "init", "reflected", "xorout")}
    endian = rule.get("endian", "little")

    def good(body: bytes, size: int) -> bool:
        pos = 0
        while pos < len(body):
            n = min(size, len(body) - pos - 2)
            if n <= 0:
                return False
            if checksum(body[pos:pos + n], **key) != int.from_bytes(body[pos + n:pos + n + 2], endian):
                return False
            pos += n + 2
        return True

    bodies = [f[header_len:] for f in frames if len(f) > header_len + 2]
    if not bodies or not any(len(b) > 2 + min(sizes) + 2 for b in bodies):
        return None
    ok = [s for s in sizes if all(good(b, s) for b in bodies)]
    return ok[0] if ok else None


def find_length_rule(frames: list[bytes], max_offset: int = 5) -> list[dict]:
    """Length-field search that works with a handful of frames and with unchecked protocols.

    A rule is (offset, mask, overhead): len(frame) == (frame[offset] & mask) + overhead for every frame.
    mask 0xFF is a plain length byte; 0x0F is SeaTalk's low-nibble length. Only rules that fit frames of at
    least two distinct lengths are returned (a fixed-size set fits any rule with the right constant)."""
    if len({len(f) for f in frames}) < 2:
        return []
    hits = []
    for k in range(max_offset + 1):
        if not all(len(f) > k for f in frames):
            continue
        for mask in (0xFF, 0x0F, 0x1F, 0x3F, 0x7F):
            diffs = {len(f) - (f[k] & mask) for f in frames}
            if len(diffs) == 1 and len({f[k] & mask for f in frames}) >= 2:
                hits.append({"offset": k, "mask": mask, "overhead": diffs.pop(),
                             "distinct_lengths": len({len(f) for f in frames})})
    # plain byte before masked variants; a wider mask explains the data with fewer assumptions
    hits.sort(key=lambda h: (-h["mask"], h["offset"]))
    return hits


def learn_from_identification(kb: KnowledgeBase, result: dict, protocol: str) -> str:
    """Record a rule only when it is well supported: high confidence, or medium with three or more frames.

    One or two frames can match a rule by luck, and a rule recorded from luck would then be preferred next time -
    the same self-reinforcement trap the capture-based learning guards against."""
    if not result["matches"]:
        return "nothing to learn: no rule validated"
    if result.get("partial") or result.get("seeded_crc16") and not result["matches"]:
        return "not recorded: no rule fit every frame, so this identification was not validated"
    ok = result["confidence"] == "high" or (result["confidence"] == "medium" and result["frames"] >= 3)
    if not ok or len(result["matches"]) != 1:
        return f"not recorded: confidence {result['confidence']}, {len(result['matches'])} candidate rule(s) on {result['frames']} frame(s)"
    kb.record_checksum({k: v for k, v in result["matches"][0].items() if k != "trailer_len"}, protocol)
    return f"recorded {result['matches'][0]['algo']} for {protocol}"
