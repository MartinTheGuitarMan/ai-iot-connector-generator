"""Byte-stuffing layers: HDLC/PPP (flag 7E, escape 7D, xor 0x20) and SLIP (END C0, ESC DB).

`detect(frames)` returns a scheme only when it is supported by the data: every frame starts and ends
with the same flag byte, and the flag never appears raw inside. Without that evidence, frames are
left alone (a stray 7D in an ordinary binary frame must not be treated as an escape)."""
from __future__ import annotations


def unstuff_hdlc(frame: bytes, flag: int = 0x7E, esc: int = 0x7D) -> bytes | None:
    body = frame[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        b = body[i]
        if b == flag:
            return None
        if b == esc:
            if i + 1 >= len(body):
                return None
            out.append(body[i + 1] ^ 0x20)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def unstuff_slip(frame: bytes, end: int = 0xC0, esc: int = 0xDB) -> bytes | None:
    body = frame[1:-1] if frame[:1] == bytes([end]) else frame[:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        b = body[i]
        if b == end:
            return None
        if b == esc:
            if i + 1 >= len(body) or body[i + 1] not in (0xDC, 0xDD):
                return None
            out.append(end if body[i + 1] == 0xDC else esc)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def detect(frames: list[bytes]) -> dict | None:
    """-> {'scheme','flag','esc','escapes_seen'} or None."""
    if not frames or any(len(f) < 3 for f in frames):
        return None
    flag = frames[0][0]
    if any(f[0] != flag or f[-1] != flag for f in frames):
        return None
    if flag == 0x7E:
        un = [unstuff_hdlc(f) for f in frames]
        if all(u is not None for u in un):
            return {"scheme": "hdlc", "flag": 0x7E, "esc": 0x7D,
                    "escapes_seen": sum(f[1:-1].count(0x7D) for f in frames)}
    if flag == 0xC0:
        un = [unstuff_slip(f) for f in frames]
        if all(u is not None for u in un):
            return {"scheme": "slip", "flag": 0xC0, "esc": 0xDB,
                    "escapes_seen": sum(f.count(0xDB) for f in frames)}
    return None


def unstuff(frames: list[bytes], scheme: dict) -> list[bytes]:
    fn = unstuff_hdlc if scheme["scheme"] == "hdlc" else unstuff_slip
    return [fn(f) for f in frames]
