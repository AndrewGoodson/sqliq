"""`python -m aef.services.eval.premise_cli` — run the false-premise suite.

Prints a per-case verdict with the reason each gate failed, the per-gate
rates, and one score. Exits non-zero below `--min-score` so it can gate.

The oracle block is printed separately and is never folded into the score:
an oracle failing means the CASE FILE is stale, which is a different fact
from an agent scoring badly, and merging the two would let a rotted corpus
read as a bad agent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aef.services.eval.premise import (
    CaseError,
    SubmissionError,
    load_cases,
    load_submissions,
    run_suite,
)

DEFAULT_CASES = Path(__file__).parent / "cases" / "false_premise_v1.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="premise-eval")
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ap.add_argument("--submissions", type=Path, required=True)
    ap.add_argument(
        "--repo", type=Path, default=None, help="checkout to re-derive oracled ground truths from"
    )
    ap.add_argument("--min-score", type=float, default=1.0)
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--label", default=None)
    args = ap.parse_args(argv)

    try:
        cases = load_cases(args.cases)
        submissions = load_submissions(args.submissions)
    except (CaseError, SubmissionError, KeyError, json.JSONDecodeError) as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    unknown = sorted(set(submissions) - {c.case_id for c in cases})
    if unknown:
        print(f"FATAL: submissions for unknown case ids: {unknown}", file=sys.stderr)
        return 2

    suite = run_suite(cases, submissions, repo=args.repo)
    label = args.label or args.submissions.stem

    if args.as_json:
        print(
            json.dumps(
                {
                    "label": label,
                    "suite": str(args.cases),
                    "score": round(suite.score, 4),
                    "passed": suite.passed,
                    "total": suite.total,
                    "gate_rates": {k: list(v) for k, v in suite.gate_rates().items()},
                    "oracles": [
                        {"case_id": c, "ok": ok, "detail": d} for c, ok, d in suite.oracle_report
                    ],
                    "cases": [
                        {
                            "case_id": r.case_id,
                            "passed": r.passed,
                            "gates": r.record.domain_gates,
                            "reasons": list(r.reasons),
                        }
                        for r in suite.results
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"=== false-premise eval :: {label} ===")
        print(f"cases: {args.cases}")
        print()
        for r in suite.results:
            mark = "PASS" if r.passed else "FAIL"
            gates = " ".join(f"{n}={'Y' if ok else 'N'}" for n, ok in r.record.domain_gates.items())
            print(f"[{mark}] {r.case_id}  ({gates})")
            for reason in r.reasons:
                print(f"        - {reason}")
        print()
        print("gate rates:")
        for name, (ok, total) in sorted(suite.gate_rates().items()):
            print(f"  {name:<18} {ok}/{total}")
        if args.repo:
            print()
            print(f"oracles re-derived from {args.repo}:")
            for cid, ok, detail in suite.oracle_report:
                print(f"  [{'OK ' if ok else 'BAD'}] {cid}  {detail}")
            if not suite.oracles_ok:
                print("  !! an oracle failed: the CASE FILE is stale, not the agent")
        elif any(c.oracle for c in cases):
            n = sum(1 for c in cases if c.oracle)
            print(f"\noracles: not run ({n} available; pass --repo to re-derive)")
        print()
        print(
            f"SCORE {suite.passed}/{suite.total} = {suite.score:.3f}"
            f"   (threshold {args.min_score:.3f})"
        )

    if args.repo and not suite.oracles_ok:
        return 3
    return 0 if suite.score >= args.min_score else 1


if __name__ == "__main__":
    raise SystemExit(main())
