# Validation evidence

Local validation, 2026-10-06:

- 62 automated tests pass, including approval tampering/expiry/replay, concurrent
  nonce reservation, fail-closed audit failures, source integrity, mocked Azure/ODBC
  transport boundaries, offline graph behavior and governed learning.
- Ruff checks pass. Runtime source-manifest integrity verification passes.
- pip-audit reports no known vulnerabilities in the indexed installed packages.
  It cannot audit vendored AEF Core against PyPI; the editable project is excluded.
- detect-secrets 1.5.0 findings reviewed: pinned commit/file hashes and an upstream
  example credential URL. No actual credential identified in the publication set.
- Browser checked at 1440px desktop and 390px mobile: no horizontal overflow,
  working graph-selection interaction, zero console errors or warnings.
- Earlier clean-copy offline installation and the then-current 56-test suite passed;
  the six learning tests were added afterward and pass in the working checkout.

These checks do not establish compliance certification or production readiness.
No live Azure tenant, private endpoint, database, managed identity, ODBC driver,
external approval service or deployment isolation was exercised. See
[security deployment gates](security.md) before enabling any live reads. Schema
write execution does not exist. Browser and scanner artifacts are local and are
not published; GitHub Actions provides repeatable code/test/dependency checks.
