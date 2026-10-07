"""OpenTelemetry GenAI semantic convention attribute keys, pinned as string
constants rather than imported from `opentelemetry-semantic-conventions`.

Deviation from a literal reading of report §17: that package ships GenAI
attributes under an explicitly *incubating* namespace
(`opentelemetry.semconv._incubating`) with no stability guarantee — a
kernel-adjacent observability module churning on every OTel point release is
a worse outcome than pinning today's stable attribute names ourselves and
updating them deliberately when the spec stabilizes. See docs/adr/0008.
Values below match the GenAI semconv spec as of 2026-08 (the conventions
remain experimental/incubating — no stable release — so pinning per ADR
0008 still holds). Only the two attributes AEF actually emits today
(`gen_ai.response.model`, `gen_ai.usage.output_tokens`) are load-bearing;
the rest are forward-declared for the reasoning plane. `gen_ai.provider.name`
is the current name for what was `gen_ai.system` (renamed in semconv
v1.37.0, Aug 2025) — updated here per ADR 0008's "update deliberately when
the spec moves."
"""

# Renamed from `gen_ai.system` in semconv v1.37.0 (Aug 2025).
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"

# AEF-specific span attributes (not part of the GenAI semconv spec, namespaced
# under `aef.*` to avoid colliding with future upstream additions).
AEF_RUN_ID = "aef.run_id"
AEF_AGENT_ID = "aef.agent_id"
AEF_NODE_ID = "aef.node_id"
AEF_GRAPH_VERSION = "aef.graph_version"
AEF_NODE_DETERMINISTIC = "aef.node.deterministic"
AEF_NODE_SIDE_EFFECTS = "aef.node.side_effects"
AEF_NODE_TELEMETRY_TAGS = "aef.node.telemetry_tags"
AEF_CHECKPOINT_SEQ = "aef.checkpoint_seq"
AEF_EDGE_REQUIRES_DETERMINISTIC_FALLBACK = "aef.edge.requires_deterministic_fallback"
