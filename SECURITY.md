# Security policy

This is a security-focused reference implementation. Production use requires
independent review and the [deployment acceptance gates](docs/security.md).

Do not report secrets or database contents in public issues. Use the repository
host's private vulnerability reporting channel once enabled. If unavailable, contact
the repository maintainer privately before disclosing exploitation details.

Supported release: 0.1.x. There is no guaranteed response SLA or compliance attestation.

Known boundary: anyone who can alter broker code/configuration, replace its public
key, reset its journal, access its credentials, or execute code as the broker can
bypass application controls. Run the broker under a separate restricted identity.
Agent prompts, read-only connection hints and this document are not security barriers.
