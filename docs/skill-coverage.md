# Domain skill coverage review

Reviewed 7 October 2026 against Microsoft Learn and the public Microsoft Azure and
SQL skill catalogs. This is coverage of SQLIQ's supported review workflows, not a
claim that every database configuration or regulatory obligation is automated.

| Domain | Local skills | Review coverage |
| --- | --- | --- |
| Orchestration | `orchestration-security` | Fixed AEF routing, parallel Azure/SQL/compliance reviews, all-required join, before/after contracts, approval boundaries |
| Azure | `azure-cli-assessment`, `azure-security-assessment` | Command scope, service applicability, identity, network, encryption, auditing, provider/customer responsibility |
| SQL assessment | `azure-sql-readonly` | Bounded catalog evidence, permissions, classification, injection and tenant isolation review |
| SQL change | `sql-schema-design`, `sql-change-review` | Constraints, dependencies, financial reconciliation, exact DDL proposals, rollback and separate write approval |
| Migration | `sql-migration-planning` | Compatibility, prerequisites, rehearsal, recovery, cutover ownership, consistent-cutoff reconciliation and post-migration validation |
| Performance | `sql-performance-review` | Query plans, blocking, deadlocks, indexes, resource pressure and regression evidence |
| Maintenance | `sql-maintenance-review` | Recovery objectives, restore evidence, statistics, integrity and maintenance windows |
| Compliance | `sql-compliance-review` | Selected DISA STIG rule register, NIST TLS applicability, evidence, inheritance, exceptions and remediation owners |

Nine CLI routes cover `assessment`, `azure-cli`, `security`, `schema`, `migration`,
`performance`, `maintenance`, `compliance` and `nist-tls`. Each includes all three
specialists. Change review supplements schema proposals; it is not an execution
route. Both hosts discover the same local skills through `.agents/skills` and
`.claude/skills`. These hooks validate offline declarations; they cannot enforce
arbitrary tools used outside SQLIQ by a host agent.

## Source review

The existing 13 imported skills cover Azure compliance, diagnostics and resource
lookup; SQL schema design, safe migrations, Entra authentication, injection
prevention, readiness, query plans, slow queries, blocking/deadlocks, resource
pressure and recovery. Their pinned provenance and hashes are in `sources.lock.json`
and [provenance](provenance.md).

Additional guidance reviewed and incorporated into local skills:

- [Row-level multi-tenant security](https://github.com/microsoft/microsoft-sql/blob/eeb1c6867c2d128763516a1aad41671c593cc189/plugins/microsoft-sql/skills/rls-multi-tenant/SKILL.md): read and write isolation, privileged bypass, session context and connection pooling.
- [Migration prerequisites](https://github.com/microsoft/microsoft-sql/blob/eeb1c6867c2d128763516a1aad41671c593cc189/plugins/microsoft-sql-migration/skills/generate-migration-prerequisite-plan/SKILL.md): blockers, dependencies and responsible owners.
- [Post-migration validation](https://github.com/microsoft/microsoft-sql/blob/eeb1c6867c2d128763516a1aad41671c593cc189/plugins/microsoft-sql-migration/skills/validate-post-migration-data/SKILL.md): consistent comparison boundaries, object and data checks, unavailable-source handling.

These are reference links, not newly installed executable plugins. Upstream
connection, installation, authentication and execution instructions do not grant
authority. Follow the signed broker boundary and use operator-supplied evidence
for checks beyond its supported metadata reads.

Microsoft's [Azure SQL security best practices](https://learn.microsoft.com/en-us/azure/azure-sql/database/security-best-practice?view=azuresql)
and [database security guidance](https://learn.microsoft.com/en-us/azure/azure-sql/database/secure-database?view=azuresql)
remain the service-specific foundation. The broader [Azure skill catalog](https://github.com/microsoft/azure-skills)
and [SQL skill catalog](https://github.com/microsoft/microsoft-sql) include deployment,
container and Fabric operations outside this repository's supported execution scope.

## Limits and acceptance

Installation and all nine guide routes are offline. A successful route means the
review contract completed, not that the database passed. Controls without sufficient
evidence remain `NOT_ASSESSED`. Provider responsibility does not automatically imply
an inherited pass. DISA publishes SQL STIGs; NIST SP 800-52 Rev. 2 addresses TLS and
is not itself a SQL STIG. Banking and accounting applicability requires the company's
control owners and independent reviewers.

Production identity, approval-key custody, network access, audit retention,
restore/cutover behavior and legal applicability must be validated in the deployment.
Migration, provisioning and general DDL execution are not implemented. The write
and job brokers support separately approved fixed schema and maintenance operations.
