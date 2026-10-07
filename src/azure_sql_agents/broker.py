"""The approval boundary lives here, including for direct tool invocations."""
from __future__ import annotations

import base64
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from aef.security.tool import Tool
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .models import Approval, Plan, Policy, Target, canonical, digest, make_plan


class Denied(RuntimeError):
    pass


def target_key(target: Target) -> str:
    # Public Azure SQL server DNS names are globally unique. Ignore ARM path aliases.
    return digest({"server": target.server.lower(), "database": target.database.casefold()})


class Journal:
    """Durable nonce reservation. Directory must belong to the isolated broker account."""

    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise Denied("Journal directory must be owned by broker and mode 0700")
        self.path = directory / "audit.db"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise Denied("Unsafe journal file")
        finally:
            os.close(fd)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS approvals "
                       "(nonce TEXT PRIMARY KEY, plan_hash TEXT NOT NULL, "
                       "key_hash TEXT NOT NULL, at INTEGER NOT NULL, status TEXT NOT NULL)")

            db.execute("CREATE TABLE IF NOT EXISTS job_targets "
                       "(target_hash TEXT PRIMARY KEY, job_id TEXT UNIQUE NOT NULL)")

    def claim_target(self, target: Target, owner: str):
        try:
            with self.connect() as db:
                db.execute("INSERT INTO job_targets VALUES (?, ?)", (target_key(target), owner))
        except sqlite3.Error:
            raise Denied("Target busy or requires reconciliation; approval consumed") from None

    def release_target(self, target: Target, owner: str):
        with self.connect() as db:
            db.execute("DELETE FROM job_targets WHERE target_hash=? AND job_id=?",
                       (target_key(target), owner))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, approval: Approval, key: str):
        try:
            with self.connect() as db:
                db.execute("INSERT INTO approvals VALUES (?, ?, ?, ?, ?)",
                           (approval.nonce, approval.plan_sha256, digest({"key": key}),
                            int(time.time()), "reserved"))
        except sqlite3.Error:
            raise Denied("Approval replay or unavailable audit journal") from None

    def finish(self, nonce: str, status: str):
        with self.connect() as db:
            db.execute("UPDATE approvals SET status=? WHERE nonce=?", (status, nonce))


class ReadBroker(Tool):
    name = "approved_metadata_read"
    required_scopes = ("azure-sql:metadata:read",)

    def __init__(self, policy: Policy, skills_sha256: str, journal: Journal,
                 reader: Callable[[Policy, Plan], dict], clock=time.time):
        self.policy = policy
        self.skills_sha256 = skills_sha256
        self.journal = journal
        self.reader = reader
        self.clock = clock

    def invoke(self, arguments: dict) -> dict:
        try:
            if set(arguments) != {"plan", "approval"}:
                raise ValueError("Expected plan and approval only")
            plan = Plan.model_validate_json(canonical(arguments["plan"]))
            approval = Approval.model_validate_json(canonical(arguments["approval"]))
            expected = make_plan(self.policy, plan.action, self.skills_sha256)
            if not self.policy.live_enabled or canonical(plan) != canonical(expected):
                raise ValueError("Disabled access or changed plan/policy/catalog/skills")
            now = self.clock()
            if not (approval.issued_at <= now < approval.expires_at
                    and 0 < approval.expires_at - approval.issued_at <= 300):
                raise ValueError("Approval outside its validity window")
            if approval.plan_sha256 != digest(plan):
                raise ValueError("Approval belongs to another plan")
            key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(self.policy.approver_public_key, validate=True))
            key.verify(base64.b64decode(approval.signature, validate=True),
                       canonical(approval.payload()))
        except Exception:
            raise Denied("Read denied: valid explicit approval and unchanged policy required") from None
        # No credentials, DNS, connections or provider factories before this durable write.
        self.journal.reserve(approval, self.policy.approver_public_key)
        try:
            result = self.reader(self.policy, plan)
            if len(canonical(result)) > plan.max_output_bytes:
                raise Denied("Output exceeds approved limit")
            self.journal.finish(approval.nonce, "completed")
            return result
        except Exception:
            try:
                self.journal.finish(approval.nonce, "failed")
            except Exception:
                pass  # Reservation remains consumed; do not release any result.
            # Provider exceptions can contain connection strings, tokens or metadata.
            raise Denied("Read failed; approval consumed. Review broker health before new approval.") from None
