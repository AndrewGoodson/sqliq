#!/usr/bin/env python3
"""One request per isolated process; all authority comes from fixed trusted paths."""
import json
import sys
from pathlib import Path

from azure_sql_agents.broker import Journal, ReadBroker
from azure_sql_agents.live import read_metadata
from azure_sql_agents.models import Policy
from azure_sql_agents.skills import verify_sources

ROOT = Path("/opt/azure-sql-agents")
POLICY = Path("/etc/azure-sql-agents/policy.json")
JOURNAL = Path("/var/lib/azure-sql-agents")


def main():
    try:
        data = sys.stdin.buffer.read(131073)
        if len(data) > 131072:
            raise ValueError("Request too large")
        policy = Policy.model_validate_json(POLICY.read_bytes())
        if policy.identity != "managed_identity":
            raise ValueError("Production entrypoint requires managed identity")
        broker = ReadBroker(policy, verify_sources(ROOT), Journal(JOURNAL), read_metadata)
        print(json.dumps(broker.invoke(json.loads(data)), allow_nan=False))
    except Exception:
        print('{"error":"request denied or failed; inspect protected audit status"}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
