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


def identify(frames: list[bytes], kb: KnowledgeBase | None = None, max_results: int = 5) -> dict:
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
    bits = 8 * (CHECKSUM_ALGOS[uniq[0][1]["algo"]] if uniq else 1)
    return {
        "frames": len(frames),
        "sync": frames[0][:sync_len].hex(" ") if sync_len and len(frames) > 1 else "",
        "matches": [{"trailer_len": t, **p} for t, p in uniq[:max_results]],
        "n_candidates_validating": len(uniq),
        # chance that a random rule validates all frames ~ candidates_tried * 2^(-bits*frames)
        "confidence": (lambda n: "high" if n * bits >= 40 else "medium" if n * bits >= 20 else "low")(len(frames) - (1 if solved else 0)),
        "constant_solved_from_data": solved,
        "stuffing": stuffing["scheme"] if stuffing else None,
    }


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
    ok = result["confidence"] == "high" or (result["confidence"] == "medium" and result["frames"] >= 3)
    if not ok or len(result["matches"]) != 1:
        return f"not recorded: confidence {result['confidence']}, {len(result['matches'])} candidate rule(s) on {result['frames']} frame(s)"
    kb.record_checksum({k: v for k, v in result["matches"][0].items() if k != "trailer_len"}, protocol)
    return f"recorded {result['matches'][0]['algo']} for {protocol}"
