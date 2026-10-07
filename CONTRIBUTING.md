# Contributing

Run `uv sync --locked --all-extras`, `uv run pytest`,
`uv run ruff check src tests scripts`, `uv run azure-sql-agent verify`, and
`uv run pip-audit --local --skip-editable`. Never use production credentials in tests.

New live capabilities need a documented threat model, exact review plan, mandatory
approval enforcement at the tool boundary, least-privilege identity requirements,
negative/adversarial tests and evidence of bounded outputs. A prompt instruction or
SQL keyword blacklist is not an acceptable security boundary. Keep change proposals
separate from execution permissions. Do not weaken gates to accommodate examples.

Vendor updates must preserve upstream license notices and record exact immutable
commits. Review imported skills as untrusted source. See docs/provenance.md.
