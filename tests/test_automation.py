import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from azure_sql_agents.automation import JobBroker, JobRequest, job_plan
from azure_sql_agents.broker import Denied, Journal
from azure_sql_agents.models import canonical, digest
from azure_sql_agents.writes import WriteBroker, WritePolicy


@pytest.fixture
def job(tmp_path):
    key = Ed25519PrivateKey.generate()  # Synthetic test key only.
    data = json.loads((Path(__file__).parents[1] / 'config/example.json').read_text())
    data.update(live_enabled=True, writes_enabled=True,
                approver_public_key=base64.b64encode(key.public_key().public_bytes_raw()).decode())
    policy = WritePolicy.model_validate(data)
    readiness = {name: 'b' * 64 for name in (
        'azure_review_sha256', 'sql_review_sha256', 'compliance_review_sha256',
        'financial_reconciliation_plan_sha256', 'recovery_rehearsal_sha256')}
    readiness.update(change_window_start=90, change_window_end=300,
                     financial_close_clearance='approved')
    changes = [dict(kind='add_nullable_column', schema_name='dbo', table='Ledger',
                    column=name, data_type='int', change_ticket='T-1',
                    recovery_evidence='restore-reviewed', staging_evidence='staging-reviewed')
               for name in ('ReviewId', 'BatchId', 'VersionId')]
    request = JobRequest.model_validate(dict(job_id=str(uuid4()), workflow='schema',
                                            readiness=readiness, changes=changes))
    plan = job_plan(policy, request, 'a' * 64).model_dump(mode='json')
    journal = Journal(tmp_path / 'journal')
    calls = []
    broker = JobBroker(policy, 'a' * 64, journal, lambda p, s: calls.append(s.sql), clock=lambda: 100)

    def sign(value=None, **overrides):
        payload = dict(audience='azure-sql-job-broker/v1', plan_sha256=digest(value or plan),
                       nonce=str(uuid4()), issued_at=99, expires_at=200)
        payload.update(overrides)
        return {**payload, 'signature': base64.b64encode(key.sign(canonical(payload))).decode()}

    return broker, plan, sign, calls


def fresh(plan):
    result = copy.deepcopy(plan)
    result['request']['job_id'] = str(uuid4())
    return result


def test_offline_plan_deterministic_and_workflow_mismatch(job):
    broker, plan, _, calls = job
    request = JobRequest.model_validate(plan['request'])
    assert canonical(job_plan(broker.policy, request, broker.source_hash)) == canonical(plan)
    changed = copy.deepcopy(plan['request'])
    changed['workflow'] = 'maintenance'
    with pytest.raises(ValueError, match='workflow'):
        job_plan(broker.policy, JobRequest.model_validate(changed), broker.source_hash)
    assert not calls


def test_job_order_single_use_and_duplicate_id(job):
    broker, plan, sign, calls = job
    approval = sign()
    result = broker.invoke(dict(plan=plan, approval=approval))
    assert calls == [step['sql'] for step in plan['steps']]
    assert result['status'] == 'executed_unverified'
    assert result['verification'] == 'NOT_ASSESSED'
    assert result['completed_steps'] == 3
    for next_approval in (approval, sign()):
        with pytest.raises(Denied):
            broker.invoke(dict(plan=plan, approval=next_approval))
    assert len(calls) == 3


@pytest.mark.parametrize('mutation', ['sql', 'order', 'evidence', 'source', 'target',
                                      'audience', 'signature', 'expired', 'future',
                                      'window', 'contract', 'disabled'])
def test_job_tampering_denied_before_dispatch(job, mutation):
    broker, original, sign, calls = job
    plan = copy.deepcopy(original)
    approval = sign()
    if mutation == 'sql':
        plan['steps'][0]['sql'] = 'DROP TABLE dbo.Ledger;'
    elif mutation == 'order':
        plan['steps'].reverse()
    elif mutation == 'evidence':
        plan['request']['readiness']['azure_review_sha256'] = 'c' * 64
    elif mutation == 'source':
        broker.source_hash = 'c' * 64
    elif mutation == 'target':
        plan['steps'][0]['target']['database'] = 'DifferentDB'
    elif mutation == 'audience':
        approval = sign(audience='azure-sql-write-broker/v1')
    elif mutation == 'signature':
        approval['signature'] = base64.b64encode(b'\0' * 64).decode()
    elif mutation == 'expired':
        approval = sign(issued_at=1, expires_at=99)
    elif mutation == 'future':
        approval = sign(issued_at=101, expires_at=200)
    elif mutation == 'window':
        plan['request']['readiness'].update(change_window_start=150, change_window_end=250)
        approval = sign(plan)
    elif mutation == 'contract':
        plan['review_contract_sha256'] = 'c' * 64
    else:
        broker.policy = broker.policy.model_copy(update={'writes_enabled': False})
    with pytest.raises(Denied):
        broker.invoke(dict(plan=plan, approval=approval))
    assert not calls


