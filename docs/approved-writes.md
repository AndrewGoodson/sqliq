# Exact approval for writes

Live access defaults off. A separate write broker executes four fixed operations
only after a fresh, externally signed approval for the exact plan. No arbitrary
SQL, business-row edits, deletes, bulk data migration, database creation, cutover,
swaps or automatic rollback is supported.

| Operation | Required operation fields | Fixed behavior |
|---|---|---|
| `add_nullable_column` | `column`, `data_type` | Add one nullable column without a default |
| `create_index` | `index`, `column` | Create one nonunique nonclustered index on one column; MAXDOP 1 |
| `update_statistics` | `statistic` | Update one named statistic with engine-selected sampling; MAXDOP 1 |
| `reorganize_index` | `index` | Reorganize one named rowstore index with LOB compaction disabled |

Each change also requires `kind`, `schema_name`, `table`, `change_ticket`,
`recovery_evidence` and `staging_evidence`. Irrelevant fields are rejected. Column
types are restricted to `int`, `bigint`, `bit`, `datetime2(7)` and `nvarchar(255)`.
Identifiers use a restricted alphabet; users cannot supply SQL expressions or options.
Evidence references are required but their contents are not independently verified
by code. The reviewer must inspect current schema, staging results and recovery evidence.

For one approval covering 1–10 ordered operations, use the
[approved job workflow](automation.md). Individual writes and jobs share a physical server/database lock. All workers must
use one broker-owned journal; separate journal replicas bypass this exclusion.
Unknown individual-write outcomes retain the lock too, with no runtime unlock.

## Deployment and approval

Use a separate write-capable workload identity restricted to the approved objects
and a separate broker endpoint. Never increase the read principal's permissions.
Keep credentials, policy and journal outside agent reach. Verify DDL triggers,
table/index features, available storage, lock/workload impact, query-plan effects
and application compatibility in staging. Even an additive change can block
workloads or trigger database-side code.

1. A trusted operator supplies a private `WritePolicy` (the fields of `Policy` plus
   `writes_enabled`, default false). Both `live_enabled` and `writes_enabled` must
   be true in the reviewed deployment policy. Agents must not enable these flags.
2. Prepare private change JSON using the fields above. Keep it outside Git.
3. Generate the exact plan:
   `uv run azure-sql-agent write-plan --policy PRIVATE_POLICY --change PRIVATE_CHANGE`.
   Save the plan outside Git. It binds the target, SQL, policy and source hashes.
4. The external reviewer signs the canonical approval with audience
   `azure-sql-write-broker/v1`, the exact plan SHA-256, a unique UUID nonce and validity
   no longer than 300 seconds. Read and job approvals cannot authorize this endpoint.
   The repository provides no signing command; agents must never access reviewer keys.
5. The isolated operator runs
   `uv run azure-sql-agent write --policy PRIVATE_POLICY --plan PRIVATE_PLAN --approval PRIVATE_APPROVAL --journal PRIVATE_JOURNAL`.

Validation precedes credentials and provider calls. The approval nonce is durably
consumed first. Execution uses encrypted private connectivity, explicit credentials,
Azure posture gates, XACT_ABORT, a 5-second lock wait and a 10-second driver timeout.
These bounds do not guarantee total runtime, resource consumption or immediate
completion of server cancellation. An independent supervisor must enforce run deadlines.

## Transactions and failure

Nullable-column additions, index creation and statistics updates use a connection
transaction with an explicit commit. Index reorganization uses autocommit on a fresh
connection. Microsoft documents that REORGANIZE is not undone by transaction rollback;
a cancellation or failure can leave partial progress. LOB compaction is disabled,
and the reviewer must confirm rowstore applicability and page-lock support.

There are no retries or automatic rollback DDL. Timeout, disconnect or audit failure
can mean **outcome unknown** even when the server committed. Reconcile actual database
state before approving a new change. A reported `committed` result records executor
completion, not independently verified financial or operational correctness.

Only mocked execution has been tested. Live Azure/ODBC behavior, effective permissions,
trigger behavior, independent audit export and identity isolation remain unverified.
Approval is mandatory but does not establish production readiness.

## Microsoft references

- [ALTER TABLE](https://learn.microsoft.com/en-us/sql/t-sql/statements/alter-table-transact-sql?view=sql-server-ver17)
- [CREATE INDEX](https://learn.microsoft.com/en-us/sql/t-sql/statements/create-index-transact-sql?view=sql-server-ver17)
- [UPDATE STATISTICS](https://learn.microsoft.com/en-us/sql/t-sql/statements/update-statistics-transact-sql?view=sql-server-ver17)
- [ALTER INDEX: reorganization and transaction behavior](https://learn.microsoft.com/en-us/sql/t-sql/statements/alter-index-transact-sql?view=sql-server-ver17)

Confirm applicability to the target Azure SQL Database service and features before approval.
