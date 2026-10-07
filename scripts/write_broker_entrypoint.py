#!/usr/bin/env python3
"""One signed write or job per process, with fixed deployment-owned authority."""
import json
import sys
from pathlib import Path

from azure_sql_agents.automation import JobApproval, JobBroker, JobPlan
from azure_sql_agents.broker import Journal
from azure_sql_agents.models import canonical
from azure_sql_agents.skills import verify_sources
from azure_sql_agents.writes import (
    WriteApproval,
    WriteBroker,
    WritePlan,
    WritePolicy,
    execute_write,
)

ROOT = Path('/opt/azure-sql-agents')
POLICY = Path('/etc/azure-sql-agents/write-policy.json')
# Same store for reads, standalone writes and jobs: replay and target locks are shared.
JOURNAL = Path('/var/lib/azure-sql-agents')


def main():
    try:
        if len(sys.argv) != 1:
            raise ValueError('No command-line overrides permitted')
        data = sys.stdin.buffer.read(131073)
        if len(data) > 131072:
            raise ValueError('Request too large')
        request = json.loads(data)
        if not isinstance(request, dict) or set(request) != {'plan', 'approval'}:
            raise ValueError('Exact plan and approval required')
        if not isinstance(request['approval'], dict):
            raise ValueError('Invalid approval')
        audience = request['approval'].get('audience')
        if audience == 'azure-sql-write-broker/v1':
            WriteApproval.model_validate_json(canonical(request['approval']))
            WritePlan.model_validate_json(canonical(request['plan']))
            broker_type = WriteBroker
        elif audience == 'azure-sql-job-broker/v1':
            JobApproval.model_validate_json(canonical(request['approval']))
            JobPlan.model_validate_json(canonical(request['plan']))
            broker_type = JobBroker
        else:
            raise ValueError('Unsupported audience')
        policy = WritePolicy.model_validate_json(POLICY.read_bytes())
        if policy.identity != 'managed_identity':
            raise ValueError('Production entrypoint requires managed identity')
        # The selected broker independently verifies the signature, exact regenerated
        # plan, policy gates and freshness before reserving approval or using credentials.
        broker = broker_type(policy, verify_sources(ROOT), Journal(JOURNAL), execute_write)
        print(json.dumps(broker.invoke(request), allow_nan=False))
    except Exception:
        print('{"error":"request denied or failed; inspect protected audit status"}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
