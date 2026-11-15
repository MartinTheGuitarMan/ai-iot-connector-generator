"""Eval harness: score a candidate ProtocolSpec against a reference connector.

A candidate spec (from an LLM, or anyone) is judged on behaviour, not on how
it is written: validate it, generate a connector, run that connector over the
same inputs as the hand-written reference connector, and diff the canonical
output. Inputs include the captured sample data plus negative cases (bad
checksum, wrong message type, missing registers) where the reference returns
None.

    python -m authoring.eval --protocol wxobs --spec path/to/candidate.yaml
    python -m authoring.eval --references        # sanity: references vs themselves
"""

from __future__ import annotations

import argparse
import datetime as dt
import inspect
import json
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from canonical import CanonicalReading
from connectors import weather_station_modbus, weather_station_obs, weather_station_obs_full
from generator.codegen import generate
from generator.spec import ProtocolSpec

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "sample_data"
# Deliberately not the date in the captures (2026-09-21), so a spec that ignores
# a sentence's own date field is distinguishable from one that uses it.
REFERENCE_DATE = dt.date(2031, 2, 3)
DEVICE_ID = "eval-device"


@dataclass
class Case:
    label: str
    kwargs: dict[str, Any]  # arguments to parse(); filtered to the connector's signature


@dataclass
class Protocol:
    key: str
    doc: Path
    reference_spec: Path
    reference_module: types.ModuleType
    cases: list[Case]


def _corrupt_checksum(sentence: str) -> str:
    return sentence[:-2] + ("00" if not sentence.endswith("00") else "FF")


def _resign(body: str) -> str:
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}"


def _obs_cases(suffix: str) -> list[Case]:
    lines = [l.strip() for l in (SAMPLE_DIR / "weather_obs_log.txt").read_text().splitlines() if l.strip()]
    own = [l for l in lines if l.split(",")[0].endswith(suffix)]
    other = [l for l in lines if not l.split(",")[0].endswith(suffix)]
    cases = [
        Case(f"capture[{i}]", {"sentence": l, "reference_date": REFERENCE_DATE})
        for i, l in enumerate(own)
    ]
    cases.append(Case("bad-checksum", {"sentence": _corrupt_checksum(own[0]), "reference_date": REFERENCE_DATE}))
    # Sibling sentence type with a valid checksum: must be refused, not misparsed.
    cases.append(Case("wrong-message-type", {"sentence": other[0], "reference_date": REFERENCE_DATE}))
    body = own[0].split("*")[0][1:]
    # Different talker id on a valid sentence: resellers re-badge the device.
    cases.append(Case("other-talker-id", {"sentence": _resign(body.replace("ST", "XX", 1)), "reference_date": REFERENCE_DATE}))
    # Southern/western hemispheres: the captures are all N/E, so without
    # these a spec that wires the wrong direction field would pass.
    for label, swap in (("south-east", {"N": "S"}), ("north-west", {"E": "W"}), ("south-west", {"N": "S", "E": "W"})):
        parts = [swap.get(p, p) for p in body.split(",")]
        cases.append(Case(label, {"sentence": _resign(",".join(parts)), "reference_date": REFERENCE_DATE}))
    # Own-date sentences: a date other than the captures'.
    if suffix == "WXOBSF":
        parts = body.split(",")
        parts[9] = "150325"
        cases.append(Case("other-date", {"sentence": _resign(",".join(parts)), "reference_date": REFERENCE_DATE}))
    cases.append(Case("garbage", {"sentence": "not a sentence", "reference_date": REFERENCE_DATE}))
    return cases


def _modbus_cases() -> list[Case]:
    cases = []
    for i, line in enumerate((SAMPLE_DIR / "weather_station_modbus.jsonl").read_text().splitlines()):
        rec = json.loads(line)
        regs = {int(k): v for k, v in rec["registers"].items()}
        cases.append(Case(f"capture[{i}]", {"registers": regs, "timestamp": rec["timestamp"]}))
    base = {0: 7, 1: 250, 2: 401, 3: 55}
    ts = "2026-09-21T09:00:00Z"
    cases.append(Case("max-values", {"registers": {0: 65535, 1: 65535, 2: 65535, 3: 65535}, "timestamp": ts}))
    cases.append(Case("zeros", {"registers": {0: 0, 1: 0, 2: 0, 3: 0}, "timestamp": ts}))
    for missing in range(4):
        cases.append(Case(f"missing-register-{missing}",
                          {"registers": {k: v for k, v in base.items() if k != missing}, "timestamp": ts}))
    cases.append(Case("extra-registers-ignored", {"registers": {**base, 4: 0, 9: 0}, "timestamp": ts}))
    return cases


