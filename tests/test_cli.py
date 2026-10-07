import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from azure_sql_agents.broker import Journal, ReadBroker
from azure_sql_agents.models import Policy, digest, make_plan

ROOT = Path(__file__).resolve().parents[1]


def test_actual_reviewer_cli_to_broker_roundtrip(tmp_path):
    key = Ed25519PrivateKey.generate()
    private = tmp_path / "test-only.key"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    private.chmod(0o600)
    data = json.loads((ROOT / "config/example.json").read_text())
    data.update(live_enabled=True,
                approver_public_key=base64.b64encode(key.public_key().public_bytes_raw()).decode())
    policy = Policy.model_validate(data)
    plan = make_plan(policy, "index_inventory", "a" * 64)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json())
    out = tmp_path / "approval.json"
    args = [sys.executable, str(ROOT / "scripts/approve.py"), "--plan", str(plan_file),
            "--private-key", str(private), "--out", str(out)]
    rejected = subprocess.run(args, input="yes\n", text=True, capture_output=True)
    assert rejected.returncode != 0 and not out.exists()
    accepted = subprocess.run(args, input=f"APPROVE {digest(plan)}\n", text=True, capture_output=True)
    assert accepted.returncode == 0, accepted.stderr
    assert os.stat(out).st_mode & 0o777 == 0o600
    broker = ReadBroker(policy, "a" * 64, Journal(tmp_path / "journal"), lambda *_: {"rows": []})
    assert broker.invoke({"plan": plan.model_dump(mode="json"),
                          "approval": json.loads(out.read_text())}) == {"rows": []}


def test_cli_rejects_disabled_read_without_loading_credentials(tmp_path):
    fake = tmp_path / "request.json"
    fake.write_text("{}")
    result = subprocess.run([sys.executable, "-m", "azure_sql_agents.cli", "read",
                             "--policy", str(ROOT / "config/example.json"), "--plan", str(fake),
                             "--approval", str(fake), "--journal", str(tmp_path / "journal")],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 1 and not result.stdout
    assert "failed closed" in result.stderr
