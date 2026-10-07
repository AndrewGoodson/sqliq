import hashlib
import json

import pytest

from azure_sql_agents.compliance import compliance_html, stig_register
from azure_sql_agents.orchestration import guide

XML = b'''<Benchmark xmlns="http://checklists.nist.gov/xccdf/1.2" id="synthetic">
<version>test-only</version><Group id="g"><Rule id="r1" selected="false" severity="high">
<title>&lt;script&gt;alert(1)&lt;/script&gt;</title></Rule><Group id="nested">
<Rule id="r2" severity="low"><title>Test control</title></Rule></Group></Group></Benchmark>'''


def benchmark(tmp_path, data=XML):
    path = tmp_path / 'benchmark.xml'
    path.write_bytes(data)
    return path


def test_inventory_includes_nested_and_unselected_rules(tmp_path):
    result = stig_register(benchmark(tmp_path))
    assert result['total_rules'] == 2
    assert result['assessed_rules'] == 0
    assert all(row['status'] == 'NOT_ASSESSED' for row in result['controls'])
    assert result['benchmark_sha256'] == hashlib.sha256(XML).hexdigest()
    assert result['compliance_claim'] is False


@pytest.mark.parametrize('data', [b'<!DOCTYPE foo><foo/>', b'<!ENTITY x "bad">',
    XML.replace(b'id="r2"', b'id="r1"'), XML.replace(b'id="r2"', b''),
    b'<Benchmark/>', XML.replace(b'<version>test-only</version>', b''),
    XML.decode().encode('utf-16'), b'x' * (16 * 1024 * 1024 + 1)])
def test_rejects_unsafe_or_incomplete_benchmark(tmp_path, data):
    with pytest.raises((ValueError, UnicodeError)):
        stig_register(benchmark(tmp_path, data))


def test_html_escapes_benchmark_and_preserves_unknowns(tmp_path):
    report = compliance_html(stig_register(benchmark(tmp_path)))
    assert '<script>' not in report
    assert '&lt;script&gt;' in report
    assert 'NOT_ASSESSED: 2' in report
    assert 'No database connection' in report


def evidence(tmp_path, **changes):
    doc = {'benchmark_sha256': hashlib.sha256(XML).hexdigest(), 'target': 'synthetic-db',
           'findings': [{'rule_id': 'r1', 'status': 'FAIL', 'evidence': '<img src=x>',
                         'observed_at': '2026-10-06T12:00:00+00:00', 'reviewer': 'test-reviewer',
                         'rationale': 'Synthetic violation', 'remediation': 'Review proposal'}]}
    doc.update(changes)
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps(doc))
    return path


def test_report_shows_supplied_fault_and_remaining_gap(tmp_path):
    report = compliance_html(stig_register(benchmark(tmp_path)), evidence(tmp_path))
    assert 'FAIL: 1' in report and 'NOT_ASSESSED: 1' in report
    assert '<img' not in report and '&lt;img' in report
    assert 'operator-supplied assertions' in report


def test_rejects_evidence_for_other_benchmark(tmp_path):
    with pytest.raises(ValueError):
        compliance_html(stig_register(benchmark(tmp_path)),
                        evidence(tmp_path, benchmark_sha256='wrong'))


def test_compliance_runs_on_every_workflow():
    result = guide('compliance')['compliance_review']
    assert result['status'] == 'NOT_ASSESSED'
    assert result['compliance_claim'] is False


def test_report_cli_and_no_overwrite(tmp_path, monkeypatch, capsys):
    from pathlib import Path

    from azure_sql_agents.cli import main

    target = tmp_path / 'report.html'
    monkeypatch.setattr('sys.argv', ['azure-sql-agent', '--root',
                        str(Path(__file__).resolve().parents[1]), 'compliance-report',
                        '--benchmark', str(benchmark(tmp_path)), '--output', str(target)])
    main()
    assert target.read_text().startswith('<!doctype html>')
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(capsys.readouterr().out)['live_checks'] is False
    before = target.read_bytes()
    with pytest.raises(SystemExit):
        main()
    assert target.read_bytes() == before
