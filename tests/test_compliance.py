import hashlib
import json

import pytest

from azure_sql_agents.compliance import compliance_html, control_details, stig_register
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


def test_audit_pdf_full_coverage(tmp_path):
    from io import BytesIO

    from pypdf import PdfReader

    from azure_sql_agents.audit_report import audit_pdf

    source = tmp_path / 'benchmark.xml'
    source.write_text('''<Benchmark xmlns="http://checklists.nist.gov/xccdf/1.2" id="demo">
    <version>synthetic</version><Rule id="control-one" severity="high"><title>Demo</title>
    <description>Source requirement</description><reference href="https://example.org">
    Reference title</reference><check><check-content>Source check</check-content></check>
    <fixtext>Source fix</fixtext><ident system="CCI">CCI-TEST</ident></Rule>
    <Rule id="control-two"><title>Second control</title></Rule></Benchmark>''')
    register = stig_register(source)
    pdf = PdfReader(BytesIO(audit_pdf(register, company_name='Demo organization')))
    text = '\n'.join(page.extract_text() for page in pdf.pages)
    for phrase in ['control-one', 'control-two', 'Source requirement', 'Source check',
                   'Source fix', 'CCI-TEST', 'https://example.org', 'NOT_ASSESSED: 2',
                   'PASS: 0', 'Recommendation']:
        assert phrase in text
    assert all('Demo organization' in page.extract_text() for page in pdf.pages)
    assert 'Source fix' in compliance_html(register)


def test_audit_pdf_rejects_mismatched_evidence(tmp_path):
    from azure_sql_agents.audit_report import audit_pdf

    source = tmp_path / 'benchmark.xml'
    source.write_bytes(XML)
    bad = tmp_path / 'evidence.json'
    bad.write_text('{"benchmark_sha256":"wrong","target":"demo","findings":[]}')
    with pytest.raises(ValueError, match='benchmark mismatch'):
        audit_pdf(stig_register(source), bad)


def description_register(tmp_path, description):
    from html import escape

    data = XML.replace(b'<title>Test control</title>',
                       ('<title>Test control</title><description>' + escape(description)
                        + '</description>').encode())
    return stig_register(benchmark(tmp_path, data))


def test_disa_description_is_readable_in_html_and_pdf_without_changing_source(tmp_path):
    from copy import deepcopy
    from io import BytesIO

    from pypdf import PdfReader

    from azure_sql_agents.audit_report import audit_pdf

    description = ('<VulnDiscussion>Protect financial records &amp; audit evidence.</VulnDiscussion>'
                   '<FalsePositives></FalsePositives><FalseNegatives/>'
                   '<Documentable>false</Documentable>'
                   '<Mitigations>Retain an independently reviewed exception.</Mitigations>'
                   '<SeverityOverrideGuidance>Review severity.</SeverityOverrideGuidance>'
                   '<PotentialImpacts>Reconcile posting totals.</PotentialImpacts>'
                   '<ThirdPartyTools>Review tool scope.</ThirdPartyTools>'
                   '<MitigationControl>Approved compensating control.</MitigationControl>'
                   '<Responsibility>Customer control owner.</Responsibility>'
                   '<IAControls>IA-TEST</IAControls>')
    register = description_register(tmp_path, description)
    original = deepcopy(register)
    source_bytes = (tmp_path / 'benchmark.xml').read_bytes()
    html = compliance_html(register)
    pdf = PdfReader(BytesIO(audit_pdf(register, company_name='Demo organization')))
    text = '\n'.join(page.extract_text() for page in pdf.pages)
    expected = [
        'Discussion: Protect financial records', 'Documentable: false',
        'Mitigations: Retain an independently reviewed exception.',
        'Severity override guidance: Review severity.',
        'Potential impacts: Reconcile posting totals.', 'Third-party tools: Review tool scope.',
        'Mitigation control: Approved compensating control.',
        'Responsibility: Customer control owner.', 'IA controls: IA-TEST',
    ]
    for phrase in expected:
        assert phrase in html and phrase in text
    for field in ['VulnDiscussion', 'FalsePositives', 'FalseNegatives']:
        assert field not in html and field not in text
    assert '<br><br>Documentable: false' in html
    assert register == original
    assert register['controls'][1]['requirement'] == description
    assert register['benchmark_sha256'] == hashlib.sha256(source_bytes).hexdigest()
    assert (tmp_path / 'benchmark.xml').read_bytes() == source_bytes


