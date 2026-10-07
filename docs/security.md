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
| Safe SQL vocabulary | Two fixed catalog SELECTs plus four fixed typed write templates; no free-form SQL |
| Azure scope | Three fixed resource GETs; no redirect following; no ambient proxy settings |
| SQL transport | ODBC 18, TLS verification, explicit encryption; private DNS prerequisite |
| Posture gate | Public network disabled, Entra-only enabled, TLS minimum 1.2, DB online |
| Data minimization | Metadata only, <=100 rows, <=64 KiB serialized result |
| Failure handling | No retry; nonce consumed on provider failure; no raw provider errors |
| Approved changes | Separate write/job audiences; nullable-column, single-column index, named-statistic and named-index templates |
| Job execution | 1–10 serial steps, whole-plan signature, required readiness hashes/close clearance/window, durable step intent |
| Write exclusion | Shared journal locks physical server/database across individual writes and jobs; unresolved outcomes retain lock |

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
- All individual-write and job workers must use one broker-owned persistent journal
  for the same physical target; separate journal replicas bypass mutual exclusion.
  Unknown outcomes and crashes retain target locks. No agent-accessible unlock exists;
  trusted operators must reconcile and review recovery before any controlled release.
  Restrict external DBA tools and alternate credentials to prevent out-of-band changes.
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

See [approved writes](approved-writes.md) and [approved jobs](automation.md).
Individual writes and whole-plan jobs use distinct audiences, exact signed plans,
consumed nonces and shared target locks. The supported templates add one nullable
column, create one single-column nonunique index, update one named statistic or
reorganize one named index. No arbitrary SQL or business-row edits are supported.

Write identities must be separate from read identities. Nullable DDL, index creation
and statistics updates use a transaction; index reorganization uses autocommit and
can preserve partial progress after failure. Jobs are not atomic across steps.
No automatic retry, rollback DDL or crash resume is provided. A failure can occur
after commit; reconcile before another approval.

Job readiness hashes bind privately reviewed artifacts but do not validate them.
The reviewer must establish financial close clearance and evidence adequacy. Approval
and window validity are checked before each step; an in-flight step is not interrupted
solely because either expires. Successful jobs remain executed_unverified pending
independent financial and operational reconciliation. No scheduler or model provider
is configured. Live write controls and financial safeguards remain unverified.

## Isolated write/job entrypoint

Production deployments can expose `scripts/write_broker_entrypoint.py` through a
restricted, authenticated service wrapper. It accepts one JSON request on standard
input containing exactly `plan` and `approval`, with a 128 KiB input limit. It accepts
no command-line overrides or request-supplied paths. Both write and job approval
schemas are checked before dispatch; each broker then verifies the external signature,
exact regenerated plan, policy, source integrity, freshness and durable nonce.

The installed entrypoint uses fixed, deployment-owned locations:

| Location | Purpose |
|---|---|
| `/opt/azure-sql-agents` | Reviewed installation and source manifest |
| `/etc/azure-sql-agents/write-policy.json` | Write policy and pinned reviewer public key |
| `/var/lib/azure-sql-agents` | Shared durable journal for read, write and job endpoints |

The policy must select `managed_identity`; the entrypoint rejects Azure CLI credentials.
Standalone writes and jobs use the same journal and physical-target lock. Run all
workers against that one store. The agent must have no write access to installation,
policy, journal or service configuration and no access to the broker identity or an
alternate execution path. Do not expose the developer CLI, which permits explicit
paths, as an agent-facing production API. Do not launch this script under an
agent-controlled Python environment, module search path or working directory.

The service wrapper must provide authentication, authorized reviewer routing, rate
limits, total deadlines, private networking and protected audit/status access. No
service installation, managed identity provisioning, scheduler or signing service is
performed by this script. It emits generic errors without provider details; operators
must inspect the protected journal and independently reconcile unresolved outcomes.
These deployment controls and live behavior remain unverified.
