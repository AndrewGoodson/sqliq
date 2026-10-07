# SQL Server STIG coverage and Azure applicability

SQLIQ bundles the complete **102-rule** DISA SQL Server 2022 April 2026 package:

| Benchmark | Publisher release | Rules | Original source |
| --- | --- | ---: | --- |
| SQL Server 2022 Instance | Version 1, Release 4, 1 April 2026 | 79 | [XCCDF](../compliance/sources/stig/U_MS_SQL_Server_2022_Instance_STIG_V1R4_Manual-xccdf.xml) |
| SQL Server 2022 Database | Version 1, Release 3, 1 April 2026 | 23 | [XCCDF](../compliance/sources/stig/U_MS_SQL_Server_2022_Database_STIG_V1R3_Manual-xccdf.xml) |

All rules are imported, including those unselected by a profile. Original rule IDs,
STIG IDs, severity, requirement, CCI identifiers, publisher references, check text and
remediation text remain in the original XML and the generated register. Every rule
starts `NOT_ASSESSED`; coverage is not a compliance result. These SQL Server 2022
benchmarks are not a claim to include every SQL product/version benchmark.

The [provenance manifest](../compliance/sources/stig/manifest.json) records the
[DISA archive](https://dl.dod.cyber.mil/wp-content/uploads/stigs/zip/U_MS_SQL_Server_2022_Y26M04_STIG.zip),
its exact SHA-256 from the [NIST checklist download record](https://ncp.nist.gov/checklist/1292/download/18495),
and each retained file hash. Archive SHA-256:
`e46bfae4b5e2ad7a068bef2539f6f98467c9cdfa8a012f01f7868bf4e07a5e64`.
Publisher overview, readme, release memo and revision history remain with the sources.
The external publisher material is not relicensed under SQLIQ's software license.
No DISA endorsement is implied. Supplemental executable SQL was intentionally excluded;
source check/fix text is reference material and is never automatically executed.
This is a pinned, verified release, not a promise that it is the latest available release.

## Address every rule

For each rule, follow its complete publisher check and fix guidance in the report.
Record the exact service, engine/version and boundary; applicability; customer/provider
responsibility; evidence reference and date; reviewer; finding; proposed remediation;
and exception owner/expiry when relevant. Missing evidence remains `NOT_ASSESSED`.
`PASS` requires current evidence for the complete requirement. `NOT_APPLICABLE` requires
a reviewed technical rationale. Reports describe submitted statuses as operator assertions.
Neither cloud hosting nor an unavailable PaaS feature establishes `PASS` or `NOT_APPLICABLE`.

| Target | Applicability and responsibility review |
| --- | --- |
| SQL Server 2022 on Azure VM | Review instance and database rules against the deployed engine and workload. Customer OS, instance and database administration need evidence; Azure physical infrastructure assurance is separate. Assess applicable OS/platform benchmarks separately. |
| Azure SQL Managed Instance | Review every rule against supported service features. Map OS/service operations to provider assurance only when that exact responsibility is documented. Customer identity, authorization, data, network choices, auditing, application and operational controls still require evidence. |
| Azure SQL Database | Treat these SQL Server benchmarks as a source for a reviewed tailoring exercise. OS, instance and local filesystem instructions may require provider evidence or documented alternatives; do not run them against PaaS or silently omit their rules. Customer database and configuration responsibilities remain in scope. |

Use Microsoft's [shared responsibility guidance](https://learn.microsoft.com/en-us/azure/security/fundamentals/shared-responsibility)
and [Azure SQL security overview](https://learn.microsoft.com/en-us/azure/azure-sql/database/security-overview?view=azuresql)
with the service's current assurance evidence. Customer/provider mappings must be reviewed
per rule; no inheritance assertion automatically grants a passing finding.

DISA SQL STIGs derive from NIST SP 800-53 and related material. **SP 800-52 is separate
TLS guidance**, not the name of a SQL STIG. Preserve CCI references without inventing
CCI-to-NIST mappings. The full NIST catalog and financial framework applicability
reviews do not become tested merely because these SQL STIGs were imported.

## Reproduce the pinned import

Download the archive from the manifest's official URL to a local temporary file, then:

```sh
uv run python scripts/import_stigs.py /path/to/U_MS_SQL_Server_2022_Y26M04_STIG.zip \
  --destination /tmp/sqliq-reviewed-stig-import
```

The destination must not exist. The importer first checks the exact reviewed archive
hash and extracts only fixed allowlisted files without archive paths. Updating the pin
requires maintainer review of publisher provenance, changed rules and the complete diff.
Run `uv run pytest tests/test_bundled_stigs.py` to reconcile all 102 IDs, source references,
checks, fixes and hashes. No database access is used for this verification.
