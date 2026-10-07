"""Bounded parallel offline AEF agents, with a separate security broker."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from aef.kernel import END, Edge, Graph, GraphExecutor, Node, Services
from aef.state import AEFState, StateDelta

from .compliance import compliance_review
from .domain_hooks import after_domain, before_domain
from .models import Policy, make_plan
from .workflows import select_workflow


def _run_specialist(domain: str, selection: dict) -> dict:
    skill = selection[f"{domain}_skill"]
    dispatch = before_domain(domain, selection["workflow"], skill)

    def review(state, context, services):
        detail = {"selected_skill": skill, "status": "NOT_ASSESSED", "live_tools": [],
                  "required_reviews": dispatch["required_reviews"]}
        if domain == "azure":
            detail.update({"required": ["private endpoint", "public network disabled",
                                       "Entra only", "TLS 1.2", "scoped RBAC",
                                       "immutable audit export", "recovery exercise"],
                           "deployment_evidence": "NOT VERIFIED"})
        elif domain == "sql":
            detail.update({"write_execution": "separate exact signed approval; nullable-column additions only",
                           "read_scope": "bounded system catalog metadata",
                           "principal_permissions": "DBA verification required"})
        else:
            detail.update(compliance_review())
        if selection['workflow'] == 'nist-tls':
            detail.update(assessment_catalog=selection['assessment_catalog'],
                          assessment_guide=selection['assessment_guide'],
                          assessment_profile=selection['assessment_profile'])
            if domain == 'azure':
                detail['required'] = ['exact SQL service and endpoint scope',
                                      'provider TLS assurance and customer configuration',
                                      'protocol support and negotiation; TLS floor is insufficient']
            elif domain == 'compliance':
                detail['required'] = ['NIST SP 800-52 Rev. 2 source-clause applicability',
                                      'server sections 3.1-3.8 and client sections 4.1-4.8',
                                      'appendices C and D applicability',
                                      'evidence, exceptions, remediation, owner and independent review']
        return StateDelta(working_memory={"review": detail}), END

    graph = Graph(f"{domain}-specialist", "1.2.0",
                  {domain: Node(domain, "1.2.0", review, deterministic=True)}, [], domain)
    result = GraphExecutor(graph.compile(), Services()).run(
        AEFState(run_id=str(uuid4()), agent_id=f"{domain}-specialist",
                 objective=f"Offline {selection['workflow']} review contract"))
    detail = result.final_state.working_memory["review"]
    hook = after_domain(domain, detail, dispatch)
    return {"review": detail, "hooks": [dispatch, hook]}


def _parallel_reviews(selection: dict) -> dict:
    # Pinned AEF does not implement fan-out. Each worker owns an independent AEF
    # graph/state/services; only this fixed offline function is dispatched.
    domains = ("azure", "sql", "compliance")
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="sqliq-offline") as pool:
        futures = {domain: pool.submit(_run_specialist, domain, selection.copy())
                   for domain in domains}
        # Fixed order makes the join deterministic, regardless of finish order.
        reviews = {domain: futures[domain].result() for domain in domains}
    return {**{f"{domain}_review": value["review"] for domain, value in reviews.items()},
            "domain_hooks": {domain: value["hooks"] for domain, value in reviews.items()},
            "parallel_execution": {"mode": "offline", "max_workers": 3,
                                   "domains": list(domains), "join": "all-required",
                                   "live_tools": [], "model_calls": 0}}


def build_graph(workflow: str = "assessment"):
    selection = select_workflow(workflow)

    def orchestrator(state, context, services):
        return StateDelta(working_memory={"mode": "offline", "live_access": "requires approval",
                                           "workflow_guide": selection}), "specialists"

    def specialists(state, context, services):
        return StateDelta(working_memory=_parallel_reviews(selection)), END

    nodes = {name: Node(name, "1.2.0", fn, deterministic=True)
             for name, fn in (("orchestrator", orchestrator), ("specialists", specialists))}
    return Graph("azure-sql-orchestration", "1.2.0", nodes,
                 [Edge("orchestrator", "specialists")], "orchestrator")


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
