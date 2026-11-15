"""Text framing layers that sit on top of binary checksums.

Two shapes cover a lot of marine and energy kit:
  * ':' + ASCII hex   (Victron VE.Direct HEX):  ':7A8ED00B9' -> bytes 07 A8 ED 00 B9
    The first character is a lone nibble (the command), the rest are byte pairs.
  * '$' or '!' ... '*HH' (NMEA 0183 and relatives): XOR of the characters between the
    start mark and '*', stored as two ASCII hex digits.
"""

from __future__ import annotations

import re

_HEX = re.compile(r"^[0-9A-Fa-f]+$")


def decode_hex_ascii(line: str) -> bytes | None:
    """':7A8ED00B9' -> bytes; None if the line is not this shape."""
    s = line.strip()
    if len(s) < 4 or s[0] != ":" or not _HEX.match(s[1:]) or len(s) % 2 == 1:
        return None
    return bytes([int(s[1], 16)]) + bytes.fromhex(s[2:])


def nmea_check(line: str) -> dict | None:
    """Check the '*HH' XOR of a '$'/'!' sentence. None if the line is not one."""
    s = line.strip()
    if len(s) < 6 or s[0] not in "$!" or s[-3] != "*" or not _HEX.match(s[-2:]):
        return None
    x = 0
    for ch in s[1:-3]:
        x ^= ord(ch)
    return {"stored": s[-2:].upper(), "computed": f"{x:02X}", "ok": f"{x:02X}" == s[-2:].upper()}


def classify(lines: list[str]) -> str:
    """'hex_ascii', 'nmea' or 'binary' for a list of frame lines."""
    n = [l for l in lines if l.strip()]
    if n and all(decode_hex_ascii(l) is not None for l in n):
        return "hex_ascii"
    if n and all(nmea_check(l) is not None for l in n):
        return "nmea"
    return "binary"
