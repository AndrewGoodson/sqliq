"""Exact-approved, serial SQL jobs. No scheduler, credentials or signing keys here."""
from __future__ import annotations

import base64
import time
from typing import Literal
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, field_validator

from .broker import Denied, Journal, target_key
from .models import Approval, StrictModel, canonical, digest
from .orchestration import guide
from .writes import WriteChange, WritePlan, WritePolicy, write_plan


class Readiness(StrictModel):
    """Hashes of private reviewed artifacts; never a machine compliance verdict."""
    azure_review_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    sql_review_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    compliance_review_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    financial_reconciliation_plan_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    recovery_rehearsal_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    change_window_start: int
    change_window_end: int
    financial_close_clearance: Literal['approved']


class JobRequest(StrictModel):
    job_id: str
    workflow: Literal['schema', 'maintenance']
    readiness: Readiness
    changes: list[WriteChange] = Field(min_length=1, max_length=10)

    @field_validator('job_id')
    @classmethod
    def valid_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError('Canonical job UUID required')
        return value


class JobPlan(StrictModel):
    audience: Literal['azure-sql-job-plan/v1'] = 'azure-sql-job-plan/v1'
    request: JobRequest
    review_contract_sha256: str
    steps: list[WritePlan] = Field(min_length=1, max_length=10)
    failure_action: Literal['stop_and_reconcile'] = 'stop_and_reconcile'
    post_execution_status: Literal['executed_unverified'] = 'executed_unverified'


class JobApproval(Approval):
    audience: Literal['azure-sql-job-broker/v1']


def job_plan(policy: WritePolicy, request: JobRequest, source_hash: str) -> JobPlan:
    request = JobRequest.model_validate_json(canonical(request))
    readiness = request.readiness
    if not (0 < readiness.change_window_end - readiness.change_window_start <= 3600):
        raise ValueError('Change window must be positive and at most one hour')
    kinds = ({'add_nullable_column', 'create_index'} if request.workflow == 'schema'
             else {'update_statistics', 'reorganize_index'})
    if any(change.kind not in kinds for change in request.changes):
        raise ValueError('Operation does not belong to the selected workflow')
    steps = [write_plan(policy, change, source_hash) for change in request.changes]
    if len({step.sql for step in steps}) != len(steps):
        raise ValueError('Duplicate SQL steps are not allowed')
    # Required parallel AEF domain hooks run before a job can be generated or accepted.
    review = guide(request.workflow)
    return JobPlan(request=request, review_contract_sha256=digest(review), steps=steps)


