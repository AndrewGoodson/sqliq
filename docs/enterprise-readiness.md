# Enterprise adoption and financial control readiness

**Release posture: reference implementation suitable for controlled offline evaluation.
Production acceptance: NOT VERIFIED. No organization or database is certified.**

This register is a deployment and evidence checklist, not an exhaustive regulatory
crosswalk. A company's control owners must approve the applicable jurisdictions,
regulators, financial reporting scope, framework versions and assessment period.
A Fortune 500 designation does not define a common compliance standard.

## Scope and accountable owners

Record legal entity, service (SQL Database, Managed Instance or SQL Server on VM),
region, database, data classification, financial processes, reporting assertions,
material interfaces, control owners and independent reviewers. The current live
broker supports Azure public-cloud SQL Database only. Other services require separate
implementation and validation; offline benchmark inventory alone does not add support.

## Readiness register

Every row starts NOT_ASSESSED for each deployment. Attach evidence, observation date,
owner, reviewer, finding, remediation due date, validation result and exception expiry.
Track accepted risks as findings; do not convert them into passes.

| Area | Required evidence and acceptance decision | Accountable owner |
| --- | --- | --- |
| SQL STIG baseline | Reviewed official product/release; complete rule register; applicability and evidence for every rule; unresolved rules explicitly counted | Security / DBA |
| TLS | Separate NIST SP 800-52 applicability; actual protocol/cipher negotiation, certificates, validation and cryptographic module evidence | Security |
| Vulnerability assessment | Approved Defender for SQL coverage, scan scope/time/results, baseline approval, remediation SLA, exceptions and rescan evidence; baseline acceptance is not remediation | Security / DBA |
| Platform patching | Provider assurance for managed service scope; customer patch evidence for clients, drivers, broker and any VM workloads | Platform |
| Identity and privilege | Effective grants including group membership; dedicated read/write identities; MFA/PAM, access reviews, joiner/mover/leaver controls | IAM / DBA |
| Segregation of duties | Agent cannot sign approvals or alter broker; requestor, approver, executor and reviewer duties documented and tested | IT controls |
| Network | Private endpoints/DNS, denied public access, egress enforcement, broker isolation and attempted-bypass tests | Network security |
| Encryption and keys | At-rest settings, key custody, rotation, access review, recovery and applicable customer-managed key policy | Security |
| Financial change management | Ticket, business approval, schema-impact analysis, staging results, financial reconciliation, rollback/recovery evidence and separate exact write approval | Finance systems / DBA |
| Financial data integrity | Completeness/accuracy reconciliation across ledgers, subledgers, imports and reports; duplicate/missing transaction and period-close checks | Controller / application owner |
| Reporting controls | Report/query version control, parameter review, source lineage, completeness/accuracy of information used in controls and independent review | Finance / internal audit |
| Logging and monitoring | Audit coverage, protected retention, immutable export, clock integrity, alert ownership, tested response and evidence of review | SOC |
| Retention and privacy | Approved schedules, legal holds, deletion controls, data minimization, residency and privileged access to evidence | Legal / privacy |
| Recovery | Successful restores, approved RPO/RTO, continuity exercise and financial reconciliation after recovery | Operations / finance |
| Incident response | Tested escalation, regulator-specific notification assessment, evidence preservation and provider coordination | Security / legal |
| Provider inheritance | Current service/region/period-specific assurance, complementary customer controls and reviewer-approved per-control inheritance rationale | Vendor risk |
| AI use | Organizational approval, permitted models/data, residency/retention, contractual terms, prompt-injection evaluation and human review; offline mode where AI is prohibited | AI governance / security |
| Software supply chain | Reviewed pins, licenses, SBOM, dependency/secret scans, signed release provenance and independent security review | Engineering security |

## Framework applicability, kept separate

- **SOX / internal control over financial reporting:** determine entity and process
  scope with finance and auditors. SQL security supports IT controls but does not
  establish effective financial reporting controls. See [SEC ICFR guidance](https://www.sec.gov/info/accountants/stafficreporting.htm).
- **GLBA / safeguards:** identify the actual regulator and covered entity. The
  [FTC Safeguards Rule guidance](https://www.ftc.gov/business-guidance/resources/ftc-safeguards-rule-what-your-business-needs-know)
  applies to institutions within its jurisdiction, not every financial company.
  Map risk assessment, safeguards, service providers and incident obligations accordingly.
- **PCI DSS:** determine whether cardholder-data scope applies. Obtain the current
  official standard and qualified assessment guidance; do not infer compliance from a SQL STIG.
- **Other obligations:** counsel/control owners determine privacy, residency,
  contractual, banking, records-retention and jurisdiction-specific requirements.
  SOC assurance reports and ISO certifications have defined scopes and cannot be
  inherited as a blanket customer pass.

## Azure evidence and vulnerability workflow

Microsoft documents [Azure SQL vulnerability assessment](https://learn.microsoft.com/en-us/azure/defender-for-cloud/sql-azure-vulnerability-assessment-overview)
and [shared responsibility](https://learn.microsoft.com/en-us/azure/security/fundamentals/shared-responsibility).
Use an authorized operator to obtain scoped scan evidence. SQLIQ does not currently
execute Defender scans or ingest its raw export automatically. Review and map findings
to the exact benchmark rules before supplying the documented evidence JSON. Preserve
original evidence privately and retain traceable references in the report.

## Release acceptance

1. Complete [deployment security gates](security.md) and an independent security review.
2. Verify denied access, replay rejection, target substitution rejection and restrictive
   effective permissions in a nonproduction tenant with external approvals.
3. Test the supported write in staging, including failure/unknown-outcome reconciliation,
   DDL trigger effects and approved recovery. Do not enable unsupported write actions.
4. Obtain control-owner acceptance of benchmark coverage and every unresolved finding.
5. Obtain organizational production and AI-use authorization. Keep evidence and keys
   outside this public repository; publish only synthetic examples.

Passing unit tests is development evidence, not completion of these acceptance gates.
