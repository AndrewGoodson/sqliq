# Approved schema and maintenance jobs

SQLIQ can execute a bounded sequence of schema changes or maintenance operations
after a human approves the exact whole plan. Codex or Claude helps prepare the request
and review evidence; a separate deterministic runner performs the approved operations.
There is no configured scheduler or model provider, unattended discovery, self-issued
approval, automatic remediation decision or unrestricted database administrator.

This workflow targets Azure SQL Database in Azure public cloud. It does not execute
bulk data migrations, data backfills, cutover, failover, database creation or destructive
schema changes. Any deployment scheduler must prepare a new exact request and wait
for fresh external reviewer approval on every run; none is installed by this repo.
Use the migration review workflow for planning those operations and
a separately governed release process for execution.

## What a job contains

A job has one canonical UUID, one database target and 1–10 ordered changes:

| Workflow | Permitted operations |
|---|---|
| `schema` | `add_nullable_column`, `create_index` |
| `maintenance` | `update_statistics`, `reorganize_index` |

The [write operation reference](approved-writes.md) defines the fields and fixed SQL.
Mixed workflows and duplicate SQL steps are rejected. Steps run serially. This is not
an atomic batch: earlier steps may remain committed when a later step fails.

The private job request contains `job_id`, `workflow`, `changes` and `readiness`.
Readiness requires:

- `azure_review_sha256`, `sql_review_sha256`, `compliance_review_sha256`: hashes of the
  respective reviewed, privately retained evidence packets.
- `financial_reconciliation_plan_sha256`: the reviewed plan for checking financial
  totals, ledger balances, reporting output and other applicable controls after changes.
- `recovery_rehearsal_sha256`: the reviewed recovery rehearsal artifact.
- `financial_close_clearance`: the literal `approved`, backed by the finance owner's
  clearance for the accounting close period.
- `change_window_start` and `change_window_end`: Unix seconds defining a positive
  window no longer than one hour.

Hashes bind the selected artifacts; they do not prove accuracy, adequacy or review.
The runner validates hash format and required fields, not artifact contents. The
external reviewer must verify the evidence, responsible owners and close clearance.
Do not substitute invented hashes or empty evidence to make a request pass.

## Synthetic request example

This example is for offline format checks only. The repeated hashes are placeholders,
not evidence, and `approved` is a required schema literal, not actual finance approval.
Replace every hash with the SHA-256 of its reviewed private artifact, replace the
sample evidence references, obtain real close clearance and set an authorized window
before requesting a signature. Never sign this example for execution.

```json
{
  "job_id": "11111111-1111-4111-8111-111111111111",
  "workflow": "schema",
  "readiness": {
    "azure_review_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "sql_review_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "compliance_review_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "financial_reconciliation_plan_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "recovery_rehearsal_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "change_window_start": 1800000000,
    "change_window_end": 1800001800,
    "financial_close_clearance": "approved"
  },
  "changes": [
    {
      "kind": "add_nullable_column",
      "schema_name": "dbo",
      "table": "Ledger",
      "column": "ReviewedAt",
      "data_type": "datetime2(7)",
      "change_ticket": "SYNTHETIC-EXAMPLE-ONLY",
      "recovery_evidence": "REPLACE with reviewed private recovery evidence",
      "staging_evidence": "REPLACE with reviewed private staging evidence"
    }
  ]
}
```

## From request to execution

1. Codex or Claude applies the local domain skills, prepares the private request and
   obtains Azure, SQL and compliance review evidence. No database access is implied.
2. Generate an offline plan:

   ```sh
   uv run azure-sql-agent job-plan --policy PRIVATE_POLICY --request PRIVATE_REQUEST
   ```

   Save the exact output outside Git. Plan generation runs the required AEF domain
   hooks and includes their review-contract hash. Hooks establish required checklists;
   they do not independently validate the review evidence or certify agent behavior.
3. An authorized human reviews all SQL, ordering, target, evidence, maintenance window
   and partial-completion risks. Their external signing service signs the exact whole
   `JobPlan` hash using audience `azure-sql-job-broker/v1`, a unique nonce and validity
   of at most 300 seconds. There is no runtime signing command or reviewer key access.
   Read approvals and individual-write approvals are not job approvals.
4. An isolated operator executes the approved plan:

   ```sh
   uv run azure-sql-agent job-run --policy PRIVATE_POLICY --plan PRIVATE_PLAN --approval PRIVATE_APPROVAL --journal PRIVATE_JOURNAL
   ```

   The runner regenerates and verifies the complete plan before any provider call,
   consumes the nonce, reserves the target and records each step's intent durably.
   It checks approval validity and the change window before starting every step.
   A step already in progress is not automatically interrupted when a window expires.
5. Inspect the recorded status:

   ```sh
   uv run azure-sql-agent job-status --policy PRIVATE_POLICY --journal PRIVATE_JOURNAL --job-id JOB_UUID
   ```

   Protect journal access and status output as operational evidence. Status reads
   report the journal; they do not query Azure or SQL to verify results.
6. Independently reconcile schema, workload health and the financial controls in the
   approved plan. Produce a post-change report and have its named owners review it.
   Any new live evidence read requires its own appropriate signed read approval.

## Outcomes and recovery

Successful steps and jobs are recorded as `executed_unverified`; verification remains
`NOT_ASSESSED`. A successful executor return does not establish that accounting output,
compliance controls or performance remained correct. The runner releases its target
lock after completing and recording a successful job; deployment controls must enforce
any required post-change review before subsequent work.

On failure, the runner stops and retains the target lock. A failed step can be marked
`outcome_unknown`; the job is marked `stopped_reconciliation_required` where journal
writes succeed. A process crash can leave `executing` records and a retained lock.
Treat either case as unresolved. Never infer rollback from a client exception.

There is no automatic retry, crash resume, compensation or runtime unlock command.
A trusted deployment operator must reconcile recorded steps with the actual database,
review immutable audit evidence, document recovery and authorize any controlled lock
recovery outside agent reach. Do not delete or reset the replay journal. A new job
requires a new exact plan and fresh external approval.

All individual-write and job workers for a physical server/database target must share
one broker-owned durable journal and target locks. Both paths reject a locked target;
unknown individual-write outcomes retain the same lock. Separate journals bypass this
exclusion. External DBA tools and alternate credentials still require deployment
isolation to prevent out-of-band changes. Restrict this surface before production use.

## Production acceptance

Run the broker under a separate, restricted identity with reviewed workload permissions,
private connectivity, immutable audit export, host isolation and an overall supervisor
deadline. Agents must not hold credentials or modify broker code, policy, public keys
or journals. Follow the [security deployment gates](security.md).

Per-operation limits and transaction semantics are documented in [approved writes](approved-writes.md),
including Microsoft's warning that index reorganization can preserve partial progress.
Live Azure execution, financial reconciliation, disaster recovery, concurrency isolation
and production compliance remain unverified. A deployment must demonstrate them with
its own evidence before use on accounting or financial systems.
