"""`aef` CLI entry point: init, adopt, doctor, run, eval, trace, loop."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aef.cli.adopt import run_adopt
from aef.cli.doctor import run_doctor
from aef.cli.eval import eval_run
from aef.cli.init import run_init
from aef.cli.loop import add_loop_parser
from aef.cli.migrate import DEFAULT_MIGRATED_OUT, LEGACY_MIGRATED_OUT
from aef.cli.run import run_graph_module
from aef.cli.trace import trace_run
from aef.harness.zones import DEFAULT_AGENT_ROOT
from aef.reasoning.prompt_agent import DEFAULT_PROMPT_AGENT_DIR


def _cmd_init(args: argparse.Namespace) -> int:
    result = run_init(args.agent_name, Path(args.dir))
    for path in result.written_files:
        print(f"wrote {path}")
    for path in result.skipped_files:
        print(f"skipped {path} (already exists)")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    from aef.cli.migrate import report, run_migrate

    result = run_migrate(
        Path(args.dir),
        force=bool(args.force),
        out=args.out,
        agent_root=args.agent_root,
        prompt_agents_dir=args.prompt_agents,
    )
    print(report(result))
    return 0


def _cmd_adopt(args: argparse.Namespace) -> int:
    result = run_adopt(Path(args.dir), profile=args.profile, with_workflows=args.with_workflows)
    print(f"detected framework: {result.detection()}")
    for path in result.written_files:
        print(f"wrote {path}")
    # The third verb (ADR 0153): the file was the adopter's, its bytes are
    # untouched, and it now carries the scaffold block between markers.
    for path in result.appended_files:
        print(f"appended aef block to {path} (your bytes outside it are unchanged)")
    for path in result.skipped_files:
        print(f"skipped {path} ({result.skip_reason(path)})")
    print("\nmigration checklist:")
    for i, item in enumerate(result.checklist, 1):
        print(f"  {i}. {item}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks = run_doctor(
        Path(args.dir),
        agent_path=args.agent_path,
        agent_root=getattr(args, "agent_root", None) or DEFAULT_AGENT_ROOT,
    )
    ok = True
    for check in checks:
        if check.ok:
            status = "OK"
        elif check.level == "advisory":
            status = "WARN"  # surfaced but never fails the exit code
        else:
            status = "FAIL"
            ok = False
        print(f"[{status}] {check.name}: {check.detail}")
    return 0 if ok else 1


def _resolve_objective(args: argparse.Namespace) -> str:
    """`--objective`, else `objectives` from the config.

    These were two things with one name and no connection: the config field
    was required, validated, and never read, while the flag was required on
    every invocation. An owner who wrote their objective once in `aef.yaml`
    had to repeat it on the command line, and could not tell that the file's
    copy did nothing (ADR 0092, ADR 0100).

    The flag still wins when both are present — a per-run override is the
    point of a flag — and neither is an error rather than an empty objective,
    which every evaluator would then score against nothing.
    """
    if args.objective:
        return str(args.objective)
    if args.config is not None:
        from aef.config import load_agent_config

        objectives = load_agent_config(args.config).objectives.strip()
        if objectives:
            return objectives
    raise SystemExit(
        "no objective: pass --objective, or set `objectives` in the file given to --config"
    )


def _cmd_run(args: argparse.Namespace) -> int:
    state = run_graph_module(
        args.module,
        agent_id=args.agent_id,
        objective=_resolve_objective(args),
        working_memory=json.loads(args.working_memory) if args.working_memory else None,
        audit_log_path=args.audit_log,
        memory_path=args.memory,
        config_path=args.config,
        checkpoints_dir=args.checkpoints_dir,
        record_runs_dir=args.record_runs,
    )
    if args.observations:
        from datetime import UTC, datetime

        from aef.cli.eval import build_evaluator
        from aef.cli.run import append_observation

        # The adopter's own `evaluator.suites`, not a bare evaluator — an
        # observation scored without the gates the owner declared reports a
        # pass the owner never agreed to (ADR 0100).
        record = build_evaluator(args.config).evaluate(state)
        append_observation(
            Path(args.observations),
            at=datetime.now(UTC).isoformat(),
            passed=record.passed,
            cost_tokens=record.cost_tokens,
        )
    print(state.model_dump_json(indent=2))
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    record = eval_run(Path(args.checkpoints_dir), args.run_id, config_path=args.config)
    print(f"task_completion={record.task_completion}")
    print(f"tool_call_accuracy={record.tool_call_accuracy}")
    print(f"trajectory_quality={record.trajectory_quality}")
    print(f"cost_tokens={record.cost_tokens}")
    print(f"cost_dollars={record.cost_dollars}")
    print(f"latency_ms={record.latency_ms}")
    print(f"domain_gates={record.domain_gates}")
    print(f"passed={record.passed}")
    return 0 if record.passed else 1


def _cmd_trace(args: argparse.Namespace) -> int:
    provenance = trace_run(Path(args.checkpoints_dir), args.run_id)
    for p in provenance:
        line = (
            f"{p.ts.isoformat()}  node={p.node_id}  model={p.model}  "
            f"tokens={p.token_cost}  trace_id={p.trace_id}"
        )
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aef", description="Agent Engineering Foundation CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_init = subparsers.add_parser("init", help="scaffold a brand-new agent")
    p_init.add_argument("agent_name")
    p_init.add_argument("--dir", default=".")
    p_init.set_defaults(handler=_cmd_init)

    p_adopt = subparsers.add_parser("adopt", help="migrate an existing repo onto AEF")
    p_adopt.add_argument("--dir", default=".")
    p_adopt.add_argument("--profile", choices=("offline", "model"), default="model")
    p_adopt.add_argument(
        "--with-workflows",
        action="store_true",
        help="opt in to loop workflows (model profile only)",
    )
    p_adopt.set_defaults(handler=_cmd_adopt)

    p_migrate = subparsers.add_parser(
        "migrate",
        help="generate node wrappers for this repo's real LLM call sites",
    )
    p_migrate.add_argument("--dir", default=".")
    p_migrate.add_argument(
        "--out",
        default=None,
        help=(
            f"where to write the generated graph, relative to --dir (default: "
            f"{DEFAULT_MIGRATED_OUT}). The default is inside Zone A — the only tree the "
            f"self-rewiring loop may propose changes to. Writing it anywhere else means "
            f"a candidate touching it is rejected by G0 (ADR 0143); the report names the "
            f"zone of whatever path you choose."
        ),
    )
    p_migrate.add_argument(
        "--agent-root",
        default=DEFAULT_AGENT_ROOT,
        help=(
            f"the Zone A root the generated graphs land under (default: "
            f"{DEFAULT_AGENT_ROOT}). Set it to {DEFAULT_PROMPT_AGENT_DIR} to put the "
            f"generated graphs BESIDE the persona files, which is what makes the "
            f"personas themselves Zone A — the only way the loop can ever propose a "
            f"change to a prompt (ADR 0152). Widening Zone A is a scope decision, so it "
            f"is opt-in and the report says in words what it added. Pass the same value "
            f"to every `aef loop` command, or bless and the gate describe different trees."
        ),
    )
    p_migrate.add_argument(
        "--prompt-agents",
        default=DEFAULT_PROMPT_AGENT_DIR,
        help=(
            f"directory of markdown persona files to migrate, one graph each "
            f"(default: {DEFAULT_PROMPT_AGENT_DIR}). Searched recursively, because the "
            f"Claude Code CLI reads subdirectories too."
        ),
    )
    p_migrate.add_argument(
        "--force",
        action="store_true",
        help=(
            "overwrite an existing generated file. An edited one is preserved as a "
            "`.bak` beside it first, and the report says so (ADR 0140)."
        ),
    )
    p_migrate.set_defaults(handler=_cmd_migrate)

    p_doctor = subparsers.add_parser("doctor", help="sanity-check an AEF setup")
    p_doctor.add_argument("--dir", default=".")
    p_doctor.add_argument(
        "--agent-path",
        default=None,
        help=(
            f"the module that builds your graph, repo-relative — the same flag "
            f"`aef loop doctor` takes. Without it, doctor scans every entry the adoption "
            f"contract names that exists (aef_adapter.py, {DEFAULT_MIGRATED_OUT}, "
            f"every graph.py anywhere under the agent root, and {LEGACY_MIGRATED_OUT} "
            f"for a repo migrated before ADR 0143) for model calls the harness cannot see."
        ),
    )
    p_doctor.add_argument(
        "--agent-root",
        default=DEFAULT_AGENT_ROOT,
        help=(
            f"the Zone A root this repo runs its loop with (default {DEFAULT_AGENT_ROOT!r}) "
            f"— pass the SAME value you passed to `aef migrate --agent-root` and to every "
            f"`aef loop` command. Without it, a repo that widened Zone A to "
            f"`.claude/agents` had doctor report on the two files outside that root and "
            f"say nothing about the graphs and personas inside it (ADR 0168)."
        ),
    )
    p_doctor.set_defaults(handler=_cmd_doctor)

    p_run = subparsers.add_parser("run", help="run a graph module's build_graph()")
    p_run.add_argument(
        "module",
        help=(
            "the graph to run, as an importable dotted module path "
            "(`agents.migrated.my_agent.graph`) OR a path to the .py file itself "
            "(`.claude/agents/migrated/my_agent/graph.py`). Either exposes build_graph(). "
            "The file form exists because an agent root like `.claude/agents` has no "
            "dotted spelling at all — `aef migrate` prints whichever form is runnable "
            "for the root you chose (ADR 0168)."
        ),
    )
    p_run.add_argument("--agent-id", default="cli-agent")
    p_run.add_argument(
        "--objective",
        default=None,
        help="the run's objective; defaults to `objectives` from --config if given",
    )
    p_run.add_argument(
        "--config",
        default=None,
        help="aef.yaml path; model_provider: null selects offline execution",
    )
    p_run.add_argument(
        "--checkpoints-dir",
        default=None,
        help="persist checkpoints here (FileDurabilityBackend) so `aef eval`/`aef trace` "
        "can find this run afterward; omit for a one-off in-memory run",
    )
    p_run.add_argument(
        "--memory",
        default=None,
        help="durable memory JSONL for a reflect node to write to. Point `aef loop cycle "
        "--memory` at the same file, or the proposer never sees what was learned.",
    )
    p_run.add_argument(
        "--audit-log",
        default=None,
        help=(
            "append every policy decision to this JSONL file. Without it the audit trail "
            "is in-process and dies with the run. Argument VALUES are redacted; the names "
            "and the decision are kept."
        ),
    )
    p_run.add_argument(
        "--working-memory",
        default=None,
        help="JSON object seeding AEFState.working_memory, e.g. '{\"difficulty\": 9}'. "
        "Without it you cannot produce a failing run from the CLI, and the corpus "
        "cannot record the failures the gates need.",
    )
    p_run.add_argument(
        "--record-runs",
        default=None,
        help="persist full runs (initial state + trace) here so `aef loop harvest` can "
        "promote them into scenarios. Observations record only pass/fail, which is not "
        "enough to rebuild a run as a test case.",
    )
    p_run.add_argument(
        "--observations",
        default=None,
        help="append one JSON line per run here, for post-merge monitoring. Without this "
        "the monitor sees no live runs and every window rolls back for lack of evidence.",
    )
    p_run.set_defaults(handler=_cmd_run)

    p_eval = subparsers.add_parser("eval", help="score a checkpointed run")
    p_eval.add_argument("--checkpoints-dir", required=True)
    p_eval.add_argument("--run-id", required=True)
    p_eval.add_argument(
        "--config",
        default=None,
        help="aef.yaml path; applies the agent's `evaluator.suites` as domain gates",
    )
    p_eval.set_defaults(handler=_cmd_eval)

    p_trace = subparsers.add_parser("trace", help="print a checkpointed run's provenance trail")
    p_trace.add_argument("--checkpoints-dir", required=True)
    p_trace.add_argument("--run-id", required=True)
    p_trace.set_defaults(handler=_cmd_trace)

    add_loop_parser(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = args.handler
    try:
        return int(handler(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