class JobBroker:
    """One signed batch, one target, serial dispatch, no retry or crash resume.

    Job locks and approval replay storage must be shared by all workers. A leftover
    target lock means interrupted/unknown outcome and requires trusted reconciliation.
    The agent cannot unlock it. Standalone writes share the same target lock.
    """

    def __init__(self, policy: WritePolicy, source_hash: str, journal: Journal,
                 executor, clock=time.time):
        self.policy, self.source_hash, self.journal = policy, source_hash, journal
        self.executor, self.clock = executor, clock
        with journal.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS job_targets '
                       '(target_hash TEXT PRIMARY KEY, job_id TEXT UNIQUE NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS job_runs '
                       '(job_id TEXT PRIMARY KEY, plan_hash TEXT NOT NULL, '
                       'status TEXT NOT NULL, completed INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS job_steps '
                       '(job_id TEXT NOT NULL, step INTEGER NOT NULL, '
                       'plan_hash TEXT NOT NULL, status TEXT NOT NULL, '
                       'PRIMARY KEY(job_id, step))')

    def status(self, job_id: str) -> dict:
        with self.journal.connect() as db:
            row = db.execute('SELECT plan_hash,status,completed FROM job_runs WHERE job_id=?',
                             (job_id,)).fetchone()
            steps = db.execute('SELECT step,plan_hash,status FROM job_steps '
                               'WHERE job_id=? ORDER BY step', (job_id,)).fetchall()
        if row is None:
            raise Denied('Job not found')
        return {'job_id': job_id, 'plan_sha256': row[0], 'status': row[1],
                'interruption_warning': ('Executing may mean interrupted; never replay'
                                         if row[1] == 'executing' else None),
                'completed_steps': row[2], 'verification': 'NOT_ASSESSED',
                'steps': [{'step': s[0], 'plan_sha256': s[1], 'status': s[2]} for s in steps]}

    def invoke(self, arguments: dict) -> dict:
        try:
            if set(arguments) != {'plan', 'approval'}:
                raise ValueError('Unexpected fields')
            plan = JobPlan.model_validate_json(canonical(arguments['plan']))
            approval = JobApproval.model_validate_json(canonical(arguments['approval']))
            if not (self.policy.live_enabled and self.policy.writes_enabled):
                raise ValueError('Disabled')
            expected = job_plan(self.policy, plan.request, self.source_hash)
            if canonical(plan) != canonical(expected):
                raise ValueError('Changed plan')
            now = self.clock()
            window = plan.request.readiness
            if not (approval.issued_at <= now < approval.expires_at
                    and 0 < approval.expires_at - approval.issued_at <= 300
                    and window.change_window_start <= now < window.change_window_end):
                raise ValueError('Outside approval or change window')
            if approval.plan_sha256 != digest(plan):
                raise ValueError('Wrong plan')
            key = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(self.policy.approver_public_key, validate=True))
            key.verify(base64.b64decode(approval.signature, validate=True),
                       canonical(approval.payload()))
        except Exception:
            raise Denied('Job denied: exact external job approval and readiness required') from None
        self.journal.reserve(approval, self.policy.approver_public_key)
        job_id = plan.request.job_id
        target_hash = target_key(self.policy.target)
        try:
            with self.journal.connect() as db:
                db.execute('INSERT INTO job_targets VALUES (?, ?)', (target_hash, job_id))
                db.execute('INSERT INTO job_runs VALUES (?, ?, ?, ?)',
                           (job_id, digest(plan), 'executing', 0))
        except Exception:
            raise Denied('Job already used or target locked; approval consumed') from None
        completed = 0
        try:
            for index, step in enumerate(plan.steps):
                now = self.clock()
                if not (approval.issued_at <= now < approval.expires_at
                        and window.change_window_start <= now < window.change_window_end):
                    raise Denied('Window ended')
                # Record intent durably BEFORE any credential or provider call.
                with self.journal.connect() as db:
                    db.execute('INSERT INTO job_steps VALUES (?, ?, ?, ?)',
                               (job_id, index, digest(step), 'executing'))
                self.executor(self.policy, step)
                with self.journal.connect() as db:
                    db.execute('UPDATE job_steps SET status=? WHERE job_id=? AND step=?',
                               ('executed_unverified', job_id, index))
                    db.execute('UPDATE job_runs SET completed=? WHERE job_id=?',
                               (completed + 1, job_id))
                completed += 1
            result = {'job_id': job_id, 'plan_sha256': digest(plan),
                      'status': 'executed_unverified', 'completed_steps': completed,
                      'verification': 'NOT_ASSESSED',
                      'steps': [{'step': i, 'plan_sha256': digest(s),
                                 'status': 'executed_unverified'}
                                for i, s in enumerate(plan.steps)]}
            # Mark approval first; any audit failure retains target lock.
            self.journal.finish(approval.nonce, 'job_executed_unverified')
            with self.journal.connect() as db:
                db.execute('UPDATE job_runs SET status=? WHERE job_id=?',
                           ('executed_unverified', job_id))
                db.execute('DELETE FROM job_targets WHERE target_hash=? AND job_id=?',
                           (target_hash, job_id))
            return result
        except Exception:
            try:
                with self.journal.connect() as db:
                    db.execute('UPDATE job_runs SET status=? WHERE job_id=?',
                               ('stopped_reconciliation_required', job_id))
                    db.execute('UPDATE job_steps SET status=? WHERE job_id=? AND status=?',
                               ('outcome_unknown', job_id, 'executing'))
                self.journal.finish(approval.nonce, 'job_stopped_reconciliation_required')
            except Exception:
                pass
            raise Denied('Job stopped; target locked and approval consumed. '
                         'Reconcile recorded steps before further changes; never replay.') from None
