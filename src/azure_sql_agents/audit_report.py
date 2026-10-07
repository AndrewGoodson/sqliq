"""Offline full-benchmark audit evidence report; no live checks or certification."""
from collections import Counter
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from .board_report import _logo_bytes
from .compliance import assessment, control_details


def audit_pdf(register: dict, evidence: Path | None = None,
              company_logo: Path | None = None, company_name: str | None = None) -> bytes:
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate

    scope, findings = assessment(register, evidence)
    if company_name is not None and (not company_name.strip() or len(company_name) > 80
                                    or any(ord(c) < 32 for c in company_name)):
        raise ValueError('Company name must be 1-80 printable characters')
    brand = ImageReader(BytesIO(files('azure_sql_agents').joinpath(
        'assets/sqliq-logo.png').read_bytes()))
    customer = ImageReader(BytesIO(_logo_bytes(company_logo))) if company_logo else None
    styles = getSampleStyleSheet()
    styles['BodyText'].spaceAfter = 9
    styles['BodyText'].splitLongWords = True
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=(612, 792), topMargin=96,
                            bottomMargin=52, leftMargin=42, rightMargin=42,
                            title='SQLIQ SQL control audit evidence report', author='SQLIQ')

    def p(value, style='BodyText'):
        return Paragraph(escape(str(value)).replace('\n', '<br/>'), styles[style])

    def header(canvas, document):
        canvas.saveState()
        canvas.drawImage(brand, 42, 737, width=115, height=30,
                         preserveAspectRatio=True, mask='auto')
        if customer:
            canvas.drawImage(customer, 410, 737, width=160, height=30,
                             preserveAspectRatio=True, mask='auto')
        if company_name:
            canvas.setFont('Helvetica', 8)
            canvas.drawRightString(570, 725, company_name)
        canvas.setFont('Helvetica', 8)
        canvas.drawString(42, 28, 'SQLIQ | Operator-reported evidence | No certification')
        canvas.drawRightString(570, 28, str(document.page))
        canvas.restoreState()

    counts = Counter(findings.get(c['rule_id'], {}).get('status', 'NOT_ASSESSED')
                     for c in register['controls'])
    tls_scope = ('TLS section-level review results appear below. Review all applicable source '
                 'clauses before accepting an item result; these SQLIQ items are not an '
                 'exhaustive clause register or overall conformance determination.'
                 if register.get('mode') == 'offline_tls_review_profile' else
                 'TLS review remains NOT_ASSESSED: protocols, negotiation, cipher suites, '
                 'certificates, validation and cryptographic modules require separate evidence.')
    story = [p('SQL control audit evidence report', 'Title'), p('Target: ' + scope),
             p(f"Benchmark: {register['benchmark_id']} / {register['version']}"),
             p('SHA-256: ' + register['benchmark_sha256']),
             p(f"Coverage: all {register['total_rules']} selected register entries included."),
             p('Assessment coverage: ' + register.get('scope', 'Verify source scope')),
             p('Provenance: ' + register.get('provenance', 'Verify source origin')),
             p(' | '.join(f'{s}: {counts[s]}' for s in
                         ('PASS', 'FAIL', 'NOT_APPLICABLE', 'NOT_ASSESSED'))),
             p('Offline evidence review. No database connection or automated checks performed. '
               'Results are operator assertions, not independently verified audit conclusions. '
               'Missing evidence is NOT_ASSESSED. Verify official benchmark origin, product '
               'applicability, evidence freshness and reviewer authority. No overall pass issued.'),
             p('Framework boundaries', 'Heading2'),
             p('DISA publishes SQL STIG benchmarks. NIST SP 800-52 Rev. 2 addresses TLS; '
               'this report does not claim full NIST or financial framework assessment. '
               + tls_scope),
             p('NIST source: https://csrc.nist.gov/pubs/sp/800/52/r2/final'),
             p('Azure inheritance remains UNDETERMINED per rule. Provider responsibility '
               'does not establish a pass. Obtain service-specific assurance and customer '
               'configuration evidence. Finance owners must approve applicable obligations.'),
             p('Microsoft source: https://learn.microsoft.com/en-us/azure/security/'
               'fundamentals/shared-responsibility'),
             p('Recommendations are proposals. Validate fixes in staging with recovery plans. '
               'Live reads and writes each require exact externally signed approval.')]
    for control in register['controls']:
        story.extend([PageBreak(), p(control['rule_id'], 'Heading1'),
                      p(control.get('title') or 'Untitled control', 'Heading2')])
        for label, value in control_details(control, findings.get(control['rule_id'], {})):
            story.extend([p(label, 'Heading3'), p(value)])
    doc.build(story, onFirstPage=header, onLaterPages=header)
    return output.getvalue()
