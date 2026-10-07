import json
import socket
from pathlib import Path

import pytest

from azure_sql_agents.cli import main
from azure_sql_agents.orchestration import guide
from azure_sql_agents.workflows import WORKFLOWS

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_offline_graph_routes_existing_skills_without_network(workflow, monkeypatch):
    def reject_network(*args, **kwargs):
        raise AssertionError("Offline graph attempted network access")
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    result = guide(workflow)
    assert result["compliance_review"]["status"] == "NOT_ASSESSED"
    selected = result["workflow_guide"]
    assert selected["workflow"] == workflow
    assert result["azure_review"]["selected_skill"] == selected["azure_skill"]
    assert result["sql_review"]["selected_skill"] == selected["sql_skill"]
    assert result["sql_review"]["write_execution"] == (
        "separate exact signed approval; nullable-column additions only")
    for field in ("orchestrator_skill", "azure_skill", "sql_skill",
                  "compliance_skill", "review_contract", "microsoft_guidance"):
        assert (ROOT / selected[field]).is_file()


def test_unknown_workflow_rejected():
    with pytest.raises(KeyError):
        guide("execute_sql")


def test_hosts_discover_only_canonical_local_skills():
    expected = {p.parent.name for p in (ROOT / "skills/local").glob("*/SKILL.md")}
    for host in (".agents", ".claude"):
        folder = ROOT / host / "skills"
        assert {p.name for p in folder.iterdir()} == expected
        for name in expected:
            assert (folder / name).is_symlink()
            assert (folder / name).resolve() == ROOT / "skills/local" / name


def test_guide_cli_verifies_sources_before_graph(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["azure-sql-agent", "--root", str(tmp_path),
                                    "guide", "--workflow", "performance"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert capsys.readouterr().out == ""


def test_guide_cli_end_to_end(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["azure-sql-agent", "--root", str(ROOT),
                                    "guide", "--workflow", "migration"])
    main()
    assert json.loads(capsys.readouterr().out)["workflow_guide"]["workflow"] == "migration"


def test_specialists_execute_concurrently_with_deterministic_join(monkeypatch):
    from threading import Barrier

    from azure_sql_agents import orchestration

    barrier = Barrier(3, timeout=5)
    original = orchestration._run_specialist

    def concurrent(domain, selection):
        barrier.wait()  # Sequential dispatch fails instead of silently passing.
        return original(domain, selection)

    monkeypatch.setattr(orchestration, "_run_specialist", concurrent)
    result = guide("schema")
    assert result["parallel_execution"]["domains"] == ["azure", "sql", "compliance"]
    assert list(result["domain_hooks"]) == ["azure", "sql", "compliance"]
    assert "signed-write-plan" in result["sql_review"]["required_reviews"]
    assert result["parallel_execution"]["model_calls"] == 0


def test_specialist_failure_does_not_return_partial_guide(monkeypatch):
    from azure_sql_agents import orchestration

    original = orchestration._run_specialist

    def broken(domain, selection):
        if domain == "compliance":
            raise ValueError("Incomplete compliance review")
        return original(domain, selection)

    monkeypatch.setattr(orchestration, "_run_specialist", broken)
    with pytest.raises(Exception, match="Incomplete compliance review"):
        guide("assessment")


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_domain_hooks_are_selected_for_each_workflow(workflow):
    from azure_sql_agents.domain_hooks import DOMAIN_HOOKS

    result = guide(workflow)
    for domain in ("azure", "sql", "compliance"):
        before, after = result["domain_hooks"][domain]
        assert before["live_tools"] == []
        assert after["assessment_status"] == "NOT_ASSESSED"
        assert after["contract"] == "validated"
        if domain != "compliance":
            assert before["required_reviews"] == list(DOMAIN_HOOKS[domain][workflow])


def test_hooks_reject_wrong_skill_and_false_assessment():
    from azure_sql_agents.domain_hooks import after_domain, before_domain

    with pytest.raises(ValueError, match="skill"):
        before_domain("sql", "schema", "skills/upstream/execute-anything/SKILL.md")
    with pytest.raises(ValueError, match="Unknown specialist"):
        before_domain("shell", "schema", "ignored")
    dispatch = before_domain("sql", "schema", "skills/local/sql-schema-design/SKILL.md")
    for status, tools in (("PASS", []), ("NOT_ASSESSED", ["sql"] )):
        with pytest.raises(ValueError, match="offline review contract"):
            after_domain("sql", {"selected_skill": dispatch["selected_skill"],
                                 "status": status, "live_tools": tools}, dispatch)