@pytest.mark.parametrize('failure', ['provider', 'expiry', 'audit'])
def test_partial_failure_stops_locks_and_prevents_fresh_job(job, failure, monkeypatch):
    broker, plan, sign, calls = job
    now = [100]
    broker.clock = lambda: now[0]

    def execute(policy, step):
        calls.append(step.sql)
        if failure == 'provider' and len(calls) == 2:
            raise RuntimeError('private-provider-details')
        if failure == 'expiry':
            now[0] = 201

    broker.executor = execute
    if failure == 'audit':
        def fail_finish(*args):
            raise RuntimeError('journal-unavailable')
        monkeypatch.setattr(broker.journal, 'finish', fail_finish)
    with pytest.raises(Denied, match='Job stopped') as denied:
        broker.invoke(dict(plan=plan, approval=sign()))
    assert 'private-provider-details' not in str(denied.value)
    assert len(calls) == {'provider': 2, 'expiry': 1, 'audit': 3}[failure]
    status = broker.status(plan['request']['job_id'])
    assert status['status'] == 'stopped_reconciliation_required'
    if failure == 'provider':
        assert status['steps'][1]['status'] == 'outcome_unknown'
    now[0] = 100
    new = fresh(plan)
    before = len(calls)
    with pytest.raises(Denied, match='locked'):
        broker.invoke(dict(plan=new, approval=sign(new)))
    assert len(calls) == before


@pytest.mark.parametrize('lock_owner', ['job', 'standalone'])
def test_job_and_standalone_share_unresolved_target_lock(job, lock_owner):
    broker, plan, sign, calls = job
    step = plan['steps'][0]

    def fail(policy, change):
        calls.append(change.sql)
        raise RuntimeError('unknown')

    standalone = WriteBroker(broker.policy, broker.source_hash, broker.journal,
                             fail, clock=lambda: 100)
    broker.executor = fail
    single = dict(plan=step, approval=sign(step, audience='azure-sql-write-broker/v1'))
    batch = dict(plan=plan, approval=sign())
    first, first_args, second, second_args = (
        (broker, batch, standalone, single) if lock_owner == 'job'
        else (standalone, single, broker, batch))
    with pytest.raises(Denied):
        first.invoke(first_args)
    with pytest.raises(Denied):
        second.invoke(second_args)
    assert len(calls) == 1


def test_simultaneous_jobs_cannot_claim_same_target(job):
    broker, plan, sign, calls = job
    entered, release = Event(), Event()

    def execute(policy, step):
        calls.append(step.sql)
        entered.set()
        assert release.wait(5)

    broker.executor = execute
    other = JobBroker(broker.policy, broker.source_hash, broker.journal,
                      lambda p, s: pytest.fail('second worker executed'), clock=lambda: 100)
    new = fresh(plan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(broker.invoke, dict(plan=plan, approval=sign()))
        try:
            assert entered.wait(5)
            with pytest.raises(Denied, match='locked'):
                other.invoke(dict(plan=new, approval=sign(new)))
        finally:
            release.set()
        assert first.result(timeout=5)['completed_steps'] == 3
    assert len(calls) == 3


def test_cli_offline_plan_and_mock_approved_run(job, tmp_path, monkeypatch, capsys):
    from azure_sql_agents import cli

    broker, plan, sign, calls = job
    policy_path, request_path = tmp_path / 'policy.json', tmp_path / 'request.json'
    policy_path.write_bytes(canonical(broker.policy))
    request_path.write_bytes(canonical(plan['request']))
    monkeypatch.setattr(cli, 'verify_sources', lambda root: broker.source_hash)
    monkeypatch.setattr('sys.argv', ['azure-sql-agent', 'job-plan', '--policy', str(policy_path),
                                    '--request', str(request_path)])
    cli.main()
    generated = json.loads(capsys.readouterr().out)
    assert generated == plan
    assert not calls
    plan_path, approval_path = tmp_path / 'plan.json', tmp_path / 'approval.json'
    plan_path.write_bytes(canonical(generated))
    approval_path.write_bytes(canonical(sign()))

    def factory(policy, source_hash, journal, executor):
        assert policy == broker.policy
        return JobBroker(policy, source_hash, journal, executor, clock=lambda: 100)

    monkeypatch.setattr(cli, 'JobBroker', factory)
    monkeypatch.setattr(cli, 'execute_write', lambda p, s: calls.append(s.sql))
    monkeypatch.setattr('sys.argv', ['azure-sql-agent', 'job-run', '--policy', str(policy_path),
                                    '--plan', str(plan_path), '--approval', str(approval_path),
                                    '--journal', str(tmp_path / 'cli-journal')])
    cli.main()
    assert json.loads(capsys.readouterr().out)['status'] == 'executed_unverified'
    assert calls == [step['sql'] for step in plan['steps']]


def test_target_case_alias_still_locked(job):
    broker, plan, sign, calls = job
    broker.journal.claim_target(broker.policy.target, 'unresolved-operation')
    target = broker.policy.target.model_copy(update={
        'database': broker.policy.target.database.swapcase(),
        'resource_group': broker.policy.target.resource_group.swapcase()})
    policy = broker.policy.model_copy(update={'target': target})
    other = JobBroker(policy, broker.source_hash, broker.journal,
                      lambda p, s: calls.append(s.sql), clock=lambda: 100)
    regenerated = job_plan(policy, JobRequest.model_validate(plan['request']),
                           broker.source_hash).model_dump(mode='json')
    with pytest.raises(Denied, match='locked'):
        other.invoke(dict(plan=regenerated, approval=sign(regenerated)))
    assert not calls
