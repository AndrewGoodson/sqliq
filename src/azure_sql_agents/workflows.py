"""Offline workflow selection. No tool execution or credential acquisition."""
from __future__ import annotations

WORKFLOWS = {
    "nist-tls": ("azure-security-assessment", "sql-compliance-review"),
    "compliance": ("azure-security-assessment", "azure-sql-readonly"),
    "assessment": ("azure-security-assessment", "azure-sql-readonly"),
    "azure-cli": ("azure-cli-assessment", "azure-sql-readonly"),
    "migration": ("azure-security-assessment", "sql-migration-planning"),
    "schema": ("azure-security-assessment", "sql-schema-design"),
    "performance": ("azure-security-assessment", "sql-performance-review"),
    "security": ("azure-security-assessment", "azure-sql-readonly"),
    "maintenance": ("azure-security-assessment", "sql-maintenance-review"),
}


def select_workflow(name: str) -> dict:
    azure, sql = WORKFLOWS[name]  # Unknown workflows fail closed.
    selected = {
        "workflow": name,
        "orchestrator_skill": "skills/local/orchestration-security/SKILL.md",
        "azure_skill": f"skills/local/{azure}/SKILL.md",
        "sql_skill": f"skills/local/{sql}/SKILL.md",
        "compliance_skill": "skills/local/sql-compliance-review/SKILL.md",
        "review_contract": "skills/local/references/review-contract.md",
        "microsoft_guidance": "skills/local/references/microsoft-learn.md",
        "required_output": ["scope and applicability", "evidence and source URLs",
                            "findings and unknowns", "proposed changes", "validation",
                            "rollback limits", "owner and approval requirements"],
        "execution": "offline guidance only; no Azure CLI or arbitrary SQL execution",
    }
    if name == "nist-tls":
        selected.update(assessment_catalog="nist-800-52",
                        assessment_guide="docs/nist-tls-review.md",
                        assessment_profile="compliance/nist-800-52-review.json")
    elif name == "migration":
        selected["migration_contract"] = {
            "mode": "offline planning; no migration execution",
            "scope": ["source and target service, version, tier and compatibility",
                      "data classification, financial obligations and accountable owners",
                      "dependencies, client connections, downtime, RPO and RTO"],
            "stig_register": {
                "bundled_catalog": "sql-2022-stigs",
                "applicability": (
                    "Review the exact product benchmark and release for source and target; "
                    "SQL Server 2022 rules are not automatically applicable to Azure SQL."),
                "coverage": (
                    "Account for every rule, including profile-excluded rules, separately "
                    "for source and target; retain justified non-applicable rules."),
                "required_fields": ["rule ID and benchmark release/hash", "source or target",
                                    "severity and applicability rationale",
                                    "provider, customer or shared responsibility",
                                    "dated evidence and assessment method", "status",
                                    "finding and remediation", "owner and reviewer",
                                    "exception approval and expiry when applicable"],
                "status_rule": (
                    "Missing evidence stays NOT_ASSESSED. PASS requires full scoped check "
                    "evidence; NOT_APPLICABLE needs technical justification and review. "
                    "Provider responsibility alone is not evidence of a pass."),
            },
            "stages": [
                {"stage": "baseline", "required_evidence": [
                    "source and target STIG registers and service-specific provider assurance",
                    "customer identity, network, encryption, audit and recovery controls",
                    "NIST SP 800-52 server/client TLS evidence kept distinct from STIG results"]},
                {"stage": "rehearsal", "required_evidence": [
                    "compatibility, schema, dependencies and supported migration-path review",
                    "operator-supplied restore and migration rehearsal results",
                    "counts, keys, constraints and financial control totals without business rows",
                    "rollback procedure, restore point, deadline and recovery objectives"]},
                {"stage": "go-no-go", "required_evidence": [
                    "unresolved control gaps, blockers and independently approved exceptions",
                    "named DBA, security and financial control owner sign-off",
                    "exact operator cutover plan and separate change approval"]},
                {"stage": "postmigration", "required_evidence": [
                    "target STIG reassessment with dated evidence and reviewer",
                    "before/after control drift and resolved or newly introduced findings",
                    "data and financial reconciliation, application and performance validation",
                    "rollback decision, residual risk ownership and IT/board report"]},
            ],
            "execution_boundary": (
                "This guide defines required reviews; it does not evaluate evidence or approve "
                "cutover. Migration, restore, data movement and failover are unsupported broker "
                "operations. Keep them as proposals for a separately governed operator. "
                "Supported reads and nullable-column writes still require their separate, "
                "exact externally signed approvals."),
        }
    return selected
