# Security boundaries and production acceptance

## Threat model

Protect SQL schema confidentiality, Azure posture, credentials and explicit human
control. Assume prompt injection through tasks, skills and database metadata; assume
an agent attempts arbitrary SQL, approval forgery, replay and target substitution.
Treat network/provider responses as untrusted. The runtime does not send metadata
to any LLM, external telemetry exporter or long-term agent memory.

The broker trusts its installed code, locked dependencies, policy/public key,
monotonic-enough system clock, credential provider and durable journal. An attacker
with broker OS permissions, reviewer private key or deployment-administrator access
is outside this application boundary. Source hashes detect drift against a trusted
manifest; they do not prove a malicious manifest safe.

## Enforced in this repository

| Control | Enforcement |
|---|---|
| Default deny | `live_enabled=false`; missing or invalid approval rejected |
| Explicit human intent | Ed25519 signature from external reviewer; no signing tool in runtime |
| Bound scope | Exact regenerated plan includes target, SQL, policy/source hashes, limits, output |
| Short lifetime | Issued time <= now < expiry; validity <= 300 seconds |
| Single use | SQLite unique nonce, durable commit before credential or network access |
| Safe SQL vocabulary | Two fixed catalog SELECTs; parameterized row bound; no free-form SQL |
| Azure scope | Three fixed resource GETs; no redirect following; no ambient proxy settings |
| SQL transport | ODBC 18, TLS verification, explicit encryption; private DNS prerequisite |
| Posture gate | Public network disabled, Entra-only enabled, TLS minimum 1.2, DB online |
| Data minimization | Metadata only, <=100 rows, <=64 KiB serialized result |
| Failure handling | No retry; nonce consumed on provider failure; no raw provider errors |
| Offline changes | Small validated DDL proposal templates; separate signed-write broker for supported nullable-column additions |

Timeouts bound individual connection/query/HTTP operations, not the total wall-clock
run or SDK token retries. An OS/service supervisor must impose a total run deadline.
Rows at the cap are explicitly marked possibly truncated. Metadata visibility is
principal-specific and is not a complete schema/compliance assessment.

## Required deployment controls — NOT VERIFIED

All must be independently demonstrated for each environment before production:

- Separate agent/frontend, reviewer and broker OS/workload identities. Agent has no
  credentials, shell on broker, signing key or writes to broker files.
- Read-only broker code, policy, public key and source manifest; deployment changes
  reviewed by a different authorized operator. Never accept policy, public key,
  journal path or reader implementation from an agent request.
- Persistent broker-owned journal on local storage (0700 directory, 0600 file).
  Every worker for an approval key/target shares one durable replay store. Do not
  deploy separate SQLite replicas or reset the store while approvals remain valid.
- Export audit reservations and outcomes to access-controlled immutable storage.
  Current SQLite journal is durable replay protection, not a tamper-proof SIEM.
  Missing finish records mean unknown outcome and require investigation. Rejected
  requests currently produce generic denial; front-door authentication/denial logs
  must be supplied by the deployment layer without storing sensitive payloads.
- Dedicated Entra workload identity scoped to one server and database. Verify
  effective permissions including groups: no SQL writes, EXECUTE, ownership, broad
  SELECT or business-row access. Use CONNECT and selected-object VIEW DEFINITION.
  Fixed catalog queries alone cannot prove the principal has no excess privileges.
- Private endpoint, private DNS and enforced egress restrictions. DNS checks are
  defense-in-depth; an application check cannot eliminate DNS rebinding/TOCTOU.
- Azure public network disabled, Entra-only, minimum TLS 1.2; Azure Policy prevents
  drift. Review auditing, Defender, diagnostic retention, key policy, backup/PITR,
  restore exercise, incident response, residency and data classification separately.
- Reviewer authentication (MFA/enterprise signing service), public-key custody,
  rotation, emergency revocation and reliable clock synchronization. A signature
  proves possession of an approved key, not independently the signer's human identity.
- Rate limiting, run deadline, maximum request size, restricted stdout handling,
  dependency/secret scans, SBOM and independent penetration/security review.

## Approval revocation and failures

Disable live access or replace the pinned public key in trusted policy to invalidate
pending plans. A policy change requires a new plan and new signature. Never retry
with an old approval after failure. Replan and have the human explicitly approve.

## Scope limits

Azure public-cloud SQL Database only. No sovereign-cloud endpoints, SQL Managed
Instance, on-premises SQL Server, Azure VM management, failover/swapping, business
row exports, arbitrary schema execution or arbitrary performance queries. Such capabilities
require new threat models, scoped identities, tests and separate approvals.

## Separate write boundary

See [approved writes](approved-writes.md). The nullable-column write broker uses a
separate audience and policy, exact signed plan, consumed nonce and transaction.
Write identities must be separate from read identities. Live write controls remain
unverified. A failure may occur after commit; reconcile before another approval.