@pytest.mark.parametrize('description', [
    'Ordinary requirement with <angle brackets> & a comparison.',
    '<VulnDiscussion>Unclosed requirement',
    '<VulnDiscussion>Known.</VulnDiscussion><FutureField>Keep this.</FutureField>',
    '<VulnDiscussion><b>Nested content.</b></VulnDiscussion>',
    '<VulnDiscussion source="retain">Attributed requirement.</VulnDiscussion>',
    '<VulnDiscussion>Known.</VulnDiscussion>Trailing content.',
    'Leading content.<VulnDiscussion>Known.</VulnDiscussion>',
    '<VulnDiscussion><![CDATA[Retain this.]]></VulnDiscussion>',
    '<!-- Retain this. --><VulnDiscussion>Known.</VulnDiscussion>',
    '<?instruction retain?><VulnDiscussion>Known.</VulnDiscussion>',
    '<!DOCTYPE x [<!ENTITY value "Retain this.">]><VulnDiscussion>&value;</VulnDiscussion>',
])
def test_unknown_description_formats_remain_complete_and_escaped(tmp_path, description):
    from html import escape

    register = description_register(tmp_path, description)
    details = dict(control_details(register['controls'][1], {}))
    assert details['Requirement'] == description
    assert escape(description, quote=True) in compliance_html(register)


def test_description_markup_cannot_become_html_or_pdf_instructions(tmp_path):
    from io import BytesIO

    from pypdf import PdfReader

    from azure_sql_agents.audit_report import audit_pdf

    description = ('<VulnDiscussion>&lt;script&gt;visible text&lt;/script&gt;'
                   '</VulnDiscussion>')
    register = description_register(tmp_path, description)
    html = compliance_html(register)
    assert '<script>' not in html
    assert 'Discussion: &lt;script&gt;visible text&lt;/script&gt;' in html
    pdf = PdfReader(BytesIO(audit_pdf(register, company_name='Demo organization')))
    text = '\n'.join(page.extract_text() for page in pdf.pages)
    assert 'Discussion: <script>visible text</script>' in text


@pytest.mark.parametrize('description', [
    '<FutureField>Unrecognized source content.</FutureField>',
    '<VulnDiscussion>Unclosed source content.',
])
def test_unrecognized_description_is_preserved_in_pdf(tmp_path, description):
    from io import BytesIO

    from pypdf import PdfReader

    from azure_sql_agents.audit_report import audit_pdf

    register = description_register(tmp_path, description)
    pdf = PdfReader(BytesIO(audit_pdf(register, company_name='Demo organization')))
    text = '\n'.join(page.extract_text() for page in pdf.pages)
    assert description in text


def test_description_directives_are_not_passed_to_xml_parser(tmp_path, monkeypatch):
    description = '<!DOCTYPE x [<!ENTITY value "Retain this.">]><VulnDiscussion>&value;'
    register = description_register(tmp_path, description)

    def unexpected_parse(*args, **kwargs):
        pytest.fail('Display must reject XML directives before parsing')

    monkeypatch.setattr('azure_sql_agents.compliance.ET.fromstring', unexpected_parse)
    assert dict(control_details(register['controls'][1], {}))['Requirement'] == description


def test_empty_disa_fields_do_not_show_markup(tmp_path):
    description = '<VulnDiscussion/><FalsePositives> </FalsePositives>'
    register = description_register(tmp_path, description)
    assert dict(control_details(register['controls'][1], {}))['Requirement'] == (
        'Not supplied by benchmark')
    assert register['controls'][1]['requirement'] == description
