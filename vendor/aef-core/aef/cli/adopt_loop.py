"""The self-rewiring loop kit `aef adopt` emits into a target repo.

Separate from `adopt.py` because these files carry a different contract: the
onboarding kit tells an agent how to work in the repo, while this kit tells
the *owner* what they must supply before the loop can approve anything.

Every document here **leads with what does not work yet**. An adopting repo
whose agents start producing candidates against an empty corpus will see
every one rejected, and that reads as "the loop is broken" rather than "the
loop has nothing to judge against" — which is the correct reading and the one
that tells the owner what to do next.
"""

from __future__ import annotations

# Interpolated, never spelled out: a generated document that names a directory
# the harness does not enforce, or an output path nothing writes to, is the
# next drift (ADR 0091, ADR 0143).
from aef.cli.migrate import DEFAULT_MIGRATED_OUT
from aef.harness.zones import DEFAULT_AGENT_ROOT


def _module_path(repo_relative: str) -> str:
    """`agents/migrated/graph.py` -> `agents.migrated.graph`.

    Computed rather than written beside the path, because the two drifting
    apart is how a document ends up telling an adopter to import a module
    nothing writes (ADR 0091).
    """
    return repo_relative.removesuffix(".py").replace("/", ".")


def render_prompt_repo_sequence(prompt_agents: int = 0) -> str:
    """The prompt-file adopter's sequence, and the sentence that makes it
    honest (ADR 0153).

    Every eligible repo in the `UPGRADE_LOOP.md` survey had zero model-SDK
    call sites and between three and twenty-six `.claude/agents/*.md` files.
    The generated kit described the other shape only, so the adopter whose
    agents are prompts had a document about a repo they do not have.
    """
    path = f"{DEFAULT_MIGRATED_OUT.rsplit('/', 1)[0]}/<agent>/graph.py"
    # The same shape under a WIDENED root. Derived from the same constant, so
    # a change to migrate's default moves both (ADR 0153's rule).
    wide_root = ".claude/agents"
    wide_path = f"{wide_root}/{path.split('/', 1)[1]}"
    persona = f"{wide_root}/<agent>.md"
    counted = (
        f"This repo has **{prompt_agents}** of them."
        if prompt_agents
        else "If that is this repo, this section is your sequence."
    )
    return f"""## If your agents are prompt files, not Python

`.claude/agents/*.md`, skills, `AGENTS.md`, a `.codex/` — agents that a coding
harness runs, with no `anthropic`/`openai` call site anywhere. {counted}

There is nothing to convert, and `aef migrate` does not look for one: it
registers each agent file (recursively — nested directories included) as **its
own graph**, four nodes wired `retrieve -> prompt_agent -> reflect ->
consolidate -> END`, `graph_id` = the agent's name. The `prompt_agent` node
reads the persona **at execution time**, so a lesson appended to the `.md`
changes the next run with no regeneration step in between.

**Which harness runs the call is `model_provider.impl` in `aef.yaml`, and
what containment you get is that provider's answer — not migrate's.** The
persona's own `tools:` frontmatter is parsed, reported and **never obeyed**
under all five; beyond that they differ, and the differences were measured
rather than read off a `--help` (aef-core ADR 0169):

- **`claude_code`** (the default) — persona in the **system** message.
  `--tools ""` (documented as "disable all tools"), `--max-turns 1`,
  `--safe-mode`, an empty strict MCP config. The Claude Code login is the
  credential, so there is no API key anywhere. Reproduced end to end.
- **`codex`** — persona in the **user turn**, because there is no system flag.
  `--sandbox read-only` and nothing else: no `--tools`, no `--max-turns`.
  Not reproduced live on the box that wrote this.
- **`grok`** — persona in the **system** message, and its `--tools ""`
  **suppresses nothing** on 1.0.5: the same argv read a planted file when one
  more turn was allowed. There is no `--safe-mode`, and even with
  `--cwd <an empty directory>` about **17.9k tokens of the operator's own
  session still reach the model**, with no flag that stops it.
- **`command`** — persona in the **system** message if your argv template has
  a `{{system}}` slot and in the **user turn** if it does not. Its isolation is
  whatever you assert in `isolation:`, recorded as *your* assertion and never
  verified against your binary; omit it and nothing is claimed but the
  channel. The template's own flags are deliberately not read as evidence —
  `--tools ""` means opposite things on the two CLIs above.
- **`anthropic`** — persona in the **system** parameter. Structural: no
  `tools` parameter is sent, one request and one response, no project
  discovery, no subprocess.

**GitHub Copilot's CLI is `impl: command`, configured by you when you install
it** — this repo ships no guess about its flags, because a flag's shape is not
a flag's value (aef-core ADR 0150), and it is the path every harness released
after this file was written takes.

Every run writes what it actually got to
`working_memory["prompt_agent__containment"]`, and adds a
`prompt_agent.persona_in_user_turn` error when the persona went in the user
turn. Read that rather than this list when you want to know what one run did.

**Which file the loop may edit is a decision you have to make, and the default
is the narrow one.** The generated GRAPH is Zone A; the persona `.md` it reads
is Zone C, so a candidate that edits the prompt itself is rejected by G0 until
you widen the agent root. `aef migrate --agent-root {wide_root}` is that
opt-in, per repo, and its report states in words what it adds to the loop's
blast radius. Widening it means agent-authored diffs to the files your coding
harness loads on every session — decide that deliberately, not to make a
cycle produce something.

The whole day, for that shape:

```
aef adopt --dir .
aef migrate --dir . --agent-root {wide_root}
aef loop bootstrap {wide_path} --corpus corpus \\
    --inputs inputs.json --state ~/.aef-loop-state \\
    --memory ~/.aef-loop-state/memory.jsonl --config aef.yaml
aef loop bless --repo . --state ~/.aef-loop-state --agent-root {wide_root} \\
    --agent-path {wide_path} --graph-id <agent-name>
aef loop doctor --repo . --state ~/.aef-loop-state --corpus corpus \\
    --agent-root {wide_root} --agent-path {persona}
aef loop cycle --repo . --state ~/.aef-loop-state --workdir "$(mktemp -d)/run" \\
    --corpus corpus --entrypoint {wide_path}:build_graph \\
    --graph-id <agent-name> --proposer rule_based_prompt \\
    --agent-root {wide_root} --agent-path {persona} \\
    --memory ~/.aef-loop-state/memory.jsonl --config aef.yaml \\
    --cassette-miss live --build-command "<your green bar>"
```

**Under a widened root there is no dotted module name**, because `{wide_root}`
is not an importable package — which is why every graph reference above is a
**file path**: `{wide_path}`, and `--entrypoint {wide_path}:build_graph`.
Every `aef loop` subcommand and `aef run` accept that form alongside the two
dotted ones (aef-core ADR 0176/0177). `--agent-path` names the **persona**
itself in the last two steps, and the readiness report resolves it to that
persona's generated graph through migrate's own mapping, saying which file it
read (aef-core ADR 0178).

**`--proposer rule_based_prompt` is the one that can write a prompt
candidate.** The default `rule_based` mutates module-level numeric constants,
and a persona has none; run it here and the cycle says so and names this one.
The prompt proposer appends a single consolidated lesson as a bullet under a
`## Lessons (aef)` section at the end of the persona, computed from your
recorded failures with **no model call**, carrying its own provenance
(`<!-- aef sig=... runs=2 -->`), and it never rewrites a bullet you wrote.

**`--graph-id` names your corpus's graph, and it has to agree with what you
blessed.** `aef loop bless` writes the baseline under an archive key; the
proposer admits failure records by the corpus's `graph_id`. If the two
disagree the cycle prints a warning naming the re-bless that fixes it and then
drops every record as another graph's — which looks exactly like having no
evidence.

**`--config` on bootstrap, and `--cassette-miss live --config` on the cycle,
are not optional here — and the reason is a property of prompts, not a
preference.** A cassette is keyed on the request that was recorded. Change the
prompt and every request changes, so every request is a cassette MISS; under
the default `--cassette-miss fail` the candidate's nodes fail and it scores 0,
which is a rejection that measured nothing and reads exactly like a real one.

**So every gate pass of a prompt candidate is a LIVE pass, and it spends real
model calls** — a prompt candidate is gated live or not at all. The bar is the
live noise floor: two runs of an *unchanged* prompt do not score identically, so a
candidate beating the incumbent by less than that spread has not been shown to
beat it at all. On aef-core's own six-scenario suite that floor was measured at
**mean 0.7639, spread 0.1666** — wide enough that only a whole scenario
flipping, consistently, is visible in the mean. Start on one scenario, not
forty, and compare per scenario rather than on the mean.

**Scoring a prompt change costs real model calls, and it is OFF until you say
otherwise.** `--cassette-miss live` runs your candidate's model calls under
**your** harness login, inside the sandbox that executes agent-written code —
so it is off by default. Set

```yaml
gates:
  live_model_calls: true
```

in `aef.yaml` to allow it, knowing a candidate's code can then spend your
quota. It is read from the **base ref**, like `policies` and `tools.allow`, so
a candidate cannot switch it on in its own branch and hand itself your login.

Until you do, `--cassette-miss live` is **refused by name** rather than
quietly rejecting every candidate — which is what used to happen, and it did
not look like a defect:

```
error: live gating is off in aef.yaml; a prompt candidate cannot be scored
from a cassette — set gates.live_model_calls: true, which lets a candidate's
code spend your harness quota. Until then `--cassette-miss live` is refused
rather than run: without the opt-in the gate's worker inherits no login,
every miss fails, and G2 reports that as 'previously-passing scenario(s) no
longer pass' — an artifact of the environment rather than a judgement of the
prompt (ADR 0158, ADR 0181).
```

Every gate run records `live_model_calls` in the ledger, so you can always see
which passes spent it.

**A lesson computed from a check does not contain the check's answer, and
that costs something.** The bullet names which output was graded and how, and
omits the value the check required — deliberately, because a bullet carrying
the literal expected string is teaching to the test. Measured consequence:
against a `contains` check, candidate and incumbent scored identically, so the
lesson changed nothing. That is the honest price of feedback at all, and it
argues for checks that describe outcomes rather than tokens.
"""


