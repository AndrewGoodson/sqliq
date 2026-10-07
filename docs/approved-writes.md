# Exact approval for writes

Read-only remains the default. A separate write broker now supports **adding one
nullable column** using fixed templates. No arbitrary SQL, data edits, index execution,
deletes, swaps, migrations or automatic rollback is supported.

Deployment must use a separate write-capable workload identity restricted to the
approved table and a separate broker endpoint. Never increase the read principal's
permissions. Keep its credentials, policy and journal outside agent reach. Verify
DDL triggers, table features, lock/workload impact and application compatibility in
staging; an additive change may still block workloads or trigger database-side code.

1. Trusted operator supplies a private WritePolicy (same fields as Policy plus
   `writes_enabled`, default false). Both `live_enabled` and `writes_enabled` must
   be true in the reviewed deployment policy. No agent may enable these flags.
2. Create private change JSON with `kind: "add_nullable_column"`, `schema_name`,
   `table`, `column`, `data_type`, `change_ticket`, `recovery_evidence` and
   `staging_evidence`. Evidence references are required but not independently verified
   by code; reviewer must inspect them and current schema before signing.
3. Run `uv run azure-sql-agent write-plan --policy PRIVATE_POLICY --change PRIVATE_CHANGE`.
   Save the exact plan outside Git. The plan includes target and SQL and binds the
   policy/source hashes. Allowed types: int, bigint, bit, datetime2(7), nvarchar(255).
4. External reviewer signs canonical approval using audience
   `azure-sql-write-broker/v1`, exact plan SHA-256, unique UUID nonce and validity no
   longer than 300 seconds. Read approvals cannot authorize writes. The repo provides
   no signing command and agents must never access reviewer keys.
5. Isolated operator runs `uv run azure-sql-agent write --policy PRIVATE_POLICY
   --plan PRIVATE_PLAN --approval PRIVATE_APPROVAL --journal PRIVATE_JOURNAL`.

Signature, target, SQL, policy and source validation happen before any provider call.
Nonce is durably consumed first. Execution uses encrypted private connectivity,
explicit credentials, the existing Azure posture gates, a transaction, XACT_ABORT,
5-second lock wait and 10-second driver timeout. There are no retries or automatic
rollback DDL. A supervisor must enforce overall deadlines. Timeout/disconnect or audit
failure after execution means **outcome unknown**: reconcile the database before any
new approval. Transaction rollback cannot undo an already acknowledged commit.

Only mocked execution has been tested. Live Azure/ODBC DDL, effective permissions,
trigger behavior, independent audit export and identity isolation remain unverified.
Approval is mandatory but is not proof of production readiness.
