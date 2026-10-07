# Orchestration agent

Runtime: `azure_sql_agents.orchestration.build_graph`, entry `orchestrator`.
Owns routing, target scope, source integrity and review plan. Delegates Azure posture
requirements to `azure`, then SQL metadata/change review to `sql`.

Skills: [orchestration-security](../skills/local/orchestration-security/SKILL.md).
Tools: offline plan/proposal only. Approval requests are data, not permissions.
The separately operated `ReadBroker` is the only live tool contract. It validates
approval itself even when invoked directly; no AEF HITL flag can bypass it.

A future LLM frontend may read these contracts and skill references, but must have no
Azure/SQL credentials, signing key, broker shell, policy edit or audit edit capability.
No self-modifying framework, learning-floor or model-refusal changes are introduced.