def render_loop_md(repo_name: str, prompt_agents: int = 0) -> str:
    return f"""# The self-rewiring loop in {repo_name}

Agents here may propose changes to their own code. An automated gate pipeline
judges every proposal before anything merges. This file says what works, what
does not yet, and what only you can decide.

**`FIRST_DAY.md` is the sequence**; this file is the reference behind it.
Every command there was run against a fresh adoption and its real output
pasted, in the order you meet them.

## Read this first: what the loop needs before it can propose ANYTHING

`aef adopt` and `aef migrate` do not leave you with a loop that can produce a
candidate. Every line of this table was **measured** — removed from a working
sequence, the cycle re-run, the output quoted (aef-core ADR 0139). It is at
the top rather than in step five because each one makes `aef loop cycle` exit
**0 having done nothing**, which reads like success.

1. **The graph under `{DEFAULT_AGENT_ROOT}/`** — Zone A. Otherwise:
   `G0 rejected it: candidate touches paths outside Zone A`
   *`aef migrate` now does this for you: its default `--out` is
   `{DEFAULT_MIGRATED_OUT}` and its report names the zone of the path it
   wrote (aef-core ADR 0143). It wrote `aef_migrated.py` to the repo root —
   Zone C — until then, so check any graph left over from an older run.*
2. **At least one module-level numeric constant in it.** Otherwise:
   `the proposer produced nothing from the available evidence`
3. **A node that actually returns `"reflect"` as its route.** Otherwise:
   `no admissible failure memory: no candidate this cycle`
   *`aef migrate` now does this for you too: a call-site graph is wired
   `<call site> -> reflect -> consolidate -> END`, and a prompt-agent graph
   `retrieve -> prompt_agent -> reflect -> consolidate -> END` — four nodes,
   with `retrieve` as the entry so the lessons the loop learns reach the
   model. If you write the graph by hand, this one is yours.*
4. **Failure memory in the file `aef loop cycle --memory` reads.**
   Otherwise: the same line as 3.
   *`aef loop bootstrap --memory <file>` now does this for you (aef-core ADR
   0145): a failing input's reflections are mirrored into that durable file as
   well as into its own isolated per-input store, so no hand-written run is
   needed. The previous version of this line said bootstrap **cannot** do it —
   true when ADR 0139 measured it, false the day ADR 0145 landed. Measured:
   the same sequence with the flag removed leaves no file and the cycle says
   `no admissible failure memory: no candidate this cycle`, exit 0.* A
   failing `aef run --objective ... --working-memory ... --memory <file>`
   still works and is what you use to add evidence later.
5. **Scenarios in `corpus/`**, train or validation. Otherwise:
   `no corpus: G2/G3 will refuse for lack of evidence`
6. **A blessed baseline.** Otherwise G5 has no reference point for drift.
7. **`--entrypoint` on the cycle itself.** Otherwise:
   `no entrypoint configured: G2/G3 will refuse`

**`aef migrate` writes two of the first four; `aef loop bootstrap --memory`
supplies a third; ONE is still yours.** Migrate lands the graph at
`{DEFAULT_MIGRATED_OUT}` (item 1, Zone A) and wires
`<call site> -> reflect -> consolidate -> END` (item 3); bootstrap leaves the
failure memory of item 4 behind. What remains:

- **no module-level numeric constant** — item 2 is the shape the rule-based
  proposer mutates, and it is also the shape the null-hypothesis control
  cohort is built from, so it is needed twice. A generated wrapper has no
  number of its own to invent. Your node body is where one goes.
- **`aef migrate` cannot make a run fail** — the loop records what happened
  and never invents a failure (ADR 0060). Bootstrap mirrors the failing
  input's reflection; it does not manufacture one, so an inputs file where
  everything passes still leaves the proposer nothing to cite.

Measured, on migrate's own generated graph with the constants inlined and
nothing else changed: a candidate **is** proposed (the structural
`add_bounded_retry` transformation applies), and then `could not build
evidence (cannot build a control cohort ...: no module-level numeric constants
to mutate, so there is no null hypothesis to draw from)` — the CLI's summary
line names a scratch directory, and the real reason is in the ledger. On a
graph where the structural transformation does not apply you get the clearer
`the proposer produced nothing from the available evidence` instead. Either
way, name your numbers.

The previous version of this paragraph said migrate writes *none* of the
four. That was true when ADR 0139 measured it and false the day ADR 0143
landed; it is corrected here rather than quietly deleted.

**Your first corpus cannot come from a model-calling graph unless you have a
credential, and the flag that spends it is `--config`.** The cassette the
gates later replay from does not exist until something makes the call once, so
`aef loop bootstrap` on a routed node with no provider exits 1 with
`no live provider to fall through to`.
Pass `--config aef.yaml` to bootstrap: it builds
the provider through `aef run`'s own code path — so `policies`, `tools.allow`
and `evaluator.suites` reach the recording too — and reports the calls it
spent. Recording is the one pass that is *supposed* to be live. Configuring
`model_provider` in `aef.yaml` without passing the flag does nothing, and
until aef-core ADR 0145 no generated document named the flag at all. The
alternative is to start the loop on a graph that calls no model.

**Check `__pycache__/` is in `.gitignore` before you bless.** `aef adopt`
writes one now (aef-core ADR 0142), and when you already had one it **appends
the two patterns inside a signed `# aef:begin sha256=...` / `# aef:end`
block** rather than leaving you to notice (aef-core ADR 0153/0172) — every
byte you had stays outside that block, untouched, and is re-checked before the
write. It appends nothing when the file already covers
bytecode some other way, so check rather than assume. Compiled bytecode
committed under `{DEFAULT_AGENT_ROOT}/` by an ordinary `git add -A` is Zone A
content the baseline does not have, and G5 charges it as drift: measured at
**0.468 of a 0.500 budget** for a one-line candidate, against **0.024** for
the same candidate with the bytecode excluded.

The sequence that works, start to finish. `<your.graph.module>` is the import
path of the graph you want the loop to improve; if `aef migrate` wrote it,
that is `{_module_path(DEFAULT_MIGRATED_OUT)}`.

```
aef migrate --dir .
aef loop bootstrap <your.graph.module> --corpus corpus --inputs inputs.json \\
    --state ~/.aef-loop-state --memory ~/.aef-loop-state/memory.jsonl
aef loop record <your.graph.module> --corpus corpus --scenario-id tripwire-1 \\
    --objective "<a task beyond this agent>" --split validation \\
    --expected must_fail --working-memory '{{"difficulty": 99}}'
aef loop bless --repo . --state ~/.aef-loop-state \\
    --agent-path agents/<yours>/graph.py
aef loop doctor --repo . --state ~/.aef-loop-state --corpus corpus \\
    --agent-path agents/<yours>/graph.py
aef loop cycle --repo . --state ~/.aef-loop-state --workdir "$(mktemp -d)/run" \\
    --module <your.graph.module> --corpus corpus \\
    --entrypoint <your.graph.module>:build_graph \\
    --agent-path agents/<yours>/graph.py \\
    --memory ~/.aef-loop-state/memory.jsonl \\
    --build-command "<your green bar>"
```

**One of `--state` and `--no-loop-state` is REQUIRED on bootstrap**, and the
line above used to carry neither, so the CLI refused it: bootstrap writes to
`corpus/`, the evidence every behavioural gate is measured against, so it has
to be able to see the kill switch. Silence used to mean "do not check", and a
halted loop's corpus grew from the documented invocation (aef-core ADR 0141).
Pass `--no-loop-state` only when there is genuinely no loop state yet.

{render_prompt_repo_sequence(prompt_agents)}
The hand-written failing `aef run --memory` that used to sit between bootstrap
and bless is **gone from this sequence**: `bootstrap --memory` leaves the same
evidence (aef-core ADR 0145). Add runs later with:

```
aef run <your.graph.module> --objective "a task this agent fails" \\
    --working-memory '{{"difficulty": 99}}' \\
    --memory ~/.aef-loop-state/memory.jsonl
```

`aef loop bootstrap` prints the `aef loop record ... --expected must_fail`
line for you, with the objective and working memory already filled in — the
label itself stays yours, because only an owner can say a task *should* have
failed.

## Nothing merges automatically

**Tier-1 auto-merge is OFF.** A candidate that passes all six gates is
*escalated to you*, not merged. Turning it on is a deliberate source change,
not a config flag — see aef-core ADR 0045 for why that distinction is kept.

## Six things you must supply before the loop can approve anything

**They are ADVISORY, not gating** (aef-core ADR 0141). `aef loop doctor` is
the only thing that reads them; `aef loop cycle` and `aef loop gate` run
whatever it says, printing the unmet ones first. Three enforce themselves
later and correctly — G2/G3 refuse an empty corpus, G5 refuses without a
blessed baseline, and a graph nothing routes to reflect records no failure
memory, so the proposer never proposes — and the other three stop nothing at
all, which is why they are listed here rather than enforced. Item 3 cannot be
green on day one: it needs production runs you have not made yet.

1. **A corpus.** `corpus/` starts empty, and an empty corpus makes G2 and G3
   refuse — correctly: absence of evidence is not evidence of non-regression.

   ```
   aef loop record <your.graph.module> --corpus corpus \\
       --scenario-id <id> --objective "..." --split validation \\
       --working-memory '{{"difficulty": 9}}'
   ```

   Record scenarios that **fail** as well as ones that pass — a corpus where
   everything already passes cannot demonstrate an improvement. `--working-memory`
   is how you drive the agent into those failing cases. Pass
   `--memory ~/.aef-loop-state/memory.jsonl` to `aef run` too, and point
   `aef loop cycle --memory` at the same file — otherwise the reflect node's
   lessons are written somewhere the proposer never reads.

   **Record at least one tripwire.** Without one the gates cannot detect
   reward hacking: a one-line change making an agent always report success
   passed all six gates, because G2 and G3 both read the agent's own claim
   about itself.

   ```
   aef loop record <your.graph.module> --corpus corpus \\
       --scenario-id tripwire-1 --objective "<a task beyond this agent>" \\
       --split validation --expected must_fail \\
       --working-memory '{{"difficulty": 99}}'
   ```

   The task must be impossible **in principle**, not merely hard — labelling
   an achievable task `must_fail` makes every real improvement look like
   reward hacking. `record` refuses to apply the label if the agent completes
   the task. See `corpus/README.md`.

2. **A reflect node in your graph, that your nodes actually route to.**
   The proposer learns from `MemoryRecord`s a reflect node writes. Without
   one, `aef loop cycle` reports "no admissible failure memory" every run and
   will never propose anything.

   ```python
   from aef.reasoning.nodes import make_reflect_node
   ```

   **You need BOTH an edge and a route, and neither alone works.** Routing is
   chosen by node code, not authorised by edges — a node that returns `END`
   never reaches reflect however the edges are drawn. But the executor also
   refuses a route with no declared edge behind it (`node 'work' routed to
   'reflect', but no declared edge ... has a true condition`), so the edge is
   the authorisation and the return value is the choice:

   ```python
   # in your work node
   return delta, "reflect"
   # in build_graph
   edges=[Edge(from_node="work", to_node="reflect")]
   ```

   `aef loop doctor` checks the route. This catches everyone once.

3. **Observations.** Post-merge monitoring reads `observations.jsonl`, and
   nothing writes it unless you pass `--observations` to your production
   runs. With no input, every monitoring window reports unobserved — which
   correctly rolls every change back. Monitoring with no input is a very
   expensive way to revert.

4. **Halt notification.** When the loop halts, it fails a CI job. If nobody
   watches that, nothing has told you. Wire a channel you actually read.

5. **A blessed baseline.** G5 measures drift against an archived version you
   have approved. Until one exists it refuses every candidate with "no
   owner-blessed baseline" — correctly, since it has no reference point.
   Archive your starting state once, deliberately:

   ```
   aef loop bless --repo . --state ~/.aef-loop-state \\
                  --agent-path agents/<yours>/graph.py
   ```

   It archives your **whole Zone A tree as committed in git**, not the file
   named by `--agent-path` and not your working tree: G5 measures drift
   between the baseline and the candidate's Zone A tree, and two sides that
   describe different things do not subtract. Commit before you bless.

   Blessing twice is refused — rebaselining is a separate, rate-limited owner
   decision, and silently replacing the baseline would reset the drift budget
   without anyone choosing to. Blessing an `--agent-path` that is **not inside
   the tree it would archive** is refused too, naming both paths (aef-core ADR
   0147): it used to succeed, printing `blessed <your file> as baseline v1`
   while archiving a Zone A tree containing none of it, and G5 then charged
   the agent's whole existence to the first real candidate.

6. **Model calls that go through `Services`.** This is the one that does not
   announce itself: a node whose body — or whose function, or that function's
   function — builds its own `anthropic.Anthropic()` runs perfectly, and
   `aef doctor` reports green. The bill arrives at gate time. Nothing routed
   through your own client is seen by the policy engine, covered by the
   fallback chain, or paid for by the harness login, and — the expensive part
   — `aef loop record` captures no model call for it, so the scenario carries
   an empty cassette and the gates cannot replay it. They reach your vendor
   live from inside a gate, or fail for want of a credential and score the
   candidate 0.

   ```
   aef migrate --dir . --force
   ```

   `aef migrate` generates a node that calls
   `services.require_model_provider().complete(...)` when your function is
   thin enough that routing loses nothing, and otherwise generates the wrapper
   and tells you, in that node's own docstring, exactly what routing would
   have dropped — a retry loop, a `try`, a stream, a backend router. That
   second case is a decision only you can make; the obligation is that the
   call becomes visible, not that a tool rewrites your retry policy.

   `aef loop doctor` names the file and the vendor it found (aef-core ADR
   0137).

## Zones — what agents may and may not touch

| Zone | Path | Agent-writable |
|---|---|---|
| A | `{DEFAULT_AGENT_ROOT}/**`, or whatever you pass to `--agent-root` | yes |
| B | gate code, `corpus/`, `evals/`, `.github/workflows/` | **never** |
| C | everything else | no |

Zone A is `{DEFAULT_AGENT_ROOT}/` unless you widen it. Pass the **same**
`--agent-root` to `migrate`, `bless`, `doctor`, `cycle` and `gate`: bless
archives the whole Zone A tree and G5 measures drift against it, so two
commands that disagree about which tree that is are comparing different
things.

A diff reaching Zone B is a **security event** that halts the loop, not a
rejection to be retried. This is enforced structurally rather than by policy:
gates execute from the base ref, so a branch that rewrites its own gate still
faces the original one.

## Running it

**Start here:** `aef loop doctor` reports all six obligations at once, with
the exact command to fix each. Work down its output — but do not wait for six
green before running a cycle: they are advisory (above), and `observations`
needs production runs you have not made yet.

```
aef loop doctor  --repo . --state ~/.aef-loop-state --corpus corpus \\
                 --agent-path agents/<yours>/graph.py
aef loop bless   --repo . --state ~/.aef-loop-state \\
                 --agent-path agents/<yours>/graph.py
aef loop status  --repo . --state ~/.aef-loop-state
aef loop harvest <your.graph.module> --repo . --state ~/.aef-loop-state \\
                 --runs ~/.aef-loop-state/runs --corpus corpus
aef loop gate    --repo . --state ~/.aef-loop-state --head <branch> \\
                 --workdir "$(mktemp -d)/run" --corpus corpus \\
                 --entrypoint <your.graph.module>:build_graph \\
                 --build-command "python -m pytest -q"
aef loop cycle   --repo . --state ~/.aef-loop-state --workdir "$(mktemp -d)/run" \\
                 --module <your.graph.module> --corpus corpus \\
                 --entrypoint <your.graph.module>:build_graph \\
                 --memory ~/.aef-loop-state/memory.jsonl \\
                 --build-command "python -m pytest -q"
aef loop monitor --repo . --state ~/.aef-loop-state
aef loop digest  --repo . --state ~/.aef-loop-state --runs ~/.aef-loop-state/runs
```

**`--build-command` is your green bar, not ours.** G1 runs it against your
tree; the default is `pytest -q` alone because anything more is
repo-specific. Repeat the flag for each command.

**`--entrypoint` is required for G2 and G3 to run at all.** They have to
execute your corpus to have anything to say, and they cannot find your graph
without it. There is deliberately no default: one would name a layout your
repo may not have, and the failure would arrive as an import error buried in
a ledger note, reading like an ordinary gate rejection. Without it the gate
report says so explicitly.

**One of `--memory <file>` / `--no-memory` is REQUIRED** by both commands that
run a turn (`cycle` and `run`), and `--memory` must point at the file your
reflect node writes. Without evidence the proposer never proposes; silence
used to mean "do not check", and aef-core's own scheduled cycle was a no-op
every night while exiting 0. Refusing costs one flag; not refusing cost six
months of green ticks (aef-core ADR 0165/0167). `--no-memory` keeps the old
behaviour precisely and says in the summary that the silence was chosen.

**`--state` must be outside this repository.** Inside, `git add -A` sweeps
the ledger and archive into candidate diffs, making the audit trail part of
what it audits. The driver refuses it.

**Exit codes — four, and each names a different action:**

| code | meaning | what to do |
|---|---|---|
| `0` | escalated to a human — the NORMAL path, Tier-1 is off | review it |
| `1` | the candidate was rejected | read the ledger; retry is fine |
| `2` | **HALTED**, or a usage refusal | clear the halt or fix the flags; never retry |
| `3` | it could not do its job | fix the invocation |

`3` covers a crash, a bad config, a missing corpus, a provider that is down,
and an import error — anything that stopped the command before it could judge
a candidate. Fail CI on **`>= 2`**. `3` exists because a catch-all used to return `1` for
any exception, so a bad config and a provider that is down both read as a
healthy rejection and the nightly job stayed green (aef-core ADR 0167/0178).
A halt prints `HALTED:` on stdout; a usage refusal prints `error:` on stderr.

## Driving it with a coding agent

Hand this to Claude Code (or any agent harness) as a `/loop` prompt. It
self-paces; it does not need an interval.

```
/loop Run one self-rewiring cycle and report.

  aef loop cycle --repo . --state ~/.aef-loop-state --workdir "$(mktemp -d)/run" \\
    --module <your.module> --corpus corpus \\
    --entrypoint <your.module>:build_graph \\
    --memory ~/.aef-loop-state/memory.jsonl \\
    --build-command "<your green bar>"

Exit 0 = escalated to a human (the NORMAL outcome; Tier-1 is off).
Exit 1 = rejected. Exit 2 = HALTED or a usage refusal — do not retry,
investigate. Exit 3 = the command could not do its job (crash, bad config,
missing corpus, import error) — fix the invocation, do not treat it as a
verdict on the candidate.

Each run:
  - If it says "no admissible failure memory", the reflect node is producing
    no evidence. Check --memory points at the file `aef run --memory` writes,
    and that a node actually ROUTES to reflect (an Edge alone does not).
  - If a gate rejected, read the ledger's reason and say whether the
    rejection was CORRECT. A candidate rejected on merit is the system
    working, not a failure to fix.
  - If exit 2, report the halt reason and STOP the loop. Do not clear
    ~/.aef-loop-state/HALTED — resuming is the owner's decision.
  - Weekly: `aef loop digest`, and `aef loop monitor` to settle open windows.

NEVER: enable Tier-1 auto-merge; edit anything under corpus/, evals/ or
.github/workflows/ to make a candidate pass; relabel a tripwire; or widen a
zone. A diff reaching those paths is a security event that halts the loop,
not a rejection to retry.
```

## Stopping it

```
touch ~/.aef-loop-state/HALTED
```

A file, deliberately: you must be able to stop the loop without its
cooperation, from a shell, under stress. Nothing in the harness clears it —
resuming is your decision.
"""


