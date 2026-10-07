# Start an Azure SQL review

SQLIQ is a public, tenant-neutral toolkit for Codex and Claude Code. AEF Core is
vendored and pinned; no separate AEF installation is needed. Offline workflows
require Git, Python 3.11+ and [uv](https://docs.astral.sh/uv/). Clone with working
symlink support rather than relying on a ZIP archive to preserve host skill links.
Run the commands below from the repository root.

```sh
git clone https://github.com/AndrewGoodson/sqliq.git
cd sqliq
uv sync --locked --group dev
uv run azure-sql-agent verify
uv run azure-sql-agent graph
uv run azure-sql-agent guide --workflow compliance
uv run azure-sql-agent learn --outcomes config/learning-example.json
```

These commands verify sources, show routing and demonstrate learning from synthetic
outcomes. They do not contact Azure, inspect a database or produce compliance passes.
The live Python dependencies and Microsoft ODBC driver belong on the isolated broker
host when an operator deploys it; they are not needed for this offline start.

## 1. Define one assessment scope

Keep a private assessment folder outside the checkout. Record a database alias,
service (Azure SQL Database, Managed Instance or SQL Server on a VM), engine/version,
region/tier, environment, purpose, owner, date, approved evidence sources and retention.
Use aliases in model context. Record actual tenant, subscription, resource group,
logical server and database only in protected operator configuration.

The live adapter supports Azure public-cloud SQL Database. Managed Instance, SQL
Server on VMs and on-premises SQL Server need explicit applicability review and a
separately governed collection path; do not reuse the live adapter for them.

An offline plan can target your selected database: copy `config/example.json` to
that private folder, replace only the target fields and leave `live_enabled: false`.
The sample public key is a placeholder, not a production trust configuration.

```sh
uv run azure-sql-agent plan --policy /absolute/private/assessment/policy.json --action schema_inventory
```

This emits a plan, not query results. Treat its target metadata as confidential.
Never add private policies, evidence, plans or reports to a public fork.

## 2. Load skills in the host

Start Codex or Claude Code at the checkout root. Read `AGENTS.md`, `SECURITY.md`,
`docs/security.md` and the [shared review contract](../skills/local/references/review-contract.md).
Codex discovers `.agents/skills`; Claude discovers `.claude/skills`. Both link to
`skills/local`. If either host cannot discover them, verify symlinks resolve before
continuing. Use [host acceptance cases](agent-evaluation.md) for your installed host.

Ask for `orchestration-security` plus the relevant domain skill in the
[workflow table](agent-usage.md#select-a-workflow). Require the host to name loaded
skills and review the guide's `domain_hooks`. The graph dispatches Azure, SQL and
compliance specialists in parallel. These are deterministic AEF workers; they do not
spawn three autonomous Codex/Claude sessions or enforce restrictions on host tools.

Provide only evidence approved for the selected model environment. Redact identifiers,
query literals, financial/customer data and secrets. Where company policy prohibits
AI processing, retain evidence in approved systems and use deterministic local
register/report generation without sending it to a model. See [AI governance](ai-governance.md).

## 3. Review controls and report gaps

Run the appropriate guide, then have the host apply its skills to approved evidence.
Separate SQL STIG rules, the SP 800-53 catalog and [SP 800-52 TLS review](nist-tls-review.md).
Classify each control's applicability and provider/customer ownership; inherited
responsibility still requires supporting provider assurance evidence. See
[compliance guidance](compliance.md) for the evidence format and inheritance review.

Generate the complete bundled register in either format with one command:

```sh
uv run azure-sql-agent compliance-report --catalog all --output /absolute/private/assessment/controls.html
uv run azure-sql-agent compliance-report --catalog all --format pdf --output /absolute/private/assessment/controls.pdf --company-name 'Example Financial' --company-logo /absolute/private/assessment/company.png
```

Create the private folder first. Outputs must not already exist. Omit company options
when branding is not needed; SQLIQ branding is included. No evidence means every
entry is NOT_ASSESSED. These commands are report generation, not a database scan.
Use `--evidence /absolute/private/assessment/findings.json` with reviewed findings
matching the catalog hash and rule IDs; see [report instructions](compliance.md).
TLS checklist findings are a separate assessment artifact; the bundled `all` catalog
contains SP 800-53 and SQL STIG entries, not a complete SP 800-52 clause register.

Require evidence references/timestamps, PASS/FAIL/NA/NOT_ASSESSED, rationale,
remediation, validation, owner and approval needs. Missing provider evidence or
untested client behavior cannot be converted into PASS. A compliance owner must
select the organization's banking/accounting obligations and approve exceptions.

## 4. Approve collection or a change

Follow the [operator deployment guide](operator-guide.md) and readiness gates before
any live read. The agent must not hold Azure credentials, reviewer private keys or
broker privileges. Natural-language permission alone cannot authorize execution:
the human reviewer signs the exact fresh plan through the external approval process.

The read broker supports schema/index metadata only. The [separate write broker](approved-writes.md)
supports an approved nullable-column addition. Other DDL, performance collection,
Azure CLI actions, migrations, restores and failovers remain reviewed proposals for
the company's authorized change process. Writes are approval-controlled.

## 5. Improve the graph and learning safely

Graph engineering lives in `src/azure_sql_agents/orchestration.py` and `workflows.py`;
domain checks live in `domain_hooks.py`. See [parallel reviews](parallel-domain-reviews.md).
Learning uses AEF consolidation over bounded de-identified outcomes. See the
[learning contract](learning.md) and `config/learning-example.json` for accepted fields.

Review candidates do not persistently train models or modify skills. A maintainer
reviews provenance, proposed changes and regression evidence before a release.
After changes, run `uv run pytest`, `uv run ruff check src tests scripts` and
`uv run azure-sql-agent verify`. Changes to pinned sources require a separate
provenance review before lock regeneration. Do not unlock sources to bypass a failure.
