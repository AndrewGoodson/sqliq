import json
from pathlib import Path

import pytest

from azure_sql_agents.catalogs import bundled_register, nist_register
from azure_sql_agents.compliance import compliance_html

ROOT = Path(__file__).resolve().parents[1]


def test_nist_full_catalog_source_reconciliation():
    source = json.loads((ROOT / 'compliance/sources/nist/800-53-rev5.json').read_bytes())['catalog']
    expected = {}

    def visit(node):
        for control in node.get('controls', []):
            expected[control['id']] = control
            visit(control)
        for group in node.get('groups', []):
            visit(group)

    visit(source)
    result = nist_register(ROOT)
    assert result['total_rules'] == len(expected) == 1196
    assert {c['rule_id']: c['source_control'] for c in result['controls']} == expected
    assert sum(c['withdrawn'] for c in result['controls']) == 182
    for control in result['controls']:
        assert control['references']
        assert control['azure_guidance']['implementation']
        assert control['status'] == 'NOT_ASSESSED'
        assert control['responsibility'] == 'UNDETERMINED'


def test_full_bundle_no_dropped_or_duplicate_controls():
    result = bundled_register(ROOT, 'all')
    assert result['total_rules'] == 1298
    assert len({c['rule_id'] for c in result['controls']}) == 1298
    assert len(result['source_benchmarks']) == 3
    rendered = compliance_html(result)
    assert 'PASS: 0' in rendered
    assert 'NOT_ASSESSED: 1298' in rendered
    for control in result['controls']:
        assert control['rule_id'] in rendered
    assert 'Organization-defined parameters (unassigned)' in rendered
    assert 'Azure SQL Database responsibility' in rendered


def test_reject_unknown_catalog():
    with pytest.raises(ValueError):
        bundled_register(ROOT, '../../external')