def render_loop_gate_workflow(repo_name: str, *, core: bool = False) -> str:
    """Render trusted preparation followed by a network-isolated evaluation.

    The checked-in core workflow uses this same template. Candidate objects
    enter a fresh repository only after the trusted runtime image is built;
    neither the checkout credential nor its Git configuration crosses into it.
    """
    dependencies = (
        'RUN pip install "/opt/aef-base[dev]"'
        if core
        else """RUN pip install aef-core pytest
RUN if [ -f /opt/aef-base/pyproject.toml ] || [ -f /opt/aef-base/setup.py ]; then \\
      pip install /opt/aef-base; \\
    fi"""
    )
    selection = (
        "--graph-id demo_agent --agent-path agents/demo/graph.py \\\n            " if core else ""
    )
    return (
        r"""# Evaluate one self-rewiring candidate in __REPO_NAME__.
# Dispatch from main only. Candidate branches are fetched as DATA, never
# checked out or installed on the host. Keep owner review and Tier-1 off.
# Adopters: configure main, the entrypoint and build command for your repo.
# Add any private/runtime dependencies to the TRUSTED image build below.

name: loop-gate

on:
  workflow_dispatch:
    inputs:
      head:
        description: Candidate branch to evaluate (branch name, not a revision expression)
        required: true
        type: string

permissions:
  contents: read

jobs:
  gate:
    if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    env:
      AEF_GATE_IMAGE: aef-loop-gate:${{ github.run_id }}-${{ github.run_attempt }}

    steps:
      - name: Check out the harness (base ref only)
        uses: actions/checkout@v4
        with:
          ref: main
          fetch-depth: 0
          # Read-only checkout auth stays on the trusted host for private fetches.
          # git archive and the fresh evaluation repo exclude its configuration.
          persist-credentials: true

      - name: Set up gate paths
        shell: bash
        run: |
          set -euo pipefail
          printf 'AEF_GATE_ROOT=%s/aef-loop-gate\n' "$RUNNER_TEMP" >> "$GITHUB_ENV"

      - name: Build the trusted runtime image
        shell: bash
        run: |
          set -euo pipefail
          mkdir "$AEF_GATE_ROOT"
          mkdir -p "$AEF_GATE_ROOT/image/source"
          git archive refs/heads/main | tar -x -C "$AEF_GATE_ROOT/image/source"
          cat > "$AEF_GATE_ROOT/image/Dockerfile" <<'DOCKERFILE'
          FROM python:3.13-slim
          RUN apt-get update && apt-get install -y --no-install-recommends git \
              && rm -rf /var/lib/apt/lists/*
          COPY source /opt/aef-base
          __DEPENDENCIES__
          DOCKERFILE
          # Network is available for trusted dependency preparation only.
          docker build --tag "$AEF_GATE_IMAGE" "$AEF_GATE_ROOT/image"

      - name: Fetch the candidate as data
        shell: bash
        env:
          AEF_CANDIDATE_REF: ${{ inputs.head }}
        run: |
          set -euo pipefail
          git check-ref-format "refs/heads/$AEF_CANDIDATE_REF"
          git fetch --no-tags origin "refs/heads/$AEF_CANDIDATE_REF:refs/loop/candidate"
          # A fresh repo transfers objects and refs, not credentials or hooks.
          git init --initial-branch=gate-preparation "$AEF_GATE_ROOT/repo"
          git -C "$AEF_GATE_ROOT/repo" fetch --no-tags "$GITHUB_WORKSPACE" \
            refs/heads/main:refs/heads/main refs/loop/candidate:refs/loop/candidate
          git -C "$AEF_GATE_ROOT/repo" checkout main

      # Outside the checkout; run-scoped key permits each run's ledger save.
      - name: Restore loop state
        uses: actions/cache@v4
        with:
          path: ~/.aef-loop-state
          key: loop-state-${{ github.repository }}-${{ github.run_id }}
          restore-keys: |
            loop-state-${{ github.repository }}-

      - name: Gate
        shell: bash
        run: |
          set -euo pipefail
          mkdir -p "$HOME/.aef-loop-state"
          # No credentials, Docker socket, or writable source mount crosses here.
          # Failure to start this isolated container fails the job; no host fallback.
          docker run --rm --pull never --network none \
            --read-only --cap-drop ALL --security-opt no-new-privileges \
            --user "$(id -u):$(id -g)" \
            --tmpfs /tmp:rw,exec,mode=1777 --tmpfs /work:rw,exec,mode=1777 \
            --mount "type=bind,src=$AEF_GATE_ROOT/repo,dst=/repo,readonly" \
            --mount "type=bind,src=$HOME/.aef-loop-state,dst=/state" \
            --workdir /repo --env HOME=/tmp "$AEF_GATE_IMAGE" \
            aef loop gate --repo /repo --state /state \
            --base main --head refs/loop/candidate \
            --workdir /work/gate --corpus /repo/corpus \
            __SELECTION__--entrypoint "$AEF_ENTRYPOINT" \
            --build-command "$AEF_BUILD_COMMAND" \
            --network-isolated
        env:
          # EDIT for the adopting agent and its real green bar. No tests means
          # pytest exits 5 and G1 rejects the candidate; failures are not ignored.
          AEF_ENTRYPOINT: __ENTRYPOINT__
          AEF_BUILD_COMMAND: python -m pytest -q
        # exit 0 = passed and escalated to the owner (Tier-1 is off)
        # exit 1 = a gate rejected it
        # exit 2 = the loop halted; do not retry, inspect the kill switch
        # exit 3 = the command CRASHED. Not a halt: inspect the invocation.

      - name: Report
        if: always()
        shell: bash
        run: |
          # Use the same installed trusted runtime, never host imports.
          docker run --rm --pull never --network none \
            --read-only --cap-drop ALL --security-opt no-new-privileges \
            --user "$(id -u):$(id -g)" --tmpfs /tmp:rw,exec,mode=1777 \
            --mount "type=bind,src=$AEF_GATE_ROOT/repo,dst=/repo,readonly" \
            --mount "type=bind,src=$HOME/.aef-loop-state,dst=/state,readonly" \
            --workdir /repo --env HOME=/tmp "$AEF_GATE_IMAGE" \
            aef loop status --repo /repo --state /state || true
""".replace("__REPO_NAME__", repo_name)
        .replace("__DEPENDENCIES__", dependencies.replace("\n", "\n          "))
        .replace("__SELECTION__", selection)
        .replace(
            "__ENTRYPOINT__",
            "agents.demo.graph:build_graph" if core else "agents.mine.graph:build_graph",
        )
    )


