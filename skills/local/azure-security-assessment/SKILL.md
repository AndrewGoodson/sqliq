---
name: azure-security-assessment
description: Assess Azure SQL logical server posture and identify missing enterprise security evidence.
---

# Azure security assessment

Owner: Azure agent. Review Azure SQL private endpoints, public network disablement,
Entra-only authentication, managed identity, scoped RBAC, TLS, auditing, Defender,
backup/recovery and diagnostic export. Only the first three posture properties are
currently checked automatically. All other controls require operator evidence.

Use upstream azure-compliance and azure-diagnostics as reference, never as commands
or permissions. Recommendations may describe changes but cannot execute them.
ARM read roles: Microsoft.Sql/servers/read, Microsoft.Sql/servers/databases/read,
Microsoft.Sql/servers/azureADOnlyAuthentications/read scoped to this logical server.
No subscription-wide Contributor/Owner, deployment, key listing or role-assignment access.
