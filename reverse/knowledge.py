"""Persistent knowledge base: what earlier, *validated* runs learned.

The inference is deterministic, but it is not memoryless. After every run whose
result was confirmed (a checksum matched real frames and all anchor readings fit),
the facts that were established are recorded here, and later runs use them:

  checksums   exact parameter sets that validated real frames. Tried before the
              built-in catalogue, and their polynomials extend it - so a device
              with an unusual CRC only has to be identified once.
  framings    whole frame layouts (sync, length mode, offsets, trailer, checksum).
              A capture that fits a known layout is recognised immediately,
              without the heuristics.
  encodings   (unit, type, scale, bias) combinations that fitted. When the anchors
              cannot tell two encodings apart (e.g. int8 vs uint8 with only
              positive readings), the encoding seen for that unit in more distinct
              protocols breaks the tie. It never overrides evidence: only
              candidates that already fit the anchors are compared.

Nothing is recorded from a run that did not validate. Evidence is counted per
distinct protocol, not per run, so one device cannot talk the tool into a habit.
The file is plain JSON, meant to be read and diffed (and committed, or not).
"""

from __future__ import annotations

import json
from pathlib import Path

VERSION = 1


def _num(x: float) -> str:
    return f"{x:.8g}"


class KnowledgeBase:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.data: dict = {"version": VERSION, "runs": 0, "checksums": {}, "framings": {}, "encodings": {}}
        if self.path and self.path.exists():
            loaded = json.loads(self.path.read_text())
            if loaded.get("version") != VERSION:
                raise ValueError(f"{self.path}: unsupported knowledge base version {loaded.get('version')!r}")
            self.data.update(loaded)

    # -- persistence ---------------------------------------------------------
    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")

    # -- recording -----------------------------------------------------------
    def _bump(self, bucket: str, key: str, protocol: str, **extra) -> None:
        e = self.data[bucket].setdefault(key, {"protocols": [], **extra})
        if protocol not in e["protocols"]:
            e["protocols"].append(protocol)
            e["protocols"].sort()

    def record_checksum(self, params: dict, protocol: str) -> None:
        p = {k: params[k] for k in sorted(params)}
        self._bump("checksums", json.dumps(p, sort_keys=True), protocol, params=p)

    def record_framing(self, framing: dict, protocol: str) -> None:
        self._bump("framings", json.dumps(framing, sort_keys=True), protocol, framing=framing)

    def record_encoding(self, unit: str | None, ftype: str, scale: float, bias: float, protocol: str) -> None:
        self._bump("encodings", f"{unit or ''}|{ftype}|{_num(scale)}|{_num(bias)}", protocol)

    def record_run(self, framing: dict, fits: list[tuple[str | None, str, float, float]], protocol: str) -> None:
        """Record a validated run: its framing, checksum and every fitted field encoding."""
        self.data["runs"] += 1
        self.record_framing(framing, protocol)
        if framing.get("crc"):
            self.record_checksum(framing["crc"], protocol)
        for unit, ftype, scale, bias in fits:
            self.record_encoding(unit, ftype, scale, bias, protocol)

    # -- using ---------------------------------------------------------------
    def learned_checksums(self) -> list[dict]:
        items = sorted(self.data["checksums"].values(), key=lambda e: -len(e["protocols"]))
        return [dict(e["params"]) for e in items]

    def learned_polys(self) -> list[int]:
        return sorted({p["poly"] for p in self.learned_checksums() if p.get("algo") in ("crc8", "crc16", "crc32") and p.get("poly")})

    def learned_framings(self) -> list[tuple[int, dict]]:
        items = sorted(self.data["framings"].values(), key=lambda e: -len(e["protocols"]))
        return [(len(e["protocols"]), dict(e["framing"])) for e in items]

    def support(self, params: dict) -> int:
        """How many distinct protocols a checksum parameter set has been validated on."""
        p = {k: params[k] for k in sorted(params)}
        e = self.data["checksums"].get(json.dumps(p, sort_keys=True))
        return len(e["protocols"]) if e else 0

    def prior(self, unit: str | None, ftype: str, scale: float, bias: float) -> int:
        """How many distinct protocols used this encoding for this unit."""
        e = self.data["encodings"].get(f"{unit or ''}|{ftype}|{_num(scale)}|{_num(bias)}")
        return len(e["protocols"]) if e else 0

    def summary(self) -> str:
        d = self.data
        protos = {p for b in ("checksums", "framings", "encodings") for e in d[b].values() for p in e["protocols"]}
        return (f"{d['runs']} validated runs from {len(protos)} protocol(s): "
                f"{len(d['framings'])} framing(s), {len(d['checksums'])} checksum parameter set(s), "
                f"{len(d['encodings'])} field encoding(s)")
