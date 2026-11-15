"""CLI: infer connector specs from a raw capture + anchor readings, and teach the tool.

Usage:
    python -m reverse.cli infer --capture capture.bin --anchors anchors.yaml \\
        --protocol vessel_legacy --out-dir specs/ [--connectors-dir connectors/] \\
        [--knowledge reverse/knowledge.json] [--no-learn]
    python -m reverse.cli learn --spec specs/my_device.yaml --capture capture.bin
    python -m reverse.cli knowledge

The knowledge base (default reverse/knowledge.json) grows with every validated run, and
`learn` lets you teach it a device the heuristics cannot work out on their own (an unusual
CRC, a layout they miss) from a spec you wrote or verified.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from generator.codegen import generate
from generator.spec import ProtocolSpec
from reverse.infer import infer_specs, learn_from_spec
from reverse.knowledge import KnowledgeBase

DEFAULT_KB = Path(__file__).resolve().parent / "knowledge.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reverse")
    sub = ap.add_subparsers(dest="command", required=True)

    inf = sub.add_parser("infer", help="Infer framed_binary specs from a capture")
    inf.add_argument("--capture", required=True, type=Path)
    inf.add_argument("--anchors", required=True, type=Path)
    inf.add_argument("--protocol", required=True, help="protocol name used in the specs")
    inf.add_argument("--out-dir", required=True, type=Path, help="where to write <protocol>_<message>.yaml")
    inf.add_argument("--connectors-dir", type=Path, help="also generate connector modules here")
    inf.add_argument("--knowledge", type=Path, default=DEFAULT_KB, help=f"knowledge base file (default {DEFAULT_KB})")
    inf.add_argument("--no-learn", action="store_true", help="use the knowledge base but do not record this run")

    lrn = sub.add_parser("learn", help="teach the knowledge base a device from a verified spec + a capture of it")
    lrn.add_argument("--spec", required=True, type=Path)
    lrn.add_argument("--capture", required=True, type=Path)
    lrn.add_argument("--knowledge", type=Path, default=DEFAULT_KB)

    idn = sub.add_parser("identify", help="few-frame mode: sync/trailer/checksum from frames in a text file (one hex frame per line)")
    idn.add_argument("--frames", required=True, type=Path)
    idn.add_argument("--knowledge", type=Path, default=DEFAULT_KB)
    idn.add_argument("--protocol", help="name to record the rule under (enables learning)")

    know = sub.add_parser("knowledge", help="show what the knowledge base has learned")
    know.add_argument("--knowledge", type=Path, default=DEFAULT_KB)

    args = ap.parse_args(argv)
    kb = KnowledgeBase(args.knowledge)

    if args.command == "identify":
        from reverse.identify import identify
        from reverse.textlayer import classify, decode_hex_ascii, nmea_check
        lines = [l for l in args.frames.read_text().splitlines() if l.strip() and not l.startswith("#")]
        kind = classify(lines)
        if kind == "nmea":
            ok = [nmea_check(l)["ok"] for l in lines]
            print(f"NMEA-style text, {len(lines)} sentence(s): XOR of characters between the start mark and '*' "
                  f"holds on {sum(ok)}/{len(ok)} (stored as two ASCII hex digits)")
            return 0 if all(ok) else 1
        if kind == "hex_ascii":
            print("ASCII-hex framing (':' + hex); decoded to bytes before identification")
            frames = [decode_hex_ascii(l) for l in lines]
        else:
            frames = [bytes.fromhex(l.replace(" ", "")) for l in lines]
        from reverse.report import format_report, found_anything
        res = identify(frames, kb)
        print("\n".join(format_report(res)))
        if args.protocol:
            from reverse.identify import learn_from_identification
            print(learn_from_identification(kb, res, args.protocol))
            kb.save()
        return 0 if found_anything(res) else 1

    if args.command == "knowledge":
        print(f"{args.knowledge}: {kb.summary()}")
        return 0

    if args.command == "learn":
        spec = ProtocolSpec.from_yaml(args.spec)
        print(learn_from_spec(kb, spec, args.capture.read_bytes()))
        kb.save()
        print(f"saved {args.knowledge}: {kb.summary()}")
        return 0

    anchors = yaml.safe_load(args.anchors.read_text())["anchors"]
    _, specs, report = infer_specs(args.capture.read_bytes(), anchors, args.protocol, kb, learn=not args.no_learn)
    print("\n".join(report))
    if not args.no_learn:
        kb.save()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for msg, d in specs.items():
        ProtocolSpec.from_dict(d)  # same validation as hand-written specs
        path = args.out_dir / f"{args.protocol}_{msg}.yaml"
        path.write_text(yaml.safe_dump(d, sort_keys=False))
        print(f"wrote {path}")
        if args.connectors_dir:
            args.connectors_dir.mkdir(parents=True, exist_ok=True)
            out = args.connectors_dir / f"{args.protocol}_{msg}.py"
            out.write_text(generate(ProtocolSpec.from_yaml(path)))
            print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
