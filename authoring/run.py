"""Run the authoring loop for one or all protocols and write a report.

    python -m authoring.run --protocol all --max-attempts 3 --feedback errors
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from authoring.author import DEFAULT_MODEL, FEEDBACK_MODES, anthropic_caller, author_spec
from authoring.eval import PROTOCOLS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="authoring.run")
    ap.add_argument("--protocol", default="all", choices=["all", *sorted(PROTOCOLS)])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--feedback", choices=FEEDBACK_MODES, default="errors")
    ap.add_argument("--out", default="runs", help="directory for specs and report.json")
    args = ap.parse_args(argv)

    caller = anthropic_caller(args.model)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    keys = sorted(PROTOCOLS) if args.protocol == "all" else [args.protocol]

    report = []
    for key in keys:
        run = author_spec(key, caller, args.max_attempts, args.feedback)
        last = run.attempts[-1].result
        if run.final_spec:
            (out / f"{key}.yaml").write_text(run.final_spec)
        row = {
            "protocol": key, "model": args.model, "feedback": args.feedback,
            "ok": run.ok, "attempts": run.attempts_used,
            "final": last.summary() if last else run.attempts[-1].note,
            "per_attempt": [
                {"n": a.n, "note": a.note,
                 "stage": a.result.stage if a.result else None,
                 "passed": a.result.passed if a.result else None,
                 "total": a.result.total if a.result else None,
                 "input_tokens": a.input_tokens, "output_tokens": a.output_tokens}
                for a in run.attempts
            ],
        }
        report.append(row)
        print(f"{key}: {'PASS' if run.ok else 'FAIL'} in {run.attempts_used} attempt(s) - {row['final']}")

    (out / "report.json").write_text(json.dumps(report, indent=2))
    print(f"first-try passes: {sum(r['ok'] and r['attempts'] == 1 for r in report)}/{len(report)}; "
          f"eventual passes: {sum(r['ok'] for r in report)}/{len(report)}")
    return 0 if all(r["ok"] for r in report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
