"""Fixed offline hooks; never resolve shell commands or plugins from task content."""
from __future__ import annotations

DOMAIN_HOOKS = {
    "azure": {
        "assessment": ("private-network", "identity-boundary", "audit-and-recovery"),
        "azure-cli": ("command-scope", "identity-boundary", "no-live-cli"),
        "security": ("private-network", "identity-boundary", "encryption-and-audit"),
        "migration": ("target-service-support", "private-network", "recovery-and-cutover"),
        "schema": ("deployment-scope", "identity-boundary", "change-approval"),
        "performance": ("service-tier-and-limits", "resource-pressure", "cost-impact"),
        "maintenance": ("backup-and-restore", "availability", "maintenance-window"),
        "compliance": ("shared-responsibility", "provider-evidence", "customer-configuration"),
    },
    "sql": {
        "assessment": ("metadata-only", "least-privilege", "bounded-evidence"),
        "azure-cli": ("metadata-only", "sql-permissions", "bounded-evidence"),
        "security": ("sql-permissions", "data-classification", "sql-injection-review"),
        "migration": ("compatibility", "financial-reconciliation", "rollback-and-cutover"),
        "schema": ("integrity-and-constraints", "financial-reconciliation", "signed-write-plan"),
        "performance": ("query-plan-evidence", "blocking-and-indexes", "regression-validation"),
        "maintenance": ("restore-evidence", "statistics-and-integrity", "recovery-objectives"),
        "compliance": ("stig-applicability", "sql-permissions", "evidence-completeness"),
    },
}
COMPLIANCE_HOOKS = ("control-coverage", "evidence-provenance", "no-inherited-pass",
                    "finance-applicability", "remediation-owner")


def before_domain(domain: str, workflow: str, skill: str) -> dict:
    """Reject unsupported routing before dispatch. Skills are fixed local references."""
    from .workflows import select_workflow

    selected = select_workflow(workflow)
    if domain not in ("azure", "sql", "compliance"):
        raise ValueError("Unknown specialist")
    if skill != selected[f"{domain}_skill"]:
        raise ValueError("Specialist skill does not match the workflow")
    checks = (COMPLIANCE_HOOKS if domain == "compliance"
              else DOMAIN_HOOKS[domain][workflow])
    return {"phase": "before-domain", "domain": domain, "workflow": workflow,
            "selected_skill": skill, "mode": "offline", "live_tools": [],
            "required_reviews": list(checks)}


def after_domain(domain: str, result: dict, dispatch: dict) -> dict:
    """Fail the whole guide if a specialist changes scope or claims assessment."""
    if (dispatch["domain"] != domain or result.get("selected_skill") != dispatch["selected_skill"]
            or result.get("status") != "NOT_ASSESSED"
            or result.get("live_tools") != []):
        raise ValueError("Specialist output violates the offline review contract")
    return {"phase": "after-domain", "domain": domain,
            "contract": "validated", "assessment_status": "NOT_ASSESSED",
            "required_reviews": dispatch["required_reviews"]}
