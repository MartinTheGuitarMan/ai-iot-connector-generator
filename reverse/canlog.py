"""candump -L logs: 29-bit id + up to 8 data bytes. Finds multi-frame ('fast packet') streams without being told.

Signature searched for, per CAN id: the first data byte splits into a 3-bit sequence counter (constant across
the frames of one message) and a 5-bit frame index that runs 0,1,2,... ; the second byte of frame 0 is the total
payload length, and the number of frames matches it (6 payload bytes in frame 0, 7 in each later frame)."""

from __future__ import annotations

import collections
import re

_LINE = re.compile(r"\(([\d.]+)\)\s+(\S+)\s+([0-9A-Fa-f]+)#([0-9A-Fa-f]*)")


def parse_candump(text: str) -> list[tuple[float, int, bytes]]:
    out = []
    for m in _LINE.finditer(text):
        out.append((float(m.group(1)), int(m.group(3), 16), bytes.fromhex(m.group(4))))
    return out


def pgn_of(can_id: int) -> int:
    """PGN from a 29-bit id (J1939 / NMEA 2000 layout)."""
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    dp = (can_id >> 24) & 0x03
    return (dp << 16) | (pf << 8) | (ps if pf >= 240 else 0)


def fast_packet_ids(frames: list[tuple[float, int, bytes]]) -> dict[int, dict]:
    """CAN ids whose traffic fits the counter/index/length signature.

    A capture starts and ends mid-message, so frames before the first index-0 frame and a final message cut off
    by the end of the capture are out of scope. Of the frames in scope, at least 90% must belong to messages that
    reassemble cleanly - this rejects single-frame messages whose first byte is a per-frame sequence id, which
    reaches 'index 0' by counting."""
    by_id = collections.defaultdict(list)
    for _, cid, data in frames:
        by_id[cid].append(data)
    res = {}
    for cid, seq in by_id.items():
        starts = [i for i, d in enumerate(seq) if len(d) == 8 and d[0] & 0x1F == 0]
        if not starts:
            continue
        i, msgs, clean, consumed, scope, longest = starts[0], 0, 0, 0, 0, 0
        while i < len(seq):
            d = seq[i]
            if not (len(d) == 8 and d[0] & 0x1F == 0):
                scope += 1
                i += 1
                continue
            total, ctr = d[1], d[0] >> 5
            n = 1 + max(0, -(-(total - 6) // 7))
            chunk = seq[i:i + n]
            if len(chunk) < n and i + len(chunk) == len(seq):      # cut off by the end of the capture
                break
            good = all(len(c) == 8 and c[0] >> 5 == ctr and c[0] & 0x1F == k for k, c in enumerate(chunk))
            msgs += 1
            if good:
                clean += 1
                longest = max(longest, n)
                consumed += n
                scope += n
                i += n
            else:
                scope += 1
                i += 1
        # one message is enough only if it is long enough that counter + index run + length cannot be chance
        if (msgs >= 2 or longest >= 3) and scope and consumed >= 0.9 * scope:
            res[cid] = {"messages": msgs, "clean": clean, "frames": len(seq)}
    return res
