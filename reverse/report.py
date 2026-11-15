"""Human-readable report for identify() results. One function, no side effects: the CLI prints it and the README quotes it."""
from __future__ import annotations

_NAMES = {"crc8": "CRC-8", "crc16": "CRC-16", "crc32": "CRC-32", "fletcher8": "Fletcher-8", "fletcher8_256": "Fletcher-8 (mod 256)",
          "sum8": "8-bit sum", "sum16": "16-bit sum", "negsum8": "negated 8-bit sum", "negsum16": "negated 16-bit sum", "xor8": "XOR of bytes",
          "ascii_hex_sum": "sum of ASCII characters", "ascii_hex_negsum": "negated sum of ASCII characters",
          "ascii_hex_invsum": "inverted sum of ASCII characters (no +1)"}


def describe_rule(m: dict) -> str:
    algo = m["algo"]
    bits = {"crc8": 8, "crc16": 16, "crc32": 32}.get(algo)
    out = _NAMES.get(algo, algo)
    if bits:
        w = bits // 4
        out += (f", poly 0x{m['poly']:0{w}X}, init 0x{m['init']:0{w}X}, "
                f"{'reflected' if m['reflected'] else 'not reflected'}, final XOR 0x{m['xorout']:0{w}X}")
        if bits > 8 and m.get("endian"):
            out += f", {m['endian']}-endian"
    if "width_chars" in m:
        out += f", written as {m['width_chars']} hex characters"
    cover = f"covers bytes from offset {m['start']}"
    if "header_len" in m:
        cover += f" up to the check at offset {m['check_offset']} (mid-frame, header is {m['header_len']} bytes)"
    elif m.get("trailer_len"):
        cover += f" up to {m['trailer_len']} byte(s) before the end"
    else:
        cover += " to the check"
    return f"{out}; {cover}"


def format_report(res: dict) -> list[str]:
    found = False
    rank = {"low": 0, "medium": 1, "high": 2}
    confs = [res["confidence"]] if res["matches"] else []
    confs += [h["confidence"] for h in (res.get("header_checks") or []) + (res.get("ascii_hex_checks") or [])]
    if res.get("padded_check"):
        confs.append(res["padded_check"]["confidence"])
    if res.get("nested"):
        confs.append(res["nested"]["confidence"])
    if res.get("seeded_crc16") and res.get("seeded_confidence") in ("medium", "high"):
        confs.append(res["seeded_confidence"])
    head = max(confs, key=rank.get) if confs else None
    lines = [f"{res['frames']} frame(s), confidence {head}" if head else f"{res['frames']} frame(s)"]
    if res.get("stuffing"):
        lines.append(f"byte stuffing detected and removed first: {res['stuffing']}")
    for m in res["matches"][:3]:
        found = True
        lines.append("  rule: " + describe_rule(m))
    if len(res["matches"]) > 3:
        lines.append(f"  ... and {len(res['matches']) - 3} more that also validate")
    if res["matches"]:
        lines.append(f"  {res['n_candidates_validating']} rule(s) validate every frame"
                     + ("; the list is ambiguous, more frames would separate them" if res["n_candidates_validating"] > 1 else ""))
    if res.get("partial"):
        pa = res["partial"]
        lines.append(f"  note: no rule fits every frame; this one fits {pa['validated']} of {pa['of']}. "
                     f"Frames that do not fit (0-based): {pa['outliers']}")
    for h in res.get("header_checks") or []:
        found = True
        lines.append(f"  mid-frame check ({h['confidence']}): " + describe_rule(h))
    for h in res.get("ascii_hex_checks") or []:
        found = True
        lines.append(f"  hex-text checksum ({h['confidence']}): " + describe_rule(h))
    if res.get("padded_check"):
        found = True
        p = res["padded_check"]
        lines.append(f"  check stored zero-extended in a wider field ({p['pad_bytes']} pad bytes, confidence {p['confidence']}):")
        for m in p["matches"]:
            lines.append("    rule: " + describe_rule(m))
    seeded = res.get("seeded_crc16") or []
    if seeded and (res.get("seeded_confidence") in ("medium", "high") or not found):
        found = True
        for s in seeded:
            lines.append(f"  seeded CRC-16 ({res.get('seeded_confidence')}): {describe_rule(s)}; hidden constant byte(s) {s['append']}")
    elif seeded:
        lines.append(f"  ({len(seeded)} low-confidence seeded-CRC candidate(s) hidden: with so few frames a hidden constant can always be solved)")
    if res.get("nested"):
        found = True
        n = res["nested"]
        lines.append(f"  second check inside the data ({n['confidence']}):")
        for m in n["matches"]:
            lines.append("    rule: " + describe_rule(m))
    if res.get("poly_sweep"):
        lines.append("  note: a full CRC-8 polynomial sweep was needed; its extra chance matches are priced into the confidence")
    if not found:
        lines.append("  no rule found. More frames, or a check type this tool does not know yet.")
    elif head == "low":
        lines.append("  low confidence: few frames, so rules that agree by chance cannot be ruled out. Add frames.")
    return lines


def found_anything(res: dict) -> bool:
    return bool(res["matches"] or res.get("header_checks") or res.get("ascii_hex_checks") or res.get("padded_check")
                or res.get("seeded_crc16") or res.get("nested"))
