# Use SchemaIQ with Codex or Claude Code

Clone the repository and launch your agent from its root. Codex discovers the local
workflows through `.agents/skills`; Claude Code uses `.claude/skills`. Both directories
contain symlinks to the same reviewed `skills/local` content. Enable Git symlink
support on your platform; if links appear as text files, fix checkout support before
using skills. Resolve reference links from the canonical skill folder.

Read `AGENTS.md`, `SECURITY.md` and `docs/security.md` first. The host must have no live
Azure/SQL credentials or access to broker/reviewer secrets. Skill instructions alone
cannot restrict an otherwise privileged agent. Install dependencies with `uv sync
--locked --all-extras`, then run `uv run azure-sql-agent verify` before offline work.

## Select a workflow

| Task | Local skill | AEF guide |
|---|---|---|
| Azure CLI assessment planning | azure-cli-assessment | azure-cli |
| Security posture | azure-security-assessment | security |
| Bounded catalog metadata | azure-sql-readonly | assessment |
| Migration readiness and cutover plan | sql-migration-planning | migration |
| Schema and financial integrity | sql-schema-design | schema |
| Query plans, indexing, blocking, resource pressure | sql-performance-review | performance |
| Recovery and maintenance | sql-maintenance-review | maintenance |
| STIG, TLS and finance evidence | sql-compliance-review | compliance |
| DDL proposal review | sql-change-review | schema |

For example, ask Codex to use `$sql-performance-review`, or invoke Claude's
`/sql-performance-review`, with redacted, already-collected evidence. Ask the host to
apply `orchestration-security` plus the domain skill. Natural-language matching also
uses skill descriptions; verify the host reports which skills it loaded.

```sh
uv run azure-sql-agent guide --workflow performance
uv run azure-sql-agent guide --workflow migration
```

The pinned AEF graph runs orchestrator → Azure → SQL → compliance, returning local skill paths,
Microsoft guidance and required assessment outputs. The host reads and applies those
skills to supplied evidence. The deterministic graph does not call an LLM, collect
performance data, or prove host compliance. Source verification runs before guidance.

## Required assessment output

Use the [shared review contract](../skills/local/references/review-contract.md).
Every material finding needs a scope, evidence ID/time, official source URL and
applicability, confidence, impact, proposed change, validation, rollback and owner.
Unknowns remain unknown. Preserve accounting control totals and correction history;
never include customer rows or secrets in a public issue, Git commit or model input.

Only two fixed metadata reads are executable, through the separately deployed signed
broker. Azure CLI, arbitrary DMVs, schema writes, migrations, restores and failover
are planning/review only. Even an approved write cannot execute in this runtime.
Microsoft examples and “offline migration assessment” commands may still connect or
mutate systems: imported examples do not expand these boundaries.

## Quality and maintenance

Run tests, lint and source verification after changes. Rehearse the cases in
`docs/agent-evaluation.md` in actual Codex and Claude sessions before rollout; record
model/version, selected skills, output and pass/fail evidence. Automated repository
checks do not establish agent quality rankings or enterprise certification. Refresh
Microsoft sources through reviewed commits, never unattended self-learning promotion.

## Compliance review

The fourth AEF agent reviews SQL STIG coverage, NIST TLS evidence and financial
control applicability. `uv run azure-sql-agent guide --workflow compliance` prepares
an offline review. `uv run azure-sql-agent stig-register --benchmark FILE` inventories
every rule in a supplied XCCDF benchmark, initially NOT_ASSESSED. See
[compliance coverage](compliance.md).
