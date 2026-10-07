#!/usr/bin/env python3
"""Run ONLY on the human reviewer's workstation, outside agent permissions."""
import argparse
import base64
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from azure_sql_agents.models import Approval, Plan, canonical, digest

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--plan", type=Path, required=True)
parser.add_argument("--private-key", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
document = json.loads(args.plan.read_bytes())
plan = Plan.model_validate_json(canonical(document.get("plan", document)))
print(json.dumps(plan.model_dump(mode="json"), indent=2))
print("This reads confidential schema metadata and Azure posture; output goes to broker stdout.")
plan_hash = digest(plan)
answer = input(f"Type APPROVE {plan_hash} to authorize this exact read for 5 minutes: ")
if answer != f"APPROVE {plan_hash}":
    raise SystemExit("Not approved")
key = serialization.load_pem_private_key(args.private_key.read_bytes(), password=None)
if not isinstance(key, Ed25519PrivateKey):
    raise SystemExit("Expected Ed25519 key")
now = int(time.time())
payload = {"audience": "azure-sql-read-broker/v1", "plan_sha256": plan_hash,
           "nonce": str(uuid4()), "issued_at": now, "expires_at": now + 300}
approval = Approval(**payload, signature=base64.b64encode(key.sign(canonical(payload))).decode())
fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w") as output:
    output.write(json.dumps(approval.model_dump()) + "\n")
print("Approval saved; expires in five minutes.")
