"""Four dedicated deterministic AEF agents, with a separate security broker."""
from __future__ import annotations

from uuid import uuid4

from aef.kernel import END, Edge, Graph, GraphExecutor, Node, Services
from aef.state import AEFState, StateDelta

from .compliance import compliance_review
from .models import Policy, make_plan
from .workflows import select_workflow


def build_graph(workflow: str = "assessment"):
    selection = select_workflow(workflow)

    def orchestrator(state, context, services):
        return StateDelta(working_memory={"mode": "offline", "live_access": "requires approval",
                                           "workflow_guide": selection}), "azure"

    def azure(state, context, services):
        return StateDelta(working_memory={"azure_review": {
            "selected_skill": selection["azure_skill"],
            "skills": ["azure-security-assessment", "azure-compliance", "azure-diagnostics"],
            "required": ["private endpoint", "public network disabled", "Entra only", "TLS 1.2",
                         "scoped RBAC", "immutable audit export", "recovery exercise"],
            "deployment_evidence": "NOT VERIFIED"}}), "sql"

    def sql(state, context, services):
        return StateDelta(working_memory={"sql_review": {
            "selected_skill": selection["sql_skill"],
            "skills": ["azure-sql-readonly", "sql-change-review", "entra-id-auth",
                       "prevent-sql-injection", "schema-migrations-safely", "diagnose-slow-query"],
            "write_execution": "disabled", "read_scope": "bounded system catalog metadata",
            "principal_permissions": "DBA verification required"}}), "compliance"

    def compliance(state, context, services):
        return StateDelta(working_memory={"compliance_review": compliance_review()}), END

    nodes = {name: Node(name, "1.1.0", fn, deterministic=True)
             for name, fn in (("orchestrator", orchestrator), ("azure", azure), ("sql", sql),
                              ("compliance", compliance))}
    return Graph("azure-sql-orchestration", "1.1.0", nodes,
                 [Edge("orchestrator", "azure"), Edge("azure", "sql"),
                  Edge("sql", "compliance")], "orchestrator")


def assess(policy: Policy, action: str, skills_sha256: str):
    result = GraphExecutor(build_graph().compile(), Services()).run(
        AEFState(run_id=str(uuid4()), agent_id="azure-sql-orchestrator",
                 objective="Prepare an approval-gated metadata assessment"))
    return {"assessment": result.final_state.working_memory,
            "plan": make_plan(policy, action, skills_sha256).model_dump(mode="json")}


def guide(workflow: str):
    result = GraphExecutor(build_graph(workflow).compile(), Services()).run(
        AEFState(run_id=str(uuid4()), agent_id="azure-sql-orchestrator",
                 objective=f"Prepare offline {workflow} workflow guidance"))
    return result.final_state.working_memory
