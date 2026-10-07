# Use SQLIQ with Codex or Claude Code

Start with the [database onboarding guide](getting-started.md) for a scoped first review.

Clone the repository and launch your agent from its root. Codex discovers the local
workflows through `.agents/skills`; Claude Code uses `.claude/skills`. Both directories
contain symlinks to the same reviewed `skills/local` content. Enable Git symlink
support on your platform; if links appear as text files, fix checkout support before
using skills. Resolve reference links from the canonical skill folder.

Read `AGENTS.md`, `SECURITY.md` and `docs/security.md` first. The host must have no live
Azure/SQL credentials or access to broker/reviewer secrets. Skill instructions alone
cannot restrict an otherwise privileged agent. Install dependencies with `uv sync
--locked --group dev`, then run `uv run azure-sql-agent verify` before offline work.

## Select a workflow

| Task | Local skill | AEF guide |
|---|---|---|
| SQL STIG coverage and compliance evidence | sql-compliance-review | compliance |
| Migration readiness, control continuity and cutover plan | sql-migration-planning | migration |
| NIST SP 800-52 server/client TLS | sql-compliance-review | nist-tls |
| Azure CLI assessment planning | azure-cli-assessment | azure-cli |
| Security posture | azure-security-assessment | security |
| Bounded catalog metadata | azure-sql-readonly | assessment |
| Schema and financial integrity | sql-schema-design | schema |
| Query plans, indexing, blocking, resource pressure | sql-performance-review | performance |
| Recovery and maintenance | sql-maintenance-review | maintenance |
| DDL proposal review | sql-change-review | schema |

For example, ask Codex to use `$sql-performance-review`, or invoke Claude's
`/sql-performance-review`, with redacted, already-collected evidence. Ask the host to
apply `orchestration-security` plus the domain skill. Natural-language matching also
uses skill descriptions; verify the host reports which skills it loaded.

```sh
uv run azure-sql-agent guide --workflow performance
uv run azure-sql-agent guide --workflow migration
```

The pinned AEF orchestrator dispatches Azure, SQL and compliance specialists in
parallel, each with a separate AEF graph, state and services. It joins all three
results in a fixed order; any specialist failure fails the complete guide. The
pinned AEF executor does not yet execute fan-out edges, so SQLIQ provides a bounded
three-worker adapter around independent AEF graphs without modifying the vendor.

Each specialist receives its workflow-specific local skill. Fixed before-domain
hooks validate that assignment and attach the required domain review checks.
After-domain hooks reject changed skills, live tools or a claimed assessment result.
For example, schema work routes integrity, financial reconciliation and signed-write
plan reviews; performance work routes query plans, blocking/indexes and regression
validation; compliance routes control coverage, provenance, inheritance and finance
applicability. See [parallel domain reviews](parallel-domain-reviews.md).

The output contains local skill paths, Microsoft guidance and required assessment
outputs. The host reads and applies those skills to supplied evidence. These
hooks validate offline routing, not actual database findings or host tool use.
The deterministic graphs do not call an LLM, collect performance data, or prove
host compliance. Source verification runs before CLI guidance.

## Required assessment output

Use the [shared review contract](../skills/local/references/review-contract.md).
Every material finding needs a scope, evidence ID/time, official source URL and
applicability, confidence, impact, proposed change, validation, rollback and owner.
Unknowns remain unknown. Preserve accounting control totals and correction history;
never include customer rows or secrets in a public issue, Git commit or model input.

Only two fixed metadata reads are executable through the signed read broker.
The separate write broker supports an exact approved nullable-column addition.
Azure CLI, arbitrary DMVs, other schema writes, migrations, restores and failover
remain planning/review only. Specialist parallelism never executes broker calls.
Microsoft examples and “offline migration assessment” commands may still connect or
mutate systems: imported examples do not expand these boundaries.

## Quality and maintenance

Run tests, lint and source verification after changes. Rehearse the cases in
`docs/agent-evaluation.md` in actual Codex and Claude sessions before rollout; record
model/version, selected skills, output and pass/fail evidence. Automated repository
checks do not establish agent quality rankings or enterprise certification. Refresh
Microsoft sources through reviewed commits, never unattended self-learning promotion.

## SQL STIG and compliance review

Start with `uv run azure-sql-agent guide --workflow compliance`. Select the exact
service/version and benchmark; account for every rule with evidence, applicability,
provider/customer responsibility, owner and proposed remediation. Use
`uv run azure-sql-agent compliance-report --catalog sql-2022-stigs --output stig-review.html`
for the pinned SQL Server 2022 register. Reports start NOT_ASSESSED and do not scan
a live database. Azure SQL PaaS needs reviewed rule-by-rule tailoring. See
[STIG applicability](stig-coverage.md) and [compliance evidence](compliance.md).

## SQL migration with compliance review

Use `uv run azure-sql-agent guide --workflow migration` and load
`orchestration-security`, `sql-migration-planning` and `sql-compliance-review`.
The three specialist contracts require source/target compatibility and service
scope, every-rule STIG applicability, provider/customer evidence, financial
reconciliation, rehearsal and rollback, go/no-go review and postmigration checks.
Follow [migration review gates](sql-migration-review.md). Before/after hooks validate
the declared contract; the host and reviewers must still examine the evidence.

Ask the host:

```text
Prepare a SQL migration review using orchestration-security,
sql-migration-planning and sql-compliance-review. Run guide --workflow migration
and confirm every required domain check. Request a redacted source/target scope
and already-approved evidence. Account for every selected STIG rule before and
after, track provider/customer responsibility and review TLS separately. Define
financial reconciliation, rehearsal, rollback, go/no-go and postmigration
acceptance with named owners. Deliver unresolved blockers and an operator plan;
do not connect, execute migration tools or sign approvals.
```

The migration workflow cannot execute a cutover. A separately governed operator
needs the organization's approved change process. Fixed metadata reads and supported
nullable-column additions retain their separate exact signed broker approvals.

## Separate SQL TLS review

Use `uv run azure-sql-agent guide --workflow nist-tls` for NIST SP 800-52.
Azure reviews endpoint scope, provider assurance and configuration; SQL reviews
client inventory, certificate validation and negotiated crypto evidence; compliance
reviews source-clause applicability and evidence completeness. The guide identifies
`--catalog nist-800-52` for HTML/PDF reporting. See [TLS assessment](nist-tls-review.md).
Hooks validate routing; they do not perform live TLS tests or prove findings.
