---
name: new-model-check
description: Run once per model release - re-audits every model-facing surface in this repo (prompts, agents, skills, scaffold templates, SDK call sites) against the new model's documented breaking changes and behavioral guidance, applies the fixes, proves them, commits per step, stops. Use on "new model", "model update", "/new-model-check", or when the session's model differs from the one the repo was last checked against.
---

# New-model check

A model release changes two things in a repo like this one: which API
parameters still work, and what the prompts that drive the coding agent
should say. Nothing re-checks either on its own. This skill does, once,
when a person runs it. It carries **no per-model facts** — those rot the
day the next model ships — and reads them fresh each run.

Run it as: `/new-model-check <model-id>`. With no argument, the target is
the model named in the current session's system prompt.

## Rules that do not bend

- **No facts from memory.** Every breaking change and every prompt
  recommendation you act on is traced to a document you loaded *this run*.
  If you cannot load one, stop and say so.
- **Reproduced and suspected are different claims.** A defect you have not
  run is a hypothesis. Report them in separate lists.
- **No green test without a mutation check.** After a fix's test passes,
  perturb the production value it asserts, confirm the test fails, revert.
- **A detector is verified against a planted fault before "nothing found"
  is believed.**
- **Never weaken a control to get green.** Never remove an instruction that
  tells the agent to verify its own work — every recent model guide says to
  keep those.
- **Bounded.** When both checklists below are exhausted, stop. Do not
  manufacture findings to keep running.
- The repo's own HARD-STOP gates (see its `AUTONOMY.md` or
  `docs/autonomy/`) apply unchanged: external publish beyond
  `git push origin main` of this repo, weakening a security control,
  overwriting a user file in an adopted repo, or an uncertain break of a
  public contract — each needs a human.

## Step 0 — target and tree

1. Target model ID = `$ARGUMENTS`, else the model in the session prompt.
   Write it down; every later step names it.
2. `git status --porcelain` must be empty. A dirty tree is a stop: a
   concurrent process is editing, and your diffs would blur with theirs.
3. Create a branch `model-check/<model-id>`.

## Step 1 — load the model's facts

In order, first that works:

1. Invoke the bundled `claude-api` skill. Read the section of its
   `shared/model-migration.md` for the target model, and the section for
   each intermediate model if the repo's current references predate it
   (the guide says which sections layer). Extract two lists verbatim:
   - **[BLOCKS]** — API rejections (parameters that 400, removed features,
     retired IDs).
   - **[TUNE]** — behavioral guidance (prompt text to remove, prompt text
     to add, effort, long-turn handling, formatting, autonomy).
   Also extract what the guide says to **keep**.
2. If the skill is not available in this harness: `WebFetch`
   `https://docs.claude.com/en/docs/about-claude/models/migration-guide`
   and build the same two lists from it.
3. If neither loads: stop. Report "model facts unavailable" and exit. Do
   not audit against recalled facts.

Record the source and its date in the report (Step 6).

## Step 2 — inventory, then prove the inventory can see

Find surfaces by glob and grep, never by a hardcoded path list — this
skill runs in repos it has never seen.

**API surface** — files that call a model:

```
grep -rln --include='*.py' --include='*.ts' --include='*.js' --include='*.go' --include='*.rb' --include='*.java' --include='*.cs' --include='*.php' \
  -e 'anthropic' -e 'claude-' -e 'messages.create' -e 'temperature' -e 'budget_tokens' -e 'tool_choice' -e 'top_p' -e 'top_k' . \
  | grep -v -e '/\.venv/' -e '/node_modules/' -e '/\.git/'
```

plus config: `grep -rn 'model:' --include='*.yaml' --include='*.yml' --include='*.toml' --include='*.json' .`

**Prompt surface** — files a coding agent reads as instructions:
`CLAUDE.md`, `AGENTS.md`, `AUTONOMY.md`, `AGENT_INTEGRATION.md`,
`*LOOP*.md`, `.claude/agents/*.md`, `.claude/skills/*/SKILL.md`,
`.claude/commands/*.md`, `.github/copilot-instructions.md`,
`.cursor/rules/*`, `GEMINI.md` — **and any code that renders those**
(`grep -rln 'CLAUDE.md\|AUTONOMY.md' --include='*.py'` finds template
generators; their output ships to other repos and must be checked as a
prompt surface too).

**Prove the detector.** Before trusting either grep:

