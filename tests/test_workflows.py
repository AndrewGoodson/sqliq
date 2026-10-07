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
