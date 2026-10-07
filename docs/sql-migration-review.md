# SQL migration review with STIG evidence

SQLIQ prepares a migration decision packet for Azure SQL Database, Azure SQL
Managed Instance or SQL Server on Azure VM. Azure, SQL and compliance specialists
review supplied evidence in parallel. The outcome is a reviewed plan with blockers,
owners and acceptance criteria; SQLIQ does not execute migrations.

```sh
uv run azure-sql-agent guide --workflow migration
uv run azure-sql-agent catalog-register --catalog sql-2022-stigs
uv run azure-sql-agent compliance-report --catalog sql-2022-stigs --output stig-review.html
```

The report command inventories the pinned 102 SQL Server 2022 rules, all initially
NOT_ASSESSED. It does not connect to a database. Other source/target versions need
their own reviewed benchmark: use `stig-register --benchmark FILE` and
`compliance-report --benchmark FILE --output NEW_HTML`. Check provenance, release
and applicability before selecting a benchmark. The pinned package is not a claim
to cover every SQL product or to be the latest publisher release.

## Three specialist responsibilities

| Specialist | Required migration review |
|---|---|
| Azure | Source/target service support; region/tier limits; private connectivity; identity and key responsibilities; provider assurance; recovery and cutover prerequisites |
| SQL | Compatibility and schema dependencies; data integrity; financial reconciliation; rehearsal/restore evidence; cutover/rollback; application and performance regression |
| Compliance | Every-rule source/target STIG applicability; evidence provenance and responsibility; pre/post control changes; financial-owner acceptance; exceptions and postmigration evidence |

The `migration_contract` and `domain_hooks` returned by the guide declare required
checks. Before/after hooks reject altered assignments, missing required checks,
live tools or unsupported assessment verdicts; failure of a specialist aborts the
guide. These deterministic checks validate the review contract. They do not prove
that a host agent obeyed it or that a migration is ready. Load the shared skills in
Codex or Claude and retain the actual evidence review separately.

## Evidence and decision gates

| Gate | Required evidence and decision | Accountable review |
|---|---|---|
| Scope and baseline | Source and target product/version/compatibility/tier/region; dependency inventory; selected benchmark IDs/releases/hashes; data classification; downtime and RPO/RTO; current control evidence | Technical and security owners |
| Target design | Supported migration method; compatibility blockers; identity/network/key/audit/recovery design; per-rule source/target applicability and responsibility; separately scoped TLS and financial requirements | Azure, SQL and compliance owners |
| Rehearsal | Approved isolated test scope; restore test; measured duration; source/target integrity and performance comparisons; accounting control totals; unresolved defects | Technical and financial control owners |
| Go/no-go | Exact plan/version; change window; evidence gaps; remediation or approved exceptions with expiry; cutover checkpoints; rollback trigger/deadline; named approvers and operational authority | Change authority, security/compliance and financial owners |
| Postmigration acceptance | Target STIG findings; TLS/client evidence; reconciled totals; permissions/audit/retention/backup/restore tests; application/performance results; control drift and owner sign-off | Technical, security/compliance and financial owners |
| Source retirement proposal | Retention/legal hold decisions; recovery and archive access; target acceptance; credential/dependency cleanup plan; separately approved retirement change | Data owner and change authority |

Unresolved compatibility, recovery, security or reconciliation evidence must remain
visible in the decision packet. SQLIQ cannot grant a go decision or accept risk.
Any exception needs an authorized owner, rationale, compensating controls, expiry
and revalidation plan. No status is inferred from silence or a successful data copy.

## Account for every STIG rule before and after

Keep separate registers for source-before and target-after. For every rule in each
selected benchmark, including profile-excluded rules, record:

- Exact benchmark release/hash, rule ID, STIG ID, severity and source check/fix.
- Source and target service/version; applicability rationale and approving reviewer.
- Customer, provider or shared responsibility, with scoped provider assurance and
  customer implementation evidence where required.
- Dated private evidence reference, method, owner, reviewer and result: PASS, FAIL,
  NOT_APPLICABLE or NOT_ASSESSED.