```
mkdir -p .model-check-scratch
printf 'x = client.messages.create(model="claude-3-opus-20240229", temperature=0.2)\n' > .model-check-scratch/planted.py
printf 'CRITICAL: YOU MUST ALWAYS narrate every step. Do not use bullet points.\n' > .model-check-scratch/planted.md
```

Run both greps. Each planted file must appear in its list. If one does
not, the grep is wrong — fix the grep, not the expectation. Then
`rm -r .model-check-scratch` and confirm `git status` shows nothing.

## Step 3 — classify and apply

Work the **[BLOCKS]** list first, then **[TUNE]**. One commit per finding;
findings that share a file's hunks may share a commit if the body
itemizes each one with its own motivation.

### [BLOCKS] — an API rejection

For each hit in the API surface:

1. Write the test that fails today (a fake client capturing the request;
   assert the offending parameter is absent, the new one present, the
   model ID is a real one). Run it. See it fail.
2. Fix.
3. Run it. See it pass. Mutation check: reintroduce the bad value,
   confirm the test fails, revert.
4. One minimal live call against the target, asserting the answering
   model starts with the target ID. **Look for the coding agent's own
   login first** — no API key is not no credential. `claude auth status`
   printing `"loggedIn": true` is one (on macOS it lives in the Keychain,
   so there may be no credentials file to find); the call is then
   `claude -p --model <target> --tools "" --max-turns 1 --output-format json`
   and the answering model is the matching key of its `modelUsage`. Only
   if there is no such login, try `ant auth status` or the SDK's own env
   vars. Mark the finding **reproduced**. If none of these exist, mark it
   **suspected — documented in <source>, not run**. Never blur these.

Model IDs in config templates and examples: replace retired or fictional
IDs with a real current one from the loaded guide. Do not append date
suffixes.

### [TUNE] — prompt guidance

For each item, walk the prompt surface. In this order, because order
matters in the guides:

1. **Remove first.** Text the guide says the new model over-obeys:
   anti-narration ("don't narrate", "hold findings for the end"),
   anti-formatting ("no bullets"), and `CRITICAL: YOU MUST` escalation
   around tool use. Leave rules that state a contract or a fact — a
   vendor-isolation constraint is not prompt tuning.
2. **Add second.** For any surface that governs unattended runs (a loop
   prompt, an `AUTONOMY.md`, an agent definition that runs without a
   person watching): the guide's autonomy block, scope block, and
   test-sprawl / targeted-edit lines, if the guide provides them for this
   model. Quote them from the loaded guide; do not paraphrase.
3. **Keep.** Verification instructions ("run the tests", "reproduce
   first", "green bar before commit") stay. If the guide for this model
   says otherwise, quote it in the report and still ask before removing —
   this is HARD-STOP gate 4 territory.
4. **Effort and sub-agents.** If agent definitions declare a model or
   effort, re-read the guide's effort guidance for the target and adjust
   only where the guide is explicit.

Every prompt edit's commit body quotes **before** and **after** and names
the behavioral shift from the guide that motivates it. Prompt edits are
judgment calls; the commit must let a reviewer disagree with one line
without reverting the rest.

## Step 4 — green bar, every step

Run the repo's own verification before every commit. Find it in
`CLAUDE.md`, `AUTONOMY.md`, or the CI workflow; if the repo has none,
run its test command and its type checker. The test count grows or
holds — a silent shrink is a finding, not a pass.

## Step 5 — runtime spot-check

Already covered per-finding in Step 3. Summarize here: which calls ran,
against which model, through which credential (harness login, `ant`
profile, API key), or "not run: no credential" — and say which of the
three were checked before concluding there was none.

## Step 6 — record and finish

Write `docs/model-checks/<YYYY-MM-DD>-<model-id>.md`:

- target, source of facts and its date
- **reproduced** findings, each with the command that proved it
- **suspected** findings, each with the document line that names it
- what changed, per commit
- what was deliberately left, and why
- the green-bar output line (test count, type-check file count)

If a public contract changed (a provider interface, a scaffold file's
content, a node signature), add an ADR in the repo's `docs/adr/` if it
keeps one.

If the repo has a prompt-surface regression test (this one:
`tests/test_prompt_surface.py`), update its required and forbidden lists to
the new guide's — that test is what keeps this run's edits from being undone.

Merge the branch to `main`; `git push origin main` is inside the loop
contract for this repo. Nothing else is pushed anywhere.

Then stop.
