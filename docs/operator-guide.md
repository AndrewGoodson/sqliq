# Operator guide

Offline planning works with `config/example.json`. Live access needs a separately
reviewed deployment; do not enable it merely to make a test pass. No live system was
contacted to validate this repository.

1. Complete every deployment gate in [security.md](security.md). Provision a dedicated
   Entra principal and scoped rights through your DBA/IAM change process. Do not use
   an existing admin login. Install Microsoft ODBC Driver 18 on the broker host using
   your approved package source; `uv sync --locked --extra live` installs Python clients.
2. On a separate human reviewer workstation, generate and protect an Ed25519 key:
   `openssl genpkey -algorithm ED25519 -out reviewer.key`. Keep it outside this
   checkout and outside agent access; apply mode 0600 and approved key custody.
   Obtain the raw public key as base64 with Python cryptography's
   `key.public_key().public_bytes_raw()`. Never put the private key in policy.
3. Copy the sample policy into the broker's protected configuration. Supply your own
   target UUIDs, server/database, public key and managed identity client ID; enable
   live access only after the deployment gates pass. Managed identity is default.
   `azure_cli` is an explicit operator-only development alternative, tenant scoped;
   it must not run inside the untrusted agent's account/session.
4. Generate the reviewed plan using that policy:
   `uv run azure-sql-agent plan --policy /protected/policy.json --action schema_inventory > plan.json`.
   Transfer the plan to the reviewer through your approved channel. The plan contains
   target metadata, exact query, limits, source hash and output destination.
5. On the reviewer workstation, run
   `uv run python scripts/approve.py --plan plan.json --private-key /private/reviewer.key --out approval.json`.
   Read the full plan and type the requested exact approval phrase. No automation
   or agent should supply that phrase. Approval expires after five minutes.
6. The trusted operator can run the CLI from the broker account:
   `uv run azure-sql-agent read --policy /protected/policy.json --plan plan.json --approval approval.json --journal /protected/journal`.
   The CLI is an operator interface. Never expose these configurable flags as an
   agent tool. An isolated deployment should instead use the fixed-path entrypoint
   `scripts/broker_entrypoint.py` described below.
7. Treat stdout as confidential. Do not pipe results to public logs or an LLM.
   Inspect audit state after failures, obtain a fresh approval to retry and preserve
   replay history. No database writes occur through this program.

## Fixed-path process boundary

Install the reviewed checkout at `/opt/azure-sql-agents`, its environment there,
trusted policy at `/etc/azure-sql-agents/policy.json`, and a persistent broker-owned
journal at `/var/lib/azure-sql-agents`. Make source/policy read-only to both agent and
broker. Run `/opt/azure-sql-agents/.venv/bin/python /opt/azure-sql-agents/scripts/broker_entrypoint.py`
as the separate broker OS identity. It accepts one JSON request on stdin containing
only `plan` and `approval`; it does not accept paths, policy or credentials. Apply a
supervisor deadline and authenticated request transport. The entrypoint is suitable
for wrapping in your existing privileged-execution service; it is not an unauthenticated
web endpoint or a supplied production deployment.

## Schema/server change management

`propose` generates DDL only. A DBA must validate and apply it through a distinct
change pipeline after explicit write approval. Never grant the read identity extra
permissions. For server settings use the Azure specialist's required-control review,
then submit exact Azure configuration changes through the organization's deployment
pipeline. This repository intentionally contains no general write adapter.
