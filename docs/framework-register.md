# NIST, banking and accounting framework register

**Coverage status: applicability discovery, not a completed control crosswalk.**
All entries start UNDETERMINED / NOT_ASSESSED. This register is expandable: it is
not a claim that every worldwide requirement has been enumerated or implemented.
The compliance agent must never equate framework inclusion with tested compliance.

## Required assessment record

For each applicable framework, retain privately: jurisdiction and regulated entity,
regulator/contract, official publication and release/effective date, source hash,
complete requirement inventory, control IDs and assessment procedures, scoped systems,
organization-defined parameters, inherited/customer/shared responsibilities, evidence
and freshness, owner/reviewer, result, rationale, remediation, exceptions and expiry.

A crosswalk links specific requirements with rationale; it does not transfer a PASS.
One SQL STIG check rarely satisfies an entire organizational or financial control.
Reconcile every source requirement to a finding, justified non-applicability, or
NOT_ASSESSED. Report missing inventories and unmapped requirements as coverage gaps.

## Framework discovery inventory

| Framework / source | Applicability decision and evidence workstream |
| --- | --- |
| NIST CSF | Organizational cybersecurity outcomes, current/target profiles and governance; not a SQL certification |
| NIST SP 800-53, 800-53A, 800-53B | Approved control catalog, baseline, tailoring, parameters and assessment procedures; include all selected controls/enhancements, not only technical SQL settings |
| NIST SP 800-37 and FIPS 199/200 | Risk management, categorization and authorization where required |
| NIST SP 800-171 / 800-171A | CUI scope, contractual revision and assessment requirements; do not assume every bank handles CUI |
| NIST SP 800-52 | TLS server/client requirements; independently assessed beyond a minimum-version setting |
| NIST SP 800-57, 800-131A and FIPS 140 | Key management, algorithm transition and cryptographic module requirements where applicable |
| NIST SP 800-63 series | Identity assurance appropriate to human/workload access and applicable requirements |
| NIST SP 800-61, 800-34, 800-92 | Incident response, contingency planning and logging guidance relevant to the scoped system |
| NIST SP 800-137, 800-218, 800-161 | Monitoring, secure development and supply-chain risk where adopted |
| DISA SQL STIG | Exact product/release XCCDF, full rules, assessment procedures and source fix text; Azure applicability reviewed individually |
| GLBA / regulator security standards | Determine bank/nonbank regulator; information security program, safeguards, provider oversight and notification requirements |
| FFIEC IT Examination Handbook | Applicable examination expectations for governance, information security, architecture/operations, development, outsourcing and continuity |
| OCC / Federal Reserve / FDIC / NCUA | Charter/regulator-specific current guidance, third-party risk, incidents, resilience and examination scope |
| NYDFS 23 NYCRR 500 | Covered-entity determination, applicable requirements/exemptions and effective dates |
| SOX / SEC ICFR / PCAOB AS 2201 | Financial reporting scope, IT general controls, application controls, information used in controls and independent assessment |
| US GAAP / IFRS | Accounting policy and reporting assertions approved by finance; SQL configuration cannot determine accounting compliance |
| SEC / FINRA records and supervision | Broker-dealer/adviser applicability, retention, preservation, supervision and record retrieval requirements |
| BSA / AML / OFAC | Compliance-program scope; financial data lineage, completeness, access and evidence retention supporting authorized systems |
| PCI DSS | Cardholder environment, current licensed standard and required validation approach; separate from SQL hardening |
| SOC 1 / SOC 2 and ISO 27001 | Applicable assurance scope, report period, exceptions and complementary customer controls; no blanket inheritance |
| EU DORA / GDPR and other local rules | Entity/jurisdiction scope, operational resilience, ICT providers, privacy and transfer obligations; counsel-approved local additions |

## Authoritative source entry points

Resolve and pin the applicable version before assessment; publication indexes are
not substitutes for the full requirement text. Respect redistribution licenses.

- [NIST official publications](https://csrc.nist.gov/publications) and [final publications](https://csrc.nist.gov/publications/final-pubs)
- [NIST TLS publication](https://csrc.nist.gov/pubs/sp/800/52/r2/final)
- [FFIEC IT Examination Handbook](https://www.ffiec.gov/node/33)
- [Federal Reserve supervision](https://www.federalreserve.gov/publications/guidance-and-supervision.htm)
- [FTC Safeguards Rule guidance](https://www.ftc.gov/business-guidance/resources/ftc-safeguards-rule-what-your-business-needs-know)
- [NYDFS cybersecurity resources](https://www.dfs.ny.gov/industry_guidance/cybersecurity)
- [SEC ICFR guidance](https://www.sec.gov/info/accountants/stafficreporting.htm)
- [PCAOB AS 2201](https://pcaobus.org/oversight/standards/auditing-standards/details/AS2201)

## Current implementation boundary

SQLIQ imports all rules from a supplied XCCDF benchmark and generates HTML/PDF
reports with operator-supplied findings. It does **not** yet import every framework
above, maintain a verified crosswalk, perform all assessment procedures, or evaluate
accounting transactions. Those entries remain coverage gaps until their authoritative
inventories, applicability decisions and evidence are reviewed. Report generation
must not be marketed as comprehensive automated compliance auditing.

Use [enterprise readiness](enterprise-readiness.md) for deployment gates and
[compliance reporting](compliance.md) for supported evidence/report commands.
