"""Fixed offline hooks; never resolve shell commands or plugins from task content."""
from __future__ import annotations

DOMAIN_HOOKS = {
    "azure": {
        "nist-tls": ("tls-endpoint-scope", "provider-tls-assurance", "protocol-configuration"),
        "assessment": ("private-network", "identity-boundary", "audit-and-recovery"),
        "azure-cli": ("command-scope", "identity-boundary", "no-live-cli"),
        "security": ("private-network", "identity-boundary", "encryption-and-audit"),
        "migration": ("source-and-target-service-scope", "target-service-support",
                      "source-and-target-network-identity", "provider-and-customer-control-evidence",
                      "recovery-and-cutover"),
        "schema": ("deployment-scope", "identity-boundary", "change-approval"),
        "performance": ("service-tier-and-limits", "resource-pressure", "cost-impact"),
        "maintenance": ("backup-and-restore", "availability", "maintenance-window"),
        "compliance": ("shared-responsibility", "provider-evidence", "customer-configuration"),
    },
    "sql": {
        "nist-tls": ("tds-client-inventory", "certificate-validation", "negotiated-crypto-evidence"),
        "assessment": ("metadata-only", "least-privilege", "bounded-evidence"),
        "azure-cli": ("metadata-only", "sql-permissions", "bounded-evidence"),
        "security": ("sql-permissions", "data-classification", "sql-injection-review"),
        "migration": ("source-and-target-compatibility", "schema-dependency-inventory",
                      "financial-reconciliation", "rehearsal-and-restore-evidence",
                      "rollback-and-cutover", "postmigration-regression-evidence"),
        "schema": ("integrity-and-constraints", "financial-reconciliation", "signed-write-plan"),
        "performance": ("query-plan-evidence", "blocking-and-indexes", "regression-validation"),
        "maintenance": ("restore-evidence", "statistics-and-integrity", "recovery-objectives"),
        "compliance": ("stig-applicability", "sql-permissions", "evidence-completeness"),
    },
}
COMPLIANCE_HOOKS = ("control-coverage", "evidence-provenance", "no-inherited-pass",
                    "finance-applicability", "remediation-owner")
MIGRATION_COMPLIANCE_HOOKS = (
    "source-and-target-stig-applicability", "every-stig-rule-register",
    "pre-and-post-control-drift", "go-no-go-owner-approval", "postmigration-evidence-review",
)

# These describe this repository's supported boundary, not permission to act.
# Reject alternative execution claims and unknown output fields at the join.
REVIEW_BOUNDARIES = {
    "azure": {"deployment_evidence": "NOT VERIFIED"},
    "sql": {"write_execution": "separate exact signed approval; nullable-column additions only",
            "read_scope": "bounded system catalog metadata",
            "principal_permissions": "DBA verification required"},
    "compliance": {"compliance_claim": False,
                   "live_collection": "disabled; existing approved catalog reads are insufficient"},
}


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
    if domain == "compliance" and workflow == "nist-tls":
        checks += ("nist-800-52-clause-applicability", "server-and-client-evidence",
                   "no-tls-floor-only-pass")
    elif domain == "compliance" and workflow == "migration":
        checks += MIGRATION_COMPLIANCE_HOOKS
    return {"phase": "before-domain", "domain": domain, "workflow": workflow,
            "selected_skill": skill, "mode": "offline", "live_tools": [],
            "required_reviews": list(checks)}


def after_domain(domain: str, result: dict, dispatch: dict) -> dict:
    """Validate declarations, not real evidence or behavior of an external host."""
    from .workflows import select_workflow

    try:
        expected_dispatch = before_domain(domain, dispatch["workflow"], dispatch["selected_skill"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid offline review dispatch") from exc
    if dispatch != expected_dispatch:
        raise ValueError("Invalid offline review dispatch")

    expected = {"selected_skill": dispatch["selected_skill"], "status": "NOT_ASSESSED",
                "live_tools": [], "required_reviews": dispatch["required_reviews"],
                **REVIEW_BOUNDARIES[domain]}
    if dispatch["workflow"] == "nist-tls":
        selection = select_workflow("nist-tls")
        expected.update({key: selection[key] for key in
                         ("assessment_catalog", "assessment_guide", "assessment_profile")})
    allowed = set(expected) | ({"required"} if domain in ("azure", "compliance") else set())
    if (not isinstance(result, dict) or set(result) != allowed
            or any(type(result.get(key)) is not type(value) or result.get(key) != value
                   for key, value in expected.items())):
        raise ValueError("Specialist output violates the offline review contract")
    if "required" in allowed and (
            not isinstance(result["required"], list) or not result["required"]
            or any(not isinstance(item, str) or not item.strip() for item in result["required"])):
        raise ValueError("Specialist output violates the offline review contract")
    return {"phase": "after-domain", "domain": domain,
            "contract": "validated", "assessment_status": "NOT_ASSESSED",
            "required_reviews": dispatch["required_reviews"]}
