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
