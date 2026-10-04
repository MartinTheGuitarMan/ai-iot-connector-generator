"""CLI for the connector generator.

Usage:
    python -m generator.cli generate --spec specs/weather_station_obs.yaml --out connectors/weather_station_obs.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

from generator.codegen import generate
from generator.spec import ProtocolSpec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="generator")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="Generate a connector module from a protocol spec")
    gen.add_argument("--spec", required=True, help="Path to the YAML ProtocolSpec")
    gen.add_argument("--out", required=True, help="Path to write the generated connector module")

    args = parser.parse_args(argv)

    if args.command == "generate":
        spec = ProtocolSpec.from_yaml(args.spec)
        source = generate(spec)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(source)
        print(f"generated {out_path} from {args.spec} ({spec.protocol}/{spec.message.id})")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