- Changed implementation or responsibility; remediation and validation; exception
  owner/expiry; postmigration evidence and disposition.

Compare the complete rule-ID sets and totals before reviewing status changes.
A removed rule, changed release or new target requires explicit disposition.
Source PASS does not transfer automatically. Provider-managed is not PASS or
NOT_APPLICABLE; missing provider evidence remains a gap. SQL Server OS/filesystem
checks may need a reviewed PaaS alternative, while VM workloads also need applicable
OS/platform benchmarks. See [STIG applicability](stig-coverage.md).

Use separate HTML/PDF reports for before and after. The report evidence format is
documented in [compliance review](compliance.md); it accepts operator assertions and
does not authenticate reviewers or examine the referenced artifacts. Keep the
migration comparison, applicability/responsibility decisions and approvals in a
private companion record referenced by findings. The report renderer does not
automatically calculate drift or enforce organizational approval decisions.

## Financial integrity and rollback

Have the financial control owner define the affected entities, periods, ledgers,
subledgers, currencies and approved acceptance tolerances before rehearsal. Compare
counts, key/constraint integrity, decimal precision, period/control totals and
correction history as applicable. Retain aggregate, redacted operator evidence;
business transactions, credentials and customer identifiers stay out of agent
context and Git. Aggregate data still requires the owner's disclosure approval.

Identify the source of truth at every cutover checkpoint, the change freeze,
in-flight transaction handling and the point when the target accepts writes.
Document how those writes are reconciled or recovered if rollback is needed.
A backup alone does not prove a working restore or a lossless rollback. Assign
rollback authority and deadline, rehearse the procedure, and preserve evidence.

Keep selected financial obligations separate from technical STIG findings. A
completed STIG register does not establish SOX/ICFR, GLBA, PCI DSS or other legal
compliance. Applicability and acceptance belong to authorized control owners.

## Access and change approval

Guide and report commands are offline. No Azure CLI, SQL client, migration service,
BACPAC export/import, deployment, failover, cutover or retirement runs through this
workflow. Instructions in Microsoft documentation and upstream skills are reference
material, not authorization; even an "offline assessment" may connect to a server.

The isolated read broker permits only its fixed metadata reads under exact, fresh,
externally signed approval. The separate write broker supports an exact approved
schema or maintenance operation. Neither supports migration execution. A separately
governed operator must use the organization's approved change process and reviewed
migration tooling. The agent cannot sign approvals, use that operator's credentials
or expand the broker's permissions. See [security boundaries](security.md).

## Primary sources and applicability

Sources checked 2026-10-07. Recheck the selected product, version, region and tool
before making an environment-specific recommendation. SQLIQ's review gates above
are its own contract; they are not a publisher certification or an official
STIG-to-financial-framework crosswalk.

- [Microsoft SQL Server to Azure SQL Database migration guide](https://learn.microsoft.com/en-us/data-migration/sql-server/database/guide?view=azuresql): source/target validation, coordinated cutover and postmigration integrity/performance review. Its operational examples require separate approval.
- [Microsoft DMS supported scenarios](https://learn.microsoft.com/en-us/azure/dms/resource-scenario-status): verify the exact source/target and online/offline method. Support differs by target; do not assume an online route is available for every service.
- [Microsoft migration tools matrix](https://learn.microsoft.com/en-us/azure/dms/dms-tools-matrix): verify current assessment/migration tooling rather than relying on an older imported skill.
- [Microsoft shared responsibility](https://learn.microsoft.com/en-us/azure/security/fundamentals/shared-responsibility): scope provider versus customer duties by service; it is not evidence that a particular control passed.
- [DISA SQL Server 2022 source provenance](stig-coverage.md): official pinned instance/database benchmarks and original source checks. All rules remain visible through applicability review.
- [NIST SP 800-52 Rev. 2](https://csrc.nist.gov/pubs/sp/800/52/r2/final): separate TLS review for source, target and changed client paths; use the [SQL TLS workflow](nist-tls-review.md).
