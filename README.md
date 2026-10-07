# LedgerGuard SQL

<img src="site/assets/ledgerguard-logo.png" alt="LedgerGuard SQL shield logo" width="128">

AEF Core orchestration with dedicated Azure and SQL specialists, pinned Microsoft
skills, local security skills, and an independently approved metadata-read broker.
Built for accounting teams and financial institutions. Public, tenant-neutral source. No credentials, tenant discovery or database access
happen during installation or planning.

**Status:** security-focused reference implementation, not an enterprise certification.
Live access defaults off. Deployment controls must pass [readiness checks](docs/security.md)
before production use. No write execution exists; schema changes are proposals only.

## Quick start (offline)

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --locked --all-extras
uv run azure-sql-agent verify
uv run azure-sql-agent graph
uv run azure-sql-agent plan --policy config/example.json --action schema_inventory
uv run azure-sql-agent propose --kind add_nullable_column --table Orders --name ReviewedAt --type 'datetime2(7)'
uv run azure-sql-agent learn --outcomes config/learning-example.json
uv run pytest
```

Run from the repository root, or pass `--root /path/to/reviewed/checkout` before the
subcommand. Installation from this checkout uses the vendored AEF dependency via uv;
a standalone wheel does not bundle AEF or skills. See [provenance](docs/provenance.md).

## Architecture

```mermaid
flowchart LR
  Task --> Orchestrator[AEF orchestration agent]
  Orchestrator --> Azure[Azure specialist]
  Azure --> SQL[SQL specialist]
  SQL --> Plan[Offline review plan]
  Plan --> Human[Human reviewer: external signing key]
  Human --> Broker[Isolated broker: signature + policy + durable nonce]
  Broker --> ARM[Fixed Azure GET posture checks]
  ARM --> DB[Private endpoint: fixed SQL metadata queries]
  DB --> Result[Bounded metadata to stdout]
```

The three AEF nodes are deterministic specialists. They select domain controls and
produce a plan; no LLM provider, shell tool or autonomous remediation is configured.
Microsoft skill examples are reference content, never authority to run commands.
[Agent contracts](agents/orchestrator.md) document ownership and tool boundaries.

## What works

- Schema and index inventory from `sys.*` catalog views, at most 100 metadata rows.
- Fixed Azure logical-server/database/Entra posture reads before SQL connection.
- Ed25519 approvals bound to target, exact SQL, policy, skill/source hashes, limits
  and output destination; five-minute maximum lifetime; persistent single-use nonce.
- Offline nullable-column and nonclustered-index proposals with rollback cautions.
- Azure logical-server management guidance and assessment. No VM/host administration.

No arbitrary SQL, business-row exports, DDL execution, general Azure commands,
credential listing, approval bypass or self-issued approvals. For live setup, follow
[operator guide](docs/operator-guide.md). Treat returned metadata as confidential.

## Showcase and governed learning

[Website](https://andrewgoodson.github.io/ledgerguard-sql/) · [Learning contract](docs/learning.md)

AEF consolidates repeated, de-identified control outcomes into review candidates.
No automatic promotion, policy modification or model retraining. Write execution
remains absent even after a read approval; future write capability needs a separate
security-reviewed implementation and explicit user authorization.

The static `site/` showcase uses no analytics, external scripts, credentials or cloud
connections. GitHub Pages deploys only that directory.

[Validation evidence](docs/validation.md) records the tested scope and remaining deployment gates.
