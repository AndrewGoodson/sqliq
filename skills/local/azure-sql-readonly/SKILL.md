---
name: azure-sql-readonly
description: Prepare bounded Azure SQL catalog metadata reads using least privilege and explicit approval.
---

# Azure SQL metadata assessment

Owner: SQL agent. Only named, parameterized catalog queries. No user-supplied SQL,
business rows, credentials, stored procedure calls, external sources or DDL execution.
SQL metadata can still expose confidential schema names: explicit approval required.

Use Entra identity and private connectivity. Require ODBC 18, Encrypt=yes,
TrustServerCertificate=no. ApplicationIntent=ReadOnly is routing intent, not security.
Use a dedicated database user with CONNECT plus VIEW DEFINITION on selected objects.
Do not grant db_owner, db_datareader, ALTER, CONTROL, EXECUTE, INSERT, UPDATE or DELETE.
Effective rights depend on group memberships; DBA must verify them independently.

Consult upstream entra-id-auth, prevent-sql-injection and diagnose-slow-query.
Never run their examples automatically. Results describe only visible metadata;
truncation and missing visibility must be stated.
