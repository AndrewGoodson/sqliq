# Parallel domain reviews

Run `uv run azure-sql-agent guide --workflow security` (or assessment, azure-cli,
nist-tls, compliance, migration, schema, performance or maintenance). The command verifies
source locks, then dispatches three independent deterministic AEF specialist graphs
with at most three threads. It makes zero model calls and no network requests.

| Specialist | Selected skill | Workflow hooks |
|---|---|---|
| Orchestrator | orchestration-security | Fixed workflow lookup; all-required join |
| Azure | azure-security-assessment; azure-cli-assessment for azure-cli | Identity/network, deployment scope, service limits, recovery, shared responsibility as appropriate |
| SQL | sql-compliance-review for nist-tls; azure-sql-readonly or migration/schema/performance/maintenance skill | Metadata bounds, financial reconciliation, compatibility, integrity, query plans, recovery as appropriate |
| Compliance | sql-compliance-review | Coverage, evidence provenance, no assumed inherited pass, finance applicability, remediation owner |

The exact hook identifiers appear under `domain_hooks` in the JSON guide. Hooks
are Python functions in `domain_hooks.py`, not shell commands, downloaded plugins,
or instructions read from evidence. Before hooks verify the fixed domain/skill
assignment. After hooks require the same skill, no live tools and NOT_ASSESSED.
A failed worker or hook aborts the guide; no partial review is represented as complete.
The fixed join order keeps output stable despite differing worker finish order.

These are offline review requirements, not automatic security tests. The host agent
must read the selected skill and shared review contract, interpret redacted evidence,
and report findings with citations, applicability, owners and unknowns. PASS still
requires sufficient evidence and human review. No database is hardened by running
this guide. Prompt rules do not restrict a credentialed host.

Parallelism stops at this offline boundary. Neither a domain hook nor another agent
can grant access. Live reads and supported writes continue through their separate
exact-plan, fresh externally signed approval brokers and durable replay protection.
A model may summarize supplied evidence under the AI governance policy, but cannot
change the deterministic routing, expand permissions or promote learned policy.

For `nist-tls`, endpoint/provider assurance, SQL client certificate validation and
source-clause applicability hooks guide the [SP 800-52 assessment](nist-tls-review.md).
A protocol floor alone is insufficient evidence; no live TLS test is dispatched.

## Migration contract

For `migration`, all specialists receive a source/target review contract. The guide
includes `migration_contract` alongside the fixed `domain_hooks`; its requirements
are declarations for the host and human reviewers, not completed assessments.

| Specialist | Required check identifiers |
|---|---|
| Azure | `source-and-target-service-scope`, `target-service-support`, `source-and-target-network-identity`, `provider-and-customer-control-evidence`, `recovery-and-cutover` |
| SQL | `source-and-target-compatibility`, `schema-dependency-inventory`, `financial-reconciliation`, `rehearsal-and-restore-evidence`, `rollback-and-cutover`, `postmigration-regression-evidence` |
| Compliance | Existing evidence/provenance checks plus `source-and-target-stig-applicability`, `every-stig-rule-register`, `pre-and-post-control-drift`, `go-no-go-owner-approval`, `postmigration-evidence-review` |

Keep source-before and target-after control registers; never copy passing results
without new evidence. Address changed service responsibility, audit/retention,
financial control totals, exceptions and rollback limits. SP 800-52 remains a
separate TLS review. See [migration review gates](sql-migration-review.md).

After-domain validation rejects missing or altered required check declarations.
It does not validate external artifacts, decide go/no-go or authorize an operator.
Migration tools and cutover are outside both brokers' supported actions. The
separate signed write broker's nullable-column support is not migration permission.
