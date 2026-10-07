from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .broker import Journal, ReadBroker
from .compliance import compliance_html, stig_register
from .learning import OutcomeBatch, learn
from .models import Policy
from .orchestration import assess, build_graph, guide
from .proposals import propose
from .skills import verify_sources
from .workflows import WORKFLOWS


def main():
    parser = argparse.ArgumentParser(description="Offline Azure SQL planning; explicitly approved reads")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Reviewed repository checkout")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify")
    sub.add_parser("graph")
    stig = sub.add_parser("stig-register")
    stig.add_argument("--benchmark", type=Path, required=True)
    report = sub.add_parser("compliance-report")
    report.add_argument("--benchmark", type=Path, required=True)
    report.add_argument("--evidence", type=Path)
    report.add_argument("--output", type=Path, required=True)
    workflow = sub.add_parser("guide")
    workflow.add_argument("--workflow", choices=sorted(WORKFLOWS), required=True)
    learning = sub.add_parser("learn")
    learning.add_argument("--outcomes", type=Path, required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--policy", type=Path, required=True)
    plan.add_argument("--action", choices=["schema_inventory", "index_inventory"], required=True)
    run = sub.add_parser("read")
    run.add_argument("--policy", type=Path, required=True)
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--approval", type=Path, required=True)
    run.add_argument("--journal", type=Path, required=True)
    ddl = sub.add_parser("propose")
    ddl.add_argument("--kind", choices=["add_nullable_column", "create_index"], required=True)
    ddl.add_argument("--schema", default="dbo")
    ddl.add_argument("--table", required=True)
    ddl.add_argument("--name", required=True, help="Column to add or index")
    ddl.add_argument("--type", default="int")
    args = parser.parse_args()
    try:
        source_hash = verify_sources(args.root)
        if args.command == "verify":
            result = {"source_integrity": "verified", "manifest_sha256": source_hash}
        elif args.command == "compliance-report":
            rendered = compliance_html(stig_register(args.benchmark), args.evidence)
            # Exclusive create prevents replacing a prior evidence artifact.
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(rendered)
            result = {"report": str(args.output), "mode": "offline", "live_checks": False}
        elif args.command == "stig-register":
            result = stig_register(args.benchmark)
        elif args.command == "graph":
            print(build_graph().visualize())
            return
        elif args.command == "guide":
            result = guide(args.workflow)
        elif args.command == "propose":
            result = propose(args.kind, args.schema, args.table, args.name, args.type)
        elif args.command == "learn":
            if args.outcomes.stat().st_size > 131072:
                raise ValueError("Outcome input too large")
            result = learn(OutcomeBatch.model_validate_json(args.outcomes.read_bytes()))
        else:
            policy = Policy.model_validate_json(args.policy.read_bytes())
            if args.command == "plan":
                result = assess(policy, args.action, source_hash)
            else:
                from .live import read_metadata
                document = json.loads(args.plan.read_bytes())
                broker = ReadBroker(policy, source_hash, Journal(args.journal), read_metadata)
                result = broker.invoke({"plan": document.get("plan", document),
                                        "approval": json.loads(args.approval.read_bytes())})
        print(json.dumps(result, indent=2, allow_nan=False))
    except Exception:
        # No exception payloads: provider/parser errors can echo sensitive inputs.
        print("Request failed closed. Check policy, approval, source integrity and broker health.",
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