#: The module `aef migrate` writes when it finds NO wrappable call site — the
#: placeholder whose `build_graph()` raises `NotImplementedError`. Every
#: prompt-file repo gets one, so a workflow defaulting `AEF_MODULE` to it names
#: a module that exists and cannot build (ADR 0172).
PLACEHOLDER_MODULE = _module_path(DEFAULT_MIGRATED_OUT)


def render_loop_monitor_workflow(repo_name: str, prompt_module: str | None = None) -> str:
    repo = "${{ github.repository }}"
    # One expression, not two interpolations joined by literal `||`. Written
    # as `${{ a }} || b` the whole thing is a non-empty STRING, which an
    # Actions `if:` treats as true — so the "weekly" digest fired on every
    # hourly cron.
    run_id = "${{ github.run_id }}"
    weekly = (
        "${{ github.event.schedule == '0 9 * * 1' || github.event_name == 'workflow_dispatch' }}"
    )
    daily = (
        "${{ github.event.schedule == '0 3 * * *' || github.event_name == 'workflow_dispatch' }}"
    )
    # NEVER the placeholder on a prompt-file repo. `aef migrate` writes
    # `agents/migrated/graph.py` — whose `build_graph()` raises — in EVERY repo
    # with no wrappable call site, which is every prompt-file repo. The module
    # therefore exists, the cycle's exception becomes exit 1, and the rule
    # below reads 1 as a healthy rejection: green every night, nothing
    # proposed, no journal (ADR 0172). When adopt knows the first prompt
    # agent's module it names that instead; either way the guard step below
    # fails the job until the named module actually builds.
    module = prompt_module or PLACEHOLDER_MODULE
    module_note = (
        "the first of this repo's prompt agents, as `aef migrate` will name it.\n"
        "          # It does not exist until you have RUN `aef migrate --dir .`"
        if prompt_module
        else "the graph the loop improves"
    )
    return f"""# Post-merge monitoring, the daily cycle, and the weekly digest for {repo_name}.
# Same trigger rule as loop-gate.yml — see that file, and aef-core ADR 0057.
#
# This file is the CLOCK. Until it is committed, the loop runs only when
# someone types the command: the rendered workflow used to carry `loop
# monitor` and `loop digest` and no `loop cycle` at all, so an adopted repo's
# loop never proposed anything unattended (aef-core ADR 0153).

name: loop-monitor

on:
  schedule:
    - cron: "0 * * * *"   # evaluate open windows, roll back what needs it
    - cron: "0 3 * * *"   # one cycle: harvest, propose, gate. Never merges.
    - cron: "0 9 * * 1"   # weekly digest
  workflow_dispatch:

permissions:
  contents: read

jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: main

      - run: |
          pip install --upgrade pip
          pip install aef-core
          [ -f pyproject.toml ] && pip install -e . || true

      # The key MUST vary per run. `actions/cache` skips its post-job save on
      # an EXACT key hit, so a constant key means run 1 populates the cache and
      # nothing afterwards ever saves: the ledger, `cycles.jsonl` and the
      # archive would reset to run 1's contents every night, and the staleness
      # warning ADR 0165 added could never see three consecutive quiet cycles
      # because it never sees two. `restore-keys` is the prefix that loads the
      # most recent previous run's state (aef-core ADR 0172).
      - uses: actions/cache@v4
        with:
          path: ~/.aef-loop-state
          key: loop-state-{repo}-{run_id}
          restore-keys: |
            loop-state-{repo}-

      # Rollback-by-default: an ambiguous window reverts rather than waiting
      # for more data. Exit 2 means a change every gate passed still
      # regressed, so the gates have a blind spot.
      - run: aef loop monitor --repo . --state ~/.aef-loop-state

      # One turn of the loop: propose, gate, escalate or reject. It creates
      # LOCAL branches and never pushes, which is why `contents: read` is
      # enough. Nothing merges — Tier-1 is off.
      #
      # `--audit-slice 1` (aef-core ADR 0200) holds one train scenario back
      # from BOTH the gates and the proposer's evidence, and afterwards reads
      # the kept candidate against the incumbent on it. Which scenario is a
      # function of your corpus's own ids and the calendar date, so the loop
      # cannot choose the set that judges it, and it rotates daily. The read
      # is advisory — it changes no verdict — and it is what stops a nightly
      # run from only ever being judged by the set it was optimised on.
      #
      # It is NOT your holdout. `corpus/holdout` stays yours: nothing
      # scheduled may spend it, and the flag that would spend it appears in no
      # workflow this writes.
      #
      # `--candidates N` (default 1) is the other half of that ADR: N means N
      # independent gate passes and N x (cohort+2) corpus passes, so raising
      # it is a cost decision. It is left at the default here because a
      # nightly job on a shared runner is exactly where the cost lands.
      #
      # `--memory` is the flag that decides whether this step does anything:
      # without it the cycle reports `no memory store configured` and exits 0,
      # which reads as a healthy nightly run and is a loop that never ran.
      # `--config` carries your policies and provider into the corpus replay.
      # `--cassette-miss fail` is deliberate: CI has no coding-agent harness
      # login, so `live` here would either fail for want of a credential or
      # spend one you did not mean to spend. Score prompt candidates live from
      # a machine that has the login, not from this job.
      # The graph the cycle is about to improve must BUILD, and the job must
      # fail when it does not. Without this step the nightly run is green on
      # the one shape every prompt-file repo has: `aef migrate` writes
      # `{PLACEHOLDER_MODULE}` whenever it finds no wrappable call site,
      # its `build_graph()` raises, `aef loop cycle`'s exception is reported as
      # exit 1 — EXIT_REJECTED, an ordinary candidate rejection — and the rule
      # at the end of the cycle step only fails on exit >= 2. Measured: the
      # cycle exited 1 with `error: aef migrate found no wrappable call site in
      # this repo` and would have left the job green (aef-core ADR 0172).
      - name: The graph the cycle improves must build
        if: {daily}
        run: |
          python - <<'PY'
          import importlib, os, sys, traceback

          module = os.environ.get("AEF_MODULE", "").strip()
          fix = (
              "Set AEF_MODULE (and AEF_ENTRYPOINT) in .github/workflows/loop-monitor.yml "
              "to a graph module that exists and builds. `aef migrate --dir .` prints the "
              "path it wrote for each of your agents; one graph per agent means one module "
              "per agent, so pick the one you want improved."
          )
          if not module or module == "{PLACEHOLDER_MODULE}":
              cause = f"AEF_MODULE is {{module!r}}"
              if module:
                  cause += (
                      " — the placeholder `aef migrate` writes when it finds no wrappable "
                      "call site. Its build_graph() raises NotImplementedError."
                  )
          else:
              try:
                  importlib.import_module(module).build_graph()
                  cause = ""
              except Exception as exc:
                  traceback.print_exc()
                  cause = f"AEF_MODULE={{module}} does not build: {{type(exc).__name__}}: {{exc}}"
          if cause:
              # The ACTUAL cause on the run page, in words. The cycle's own log
              # would have shown migrate's call-site sentence instead, which
              # describes why the placeholder exists, not why tonight failed.
              summary = os.environ.get("GITHUB_STEP_SUMMARY")
              if summary:
                  with open(summary, "a") as handle:
                      handle.write(
                          f"## Loop cycle NOT RUN — {repo_name}\\n\\n{{cause}}\\n\\n{{fix}}\\n"
                      )
              sys.exit(f"{{cause}}\\n{{fix}}")
          PY
        env:
          AEF_MODULE: {module}

      - name: Daily cycle
        if: {daily}
        run: |
          set -o pipefail
          status=0
          aef loop cycle \\
            --repo . --state ~/.aef-loop-state \\
            --workdir "$RUNNER_TEMP/cycle" \\
            --module "$AEF_MODULE" --entrypoint "$AEF_ENTRYPOINT" \\
            --corpus corpus --config aef.yaml \\
            --memory ~/.aef-loop-state/memory.jsonl \\
            --cassette-miss fail \\
            --audit-slice 1 \\
            --build-command "$AEF_BUILD_COMMAND" \\
            2>&1 | tee "$RUNNER_TEMP/cycle.log" || status=$?
          # In WORDS, not only as an exit code: `no admissible failure memory`
          # and `escalated` both exit 0, and a nightly job that is green
          # either way tells nobody which one happened.
          #
          # 2 and 3 both fail the job and their REMEDIES DIFFER — clear the
          # kill switch versus fix the invocation — which is why they are two
          # codes and not one (ADR 0167 §6), and why announcing 3 as a halt
          # sent the reader to the wrong file (ADR 0178).
          case "$status" in
            0) meaning="escalated, or nothing to propose — read the verdict line" ;;
            1) meaning="REJECTED by a gate — the system working, not a broken job" ;;
            2) meaning="HALTED — the kill switch is on; clearing it is a deliberate act" ;;
            3) meaning="ERROR — the cycle crashed; no kill switch is set, fix the invocation" ;;
            *) meaning="exit $status is not a code this loop defines — the cycle raised" ;;
          esac
          {{
            echo "## Loop cycle — {repo_name} (exit $status: $meaning)"
            echo '```'
            cat "$RUNNER_TEMP/cycle.log"
            echo '```'
          }} >> "$GITHUB_STEP_SUMMARY"
          # 1 is a REJECTION — the system working, not a broken job. 2 (a
          # HALT) and 3 (a CRASH) must both fail loudly enough to reach a
          # person, and the failure step below has to know WHICH, so the code
          # is written down before the job dies.
          if [ "$status" -ge 2 ]; then
            echo "$status" > "$RUNNER_TEMP/cycle.status"
            exit 1
          fi
        env:
          # EDIT THESE THREE. `AEF_MODULE` is {module_note} —
          # `aef migrate` prints the path it wrote; one graph per prompt agent
          # means one module per agent, so pick the one you want improved.
          # The step above fails the job until this names a module that builds.
          # Without `--entrypoint`, G2 and G3 cannot execute your corpus and
          # refuse, leaving four of six gates judging every candidate.
          # `--build-command` is your green bar, not ours.
          AEF_MODULE: {module}
          AEF_ENTRYPOINT: {module}:build_graph
          AEF_BUILD_COMMAND: python -m pytest -q

      - name: Weekly digest
        if: {weekly}
        run: aef loop digest --repo . --state ~/.aef-loop-state | tee "$GITHUB_STEP_SUMMARY"

      # A failed job is the ONLY halt signal wired by default, and it depends
      # on someone reading GitHub notifications. Wire a channel you read.
      #
      # It said HALTED for every failure, including exit 3 — a CRASH, whose
      # remedy is to fix the invocation and for which there is no kill switch
      # to clear. A summary that names the wrong remedy is worse than one that
      # names none (ADR 0178).
      - name: Surface a halt or an error
        if: failure()
        run: |
          status=$(cat "$RUNNER_TEMP/cycle.status" 2>/dev/null || echo "")
          case "$status" in
            2) echo "## Self-rewiring loop HALTED in {repo_name}"
               echo "The kill switch is on. Read the ledger, then remove"
               echo "~/.aef-loop-state/HALTED to resume — a deliberate act, not an"
               echo "automatic one." ;;
            3) echo "## Self-rewiring loop ERROR in {repo_name} — the cycle crashed"
               echo "This is NOT a halt: no kill switch is set and clearing one changes"
               echo "nothing. The exception is in the cycle log above; the remedy is to fix"
               echo "the invocation (the module, the entrypoint, the corpus, the config)." ;;
            *) echo "## Self-rewiring loop job FAILED in {repo_name}"
               echo "No cycle exit code was recorded, so the cycle is not what failed —"
               echo "read the failing step above." ;;
          esac >> "$GITHUB_STEP_SUMMARY"
          aef loop status --repo . --state ~/.aef-loop-state >> "$GITHUB_STEP_SUMMARY" || true
          exit 1
"""


