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


OPERATIONS = [
    ({'kind': 'create_index', 'index': 'IX_Ledger_ReviewId', 'column': 'ReviewId'},
     'CREATE NONCLUSTERED INDEX [IX_Ledger_ReviewId] ON [dbo].[Ledger] '
     '([ReviewId]) WITH (MAXDOP = 1);'),
    ({'kind': 'update_statistics', 'statistic': 'ST_Ledger_ReviewId'},
     'UPDATE STATISTICS [dbo].[Ledger] [ST_Ledger_ReviewId] WITH MAXDOP = 1;'),
    ({'kind': 'reorganize_index', 'index': 'IX_Ledger_ReviewId'},
     'ALTER INDEX [IX_Ledger_ReviewId] ON [dbo].[Ledger] '
     'REORGANIZE WITH (LOB_COMPACTION = OFF);'),
]


def operation_plan(setup, operation):
    from azure_sql_agents.writes import parse_change
    broker, plan, _, _ = setup
    change = {k: v for k, v in plan['change'].items()
              if k not in {'kind', 'column', 'data_type'}}
    change.update(operation)
    updated = write_plan(broker.policy, parse_change(change), 'a'*64)
    plan.clear()
    plan.update(updated.model_dump(mode='json'))
    return updated


@pytest.mark.parametrize('operation,sql', OPERATIONS)
def test_typed_operations_exact_approval_and_replay(setup, operation, sql):
    broker, plan, sign, calls = setup
    operation_plan(setup, operation)
    assert plan['sql'] == sql
    args = dict(plan=plan, approval=sign())
    assert broker.invoke(args)['status'] == 'committed'
    with pytest.raises(Denied):
        broker.invoke(args)
    assert len(calls) == 1


@pytest.mark.parametrize('operation,sql', OPERATIONS)
@pytest.mark.parametrize('mutation', ['sql', 'object', 'irrelevant', 'blank_evidence', 'target'])
def test_operation_tampering_even_when_signed(setup, operation, sql, mutation):
    broker, plan, sign, calls = setup
    operation_plan(setup, operation)
    if mutation == 'sql':
        plan['sql'] += ' DROP TABLE Ledger;'
    elif mutation == 'object':
        field = 'statistic' if 'statistic' in operation else 'index'
        plan['change'][field] = 'x]; DROP TABLE Ledger;--'
    elif mutation == 'irrelevant':
        plan['change']['data_type'] = 'int'
    elif mutation == 'blank_evidence':
        plan['change']['staging_evidence'] = '  '
    else:
        plan['target']['database'] = 'OtherDb'
    with pytest.raises(Denied):
        broker.invoke(dict(plan=plan, approval=sign()))
    assert not calls


@pytest.mark.parametrize('operation,sql', OPERATIONS)
def test_new_operation_does_not_reuse_previous_signature(setup, operation, sql):
    broker, plan, sign, calls = setup
    approval = sign()
    operation_plan(setup, operation)
    with pytest.raises(Denied):
        broker.invoke(dict(plan=plan, approval=approval))
    assert not calls


@pytest.mark.parametrize('operation,sql', OPERATIONS)
@pytest.mark.parametrize('fail', [False, True])
def test_operation_execution_transaction_timeout_and_failure(setup, monkeypatch,
                                                            operation, sql, fail):
    broker, plan, sign, calls = setup
    typed_plan = operation_plan(setup, operation)
    events = []

    class Connection:
        timeout = None
        autocommit = False

        def cursor(self):
            assert self.timeout == 10
            assert self.autocommit == (operation['kind'] == 'reorganize_index')
            return self

        def execute(self, statement):
            events.append(statement)
            if statement == sql and fail:
                raise RuntimeError('SECRET provider failure')

        def commit(self):
            events.append('commit')

        def close(self):
            events.append('cursor-close')

    @contextmanager
    def connect(policy, *, write=False):
        assert write
        try:
            yield Connection()
        except Exception:
            events.append('connection-rollback')
            raise
        finally:
            events.append('connection-close')

    monkeypatch.setattr('azure_sql_agents.live.approved_connection', connect)
    broker.executor = execute_write
    args = dict(plan=plan, approval=sign())
    if fail:
        with pytest.raises(Denied, match='outcome unknown'):
            broker.invoke(args)
        with pytest.raises(Denied):
            broker.invoke(args)
        assert 'commit' not in events
        assert 'connection-rollback' in events
    else:
        broker.invoke(args)
        assert ('commit' in events) == (typed_plan.change.kind != 'reorganize_index')
    assert events[:2] == ['SET XACT_ABORT ON; SET LOCK_TIMEOUT 5000;', sql]
    assert events.count(sql) == 1
    assert 'cursor-close' in events
    assert events[-1] == 'connection-close'


def test_legacy_change_input_and_plan_serialization(setup):
    from azure_sql_agents.writes import parse_change
    broker, plan, _, _ = setup
    legacy = dict(plan['change'])
    legacy.pop('kind')
    assert canonical(write_plan(broker.policy, parse_change(legacy), 'a'*64)) == canonical(plan)


@pytest.mark.parametrize('kind', ['drop_column', 'rebuild_index', 'execute_sql'])
def test_unrecognized_operations_rejected(setup, kind):
    broker, plan, sign, calls = setup
    plan['change']['kind'] = kind
    with pytest.raises(Denied):
        broker.invoke(dict(plan=plan, approval=sign()))
    assert not calls
