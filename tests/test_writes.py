import base64
import json
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from azure_sql_agents.broker import Denied, Journal
from azure_sql_agents.models import canonical, digest
from azure_sql_agents.writes import Change, WriteBroker, WritePolicy, execute_write, write_plan


@pytest.fixture
def setup(tmp_path):
    key = Ed25519PrivateKey.generate()  # Synthetic test key only.
    data = json.loads((Path(__file__).parents[1] / 'config/example.json').read_text())
    data.update(live_enabled=True, writes_enabled=True,
                approver_public_key=base64.b64encode(key.public_key().public_bytes_raw()).decode())
    policy = WritePolicy.model_validate(data)
    change = Change(schema_name='dbo', table='Ledger', column='ReviewId', data_type='int',
                    change_ticket='TEST-1', recovery_evidence='test restore',
                    staging_evidence='test rehearsal')
    plan = write_plan(policy, change, 'a'*64).model_dump(mode='json')
    calls = []
    broker = WriteBroker(policy, 'a'*64, Journal(tmp_path/'journal'),
                         lambda p, q: calls.append(q), clock=lambda: 1000)

    def sign(**updates):
        payload = dict(audience='azure-sql-write-broker/v1', plan_sha256=digest(plan),
                       nonce=str(uuid4()), issued_at=1000, expires_at=1300)
        payload.update(updates)
        return dict(payload, signature=base64.b64encode(key.sign(canonical(payload))).decode())
    return broker, plan, sign, calls


def test_write_once_and_replay(setup):
    broker, plan, sign, calls = setup
    args = dict(plan=plan, approval=sign())
    assert broker.invoke(args)['status'] == 'committed'
    with pytest.raises(Denied):
        broker.invoke(args)
    assert len(calls) == 1


@pytest.mark.parametrize('updates', [
    {'audience': 'azure-sql-read-broker/v1'}, {'expires_at': 1000},
    {'expires_at': 1301}, {'issued_at': 1001}, {'plan_sha256': 'b'*64},
    {'signature': 'invalid'}, {'nonce': 'invalid'},
])
def test_rejected_before_execution(setup, updates):
    broker, plan, sign, calls = setup
    approval = sign(**{k: v for k, v in updates.items() if k != 'signature'})
    approval.update({k: v for k, v in updates.items() if k == 'signature'})
    with pytest.raises(Denied):
        broker.invoke(dict(plan=plan, approval=approval))
    assert not calls


@pytest.mark.parametrize('field,value', [
    ('sql', 'DROP TABLE dbo.Ledger'), ('policy_sha256', 'b'*64),
    ('skills_sha256', 'b'*64), ('timeout_seconds', 100), ('lock_timeout_ms', 0),
])
def test_signed_tampered_plan_denied(setup, field, value):
    broker, plan, sign, calls = setup
    plan[field] = value
    with pytest.raises(Denied):
        broker.invoke(dict(plan=plan, approval=sign()))
    assert not calls


@pytest.mark.parametrize('field', ['live_enabled', 'writes_enabled'])
def test_policy_disabled(setup, field):
    broker, plan, sign, calls = setup
    broker.policy = broker.policy.model_copy(update={field: False})
    with pytest.raises(Denied):
        broker.invoke(dict(plan=plan, approval=sign()))
    assert not calls


def test_failure_consumes_approval_and_hides_provider_error(setup):
    broker, plan, sign, calls = setup

    def fail(p, q):
        calls.append(q)
        raise RuntimeError('SECRET')
    broker.executor = fail
    args = dict(plan=plan, approval=sign())
    with pytest.raises(Denied, match='outcome unknown'):
        broker.invoke(args)
    with pytest.raises(Denied):
        broker.invoke(args)
    assert len(calls) == 1


def test_target_and_injection_denied(setup):
    broker, plan, sign, calls = setup
    plan['target']['database'] = 'other'
    with pytest.raises(Denied):
        broker.invoke(dict(plan=plan, approval=sign()))
    plan['change']['column'] = 'x]; DROP TABLE Ledger;--'
    with pytest.raises(Denied):
        broker.invoke(dict(plan=plan, approval=sign()))
    assert not calls


def test_executor_transaction_and_commit(setup, monkeypatch):
    broker, plan, _, _ = setup
    events = []

    class Connection:
        def cursor(self):
            return self

        def execute(self, sql):
            events.append(sql)

        def commit(self):
            events.append('commit')

        def close(self):
            events.append('close')

    @contextmanager
    def connect(policy, *, write=False):
        assert write
        yield Connection()

    monkeypatch.setattr('azure_sql_agents.live.approved_connection', connect)
    from azure_sql_agents.writes import WritePlan
    execute_write(broker.policy, WritePlan.model_validate(plan))
    assert events == ['SET XACT_ABORT ON; SET LOCK_TIMEOUT 5000;',
                      'ALTER TABLE [dbo].[Ledger] ADD [ReviewId] int NULL;', 'commit', 'close']