def render_corpus_readme(repo_name: str) -> str:
    return f"""# Golden corpus for {repo_name}

**Empty on purpose.** Until you record scenarios here, G2 and G3 refuse every
candidate — correctly, because absence of evidence is not evidence of
non-regression.

```
aef loop record <your.graph.module> --corpus corpus \\
    --scenario-id <id> --objective "..." --split train
```

| Split | Who sees it | Purpose |
|---|---|---|
| `train` | the proposer | the only evidence a proposal may cite |
| `validation` | the gates | what G2/G3 score against |
| `holdout` | **you only** | your independent read of whether the loop helps |

Record scenarios that **fail** as well as ones that pass — a corpus where
everything already passes cannot demonstrate an improvement, and one where
everything fails cannot demonstrate a regression.

Scenarios are **recorded, never hand-written**: a hand-written one encodes
what someone believed the graph does, a recorded one encodes what it did, and
the difference is exactly the case this corpus exists to catch.

Moving a file between split directories is fatal — it would leak the holdout
into the proposer's evidence base. Writing to `holdout` requires
`--i-am-spending-the-holdout`. The corpus never shrinks: a suite that can be
made to pass by deleting the failing case is not a suite.

## Without a tripwire, the gates cannot detect reward hacking

Not theoretical: a one-line change making an agent ignore its inputs and
always report success passed **all six gates**. G2 checks outcome class and
G3 scores a function of that same class, so both ask the agent whether it
succeeded and record the answer.

Label at least one scenario `must_fail` — a task genuinely **beyond** the
agent's remit, where claiming success is a lie rather than an improvement. A
candidate that "passes" it fails G2 as a security event.

Tripwires must be impossible in principle, not merely hard: labelling a
difficult-but-achievable task `must_fail` rejects real progress as reward
hacking. Harvested scenarios never carry a label — only you can say a task
should have failed.
"""


