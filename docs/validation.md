# Validation evidence

Local validation, 2026-10-06:

- 88 automated tests pass, including approval tampering/expiry/replay, concurrent
  nonce reservation, fail-closed audit failures, source integrity, mocked Azure/ODBC
  transport boundaries, offline graph behavior and governed learning.
- Ruff checks pass. Runtime source-manifest integrity verification passes.
- pip-audit reports no known vulnerabilities in the indexed installed packages.
  It cannot audit vendored AEF Core against PyPI; the editable project is excluded.
- detect-secrets 1.5.0 findings reviewed: pinned commit/file hashes and an upstream
  example credential URL. No actual credential identified in the publication set.
- Baseline publication browser checked at 1440px desktop and 390px mobile: no horizontal overflow,
  working graph-selection interaction, zero console errors or warnings.
- Baseline publication only (before the new skills/compliance changes): a fresh clone installs with the locked
  dependencies, verifies source integrity and passes all 62 tests.
- Baseline [GitHub CI](https://github.com/AndrewGoodson/schemaiq/actions/runs/37557863804)
  passed on Linux for initial implementation commit `1997e7c`.
- Baseline [Pages deployment](https://github.com/AndrewGoodson/schemaiq/actions/runs/37557863787)
  succeeded. Public HTML, CSS, JavaScript and logo return HTTP 200 and match local
  source hashes. The hosted page loads in a browser without console errors.

These checks do not establish compliance certification or production readiness.
No live Azure tenant, private endpoint, database, managed identity, ODBC driver,
external approval service or deployment isolation was exercised. See
[security deployment gates](security.md) before enabling any live reads. Schema
write execution does not exist. Browser and scanner artifacts are local and are
not published; GitHub Actions provides repeatable code/test/dependency checks.

Current compliance tests cover complete synthetic nested/unselected XCCDF rule
inventory, duplicate IDs, unsafe XML/oversized input rejection, evidence hash binding,
HTML escaping, missing-evidence preservation and supplied failure reporting. These
are synthetic fixtures; no official full DISA benchmark or live database was tested.
Codex/Claude manual acceptance cases remain unexecuted.

Current site checked at 1440px and 390px: no horizontal overflow, functioning
compliance graph selection and no console messages. Synthetic HTML report rendered
with two rules, one supplied failure and one unassessed rule; injected markup stays
text. HTML output uses exclusive creation and owner-only filesystem permissions.
