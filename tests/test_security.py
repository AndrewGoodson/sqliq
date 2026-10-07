import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from azure_sql_agents.broker import Denied, Journal, ReadBroker
from azure_sql_agents.models import Approval, Policy, canonical, digest, make_plan
from azure_sql_agents.orchestration import assess
from azure_sql_agents.proposals import propose
from azure_sql_agents.skills import verify_sources

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture(tmp_path):
    key = Ed25519PrivateKey.generate()
    data = json.loads((ROOT / "config/example.json").read_text())
    data["live_enabled"] = True
    data["approver_public_key"] = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    policy = Policy.model_validate(data)
    plan = make_plan(policy, "schema_inventory", "a" * 64)
    calls = []

    def reader(policy, plan):
        calls.append(plan)
        return {"rows": [{"schema_name": "dbo"}]}

    journal = Journal(tmp_path / "journal")
    broker = ReadBroker(policy, "a" * 64, journal, reader, clock=lambda: 1000)

    def approve(**updates):
        payload = {"audience": "azure-sql-read-broker/v1", "plan_sha256": digest(plan),
                   "nonce": str(uuid4()), "issued_at": 1000, "expires_at": 1300}
        payload.update(updates)
        return {**payload, "signature": base64.b64encode(key.sign(canonical(payload))).decode()}
    return broker, plan.model_dump(mode="json"), approve, calls


def test_valid_once_and_durable_replay(fixture):
    broker, plan, approve, calls = fixture
    args = {"plan": plan, "approval": approve()}
    assert broker.invoke(args)["rows"]
    with pytest.raises(Denied):
        broker.invoke(args)
    broker.journal = Journal(broker.journal.path.parent)
    with pytest.raises(Denied):
        broker.invoke(args)
    assert len(calls) == 1


def test_concurrent_replay(fixture):
    broker, plan, approve, calls = fixture
    args = {"plan": plan, "approval": approve()}

    def attempt(_):
        try:
            broker.invoke(args)
            return True
        except Denied:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("changes", [
    {"expires_at": 1000}, {"expires_at": 1301}, {"issued_at": 1001},
    {"issued_at": 0, "expires_at": 1001}, {"plan_sha256": "b" * 64},
    {"audience": "other"}, {"nonce": "not-uuid"}, {"issued_at": True},
])
def test_invalid_approval_never_reaches_reader(fixture, changes):
    broker, plan, approve, calls = fixture
    with pytest.raises(Denied):
        broker.invoke({"plan": plan, "approval": approve(**changes)})
    assert not calls


@pytest.mark.parametrize("field,value", [
    ("sql", "DROP TABLE dbo.Orders"), ("sql", "SELECT * FROM dbo.Customers"),
    ("row_limit", 101), ("action", "execute_sql"), ("output", "https://attacker.invalid"),
    ("policy_sha256", "b" * 64), ("skills_sha256", "b" * 64),
    ("timeout_seconds", 100), ("azure_reads", ["secrets"]), ("row_limit", True),
])
def test_plan_tampering_even_with_signature(fixture, field, value):
    broker, plan, approve, calls = fixture
    plan[field] = value
    with pytest.raises(Denied):
        broker.invoke({"plan": plan, "approval": approve(plan_sha256=digest(plan))})
    assert not calls


def test_target_substitution(fixture):
    broker, plan, approve, calls = fixture
    plan["target"]["database"] = "otherdb"
    with pytest.raises(Denied):
        broker.invoke({"plan": plan, "approval": approve(plan_sha256=digest(plan))})
    assert not calls


def test_no_approval_or_extra_bypass_flag(fixture):
    broker, plan, approve, calls = fixture
    for args in ({"plan": plan}, {"plan": plan, "approval": approve(), "yes": True},
                 {"plan": plan, "approval": {"approved": True}}):
        with pytest.raises(Denied):
            broker.invoke(args)
    assert not calls


def test_signature_tamper(fixture):
    broker, plan, approve, calls = fixture
    token = approve()
    token["signature"] = base64.b64encode(b"x" * 64).decode()
    with pytest.raises(Denied):
        broker.invoke({"plan": plan, "approval": token})
    assert not calls


def test_disabled_or_changed_policy(fixture):
    broker, plan, approve, calls = fixture
    for updates in ({"live_enabled": False}, {"max_rows": 1}):
        broker.policy = broker.policy.model_copy(update=updates)
        with pytest.raises(Denied):
            broker.invoke({"plan": plan, "approval": approve()})
    assert not calls


