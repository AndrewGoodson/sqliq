import base64
import importlib.util
import io
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from azure_sql_agents.automation import JobRequest, job_plan
from azure_sql_agents.models import canonical, digest
from azure_sql_agents.writes import Change, WritePolicy, write_plan


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        'write_entrypoint_test', Path(__file__).parents[1] / 'scripts/write_broker_entrypoint.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key = Ed25519PrivateKey.generate()
    data = json.loads((Path(__file__).parents[1] / 'config/example.json').read_text())
    data.update(identity='managed_identity', live_enabled=True, writes_enabled=True,
                approver_public_key=base64.b64encode(key.public_key().public_bytes_raw()).decode())
    policy_path = tmp_path / 'trusted-policy.json'
    policy_path.write_text(json.dumps(data))
    policy = WritePolicy.model_validate(data)
    monkeypatch.setattr(module, 'POLICY', policy_path)
    monkeypatch.setattr(module, 'JOURNAL', tmp_path / 'journal')
    monkeypatch.setattr(module, 'verify_sources', lambda root: 'a'*64)
    monkeypatch.setattr(sys, 'argv', ['write_broker_entrypoint.py'])
    executed = []
    monkeypatch.setattr(module, 'execute_write', lambda policy, plan: executed.append(plan))
    change = Change(schema_name='dbo', table='Ledger', column='ReviewedAt', data_type='int',
                    change_ticket='SYNTHETIC', recovery_evidence='test', staging_evidence='test')

    def request(kind='write'):
        now = int(time.time())
        if kind == 'write':
            plan = write_plan(policy, change, 'a'*64)
        else:
            readiness = {name: 'b'*64 for name in (
                'azure_review_sha256', 'sql_review_sha256', 'compliance_review_sha256',
                'financial_reconciliation_plan_sha256', 'recovery_rehearsal_sha256')}
            readiness.update(change_window_start=now-1, change_window_end=now+300,
                             financial_close_clearance='approved')
            job = JobRequest.model_validate_json(canonical(dict(
                job_id=str(uuid4()), workflow='schema', readiness=readiness,
                changes=[change.model_dump(mode='json')])))
            plan = job_plan(policy, job, 'a'*64)
        payload = dict(audience=f'azure-sql-{kind}-broker/v1', plan_sha256=digest(plan),
                       issued_at=now, expires_at=now+300, nonce=str(uuid4()))
        approval = dict(payload, signature=base64.b64encode(key.sign(canonical(payload))).decode())
        return dict(plan=plan.model_dump(mode='json'), approval=approval)

    def run(value):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw)))
        module.main()
    return module, request, run, executed, policy_path


@pytest.mark.parametrize('kind', ['write', 'job'])
def test_entrypoint_dispatches_verified_approval_and_rejects_replay(endpoint, capsys, kind):
    _, request, run, executed, _ = endpoint
    value = request(kind)
    run(value)
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == ('committed' if kind == 'write' else 'executed_unverified')
    with pytest.raises(SystemExit):
        run(value)
    assert len(executed) == 1


@pytest.mark.parametrize('mutation', ['path', 'read_audience', 'signature', 'sql',
                                      'wrong_plan_type', 'extra_approval', 'oversize', 'malformed'])
@pytest.mark.parametrize('kind', ['write', 'job'])
def test_entrypoint_rejects_bypass_before_executor(endpoint, capsys, mutation, kind):
    _, request, run, executed, _ = endpoint
    value = request(kind)
    if mutation == 'path':
        value['policy'] = '/tmp/ATTACKER-SECRET'
    elif mutation == 'read_audience':
        value['approval']['audience'] = 'azure-sql-read-broker/v1'
    elif mutation == 'signature':
        value['approval']['signature'] = 'ATTACKER-SECRET'
    elif mutation == 'sql':
        target = value['plan'] if kind == 'write' else value['plan']['steps'][0]
        target['sql'] = 'DROP TABLE ATTACKER_SECRET'
    elif mutation == 'wrong_plan_type':
        value['approval']['audience'] = ('azure-sql-job-broker/v1' if kind == 'write'
                                         else 'azure-sql-write-broker/v1')
    elif mutation == 'extra_approval':
        value['approval']['journal'] = '/tmp/ATTACKER-SECRET'
    elif mutation == 'oversize':
        value = b' ' * 131073
    else:
        value = b'{ATTACKER-SECRET'
    with pytest.raises(SystemExit):
        run(value)
    output = capsys.readouterr()
    assert not output.out
    assert 'ATTACKER' not in output.err
    assert 'request denied or failed' in output.err
    assert not executed


@pytest.mark.parametrize('failure', ['cli_credentials', 'disabled', 'sources', 'argv', 'provider'])
def test_production_gates_and_sanitized_failures(endpoint, capsys, monkeypatch, failure):
    module, request, run, executed, policy_path = endpoint
    value = request()
    if failure in {'cli_credentials', 'disabled'}:
        data = json.loads(policy_path.read_text())
        data.update(identity='azure_cli') if failure == 'cli_credentials' else data.update(
            writes_enabled=False)
        policy_path.write_text(json.dumps(data))
    elif failure == 'argv':
        monkeypatch.setattr(sys, 'argv', ['entrypoint', '--policy', '/tmp/ATTACKER-SECRET'])
    else:
        def fail(*args):
            raise RuntimeError('PROVIDER-SECRET')
        monkeypatch.setattr(module, 'verify_sources' if failure == 'sources' else 'execute_write', fail)
    with pytest.raises(SystemExit):
        run(value)
    output = capsys.readouterr()
    assert not output.out
    assert 'SECRET' not in output.err
    assert 'request denied or failed' in output.err
    assert not executed
