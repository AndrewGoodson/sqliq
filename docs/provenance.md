# Sources and updates

`sources.lock.json` records exact commits and SHA-256 for every vendored runtime and
skill file. Runtime verifies it before planning or reads. `uv.lock` pins Python
packages and distribution hashes. Updates require source review, test execution and
an intentional lock refresh; no runtime download/install of skills is allowed.

| Source | Revision | Use |
|---|---|---|
| [AEF Core](https://github.com/AndrewGoodson/aef-core) | `07b291198cfdeee9dc82095a9366931cf14b2a92` | Unmodified Python runtime and package metadata |
| [Microsoft Azure skills](https://github.com/microsoft/azure-skills) | `4190e7d253e59ee1557bcae9ed13a5d8c06263a9` | Compliance, diagnostics, resource lookup |
| [Microsoft SQL](https://github.com/microsoft/microsoft-sql) | `eeb1c6867c2d128763516a1aad41671c593cc189` | Entra, injection prevention, migrations, schema design, query plans, blocking, resource pressure, recovery |
| Our Azure365 design-ingest skill | Locally adapted; final files hashed | Identity, read-only, approval, audit principles |

AEF upstream was initially unavailable anonymously; the authenticated GitHub API
later confirmed public visibility. Runtime source was
copied from the owner's local checkout at the exact revision, without Git history,
private repository documents or local changes. Its package metadata declares MIT;
no standalone upstream LICENSE file was present in that revision. Original source
notices and its MIT declaration are preserved; no additional upstream copyright
notice has been invented. See the included third-party license notice. The local generated README is noted
in the vendored directory. Existing safety behavior is unchanged.

Microsoft repositories' MIT license files are preserved under `skills/upstream`.
Upstream skill folders remain verbatim and may contain executable write/deployment
examples; they are not installed into automatic agent tool discovery. Local wrappers
and runtime restrictions take precedence. Our source skill's bypass mode is excluded.

Maintainer update: obtain an immutable upstream commit, review every changed file,
preserve license notices, update this document and `scripts/lock_sources.py`, run the
lock script, then run tests, integrity verification, dependency audit and secret scan.
Do not refresh a lock just to suppress unexpected integrity failures.

## Authoritative design references

- [Azure SQL security best practices](https://learn.microsoft.com/en-us/azure/azure-sql/database/security-best-practice?view=azuresql)
- [Secure Azure SQL Database](https://learn.microsoft.com/en-us/azure/azure-sql/database/secure-database?view=azuresql)
- [Private endpoints](https://learn.microsoft.com/en-us/azure/azure-sql/database/private-endpoint-overview?view=azuresql)
- [ODBC and Microsoft Entra authentication](https://learn.microsoft.com/en-us/sql/connect/odbc/using-azure-active-directory?view=sql-server-ver17)
- [Azure SQL server GET API](https://learn.microsoft.com/en-us/rest/api/sql/servers/get?view=rest-sql-2023-08-01)

## Expanded reference set (2026-10-06)

Imported verbatim at the same pinned revisions: `azure-resource-lookup` from
`azure-skills/skills`; `evaluate-offline-migration-readiness` from
`microsoft-sql/plugins/microsoft-sql-migration/skills`; `design-azure-sql-schema`,
`read-execution-plan`, `diagnose-blocking-and-deadlocks`,
`diagnose-resource-pressure`, and `restore-and-recover` from
`microsoft-sql/plugins/microsoft-sql/skills`. Nested references are preserved.

Review boundary: examples include CLI authentication, live assessment, elevated
SQL access, actual-plan execution and mutating operations. Local wrappers explicitly
exclude these actions. Imported frontmatter tool permissions are never granted to
SQLIQ agents. Only local wrappers are exposed to host discovery.

Direct Microsoft Learn review is summarized in the
[control map](../skills/local/references/microsoft-learn.md), with retrieval date and
canonical URLs. These live documents are not vendored or represented as immutable;
recheck current applicability when using them. Local original summaries and workflow
contracts are included in source integrity verification.

## SQLIQ NIST SP 800-52 profile (2026-10-06)

`compliance/nist-800-52-review.json` is original SQLIQ assessment guidance,
source-locked alongside local skills. Its 18 IDs are not official NIST control IDs.
It maps server sections 3.1-3.8, client sections 4.1-4.8 and appendices C/D of
[NIST SP 800-52 Rev. 2](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-52r2.pdf)
to SQL evidence-review tasks. The official publication governs clause interpretation.
Microsoft connectivity and shared-responsibility URLs accompany each item. Live
publisher documents are references, not vendored immutable sources. Review the
[NIST publication status](https://csrc.nist.gov/pubs/sp/800/52/r2/final) at assessment
time. The profile hash binds submitted findings to the exact reviewed profile bytes.
