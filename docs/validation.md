# Validation evidence

## STIG and migration update, 2026-10-07

- 176 automated tests pass. Ruff and source-manifest verification pass.
- Migration routing dispatches Azure, SQL and compliance reviews in parallel.
  Tests reject altered dispatches, missing required reviews, changed boundaries,
  unsupported assessment verdicts and undeclared output fields. These hooks
  validate offline declarations, not external agent behavior or migration readiness.
- The pinned SQL Server 2022 package contains 79 instance and 23 database rules.
  Sample HTML/PDF reports include all 102 rules, with source references, checks and
  remediation. All are NOT_ASSESSED; no customer evidence was supplied.
- DISA description fields render as readable labels. Tests preserve original
  catalog text and safely retain unknown/malformed content; embedded markup stays
  text in both report formats. Source pins are unchanged.
- Desktop and 390px/320px mobile site checks confirm no horizontal overflow,
  functioning financial-exposure disclosures, keyboard operation and specialist
  selection. The 258-page sample PDF was checked by text inventory and visual
  inspection of its cover, first control and final page; not every page was
  visually inspected.

No live database assessment, migration, restore, cutover or financial reconciliation
was performed. SQLIQ supplies planning contracts and operator-reported evidence
reports; these checks do not establish compliance or production readiness.

## Historical baseline, 2026-10-06

- 112 automated tests pass, including approval tampering/expiry/replay, concurrent
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
- Baseline [GitHub CI](https://github.com/AndrewGoodson/sqliq/actions/runs/37557863804)
  passed on Linux for initial implementation commit `1997e7c`.
- Baseline [Pages deployment](https://github.com/AndrewGoodson/sqliq/actions/runs/37557863787)
  succeeded. Public HTML, CSS, JavaScript and logo return HTTP 200 and match local
  source hashes. The hosted page loads in a browser without console errors.

These checks do not establish compliance certification or production readiness.
No live Azure tenant, private endpoint, database, managed identity, ODBC driver,
external approval service or deployment isolation was exercised. See
[security deployment gates](security.md) before enabling any live reads. Only the narrow signed nullable-column write path exists; live writes remain unverified. Browser and scanner artifacts are local and are
not published; GitHub Actions provides repeatable code/test/dependency checks.

Baseline compliance tests covered complete synthetic nested/unselected XCCDF rule
inventory, duplicate IDs, unsafe XML/oversized input rejection, evidence hash binding,
HTML escaping, missing-evidence preservation and supplied failure reporting. These
were synthetic fixtures; no official full DISA benchmark or live database was tested
at that baseline.
Codex/Claude manual acceptance cases remain unexecuted.

Baseline site checked at 1440px and 390px: no horizontal overflow, functioning
compliance graph selection and no console messages. Synthetic HTML report rendered
with two rules, one supplied failure and one unassessed rule; injected markup stays
text. HTML output uses exclusive creation and owner-only filesystem permissions.

The IT/board PDF is four pages, visually inspected after co-branding changes. Every
page includes SQLIQ and optional company artwork. Automated checks verify embedded
logos, bounded local raster inputs, organization labels and offline generation.
Write tests cover separate approval audience, signed-plan tampering, expiry, replay,
disabled policy, consumed approvals on failure and transactional connection cleanup.
