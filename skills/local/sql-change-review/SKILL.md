---
name: sql-change-review
description: Draft and review Azure SQL schema change proposals without executing DDL.
---

# SQL change review

Owner: SQL agent. Generate proposals only: target, exact DDL, preconditions,
compatibility, locking/cost, backfill, backup/restore evidence and rollback risks.
Current templates: add a nullable column, create a nonclustered index.
The read broker cannot execute schema or server changes. The separate write broker
supports exact approved nullable-column additions and nonclustered index creation.
Named statistics updates and index reorganization use the maintenance workflow.
Other changes require a separately governed operator release.

Consult upstream schema-migrations-safely. Migration execution belongs to a separate
DBA release identity and approval pipeline. Never reuse a read approval for a write.
Dropping a new column can destroy later data; removing an index changes query plans.
User review, staging tests and a fresh production change approval remain mandatory.

Apply [shared review contract](../references/review-contract.md) and
[Microsoft Learn control map](../references/microsoft-learn.md).

For supported approved operations, follow the separate
[write broker workflow](../../../docs/approved-writes.md). Never sign for the user.
