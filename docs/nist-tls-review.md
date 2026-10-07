# NIST SP 800-52 SQL database TLS assessment

SQLIQ's primary transport-security workflow is **NIST SP 800-52 Rev. 2**.
It covers both the SQL endpoint and its application, ETL, BI, accounting and
administration clients. DISA SQL STIGs and NIST SP 800-53 remain separate assessments.

## Start offline

From a cloned repository, after `uv sync --locked --group dev`:

```sh
uv run azure-sql-agent guide --workflow nist-tls
uv run azure-sql-agent catalog-register --catalog nist-800-52
uv run azure-sql-agent compliance-report --catalog nist-800-52 --output tls-review.html
uv run azure-sql-agent compliance-report --catalog nist-800-52 --format pdf --output tls-review.pdf
```

The guide routes Azure, SQL and compliance reviews through parallel AEF specialists
with TLS-specific hooks. Codex and Claude use the local `sql-compliance-review`
skill and shared review contract. The graphs perform offline routing; they do not
call an AI provider, connect to SQL or perform TLS tests. AI can help interpret
redacted evidence and draft recommendations; code handles integrity, routing and
report generation. Findings require human review.

Reports contain every item, its source section, check instructions, suggested
responsibility, result, evidence gaps and proposed remediation. Without supplied
evidence all items are **NOT_ASSESSED**. This is an evidence review, not a live scan.
Use a private output directory for customer work. Output files cannot overwrite
existing files. PDF reports include SQLIQ branding; add
`--company-logo /private/company.png --company-name "Your organization"` for dual branding.

## Coverage and authoritative sources

The [source-locked profile](../compliance/nist-800-52-review.json) contains **18
SQLIQ-authored section-level review items**, not official NIST control IDs or an
exhaustive normative clause register. Review every applicable clause within each
section and retain the clause-level workpapers before accepting an item as PASS.
The profile covers these review areas:

| Review area | Server reference | Client reference |
|---|---|---|
| Protocol support and SQL connection negotiation | 3.1 | 4.1 |
| Keys, certificate profiles and lifecycle | 3.2 | 4.2 |
| Cryptography, cipher suites and validated implementations | 3.3 | 4.3 |
| Extension requirements and conditional applicability | 3.4 | 4.4 |
| Peer authentication and certificate validation | 3.5 | 4.5 |
| Session resumption and early data | 3.6 | 4.6 |
| TLS compression | 3.7 | 4.7 |
| Operational considerations | 3.8 | 4.8 |
| Pre-shared keys and RSA key transport applicability | Appendix C | Appendix D |

Use the [official NIST publication](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-52r2.pdf)
for requirements, including conditional clauses and exceptions. The
[publication page](https://csrc.nist.gov/pubs/sp/800/52/r2/final) carries a review
notice; verify the adopted revision and organizational applicability at assessment
time. Source review date: October 6, 2026. Financial institutions must determine
which obligations apply; this profile does not establish universal regulatory scope.

`--catalog all` retains its existing meaning: the 1,298 pinned SP 800-53 and DISA
SQL Server 2022 entries. Run `--catalog nist-800-52` separately for this TLS profile.

## Azure and SQL evidence

Record exact service (Azure SQL Database, Managed Instance or SQL Server on VM),
version, endpoints, connection routing and every distinct client/driver/OS stack.
Do not generalize SQL Database settings to Managed Instance or SQL Server versions.

[Microsoft SQL Database connectivity guidance](https://learn.microsoft.com/en-us/azure/azure-sql/database/connectivity-settings?view=azuresql-db)
documents TLS settings and client compatibility considerations. A configured TLS
floor alone does not establish the actual negotiated connection or conformance.
Request approved, TDS-aware SQL connection evidence from authorized operators;
generic HTTPS scans are not evidence of a SQL driver's behavior.

Inspect redacted encryption and certificate-validation configuration, server
identity and chain validation, relevant revocation behavior, negotiated crypto,
and implementation/module assurance. SQL or Entra login evidence does not prove
TLS client-certificate authentication. TDE does not prove transport security.
Never turn off validation or weaken production TLS to obtain test evidence.

Apply [Microsoft shared responsibility](https://learn.microsoft.com/en-us/azure/security/fundamentals/shared-responsibility)
per item. For PaaS endpoints, obtain service-specific provider assurance plus
customer configuration and client evidence. For SQL Server VMs, include customer
DB/OS responsibilities. Provider-operated does not mean PASS or NOT_APPLICABLE.
Opaque provider behavior and incomplete client coverage remain evidence gaps.

SQLIQ's live broker does not collect this complete evidence. No TLS scanner, new
live query or Azure mutation has been added. Reads still require exact, fresh,
externally signed approval; writes require the separate exact-plan signed approval.
Other hardening remains a proposal with staging validation and recovery planning.

## Submit reviewed findings

Use the profile hash printed by `catalog-register` and the evidence format in
[compliance coverage](compliance.md). Each finding must name a known SQLIQ item ID,
status, evidence reference, timezone-qualified observation time, reviewer, rationale
and remediation. Keep owner assignments, clause mappings and exception expiry in
the referenced private evidence package. Example structure, with placeholders:

```json
{
  "benchmark_sha256": "COPY_CURRENT_PROFILE_HASH",
  "target": "Private assessment scope: exact SQL service and all reviewed clients",
  "findings": [{
    "rule_id": "SQLIQ-TLS-CLIENT-SERVER-AUTHENTICATION",
    "status": "NOT_ASSESSED",
    "evidence": "Private workpaper reference; client validation evidence incomplete",
    "observed_at": "2026-10-06T12:00:00+00:00",
    "reviewer": "Assigned reviewer",
    "rationale": "Section 4.5 clause coverage not yet established",
    "remediation": "Collect approved validation evidence and resolve every applicable clause"
  }]
}
```

```sh
uv run azure-sql-agent compliance-report --catalog nist-800-52 \
  --evidence /private/findings.json --format pdf \
  --company-logo /private/company.png --company-name "Your organization" \
  --output /private/sql-tls-audit.pdf
```

The renderer checks evidence structure and profile binding, not its truth or
reviewer authority. PASS requires current evidence for all applicable clauses;
FAIL requires evidence of a violation; NOT_APPLICABLE requires a technical rationale
and reviewer approval. Missing or ambiguous evidence remains NOT_ASSESSED. No
report issues an overall compliance pass or certification. Learning records may
contain only approved de-identified outcomes, never customer evidence or approvals.
