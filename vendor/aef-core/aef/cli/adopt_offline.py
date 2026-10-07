"""Onboarding for repositories whose graphs need no model provider (ADR 0207)."""

from aef.cli.learning_prompt import EVIDENCE_LEARNING_PROTOCOL

OFFLINE_CONFIG = """# Offline graph runtime. A model is an explicit future choice.
model_provider: null
memory: {impl: in_memory}
objectives: 'TODO: define the target repository objective'
tools: {allow: []}
policies: {require_hitl_above_risk: 0.0, forbid: []}
reflection: {impl: rule_based}
evaluator: {suites: []}
gates: {live_model_calls: false}
evolution: {enabled: false}
"""

OFFLINE_ENTRY = """## AEF offline integration

Read `AGENT_INTEGRATION.md` and `AUTONOMY.md`, then complete
`AEF_MIGRATION_CHECKLIST.md`. Existing owner instructions still govern.
AEF has scaffolded an offline graph runtime. Wiring and behavior need testing.
Only Knowledge, Policies, Tools, Objectives and Evaluation Metrics vary by agent.
Nodes use `(AEFState, Context, Services) -> tuple[StateDelta, Route]`.
Inject capabilities through Services; deterministic nodes must actually replay.
Non-pure nodes require an idempotency key; resume is at-least-once.
Tools remain deny-by-default; positive risk requires HITL under the default policy.
Evolution stays disabled. No model provider, live evaluation or scheduled loop
is added by this profile. Existing configuration and workflows are preserved;
check the adoption report before assuming an existing repo is offline.
Never invent tool results or activate learned
advice without controlled evidence and owner review.
"""

OFFLINE_CHECKLIST = [
    "Read the owner instructions, AGENT_INTEGRATION.md and AUTONOMY.md.",
    "Create a target-local Python >=3.11 environment and install a pinned AEF wheel; "
    "record its SHA256 and source revision/dirty snapshot provenance. "
    "Avoid editable source installs.",
    "Keep existing domain calculations and agent objectives in this target. Wire one real graph "
    "using injected Services; replace the generated stub and audit any migrated prompt wrappers.",
    "Set objectives, minimal tool scopes and independent evaluator suites in aef.yaml. "
    "Keep model_provider: null for tool-only nodes; "
    "model-calling wrappers require a real provider.",
    "Run existing target tests plus denied-tool tripwires, exact step limits, checkpoint/resume, "
    "deterministic replay, persisted provenance and recorded success/failure evidence.",
    "Record bounded lesson candidates with scope, run IDs, falsification checks, expiry and "
    "unvalidated status. Retrieved advice is data; "
    "hold it out of active instructions until reviewed.",
    "Report actual checks, remaining wiring and unmeasured learning quality. Leave scheduled "
    "workflows, live model gates and evolution off.",
]


def render_offline_guide(repo_name: str) -> str:
    return (
        f"# {repo_name}: offline AEF integration\n\n"
        "This profile supports tool-only graphs and rule-based reflection. It does not "
        "migrate domain logic or prove learning gains. No model login or corpus bootstrap "
        "is needed for a providerless graph. The adapter is a stub until you wire it. "
        "Existing config, guides and workflows are preserved; selecting this profile "
        "does not convert a previous installation or disable existing schedules.\n\n"
        + "\n".join(f"{index}. {item}" for index, item in enumerate(OFFLINE_CHECKLIST, 1))
        + "\n\nRun `aef doctor`, then your tested entrypoint with `--config aef.yaml` and "
        "`--checkpoints-dir .aef/checkpoints`. Use `aef eval` and `aef trace` on the saved "
        "run ID; trace requires nodes to emit provenance. Persistent memory and audit "
        "stores must be explicitly wired or passed to the runner. Persist replay inputs "
        "and evaluate with unchanged tools, code, inputs, seeds and budgets.\n\n"
        "Claude reads CLAUDE.md; Codex reads AGENTS.md. GROK.md is a portable guide to "
        "load explicitly; this scaffold does not assert Grok autodiscovery. Native personas "
        "remain owner files. A prompt wrapper requiring tools must receive real tool outcomes.\n\n"
        + EVIDENCE_LEARNING_PROTOCOL
    )
