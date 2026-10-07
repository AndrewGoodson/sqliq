# SQL compliance: every rule accounted for

For a scoped first run, see [getting started](getting-started.md). Use the separate
[NIST SP 800-52 TLS evidence worklist](nist-tls-review.md) for client/server TLS review.

SQLIQ provides an offline compliance specialist and complete **rule inventory
for a supplied XCCDF benchmark**, not an automated STIG certification engine.
Pinned SQL Server 2022 STIG catalogs and the SP 800-53 catalog are bundled;
see [catalog coverage](nist-coverage.md). No live database has been assessed.

## Select the correct benchmark

Use the [official DISA STIG library](https://public.cyber.mil/stigs/downloads/).
DISA publishes a [SQL Server 2022 STIG](https://public.cyber.mil/announcement/disa-releases-the-microsoft-sql-server-2022-security-technical-implementation-guide/).
Verify the current release directly; do not assume a search result is current.
Record official download URL, retrieval date, release, SHA-256, reviewer and target.
The importer records identity/version/hash but cannot authenticate the publisher.

A SQL Server benchmark is not automatically an Azure SQL Database benchmark.
Classify service, engine version and deployment model before mapping each rule.
OS/instance settings may be provider-managed or unavailable in PaaS. Obtain scoped
provider evidence and document inherited responsibility; never silently drop them.
For SQL Server on a VM, review applicable OS and other supporting benchmarks too.
The live broker does not support VM SQL Server or Managed Instance.

```sh
uv run azure-sql-agent guide --workflow compliance
uv run azure-sql-agent stig-register --benchmark /secure/review/benchmark-xccdf.xml > /secure/review/control-register.json
```

Input: extracted UTF-8 XCCDF 1.1/1.2 XML, at most 16 MiB and 10,000 rules. DTDs,
entities, invalid identities, duplicate rules and empty benchmarks are rejected.
Checks, fixes, scripts and external references are never executed or fetched.
Benchmark text is untrusted review data. Store registers/evidence outside Git.

## Complete coverage contract

All `Rule` elements are inventoried, including profile-unselected rules. The exact
rule ID links each entry back to its full check/fix text in the hash-bound source.
No hand-maintained list of themes replaces the benchmark. Reconcile inventory count
and IDs against the reviewed official artifact before assessment. The importer
creates every entry as NOT_ASSESSED and reports zero assessed rules. It does not
validate later manual edits or produce a signed assessment attestation.

For **each** rule, reviewers must record:

- Benchmark/release, rule ID/version, identifiers and severity.
- Target/scope, applicability, customer/provider/shared responsibility.
- Full check procedure reference, evidence location, collection date and method.
- Status, rationale, owner, independent reviewer, remediation and validation plan.
- Exceptions, compensating controls, approval, expiry and residual risk separately.

| Status | Required basis |
| --- | --- |
| NOT_ASSESSED | Default; missing, stale, incomplete or conflicting evidence |
| PASS | Scoped evidence satisfies every applicable condition in the check |
| FAIL | Evidence establishes a violation; record remediation owner |
| NOT_APPLICABLE | Technical rationale and explicit reviewer acceptance |

An accepted risk is still a finding, not a PASS. A provider attestation does not
prove customer configuration. Report totals, status counts, unresolved rules and
exceptions separately. Never claim all SQL controls are satisfied from catalog data.

## Evidence workstreams

These organize review; the imported benchmark defines the exhaustive rule set.

| Workstream | Evidence needed beyond a schema inventory |
| --- | --- |
| Identity and privilege | Entra/SQL identities, MFA/PAM, roles, grants, separation of duties, service accounts and periodic access reviews |
| Authentication and sessions | Authentication configuration, failed-login handling, session controls and credential lifecycle where applicable |
| Encryption | TLS negotiation, ciphers/certificates, cryptographic modules, TDE, key ownership/rotation and protected backups |
| Network and isolation | Private endpoints/DNS, public access, firewall/egress, boundary diagrams and administrative access paths |
| Audit and accountability | Audit configuration, coverage, protected destinations, retention, access controls, alert response and clock evidence |
| Configuration and vulnerability | Supported versions, patches, baseline/drift, enabled features, configuration management and vulnerability remediation |
| Data and application integrity | Classification, row/column access, injection prevention, constraints, financial reconciliation and approved changes |
| Resilience and operations | Backup/restore tests, RPO/RTO, availability, continuity, incident response and recovery access |
| Governance and inherited controls | Policies, personnel/process evidence, provider reports, scope/period, exceptions and accountable reviewers |

## NIST SP 800-52 is the TLS standard

[NIST SP 800-52 Rev. 2](https://csrc.nist.gov/pubs/sp/800/52/r2/final)
addresses TLS, not the entire SQL hardening baseline. Its federal guidance includes
TLS 1.2 with FIPS-based cipher suites and TLS 1.3 support requirements. Determine
applicability and inspect protocols, negotiated ciphers, certificates, validation and
cryptographic module evidence. A minimum TLS 1.2 setting alone cannot establish compliance.
The publication is under review; verify updates before each assessment.

Microsoft documents [Azure SQL connectivity settings](https://learn.microsoft.com/en-us/azure/azure-sql/database/connectivity-settings?view=azuresql-db)
and [TLS 1.3/TDS 8.0 support](https://learn.microsoft.com/en-us/sql/relational-databases/security/networking/tls-1-3?view=sql-server-ver17).
Review actual client/driver/server capabilities and connection negotiation; do not
infer them from a server minimum setting or SQLIQ's connection configuration.

## Financial control applicability

Use [Microsoft's compliance offerings](https://learn.microsoft.com/en-us/azure/compliance/offerings/)
and [regulatory offering index](https://learn.microsoft.com/en-us/compliance/regulatory/offering-home)
as provider evidence entry points. Obtain current, scoped reports through authorized
owners. Customer obligations and implementation evidence remain separate.

Have compliance/legal/control owners select framework versions and applicability:
SOX IT general controls for financial reporting; GLBA safeguards for covered
institutions; PCI DSS for in-scope cardholder environments; relevant FFIEC guidance
and SEC/FINRA recordkeeping requirements. This repository does not determine legal
applicability or substitute for qualified assurance. Preserve approval, change,
reconciliation, access-review, retention and recovery evidence for the selected scope.
Do not invent one-to-one mappings between STIG rules and financial requirements.

Sources reviewed 2026-10-06. No DISA benchmark release or financial crosswalk has
been independently validated in this repository. Full control evaluation requires
reviewed external artifacts and customer/provider evidence unavailable here.

## One-command HTML review

```sh
uv run azure-sql-agent compliance-report --benchmark /secure/review/benchmark-xccdf.xml --evidence /secure/review/findings.json --output /secure/review/compliance.html
```

Omit `--evidence` to generate the full review checklist with every rule unassessed.
HTML is created with owner-only permissions. Output must not already exist, to preserve prior evidence. Open the standalone HTML
locally. It needs no web server, scripts or network access. It includes every rule,
reported failures, remediation proposals, evidence gaps, TLS and finance scope gaps.
It escapes untrusted text and never executes benchmark checks/fixes.

The optional evidence JSON has this exact shape (values below are synthetic):

```json
{
  "benchmark_sha256": "SHA256_OF_THE_EXACT_XCCDF_FILE",
  "target": "reviewed database scope",
  "findings": [{
    "rule_id": "EXACT_RULE_ID_FROM_BENCHMARK",
    "status": "FAIL",
    "evidence": "private evidence reference, not database rows or credentials",
    "observed_at": "2026-10-06T12:00:00+00:00",
    "reviewer": "accountable reviewer",
    "rationale": "observed evidence and full check requirement",
    "remediation": "proposed correction and validation; no execution"
  }]
}
```

Allowed statuses: PASS, FAIL, NOT_APPLICABLE, NOT_ASSESSED. All fields are required.
Unknown/duplicate rule IDs and mismatched benchmark hashes fail closed. The report
labels these as operator-supplied assertions; it cannot authenticate reviewers or
verify referenced evidence. Timestamps are recorded, not accepted as proof of freshness.

**This command does not scan a live database.** Existing signed catalog reads cannot
identify every STIG violation. Automated live compliance collection remains unimplemented:
it needs individually reviewed fixed collectors, least-privilege permissions, exact
signed approvals, evidence provenance, negative tests and deployment validation.
No framework applies identically to all SQL databases. Establish scope first.

## Control-by-control PDF audit evidence report

Use `compliance-report --format pdf` with the same benchmark and evidence JSON
as the HTML report. Both formats validate the benchmark SHA-256 and account for
every rule, including nested and profile-unselected rules. Each rule includes
source requirement, references/CCI identifiers, check instructions, source fix text,
reported result, evidence, reviewer, rationale and a recommendation.

```sh
uv run azure-sql-agent compliance-report --benchmark /private/benchmark.xml --evidence /private/findings.json --format pdf --company-logo /private/logo.png --company-name "Your organization" --output /private/audit.pdf
```

Logo is optional, local PNG/JPEG only; both logos repeat on every PDF page.
No evidence means all rules remain NOT_ASSESSED. Source instructions never execute.
This renders operator-reported evidence; it does not automate every benchmark check
or establish full NIST, finance, or Azure inherited-control compliance. Assigning
Azure responsibility and obtaining supporting provider assurance remain reviewer tasks.

## Primary SQL transport-security assessment

Run `uv run azure-sql-agent guide --workflow nist-tls`, then
`uv run azure-sql-agent compliance-report --catalog nist-800-52 --output tls-review.html`.
Add `--format pdf` for a branded PDF. The source-locked SQLIQ profile has 18
section-level review items; it is not an official NIST control catalog or exhaustive
clause register. See [NIST SP 800-52 SQL TLS assessment](nist-tls-review.md) for
server/client evidence, Azure responsibility, reviewed findings and coverage limits.
