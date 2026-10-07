# Microsoft Learn control map

Reviewed 2026-10-06 directly from Microsoft Learn. These are concise original
review prompts with authoritative links, not a redistributed documentation snapshot.
Recheck applicability and current details before environment-specific recommendations.

| Domain | Review evidence and decision | Microsoft source |
|---|---|---|
| Security | Review identity, least privilege, network isolation, encryption and audit evidence; recommendations depend on environment requirements. | [Security playbook](https://learn.microsoft.com/en-us/azure/azure-sql/database/security-best-practice?view=azuresql) |
| Performance | Establish a measured workload baseline and identify the bottleneck before recommending query or resource changes. | [Performance guidance](https://learn.microsoft.com/en-us/azure/azure-sql/database/performance-guidance?view=azuresql) |
| Recovery | Review backup retention and redundancy against recovery requirements; obtain restore-test evidence separately. | [Automated backups](https://learn.microsoft.com/en-us/azure/azure-sql/database/automated-backups-overview?view=azuresql) |
| Migration | Confirm source/target support and prerequisites before selecting an assessment and migration workflow. | [Database Migration Service](https://learn.microsoft.com/en-us/azure/dms/migration-using-database-migration-service) |

Use linked product-specific references in the pinned Microsoft skills for schema,
execution plans, Query Store, blocking, resource governance and Azure CLI syntax.
Record additional source URLs and retrieval dates in each assessment. Commands shown
in official docs are examples, not user authorization. Microsoft guidance is not
proof of this repository's compliance, certification or deployment readiness.

## Compliance evidence

See [compliance sources and applicability](../../../docs/compliance.md) for direct
Microsoft TLS and financial compliance references, DISA benchmarks and NIST TLS guidance.