PROTOCOLS: dict[str, Protocol] = {
    "wxobs": Protocol("wxobs", ROOT / "docs/protocols/stx_wxobs.md",
                      ROOT / "specs/weather_station_obs.yaml", weather_station_obs, _obs_cases("WXOBS")),
    "wxobsf": Protocol("wxobsf", ROOT / "docs/protocols/stx_wxobsf.md",
                       ROOT / "specs/weather_station_obs_full.yaml", weather_station_obs_full, _obs_cases("WXOBSF")),
    "modbus": Protocol("modbus", ROOT / "docs/protocols/aerolog_modbus.md",
                       ROOT / "specs/weather_station_modbus.yaml", weather_station_modbus, _modbus_cases()),
}


@dataclass
class Failure:
    case: str
    expected: Any
    got: Any


@dataclass
class EvalResult:
    protocol: str
    stage: str = "ok"  # "spec" (failed to load/validate), "codegen", "import", or "ok"
    error: str | None = None
    total: int = 0
    passed: int = 0
    failures: list[Failure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.stage == "ok" and self.total > 0 and self.passed == self.total

    def summary(self) -> str:
        if self.stage != "ok":
            return f"{self.protocol}: FAIL at {self.stage}: {self.error}"
        status = "PASS" if self.ok else "FAIL"
        return f"{self.protocol}: {status} {self.passed}/{self.total} cases"


def _call(module: types.ModuleType, case: Case) -> Any:
    params = inspect.signature(module.parse).parameters
    kwargs = {k: v for k, v in case.kwargs.items() if k in params}
    kwargs["device_id"] = DEVICE_ID
    try:
        out = module.parse(**kwargs)
    except Exception as exc:  # a crash is a distinct, reportable outcome
        return f"EXCEPTION {type(exc).__name__}: {exc}"
    return out.to_dict() if isinstance(out, CanonicalReading) else out


def _load_spec(spec: ProtocolSpec | dict | str | Path) -> ProtocolSpec:
    if isinstance(spec, ProtocolSpec):
        spec.validate()
        return spec
    if isinstance(spec, dict):
        return ProtocolSpec.from_dict(spec)
    text = Path(spec).read_text() if isinstance(spec, Path) or (isinstance(spec, str) and "\n" not in spec and Path(spec).exists()) else spec
    return ProtocolSpec.from_dict(yaml.safe_load(text))


def evaluate(protocol: str, spec: ProtocolSpec | dict | str | Path) -> EvalResult:
    """Score `spec` (object, dict, YAML text, or YAML path) for `protocol`."""
    proto = PROTOCOLS[protocol]
    result = EvalResult(protocol=protocol)

    try:
        loaded = _load_spec(spec)
    except Exception as exc:
        result.stage, result.error = "spec", f"{type(exc).__name__}: {exc}"
        return result
    try:
        source = generate(loaded)
    except Exception as exc:
        result.stage, result.error = "codegen", f"{type(exc).__name__}: {exc}"
        return result
    module = types.ModuleType("candidate_connector")
    try:
        exec(compile(source, "<candidate_connector>", "exec"), module.__dict__)
        if not callable(getattr(module, "parse", None)):
            raise AttributeError("generated module has no parse()")
    except Exception as exc:
        result.stage, result.error = "import", f"{type(exc).__name__}: {exc}"
        return result

    for case in proto.cases:
        expected = _call(proto.reference_module, case)
        got = _call(module, case)
        result.total += 1
        if got == expected:
            result.passed += 1
        else:
            result.failures.append(Failure(case.label, expected, got))
    return result


def evaluate_references() -> dict[str, EvalResult]:
    return {k: evaluate(k, p.reference_spec) for k, p in PROTOCOLS.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="authoring.eval")
    ap.add_argument("--protocol", choices=sorted(PROTOCOLS))
    ap.add_argument("--spec", help="path to candidate YAML spec")
    ap.add_argument("--references", action="store_true", help="score the hand-written specs")
    ap.add_argument("-v", "--verbose", action="store_true", help="show failing cases")
    args = ap.parse_args(argv)

    if args.references:
        results = list(evaluate_references().values())
    elif args.protocol and args.spec:
        results = [evaluate(args.protocol, Path(args.spec))]
    else:
        ap.error("give --references, or --protocol and --spec")

    for r in results:
        print(r.summary())
        if args.verbose:
            for f in r.failures[:10]:
                print(f"  - {f.case}\n      expected: {f.expected}\n      got:      {f.got}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
