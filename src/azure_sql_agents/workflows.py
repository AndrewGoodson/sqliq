"""Offline workflow selection. No tool execution or credential acquisition."""
from __future__ import annotations

WORKFLOWS = {
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
    return {
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
