"""Separate exact-plan write approval. Only a nullable column addition is supported."""
from __future__ import annotations

import base64
import time
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field

from .broker import Denied, Journal
from .models import Approval, Policy, StrictModel, Target, canonical, digest
from .proposals import identifier, propose


class WritePolicy(Policy):
    writes_enabled: bool = False


class Change(StrictModel):
    kind: Literal['add_nullable_column'] = 'add_nullable_column'
    schema_name: str
    table: str
    column: str
    data_type: Literal['int', 'bigint', 'bit', 'datetime2(7)', 'nvarchar(255)']
    change_ticket: str = Field(min_length=1, max_length=256)
    recovery_evidence: str = Field(min_length=1, max_length=512)
    staging_evidence: str = Field(min_length=1, max_length=512)


class WritePlan(StrictModel):
    audience: Literal['azure-sql-write-plan/v1'] = 'azure-sql-write-plan/v1'
    target: Target
    policy_sha256: str
    skills_sha256: str
    change: Change
    sql: str
    timeout_seconds: Literal[10] = 10
    lock_timeout_ms: Literal[5000] = 5000


class WriteApproval(Approval):
    audience: Literal['azure-sql-write-broker/v1']


def write_plan(policy: WritePolicy, change: Change, source_hash: str) -> WritePlan:
    for value in (change.schema_name, change.table, change.column):
        identifier(value)
    if any(not value.strip() for value in (change.change_ticket, change.recovery_evidence,
                                          change.staging_evidence)):
        raise ValueError('Review evidence required')
    sql = propose(change.kind, change.schema_name, change.table, change.column,
                  change.data_type)['sql']
    # Policy hash binds the exact target, credential selection and approval key.
    return WritePlan(target=policy.target, policy_sha256=digest(policy), skills_sha256=source_hash,
                     change=change, sql=sql)


class WriteBroker:
    def __init__(self, policy: WritePolicy, source_hash: str, journal: Journal,
                 executor, clock=time.time):
        self.policy, self.source_hash, self.journal = policy, source_hash, journal
        self.executor, self.clock = executor, clock

    def invoke(self, arguments: dict) -> dict:
        try:
            if set(arguments) != {'plan', 'approval'}:
                raise ValueError('Unexpected fields')
            plan = WritePlan.model_validate_json(canonical(arguments['plan']))
            approval = WriteApproval.model_validate_json(canonical(arguments['approval']))
            expected = write_plan(self.policy, plan.change, self.source_hash)
            if not (self.policy.live_enabled and self.policy.writes_enabled):
                raise ValueError('Write access disabled')
            if canonical(plan) != canonical(expected):
                raise ValueError('Changed plan')
            if not (approval.issued_at <= self.clock() < approval.expires_at
                    and 0 < approval.expires_at - approval.issued_at <= 300):
                raise ValueError('Expired approval')
            if approval.plan_sha256 != digest(plan):
                raise ValueError('Wrong plan')
            key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(self.policy.approver_public_key, validate=True))
            key.verify(base64.b64decode(approval.signature, validate=True),
                       canonical(approval.payload()))
        except Exception:
            raise Denied('Write denied: exact external approval required') from None
        self.journal.reserve(approval, self.policy.approver_public_key)
        try:
            self.executor(self.policy, plan)
            self.journal.finish(approval.nonce, 'write_completed')
            return {'status': 'committed', 'plan_sha256': digest(plan)}
        except Exception:
            try:
                self.journal.finish(approval.nonce, 'write_outcome_unknown')
            except Exception:
                pass
            raise Denied('Write outcome unknown; approval consumed. Reconcile before any new change.') from None


def execute_write(policy: WritePolicy, plan: WritePlan):
    from .live import approved_connection

    # Defense in depth: never execute caller-supplied SQL that differs from the template.
    if canonical(plan) != canonical(write_plan(policy, plan.change, plan.skills_sha256)):
        raise Denied('Changed write plan')
    with approved_connection(policy, write=True) as connection:
        cursor = connection.cursor()
        try:
            cursor.execute('SET XACT_ABORT ON; SET LOCK_TIMEOUT 5000;')
            # Only additive nullable DDL, no defaults, data reads, dynamic SQL or rollback DDL.
            # Existing column/table incompatibility fails the transaction; never retry.
            cursor.execute(plan.sql)
            connection.commit()
        finally:
            cursor.close()
