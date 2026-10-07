# NIST SP 800-52 TLS review for Azure SQL

SP 800-52 Rev. 2 addresses TLS implementations. SQL STIGs are DISA benchmarks;
SP 800-53 is a separate control catalog. Track each independently. The
[NIST publication page](https://csrc.nist.gov/pubs/sp/800/52/r2/final) currently carries
a review notice; confirm the adopted revision and organizational applicability at
assessment time. Source review date: October 6, 2026.

The [official publication](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-52r2.pdf)
separates server requirements (section 3) from client requirements (section 4),
including protocol support, certificates, cryptography and extensions. Its federal
TLS 1.3 support deadline was January 1, 2024. This checklist is an assessment aid,
not an exhaustive clause register or a claim that every requirement applies to
every financial institution.

## Evidence worklist

These are SQLIQ review tasks mapped to source sections. For each task, retain the
exact applicable clause, scope, evidence ID/date, owner, conclusion and recommendation.
Start each task as NOT_ASSESSED. The collection examples below describe evidence to
request from authorized operators; they do not authorize probes or CLI execution.

| Review task | NIST reference | Evidence to request | Owner / gap treatment |
|---|---|---|---|
| Inventory endpoint and client protocol capabilities | 3.1, 4.1 | Dated service configuration, client/driver versions and approved connection-test evidence | Shared; document incompatible clients and a tested upgrade plan |
| Examine certificate identity and validation | 3.2, 4.2 | Provider certificate assurance and client validation configuration | Provider and application owner; investigate missing trust or validation evidence |
| Review negotiated cryptography and validation scope | 3.3, 4.3 | Negotiation evidence and applicable cryptographic module validation documentation | Shared; verify exact module/version/deployment scope before accepting inheritance |
| Evaluate extension requirements | 3.4, 4.4 | Applicable clause list and implementation/test evidence from service and driver owners | Shared; log unsupported or unverified behavior for review |

Do not infer all these tasks pass from a configured TLS floor. Do not mark an
inherited task NA merely because Azure operates that component. A provider-operated
component needs an applicable assurance reference; unresolved evidence remains a gap.

## Azure SQL applicability

[Microsoft connectivity settings](https://learn.microsoft.com/en-us/azure/azure-sql/database/connectivity-settings?view=azuresql-db)
documents a lowest supported minimum of TLS 1.2 and warns that enforcing TLS 1.3
can break incompatible clients. A minimum of 1.2 can permit negotiation of 1.3;
it does not establish which protocol a particular application used. Review the
actual service and client combination before proposing a change.

SQLIQ's broker configuration checks a TLS floor and configures encrypted connections
with certificate validation. It does not collect complete TLS negotiation evidence,
validate every cryptographic module, or certify SP 800-52 conformance. Do not
recommend changing broker connection behavior without separate security review.

## Assessment handoff

Ask `sql-compliance-review` to create a separate TLS findings appendix using the
[shared review contract](../skills/local/references/review-contract.md). Include the
adopted publication/version, endpoint aliases, client inventory, applicability
rationale, provider assurance references, evidence gaps and remediation owners.
Label the worklist's coverage explicitly; do not describe it as all clauses tested.

Use PASS only for a supported conclusion within the stated scope; FAIL for evidenced
nonconformance; NA only with approved applicability rationale; NOT_ASSESSED when
evidence is missing. Each recommendation needs a validation plan and change owner.
The bundled SQLIQ control report contains SQL STIG/SP 800-53 entries; it does not
load this Markdown checklist as executable checks or automatically merge it into
that report. Deliver this appendix alongside the report for compliance-owner review.
