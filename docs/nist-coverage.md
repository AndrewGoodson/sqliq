# NIST control coverage for Azure SQL

SQLIQ inventories the complete bundled NIST SP 800-53 Revision 5 catalog, including
control enhancements. The source catalog is retained at
[`compliance/sources/nist/800-53-rev5.json`](../compliance/sources/nist/800-53-rev5.json).
Each control keeps its source identity, statement, guidance and references;
withdrawn controls remain visible rather than silently disappearing. A withdrawn
entry is historical catalog content, not a current requirement to implement.
Confirm its withdrawal and any replacement before documenting disposition.

The catalog is a starting inventory. The system owner and authorizing authority
must select and tailor an applicable baseline, assign organization-defined
parameters, document overlays and exceptions, and establish assessment methods.
Do not infer a baseline from the database product or treat every enhancement as
universally mandatory. Unassigned parameters and unavailable evidence are gaps.

## How recommendations work

[`azure-family-guidance.json`](../compliance/azure-family-guidance.json) supplies
SQLIQ-authored implementation and evidence guidance for all 20 families:

| Family | Primary review focus |
| --- | --- |
| AC | Permissions, separation of duties and access lifecycle |
| AT | Role training and exercises |
| AU | Audit coverage, protected retention and review |
| CA | Assessment, authorization and corrective action |
| CM | Configuration baselines and controlled changes |
| CP | Recovery objectives, backups and restoration exercises |
| IA | Identity, authentication and credential lifecycle |
| IR | Incident response and evidence preservation |
| MA | Controlled maintenance and remote support |
| MP | Exports, backups, media retention and disposal |
| PE | Facilities and environmental assurance |
| PL | System plans, boundaries and rules of behavior |
| PM | Enterprise security and privacy governance |
| PS | Personnel screening, transfers and termination |
| PT | Personal-data processing and privacy obligations |
| RA | Risk and vulnerability assessment |
| SA | Acquisition and secure development |
| SC | Networks, transport, cryptography and isolation |
| SI | Flaws, monitoring, integrity and validation |
| SR | Suppliers, dependencies and software provenance |

Family guidance supplements the exact individual control and enhancement text.
It is not a NIST-approved implementation, a substitute for control-specific test
procedures, or evidence that a particular feature satisfies a requirement. Review
every clause and parameter, document the target's implementation, attach dated
supporting evidence, record the assessor's rationale, and assign remediation to
an accountable owner. Recommendations require human review before execution.

The guidance distinguishes Azure SQL Database, Azure SQL Managed Instance and
SQL Server on Azure VMs. Those distinctions support offline assessment planning;
they do not expand the runtime's supported live operations. Use the documented
signed broker for each supported live read or write.

## Inheritance requires evidence

Microsoft's [shared responsibility guidance](https://learn.microsoft.com/en-us/azure/security/fundamentals/shared-responsibility)
describes different boundaries for PaaS and IaaS. For each individual requirement,
record customer, provider or shared responsibility and identify the implementation
component. Obtain assurance that names the relevant service, deployment model,
region or cloud, assessment period and control scope. Review exclusions and
customer obligations. Provider responsibility alone never produces PASS or NA.

For example, a datacenter physical protection requirement may be an inheritance
candidate. Customer office and administrative-device physical controls can remain
in scope. For SQL on a VM, the guest operating system and SQL engine introduce
customer duties that do not transfer just because the VM runs in Azure. Database
permissions, application behavior, financial reconciliations, organizational
policies and privacy decisions need their own evidence.

## Microsoft implementation references

Guidance source links were reviewed on 2026-10-06. Microsoft pages are living
references; recheck capabilities for the exact product, version, tier and region.
Benchmark v3 guidance is used as referenced guidance, not represented as the newest
Microsoft benchmark or as a complete NIST crosswalk.

- [Azure SQL security playbook](https://learn.microsoft.com/en-us/azure/azure-sql/database/security-best-practice?view=azuresql)
- [SQL Server on Azure VM security](https://learn.microsoft.com/en-us/azure/azure-sql/virtual-machines/windows/security-considerations-best-practices?view=azuresql)
- [SQL auditing](https://learn.microsoft.com/en-us/azure/azure-sql/database/auditing-overview?view=azuresql)
- [Business continuity](https://learn.microsoft.com/en-us/azure/azure-sql/database/business-continuity-high-availability-disaster-recover-hadr-overview?view=azuresql)
- [Data discovery and classification](https://learn.microsoft.com/en-us/azure/azure-sql/database/data-discovery-and-classification-overview?view=azuresql)
- [Governance and strategy](https://learn.microsoft.com/en-us/security/benchmark/azure/security-controls-v3-governance-strategy)
- [Privileged access](https://learn.microsoft.com/en-us/security/benchmark/azure/security-controls-v3-privileged-access)
- [Incident response](https://learn.microsoft.com/en-us/security/benchmark/azure/security-controls-v3-incident-response)
- [Posture and vulnerability management](https://learn.microsoft.com/en-us/security/benchmark/azure/security-controls-v3-posture-vulnerability-management)
- [DevOps security](https://learn.microsoft.com/en-us/security/benchmark/azure/security-controls-v3-devops-security)

## Scope and reporting limits

Full SP 800-53 catalog coverage does **not** mean coverage of every NIST
publication or every financial regulation. SP 800-53A assessment procedures,
SP 800-53B baselines, SP 800-52 TLS guidance, cryptographic standards, privacy
obligations and banking/accounting requirements need separate applicability and
evidence decisions. DISA SQL STIGs are distinct sources; they are not issued by
NIST SP 800-52 and cannot be applied to PaaS unchanged without an applicability
review. See [framework register](framework-register.md) and
[compliance workflow](compliance.md).

A report can establish inventory completeness while every control remains
NOT_ASSESSED. PASS requires reviewed evidence for the selected requirement and
target; FAIL needs a recorded deficiency; NA needs an approved applicability
rationale. Unknown provider evidence, missing organizational records and untested
technical settings remain visible. No catalog import, automated report, Azure
certification or family recommendation certifies the customer's system.