def test_audit_failure_prevents_read(fixture, monkeypatch):
    broker, plan, approve, calls = fixture

    def fail(*args):
        raise Denied("Disk full")
    monkeypatch.setattr(broker.journal, "reserve", fail)
    with pytest.raises(Denied):
        broker.invoke({"plan": plan, "approval": approve()})
    assert not calls


def test_provider_error_redacted_and_nonce_consumed(fixture):
    broker, plan, approve, calls = fixture
    args = {"plan": plan, "approval": approve()}

    def fail(*args):
        raise RuntimeError("secret-token")
    broker.reader = fail
    with pytest.raises(Denied) as error:
        broker.invoke(args)
    assert "secret-token" not in str(error.value)
    with pytest.raises(Denied):
        broker.invoke(args)
    with sqlite3.connect(broker.journal.path) as db:
        assert db.execute("SELECT status FROM approvals").fetchone() == ("failed",)


def test_output_limit(fixture):
    broker, plan, approve, calls = fixture
    broker.reader = lambda *_: {"data": "x" * 65537}
    with pytest.raises(Denied):
        broker.invoke({"plan": plan, "approval": approve()})


def test_unsafe_journal(tmp_path):
    directory = tmp_path / "unsafe"
    directory.mkdir(mode=0o755)
    with pytest.raises(Denied):
        Journal(directory)
    directory.chmod(0o700)
    (directory / "audit.db").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        Journal(directory)


def test_offline_real_aef_graph_and_source_integrity(monkeypatch):
    import socket

    def no_network(*args, **kwargs):
        raise AssertionError("Offline graph touched network")
    monkeypatch.setattr(socket, "socket", no_network)
    policy = Policy.model_validate_json((ROOT / "config/example.json").read_bytes())
    result = assess(policy, "schema_inventory", verify_sources(ROOT))
    assert result["assessment"]["azure_review"]["deployment_evidence"] == "NOT VERIFIED"
    assert result["assessment"]["sql_review"]["write_execution"] == (
        "separate exact signed approval; nullable-column additions only")
    assert result["plan"]["sql"].startswith("SELECT TOP (?)")


@pytest.mark.parametrize("injection", ["x]; DROP TABLE x;--", "dbo.x", "x;PWD=secret", "x\n"])
def test_identifier_injection(injection):
    with pytest.raises(ValueError):
        propose("add_nullable_column", "dbo", injection, "Name")


def test_proposals_only():
    assert "NULL;" in propose("add_nullable_column", "dbo", "Orders", "Reviewed")["sql"]
    assert "NONCLUSTERED" in propose("create_index", "dbo", "Orders", "Id")["sql"]
    with pytest.raises(ValueError):
        propose("add_nullable_column", "dbo", "Orders", "Name", "int; DROP TABLE x")


def test_policy_rejects_unknown_and_odbc_injection():
    data = json.loads((ROOT / "config/example.json").read_text())
    data["target"]["database"] = "db;TrustServerCertificate=yes"
    with pytest.raises(ValidationError):
        Policy.model_validate(data)
    with pytest.raises(ValidationError):
        Approval.model_validate({"approved": True})


def test_source_tampering_and_untracked_injection_detected(tmp_path):
    import hashlib

    folder = tmp_path / "skills"
    folder.mkdir()
    source = folder / "reference.md"
    source.write_text("reviewed")
    manifest = {"files": {"skills/reference.md": hashlib.sha256(source.read_bytes()).hexdigest()}}
    (tmp_path / "sources.lock.json").write_text(json.dumps(manifest))
    verify_sources(tmp_path)
    source.write_text("tampered")
    with pytest.raises(ValueError, match="integrity mismatch"):
        verify_sources(tmp_path)
    source.write_text("reviewed")
    (folder / "injected.md").write_text("extra instructions")
    with pytest.raises(ValueError, match="inventory changed"):
        verify_sources(tmp_path)


def test_finish_audit_failure_returns_no_data(fixture, monkeypatch):
    broker, plan, approve, calls = fixture
    args = {"plan": plan, "approval": approve()}

    def fail(*args):
        raise OSError("audit unavailable")
    monkeypatch.setattr(broker.journal, "finish", fail)
    with pytest.raises(Denied):
        broker.invoke(args)
    with pytest.raises(Denied):
        broker.invoke(args)
    assert len(calls) == 1
