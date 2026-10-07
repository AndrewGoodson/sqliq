"""Separate exact-plan approval for a small, typed SQL change vocabulary."""
from __future__ import annotations

import base64
import time
from typing import Annotated, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, TypeAdapter

from .broker import Denied, Journal
from .models import Approval, Policy, StrictModel, Target, canonical, digest
from .proposals import identifier, propose


class WritePolicy(Policy):
    writes_enabled: bool = False


class ChangeEvidence(StrictModel):
    schema_name: str
    table: str
    change_ticket: str = Field(min_length=1, max_length=256)
    recovery_evidence: str = Field(min_length=1, max_length=512)
    staging_evidence: str = Field(min_length=1, max_length=512)


class Change(ChangeEvidence):
    # Retain the original constructor and serialized fields for existing approvals.
    kind: Literal['add_nullable_column'] = 'add_nullable_column'
    column: str
    data_type: Literal['int', 'bigint', 'bit', 'datetime2(7)', 'nvarchar(255)']


class CreateIndexChange(ChangeEvidence):
    kind: Literal['create_index']
    column: str
    index: str


class UpdateStatisticsChange(ChangeEvidence):
    kind: Literal['update_statistics']
    statistic: str


class ReorganizeIndexChange(ChangeEvidence):
    kind: Literal['reorganize_index']
    index: str


WriteChange = Annotated[
    Change | CreateIndexChange | UpdateStatisticsChange | ReorganizeIndexChange,
    Field(discriminator='kind'),
]


def parse_change(value: dict) -> WriteChange:
    # Legacy input omitted kind; its only supported interpretation was nullable DDL.
    if isinstance(value, dict) and 'kind' not in value:
        value = {'kind': 'add_nullable_column', **value}
    return TypeAdapter(WriteChange).validate_json(canonical(value))


class WritePlan(StrictModel):
    audience: Literal['azure-sql-write-plan/v1'] = 'azure-sql-write-plan/v1'
    target: Target
    policy_sha256: str
    skills_sha256: str
    change: WriteChange
    sql: str
    timeout_seconds: Literal[10] = 10
    lock_timeout_ms: Literal[5000] = 5000


class WriteApproval(Approval):
    audience: Literal['azure-sql-write-broker/v1']


def write_plan(policy: WritePolicy, change: WriteChange, source_hash: str) -> WritePlan:
    # Revalidate even model_copy/model_construct inputs before rendering any SQL.
    change = parse_change(change.model_dump(mode='json'))
    target = f'{identifier(change.schema_name)}.{identifier(change.table)}'
    if any(not value.strip() for value in (change.change_ticket, change.recovery_evidence,
                                          change.staging_evidence)):
        raise ValueError('Review evidence required')
    if isinstance(change, Change):
        sql = propose(change.kind, change.schema_name, change.table, change.column,
                      change.data_type)['sql']
    elif isinstance(change, CreateIndexChange):
        sql = (f'CREATE NONCLUSTERED INDEX {identifier(change.index)} ON {target} '
               f'({identifier(change.column)}) WITH (MAXDOP = 1);')
    elif isinstance(change, UpdateStatisticsChange):
        # Engine-selected sample; no ALL, FULLSCAN, arbitrary options or data export.
        sql = f'UPDATE STATISTICS {target} {identifier(change.statistic)} WITH MAXDOP = 1;'
    else:
        # Rowstore only, avoid implicit LOB compaction. REORGANIZE can persist partial
        # progress on cancellation: it is not undone by an enclosing rollback.
        sql = (f'ALTER INDEX {identifier(change.index)} ON {target} '
               'REORGANIZE WITH (LOB_COMPACTION = OFF);')
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
        self.journal.claim_target(self.policy.target, approval.nonce)
        try:
            self.executor(self.policy, plan)
            self.journal.finish(approval.nonce, 'write_completed')
            self.journal.release_target(self.policy.target, approval.nonce)
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
        connection.timeout = plan.timeout_seconds
        if isinstance(plan.change, ReorganizeIndexChange):
            # A fresh connection has no work to commit. Avoid an explicit enclosing
            # transaction: REORGANIZE has different rollback/locking semantics.
            connection.autocommit = True
        cursor = connection.cursor()
        try:
            cursor.execute('SET XACT_ABORT ON; SET LOCK_TIMEOUT 5000;')
            # No arbitrary SQL, dynamic SQL or automatic rollback DDL. Never retry.
            # DDL/statistics use the connection transaction; REORGANIZE is autocommit.
            cursor.execute(plan.sql)
            if not isinstance(plan.change, ReorganizeIndexChange):
                connection.commit()
        finally:
            cursor.close()