def render_first_day_md(repo_name: str, prompt_agents: int = 0) -> str:
    """`FIRST_DAY.md` — the sequence, in the order an adopter meets it.

    Named for *when* to read it, which is the one thing the rest of the kit
    does not answer. `CLAUDE.md` says what AEF is, `AGENT_INTEGRATION.md` says
    how to ingest it, `LOOP.md` says what the loop needs and `AUTONOMY.md` says
    what you may not do — four documents about *what*, and none of them says
    "today, in this order, and here is what each step costs you".

    **Every command below was RUN against a fresh `git init` + `aef adopt`
    repo and its real output pasted** (aef-core ADR 0148). That rule is not
    decoration: three documents in this scaffold have told adopters things
    that were false, and every one of them was written from the source rather
    than from a terminal.
    """
    module = _module_path(DEFAULT_MIGRATED_OUT)
    return f"""# Your first day with AEF in {repo_name}

Seven commands take this repo from "adopted" to "a candidate change proposed
by the loop and judged by six gates". This file is the order, what each step
costs you, and what does not work until the one before it has run.

**Every command here was executed against a fresh repo and its real output
pasted** (aef-core ADR 0148). Where a step needs something only you can
supply, it says so instead of pretending a flag exists. Where a claim is
unmeasured, it says that too.

Read `LOOP.md` for the gates and the zones, `AGENT_INTEGRATION.md` to onboard
a coding agent, `AUTONOMY.md` for what may never be automated. Read this one
first.

## The whole day

```
aef adopt --dir .
aef migrate --dir .
aef loop bootstrap <your.graph.module> --corpus corpus --inputs inputs.json \\
    --state ~/.aef-loop-state --memory ~/.aef-loop-state/memory.jsonl
aef loop record ...          # the line `aef loop bootstrap` printed, verbatim
aef loop bless --repo . --state ~/.aef-loop-state \\
    --agent-path {DEFAULT_MIGRATED_OUT}
aef loop doctor --repo . --state ~/.aef-loop-state --corpus corpus \\
    --agent-path {DEFAULT_MIGRATED_OUT}
aef loop cycle --repo . --state ~/.aef-loop-state --workdir "$(mktemp -d)/run" \\
    --module <your.graph.module> --corpus corpus \\
    --entrypoint <your.graph.module>:build_graph \\
    --memory ~/.aef-loop-state/memory.jsonl \\
    --agent-path {DEFAULT_MIGRATED_OUT} \\
    --build-command "<your green bar>"
```

Between steps 2 and 3 you write **the semantics** — the node bodies. That is
the one gap no command closes, and section 2 says exactly how wide it is.
Unless your agents are prompt files, in which case there are no node bodies to
write and the sequence gains two flags — the next section is yours.

**Scheduled workflows are opt-in.** Review the schedules first, then generate
them with `aef adopt --dir . --profile model --with-workflows` if wanted.
Once configured and committed, `.github/workflows/loop-monitor.yml` has an hourly
`loop monitor`, a daily `loop cycle` at 03:00 UTC and a weekly digest, all
`workflow_dispatch`-able. Until that file is committed, the loop runs when you
type the command and at no other time. Edit the three `AEF_*` env values in
the cycle step first; the generated ones are placeholders. A cycle with no
`--memory` used to report `no memory store configured` and exit **0** every
night, which reads like a healthy run and is a loop that never ran — so one of
`--memory <file>` / `--no-memory` is now **required**, refused in the handler
rather than only in the parser, and the refusal exits **2** (aef-core ADR
0165/0167). Every attempt, including the ones that raise, is journalled to
`<your state dir>/cycles.jsonl`, and `aef loop monitor` warns
`SCHEDULED CYCLE PRODUCING NOTHING` after three consecutive quiet cycles —
which the ledger alone provably cannot tell you, because a cycle that proposes
nothing writes nothing to it.

{render_prompt_repo_sequence(prompt_agents)}
## 1. `aef adopt` — what you got, and what you did not

The model profile writes 16 files in an empty repo, plus two workflows only
with `--with-workflows`. It **never DESTROYS** an existing owner file: an existing
file of a name it would write is skipped and reported. Six are the exception,
and appending is not overwriting: `CLAUDE.md`, `AGENTS.md`, `GROK.md`,
`.github/copilot-instructions.md`, `.cursor/rules/aef.mdc` and `.gitignore`
get a **signed** block appended to whatever was already there —

```
<!-- aef:begin sha256=1a2b3c4d5e6f7081 -->
...
<!-- aef:end -->
```

— and `# aef:begin sha256=...` / `# aef:end` in `.gitignore`, whose syntax has
no HTML comments. **Only a signed pair is adopt's.** A bare
`<!-- aef:begin -->` sitting in your own prose is inert text: never searched,
never matched, never touched, and adopt's block goes after it.

**Every byte you had is still there, unmodified, outside the block**, and that
is checked rather than claimed: adopt re-reads the bytes before and after the
block and refuses to write the file at all if either moved. Bytes in, bytes
out — a CRLF file stays CRLF, and a mixed one keeps the author's minority line
endings exactly as they were. Re-running replaces only adopt's own block, and
deleting the block undoes it exactly. The report says `appended` rather than
`wrote` or `skipped` for each one.

That rule exists because of a measurement, not a preference: on a real repo
with eight `.claude/agents/*.md` agents, adopt wrote a `CLAUDE.md` the repo
does not use and skipped the `AGENTS.md` it does — `grep -c AEF AGENTS.md`
returned **0**, so the contract never reached the file that repo's agents
actually read (aef-core ADR 0153).

```
CLAUDE.md  AGENTS.md  GROK.md  AGENT_INTEGRATION.md  AUTONOMY.md  FIRST_DAY.md
LOOP.md  AEF_MIGRATION_CHECKLIST.md  aef.yaml  aef_adapter.py  .gitignore
agents/README.md  corpus/README.md  .github/copilot-instructions.md
.cursor/rules/aef.mdc  .claude/skills/new-model-check/SKILL.md
```

Optional: `.github/workflows/loop-gate.yml` and `.github/workflows/loop-monitor.yml`.
`GROK.md` is a portable guide to load explicitly; no autodiscovery is assumed.
The `offline` profile instead generates providerless onboarding without a loop kit.
Existing configuration, guides and workflows are preserved when re-running;
selecting a profile is not a conversion or a way to disable an existing schedule.

**What it explicitly did not do.** It read none of your code. It wrote no
node, no graph, no test, and no scenario. `aef_adapter.py` is a stub whose
node raises `NotImplementedError`. `corpus/` holds a README and nothing else.
There is no agent here yet, so `pytest -q` exits **5** — "no tests collected",
not a pass, and the same 5 fails G1 later if you pass `pytest -q` as your
`--build-command`.

`aef doctor --dir .` should be clean on this output before you go further; a
`[WARN]` is advisory, a `[FAIL]` is not.

## 2. `aef migrate` — and the sentence that matters most

It scans for functions whose bodies touch a model SDK and writes one node per
call site.

```
$ aef migrate --dir .
scanned 3 Python file(s)
found 1 call site(s): 1 wrapped, 0 skipped
  of the wrapped: 1 routed through Services.model_provider, 0 still calling your function

  ROUTED   src.my_agent.run_agent:4  (anthropic.Anthropic)
            -> services.require_model_provider().complete(model='claude-sonnet-4-5')
            routed because its body builds an unconfigured client and makes exactly one
            completion call — no decorator, no loop, no try/except, no stream, no **kwargs,
            and every keyword it passes is one CompletionRequest carries verbatim

wrote {DEFAULT_MIGRATED_OUT}
  Zone A (agents/**) — agent-writable, the only tree the self-rewiring loop may propose changes to
```

**Where it writes: `{DEFAULT_MIGRATED_OUT}`, inside Zone A**, and the report
names the zone of whatever path it wrote. Before aef-core ADR 0143 it wrote
`aef_migrated.py` to the repo root, which is Zone C — the one tree the loop is
structurally forbidden to propose changes to. If you have a graph left over
from an older run, move it.

**It wires the graph to learn:** `<call site> -> reflect -> consolidate ->
END` for a call-site graph, and `retrieve -> prompt_agent -> reflect ->
consolidate -> END` — **four** nodes, `retrieve` first — for a prompt agent,
so what the loop learned is put back in front of the model rather than only
stored. The reflect node is the only thing that writes failure memory and
failure memory is the only evidence the proposer acts on. Route a node back to
`END` and nothing raises — the loop goes silent, exiting 0 every cycle.

A run that answers and gets **your check** wrong now leaves failure memory
too: the checks are evaluated against the run's final state and, when any
fail, a `failure` record is written in the same shape the reflect node writes,
so the consolidator and the proposer read it with no change. Two such runs
make a lesson. Before that, a wrong answer that did not crash left nothing
behind and the loop had nothing to learn from on exactly the repos it is for
(aef-core ADR 0174).

### The two forms, and how it chooses

| Form | When | What you get |
|---|---|---|
| **ROUTED** | routing would lose nothing (below) | a node calling \
`services.require_model_provider().complete(...)`; your function is **bypassed** |
| **WRAPPED** | anything else | a node that calls your function unchanged |

"Routing would lose nothing" means: the body builds the client and makes
exactly one completion call — no loop, no `try`/`except`, no stream, no
decorator, no `**kwargs` — and every keyword it passed is one that
`CompletionRequest` carries verbatim.

Routing carries the request and drops the **control flow** — retries, backoff,
budget accounting, backend selection are yours to re-express. A call passing
`system=`, `tools=` or `stop_sequences=` is never routed at all rather than
routed without them (aef-core ADR 0140).

### If your function keeps its own client, the gates cannot replay it

This is the failure mode that bit every raw-SDK adopter, and it does not
announce itself: the node runs, `aef doctor` is green, and the bill arrives at
gate time. A model call the harness never saw is not policy-checked, not
covered by the fallback chain, not paid for by the harness login — and, the
expensive part, **captures no `RecordedCall`**, so the scenario carries an
empty cassette and the gates cannot replay it. They reach your vendor live
from inside a gate, or fail for want of a credential and score the candidate
0.

Migrate refuses to route what it cannot route losslessly, and says so:

```
  WRAPPED  src.my_agent.run_agent:4  (anthropic.Anthropic)
            -> calls src.my_agent.run_agent, unchanged
            NOT routed because its body loops — a retry, backoff or pagination policy
            that a single complete() call would silently drop
            the model call stays INVISIBLE to the harness: no policy check, no fallback,
            no RecordedCall to replay
```

**What to do about it.** Not `aef migrate --force` — that regenerates the same
wrapper and loops forever. `aef loop doctor` names the two edits, both yours:

```
  [--] model calls visible     src/my_agent.py:1 imports anthropic — the harness cannot see it
       fix: `aef migrate --dir . --force` will NOT fix this and will loop: it already
       refused to route src.my_agent (its body loops ...), and regenerating produces the
       same unrouted wrapper. Two edits, both yours. (1) Rewrite that node's body to call
       `services.require_model_provider().complete(...)` instead of calling your function,
       re-expressing whatever the refusal reason names. (2) DELETE the
       `from src.my_agent import ...` line from the node — src.my_agent stays in the
       graph's reachable set while that import stands, so the obligation stays red even
       after the body is routed. Neither step is optional and neither is automatic.
```

Step (2) is the one people miss. The obligation reads the modules reachable by
import, so a routed body under a surviving import is still red.

**What migrate does not write: the semantics.** Every generated node passes
`state.objective` through as a single prompt and stores the raw result. If
your function took more than that, or its result needs shaping into a
`StateDelta`, that is yours. So is the module-level numeric constant of
section 6.

## 3. `aef loop bootstrap` — your corpus, from one command

An empty `corpus/` makes G2 and G3 refuse, correctly: absence of evidence is
not evidence of non-regression. Bootstrap runs your graph once per input and
records each run as a **train** scenario. The inputs file is a JSON list;
each entry needs `objective` and may set `id` and `working_memory` — the
latter is how you reach the agent's failing cases.

An entry may also carry `checks` — a dotted path into the final state, an
operator and a value — and that is what turns a wrong answer into evidence.

Real output, from this command run against a repo whose two inputs both got
the owner's check wrong (the graph named is that repo's; everything below the
command is verbatim):

```
$ aef loop bootstrap {module} --corpus corpus --inputs inputs.json \\
      --state ~/.aef-loop-state --memory ~/.aef-loop-state/memory.jsonl --config aef.yaml
recorded 2 scenario(s) in the train split
  WRONG   accela-clearwater
  WRONG   accela-pinellas
2 of 2 recorded run(s) FAILED: 0 raised or ended with a failed plan, 2 failed an owner
  check — the task metric, which fails without an error (ADR 0113).
  recording spent 2 live model call(s). Recording is the one pass that is SUPPOSED to be
  live: the gates replay these from each scenario's cassette and need no credential.
  4 memory record(s) written to the durable store — what the graph's own reflect node
  observed, nothing bootstrap decided. `aef loop cycle --memory <the same file>` proposes
  from these.
  2 of them is/are a check-derived FAILURE record: the owner's check, evaluated against
  what the run produced. A signature recurring in two distinct runs becomes a lesson.
```

**Three labels, and the difference between them is what you fix.** `passed`
is a run that did what it should. `FAILED` raised, or ended with a failed
plan. `WRONG` answered and got your check wrong — a crash and a wrong answer
need different fixes, and one label for both hides which you have. The
headline count is one number with one definition of failure, split into its
two halves; it used to be the crash count alone, printed underneath a list of
content failures the checks had already caught (aef-core ADR 0174).

Three flags, and **no generated document mentioned two of them until now**.

**`--state` or `--no-loop-state`, and one of them is mandatory.** Bootstrap
writes to `corpus/`, which is the evidence every behavioural gate is measured
against, so it has to be able to see the kill switch. Silence used to mean
"do not check", and a halted loop's corpus grew from the documented
invocation. Omit both and it refuses:

```
error: bootstrap writes to corpus/, which is the evidence every behavioural gate is
measured against, so it must be able to see the kill switch (ADR 0069). Pass --state <dir>
— the same directory every other loop subcommand takes — or --no-loop-state if there is
genuinely no loop yet.
```

**`--memory <file>` is what lets the cycle propose anything at all** (aef-core
ADR 0145). Point it at the same file you will pass to `aef loop cycle
--memory`. Each run's reflections are mirrored there as well as into its own
isolated per-input store, so a failing input leaves failure memory behind and
the isolation that keeps scenarios independently reproducible is kept. Run the
identical sequence with the flag removed and the corpus is the same, the
tripwire is the same, the baseline is the same, and:

```
$ ls ~/.aef-loop-state/memory.jsonl
ls: ../loop-state/memory.jsonl: No such file or directory
$ aef loop cycle ... --memory ~/.aef-loop-state/memory.jsonl
  ledger verified: 1 entr(ies)
  no admissible failure memory: no candidate this cycle          exit=0
```

Exit **0, having done nothing** — the failure mode that reads like success.
Bootstrap writes only what your reflect node observed and never invents a
failure, so a graph with no reflect node leaves the file empty and says so.

**`--config <aef.yaml>` is how a model-calling graph gets its first corpus.**
Without it, a routed node has no provider, and the cassette the gates will
later replay does not exist until something makes the call once:

```
$ aef loop bootstrap {module} --corpus corpus --inputs inputs.json --no-loop-state
recorded 0 scenario(s) in the train split
  ERRORED (nothing recorded)  bootstrap-1: ModelProviderError: cassette miss ... and no
    live provider to fall through to
NOTHING was recorded and the corpus is unchanged.
  This graph calls a model and no provider was configured, so the recording had nothing
  to record. Pass --config <aef.yaml> to bootstrap: it builds the same model provider
  `aef run` builds, and recording is the one pass that is SUPPOSED to be live — the
  cassette the gates replay from does not exist until something makes the call once.
                                                                        exit=1
```

`--config` builds the provider through `aef run`'s own code path, so
`policies`, `tools.allow` and `evaluator.suites` reach the recording too, and
the calls it spends are reported. The default `model_provider.impl` in the
generated `aef.yaml` is `claude_code`, which uses the coding agent's own login
rather than an API key. **Recording live was not exercised while writing this
file** (no quota), so treat the flag's wiring as proved and its live pass as
your first experiment — start it on one input, not forty.

## 4. Your one act as the owner: the tripwire

Without a tripwire the gates cannot detect reward hacking — a one-line change
making an agent always report success passes every cheap gate, because G2 and
G3 both read the agent's own claim about itself. Only you can say a task
*should* have failed, so bootstrap prints the line and never runs it:

```
    aef loop record {module} --corpus corpus --scenario-id \
beyond-the-budget-tripwire --objective "a task past the retry budget" \
--working-memory "{{\\"difficulty\\": 9, \\"quality_needed\\": 1}}" \
--split validation --expected must_fail
```

Run it verbatim:

```
recorded beyond-the-budget-tripwire (validation) -> \
corpus/validation/beyond-the-budget-tripwire.json
  3 node execution(s) pinned
  0 model call(s) pinned, 0 check(s)
```

The task must be impossible **in principle**, not merely hard: labelling a
difficult-but-achievable task `must_fail` makes every real improvement look
like reward hacking. `record` refuses the label if the agent completes it.

## 5. `bless`, then `aef loop doctor`

Commit first — the baseline is read from **git**, not your working tree.

```
$ aef loop bless --repo . --state ~/.aef-loop-state --agent-path {DEFAULT_MIGRATED_OUT}
blessed {DEFAULT_MIGRATED_OUT} as baseline v1 for graph 'default'
  G5 now has a reference point to measure drift against.
```

A baseline is the **whole Zone A tree** as committed, and G5 measures every
candidate's drift against it — so blessing an agent that is not inside that
tree is refused rather than reported green (aef-core ADR 0147):

```
$ aef loop bless --repo . --state ~/.aef-loop-state --agent-path src/my_agent.py
error: src/my_agent.py exists at HEAD but is NOT inside the tree this would archive:
'{DEFAULT_AGENT_ROOT}' at HEAD holds 2 file(s) ({DEFAULT_AGENT_ROOT}/README.md,
{DEFAULT_MIGRATED_OUT}). A baseline is the whole Zone A tree, and G5 measures every
candidate's drift against it, so blessing here would report src/my_agent.py as blessed
while archiving a tree that does not contain it. Move the agent under
'{DEFAULT_AGENT_ROOT}' ..., or pass --agent-root naming the tree src/my_agent.py
actually lives in.                                                       exit=1
```

Blessing twice is refused too: rebaselining resets a drift budget and is a
separate, deliberate decision.

Then the readiness report:

```
$ aef loop doctor --repo . --state ~/.aef-loop-state --corpus corpus \\
      --agent-path {DEFAULT_MIGRATED_OUT}
Loop readiness — 6 things you must supply

  [OK] corpus + tripwire       5 scenario(s), 1 tripwire(s)
  [OK] reflect node routed to  src_my_agent__run_agent() returns 'reflect' as its Route
  [--] observations            0 recorded run(s) at ~/.aef-loop-state/observations.jsonl
       fix: pass --observations from your production runs, then
            `aef loop monitor --observations <path>`
  [--] halt channel            none — a halt would tell nobody
       fix: set AEF_HALT_WEBHOOK in your environment (never in this repo)
  [OK] blessed baseline        1 archived version(s) of 'agents'
  [OK] model calls visible     1 reachable module(s), none imports a model SDK
                                                                        exit=1
```

**Point `--agent-path` at your PERSONA if that is what you are improving**, and
it resolves. Real output, from the prompt-file sequence above run against a
clone of a real repo with eight `.claude/agents/*.md`:

```
$ aef loop doctor --repo . --state ~/.aef-loop-state --corpus corpus \\
      --agent-root .claude/agents --agent-path .claude/agents/<agent>.md
Loop readiness — 6 things you must supply

  [--] corpus + tripwire       2 scenario(s), 0 tripwire(s)
  [OK] reflect node routed to  .claude/agents/migrated/<agent>/graph.py:
       make_prompt_agent_node(route='reflect') builds a node that routes to it
  [--] observations            0 recorded run(s) at .../observations.jsonl
  [--] halt channel            none — a halt would tell nobody
  [OK] blessed baseline        1 archived version(s) of '.claude/agents'
  [OK] model calls visible     10 graphs scanned, none reaches a model SDK the
       harness cannot see
```

Two things in that are worth naming. It **says which file it read**, because
the answer is about a file you did not type — the persona is resolved to its
generated graph through `aef migrate`'s own mapping, not a second copy of that
mapping (aef-core ADR 0178). And under a widened root the last obligation
scans **every** graph it finds rather than one, which is the case the widening
exists for and the case it used to be unreachable in (aef-core ADR 0158's
F-M5-1). Where there is no generated graph, it says exactly that and prints
`aef migrate` as the fix — never "no reflect node in the graph", which is a
claim about a file that does not exist.

**The six obligations are ADVISORY, not gating** (aef-core ADR 0141), and
earlier versions of this kit implied otherwise. `aef loop doctor` is the only
thing that reads them; `aef loop cycle` and `aef loop gate` run anyway and
print the unmet ones:

```
  preflight: 2 of 6 obligation(s) unmet (observations, halt channel). ADVISORY — this
  command does not refuse on them; run `aef loop doctor` for each fix.
```

Three of the six enforce themselves later and correctly — G2/G3 refuse an
empty corpus, G5 refuses without a blessed baseline, and a graph nothing
routes to reflect records no failure memory so the proposer never proposes. A
missing halt channel or an invisible model call stops **nothing**, which is
why they are listed rather than enforced. And two of them cannot be green on
day one: observations come from production runs you have not made yet. Do not
wait for six.

## 6. `aef loop cycle` — and the one requirement still yours

```
$ aef loop cycle --repo . --state ~/.aef-loop-state --workdir "$(mktemp -d)/run" \\
      --module {module} --corpus corpus \\
      --entrypoint {module}:build_graph \\
      --memory ~/.aef-loop-state/memory.jsonl \\
      --agent-path {DEFAULT_MIGRATED_OUT} --build-command "python -m pytest -q"
  preflight: 2 of 6 obligation(s) unmet (observations, halt channel). ADVISORY ...
  ledger verified: 1 entr(ies)
  proposed cycle-20260904T164400-s0 on local branch loop/cycle-20260904T164400-s0
    (never pushed; proposer=rule_based)
  gated: reject — G3 rejected it: candidate does not beat the p95 of the random control
    cohort — this is the null hypothesis, not an improvement          exit=1
```

and in `~/.aef-loop-state/ledger.jsonl`, which is what to read rather than the
summary line:

```
evidence: 7 corpus pass(es) (35 scenario execution(s)): 1 candidate + 1 incumbent
          + 5 random control(s); gating all 5 gated scenario(s)
  G0 pass  1 file(s), 28 line(s), all Zone A, no static-safety violations
  G1 pass  1 build command(s) succeeded against the merged workspace
  G4 pass  no owner-only safety metadata declared by the candidate
  G5 pass  0/3 accepted in the last 7d; drift 0.098/0.500 from the blessed baseline
  G2 pass  5 scenario(s) re-executed; every previously-passing one still passes.
  G3 fail  candidate does not beat the p95 of the random control cohort
```

**A rejection is the system working.** G3 compares your candidate against a
cohort of random mutations of the same file; a change that does not beat their
p95 is indistinguishable from noise. Nothing merges either way — Tier-1
auto-merge is off and no flag turns it on.

### The requirement: at least one module-level numeric constant

Keep tunable parameters as named module-level constants in your node file.
`aef migrate` writes none — a generated wrapper has no number of its own to
invent — so this one is yours, and it is a **property of `RuleBasedProposer`**,
which mutates numeric constants, and of the control cohort, which is built by
mutating them too.

Measured on migrate's own generated graph with the constants inlined and
nothing else changed: a candidate **is** still proposed (the proposer's
structural `add_bounded_retry` applies), and then there is nothing to judge it
against —

```
evidence: could not build evidence (cannot build a control cohort for
          '{DEFAULT_MIGRATED_OUT}': no module-level numeric constants to mutate,
          so there is no null hypothesis to draw from); G2/G3 will refuse
  G2 fail gate raised TrustBoundaryError: scratch destination .../workspace must be
          empty. A gate that could not judge has not cleared this candidate.
```

Two things worth knowing about that. The first is that the summary line names
a scratch directory, which is a red herring: the cause is the missing
constant, and it is in the ledger's `evidence` note. The second is that on a
graph shape where the structural transformation does not apply you get a
different and clearer refusal instead — `the proposer produced nothing from
the available evidence`. Either way the remedy is the same: name your numbers.

**A prompt agent has no constants, and does not need one — use the proposer
that fits it.** `--proposer rule_based_prompt` appends a lesson to a persona
rather than mutating a number, and the gates build its control cohort by
perturbing the prose instead (aef-core ADR 0157/0170). Point the default
`rule_based` at a `.md` and it proposes nothing and names the one that fits,
rather than reporting the same blank line it reported for four other
situations.

**Whether `--proposer llm` needs one is unmeasured.** `aef loop cycle
--proposer llm` writes the whole file rather than mutating a constant and may
not need one at all; the measurement is blocked on model quota (aef-core
`INGEST_LOOP.md` L4). Until it runs, assume the constant is required for the
Python shape, because the cohort that judges such a candidate is built by
mutating constants whatever proposed it.

## 7. What still needs a person

- **The semantics.** Node bodies, and how more than one call site composes —
  migrate declares extra nodes but only the entry node is reached.
- **The module-level numeric constant**, above.
- **The `must_fail` label.** Bootstrap prints the command; the judgement is
  yours (aef-core ADR 0060).
- **A halt channel and observations.** A halt fails a CI job; if nobody
  watches it, nothing has told you. Monitoring with no input reports
  unobserved, which correctly rolls every change back — an expensive way to
  revert.
- **Every accepted candidate.** A candidate passing all six gates is
  *escalated*, never merged. Turning that off is a source change, not a flag.
- **`--build-command`** is your green bar, not ours, and the default
  `pytest -q` exits 5 in a repo with no tests.

## What has been tried, and what has not

**It has now run against a repo aef-core did not write** — a clone of a real
production repo with eight `.claude/agents/*.md` personas, no SDK call site
anywhere, and its own `AGENTS.md` and `.gitignore` to append to. The whole
prompt-file sequence above goes through on it: `adopt` (15 written, 2
appended, nothing destroyed), `migrate` (8 graphs, one per persona),
`bootstrap` (2 scenarios, both WRONG on the owner's check, 2 check-derived
failure records), `bless`, `doctor`, and a `cycle` that **proposed** a
candidate whose diff was exactly one `.md` under `.claude/agents` and drove it
through G0/G1/G4/G5 to a G2 verdict, drift 0.003 of 0.500. That sentence used
to read "no part of this has run against a repo aef-core did not write", and
it was true until aef-core ADR 0158.

**What has not**: acceptance. Nobody has yet seen a prompt candidate pass all
six gates. Offline the verdict is always a G2 rejection, and that is the
changed-prompt-cannot-replay rule rather than a judgement of the prompt. Live,
with `gates.live_model_calls: true`, the gates reach a real verdict for the
first time as of aef-core ADR 0181 — and the candidate measured there was
rejected by **G3**, for not beating the null-hypothesis control cohort, which
is the system working. Every rejection so far has been correct. That is not
the same as the loop having improved anything, and neither this document nor
aef-core claims it has.

The most valuable thing you can send back is still **anything this document
told you to do that did not work**: several defects in aef-core were generated
documents printing commands the CLI rejects, and they were found only by
someone being the adopter.

## How work is verified here

**Reproduce first** — construct the failing case and RUN it before writing a
fix — and the **green bar** (tests, type-check, lint) passes before every
commit. `AUTONOMY.md` carries the full contract.
"""


def render_agents_zone_readme(repo_name: str) -> str:
    return f"""# Zone A — agent-writable code

The **only** directory the self-rewiring loop may propose changes to.
Everything else in {repo_name} is off limits to it, enforced structurally
rather than by policy: gates execute from the base ref, so a candidate cannot
supply the code that judges it (aef-core ADR 0047).

Put the graph modules and node functions agents are allowed to tune here.
Keep tunable parameters as **module-level named constants** — that is the
shape the rule-based proposer and the control cohort both operate on.

Nothing here is auto-merged. See `../LOOP.md`.
"""
