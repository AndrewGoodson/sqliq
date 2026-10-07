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


def test_migration_guide_includes_full_lifecycle_and_product_applicability():
    result = guide("migration")
    contract = result["workflow_guide"]["migration_contract"]
    assert contract["mode"] == "offline planning; no migration execution"
    assert [stage["stage"] for stage in contract["stages"]] == [
        "baseline", "rehearsal", "go-no-go", "postmigration"]
    assert "not automatically applicable to Azure SQL" in contract["stig_register"]["applicability"]
    assert "including profile-excluded rules" in contract["stig_register"]["coverage"]
    assert "separately for source and target" in contract["stig_register"]["coverage"]
    assert "NOT_ASSESSED" in contract["stig_register"]["status_rule"]
    assert "Migration, restore, data movement and failover are unsupported" in (
        contract["execution_boundary"])
    assert "does not evaluate evidence or approve cutover" in contract["execution_boundary"]
    assert "source-and-target-network-identity" in result["azure_review"]["required_reviews"]
    assert "financial-reconciliation" in result["sql_review"]["required_reviews"]
    assert "every-stig-rule-register" in result["compliance_review"]["required_reviews"]
    assert "pre-and-post-control-drift" in result["compliance_review"]["required_reviews"]
    assert "go-no-go-owner-approval" in result["compliance_review"]["required_reviews"]


@pytest.mark.parametrize("domain", ["azure", "sql", "compliance"])
@pytest.mark.parametrize("mutation", ["missing", "dropped", "substituted", "duplicate"])
def test_hook_rejects_incomplete_or_changed_migration_reviews(domain, mutation):
    from copy import deepcopy

    from azure_sql_agents.domain_hooks import after_domain

    guide_result = guide("migration")
    result = deepcopy(guide_result[f"{domain}_review"])
    dispatch = guide_result["domain_hooks"][domain][0]
    if mutation == "missing":
        result.pop("required_reviews")
    elif mutation == "dropped":
        result["required_reviews"].pop()
    elif mutation == "substituted":
        result["required_reviews"][0] = "automatic-migration-approved"
    else:
        result["required_reviews"].append(result["required_reviews"][0])
    with pytest.raises(ValueError, match="offline review contract"):
        after_domain(domain, result, dispatch)


def test_hook_rejects_forged_dispatch_even_when_result_matches_it():
    from copy import deepcopy

    from azure_sql_agents.domain_hooks import after_domain

    result = deepcopy(guide("migration"))
    dispatch = result["domain_hooks"]["compliance"][0]
    dispatch["required_reviews"] = ["evidence-provenance"]
    result["compliance_review"]["required_reviews"] = dispatch["required_reviews"]
    with pytest.raises(ValueError, match="Invalid offline review dispatch"):
        after_domain("compliance", result["compliance_review"], dispatch)


@pytest.mark.parametrize("domain,field,value", [
    ("azure", "deployment_evidence", "VERIFIED"),
    ("sql", "write_execution", "approved migration and cutover"),
    ("sql", "read_scope", "business rows and arbitrary SQL"),
    ("sql", "migration_execution", "completed"),
    ("compliance", "compliance_claim", True),
    ("compliance", "compliance_claim", 0),
    ("compliance", "live_collection", "scan the target without approval"),
])
def test_migration_join_rejects_unsupported_claims(domain, field, value, monkeypatch):
    from azure_sql_agents import orchestration

    original = orchestration.after_domain

    def tampered_output(actual_domain, result, dispatch):
        if actual_domain == domain:
            result = {**result, field: value}
        return original(actual_domain, result, dispatch)

    monkeypatch.setattr(orchestration, "after_domain", tampered_output)
    with pytest.raises(Exception, match="offline review contract"):
        guide("migration")
