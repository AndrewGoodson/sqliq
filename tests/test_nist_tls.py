import json
import socket
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader

from azure_sql_agents.audit_report import audit_pdf
from azure_sql_agents.catalogs import bundled_register
from azure_sql_agents.cli import main
from azure_sql_agents.compliance import assessment, compliance_html
from azure_sql_agents.orchestration import guide

ROOT = Path(__file__).resolve().parents[1]


def test_tls_routes_both_endpoints_and_does_not_claim_assessment(monkeypatch):
    def reject_network(*args, **kwargs):
        raise AssertionError('TLS guidance attempted live access')
    monkeypatch.setattr(socket.socket, 'connect', reject_network)
    result = guide('nist-tls')
    assert result['workflow_guide']['assessment_catalog'] == 'nist-800-52'
    assert (ROOT / result['workflow_guide']['assessment_profile']).is_file()
    assert 'protocol support and negotiation; TLS floor is insufficient' in (
        result['azure_review']['required'])
    assert 'certificate-validation' in result['sql_review']['required_reviews']
    assert 'no-tls-floor-only-pass' in result['compliance_review']['required_reviews']
    for domain in ('azure', 'sql', 'compliance'):
        assert result[f'{domain}_review']['status'] == 'NOT_ASSESSED'
        assert result[f'{domain}_review']['live_tools'] == []
        assert result[f'{domain}_review']['assessment_catalog'] == 'nist-800-52'


def test_tls_profile_covers_server_client_sections_without_claiming_official_controls():
    register = bundled_register(ROOT, 'nist-800-52')
    sections = {i['value'] for c in register['controls'] for i in c['identifiers']}
    assert sections == {f'{n}.{i}' for n in (3, 4) for i in range(1, 9)} | {
        'Appendix C', 'Appendix D'}
    assert register['total_rules'] == 18
    assert 'not official NIST control IDs' in register['provenance']
    assert 'Not an exhaustive clause register' in register['scope']
    assert not register['compliance_claim']
    for control in register['controls']:
        assert control['status'] == 'NOT_ASSESSED'
        assert control['responsibility'] == 'UNDETERMINED'
        assert control['checks'] and control['fixes'] and control['responsibility_guidance']
        assert any(ref['href'].startswith('https://nvlpubs.nist.gov/')
                   for ref in control['references'])


def test_tls_findings_bind_profile_and_leave_other_items_unassessed(tmp_path):
    register = bundled_register(ROOT, 'nist-800-52')
    evidence = tmp_path / 'findings.json'
    document = {'benchmark_sha256': register['benchmark_sha256'], 'target': '<private SQL>',
                'findings': [{'rule_id': register['controls'][0]['rule_id'], 'status': 'FAIL',
                              'evidence': 'Private workpaper with all relevant clauses',
                              'observed_at': '2026-10-06T12:00:00+00:00',
                              'reviewer': 'Test reviewer', 'rationale': 'Test-only violation',
                              'remediation': 'Stage the reviewed correction <script>blocked</script>'}]}
    evidence.write_text(json.dumps(document))
    html = compliance_html(register, evidence)
    assert 'FAIL: 1' in html and 'NOT_ASSESSED: 17' in html and 'PASS: 0' in html
    assert '<script>' not in html and '&lt;private SQL&gt;' in html
    assert 'NIST SP 800-52 TLS review — NOT_ASSESSED' not in html
    pdf = PdfReader(BytesIO(audit_pdf(register, evidence, company_name='Test organization')))
    text = '\n'.join(page.extract_text() for page in pdf.pages)
    assert 'NOT_ASSESSED: 17' in text and 'FAIL: 1' in text
    assert 'not official NIST control IDs' in text
    assert 'TLS review remains NOT_ASSESSED' not in text
    for control in register['controls']:
        assert control['rule_id'] in text
    document['benchmark_sha256'] = '0' * 64
    evidence.write_text(json.dumps(document))
    with pytest.raises(ValueError, match='benchmark mismatch'):
        assessment(register, evidence)


def test_tls_cli_generates_offline_report_with_verified_sources(tmp_path, monkeypatch, capsys):
    def reject_network(*args, **kwargs):
        raise AssertionError('TLS report attempted live access')
    monkeypatch.setattr(socket.socket, 'connect', reject_network)
    output = tmp_path / 'tls.html'
    monkeypatch.setattr('sys.argv', ['azure-sql-agent', '--root', str(ROOT),
                                    'compliance-report', '--catalog', 'nist-800-52',
                                    '--output', str(output)])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result['live_checks'] is False
    assert 'NOT_ASSESSED: 18' in output.read_text()
    assert output.stat().st_mode & 0o777 == 0o600
