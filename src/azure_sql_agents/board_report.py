"""Deterministic, offline IT/board briefing; no customer assessment implied."""
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

SOURCES = [
    ('Microsoft shared responsibility',
     'https://learn.microsoft.com/en-us/azure/security/fundamentals/shared-responsibility'),
    ('Azure SQL security overview',
     'https://learn.microsoft.com/en-us/azure/azure-sql/database/security-overview'),
    ('Microsoft compliance offerings',
     'https://learn.microsoft.com/en-us/azure/compliance/offerings/'),
    ('NIST SP 800-52 Rev. 2', 'https://csrc.nist.gov/pubs/sp/800/52/r2/final'),
    ('DISA STIG distribution', 'https://public.cyber.mil/stigs/downloads/'),
]

# Responsibility guidance is not an inheritance determination or a control pass.
RESPONSIBILITIES = [
    ('Physical facilities, hosts and hypervisor', 'Provider',
     'Obtain current provider assurance covering the service, region and review period. [1,3]'),
    ('Managed OS and SQL service maintenance', 'Provider for SQL Database PaaS',
     'Verify service scope and maintenance commitments. SQL Server on a VM has different duties. [1,2]'),
    ('Data, classification, accounts and access', 'Customer',
     'IT/security: approve least privilege, access reviews, segregation of duties and data retention. [1]'),
    ('Network and application protections', 'Shared',
     'IT: demonstrate private connectivity, application security, approved identities and egress. [1,2]'),
    ('Encryption, keys and TLS', 'Shared implementation',
     'Security: verify configured encryption, key custody, clients and full applicable TLS requirements. [2,4]'),
    ('Audit, detection and incident response', 'Shared implementation',
     'Security: configure exports, retention, alert ownership and incident exercises. [2]'),
    ('Recovery and financial integrity', 'Customer outcomes; provider capabilities',
     'DBA/finance: approve recovery objectives; test restores and ledger reconciliation. [2]'),
    ('Compliance scope and exceptions', 'Customer',
     'Compliance: approve applicability, evidence, exceptions and independent review. [3,5]'),
]


def _logo_bytes(path: Path) -> bytes:
    """Bound and decode local raster artwork; never fetch URLs or parse SVG."""
    from PIL import Image

    with path.open('rb') as source:
        payload = source.read(2 * 1024 * 1024 + 1)
    if len(payload) > 2 * 1024 * 1024:
        raise ValueError('Logo must be at most 2 MiB')
    try:
        decoded = Image.open(BytesIO(payload), formats=['PNG', 'JPEG'])
    except OSError:
        raise ValueError('Logo must be a valid PNG or JPEG') from None
    with decoded as picture:
        if picture.format not in {'PNG', 'JPEG'}:
            raise ValueError('Logo must be PNG or JPEG')
        if picture.width * picture.height > 4_000_000:
            raise ValueError('Logo exceeds 4 million pixels')
        picture.load()
        normalized = BytesIO()
        picture.convert('RGBA').save(normalized, format='PNG')
        return normalized.getvalue()


def board_pdf(company_logo: Path | None = None, company_name: str | None = None) -> bytes:
    """Generate a public, generic proposal with no external resources or AI calls."""
    from reportlab.lib import colors
    from reportlab.lib.utils import ImageReader

    if company_name is not None and (not company_name.strip() or len(company_name) > 80
                                     or any(ord(c) < 32 for c in company_name)):
        raise ValueError('Company name must be 1-80 printable characters')
    brand = ImageReader(BytesIO(files('azure_sql_agents').joinpath(
        'assets/sqliq-logo.png').read_bytes()))
    customer = ImageReader(BytesIO(_logo_bytes(company_logo))) if company_logo else None
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    navy = colors.HexColor('#132f40')
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='BodyIQ', fontName='Helvetica', fontSize=10,
                              leading=14, spaceAfter=10, textColor=navy))
    styles.add(ParagraphStyle(name='CellIQ', fontSize=8.5, leading=11, textColor=navy))
    styles['Title'].textColor = navy
    styles['Heading1'].textColor = navy
    out = BytesIO()
    doc = SimpleDocTemplate(out, pagesize=(612, 792), rightMargin=42, leftMargin=42,
                            topMargin=92, bottomMargin=48, title='SQLIQ | IT and board briefing',
                            author='SQLIQ', pageCompression=1)
    story = []

    def p(text, style='BodyIQ'):
        return Paragraph(escape(text), styles[style])

    def section(title, text):
        story.extend([p(title, 'Heading2'), p(text)])

    story.extend([p('SQLIQ', 'Title'), p('AI-assisted Azure SQL governance', 'Heading1'),
                  p('IT & BOARD BRIEFING | PUBLIC REFERENCE | 6 OCTOBER 2026'),
                  Spacer(1, 16), p('Decision sought', 'Heading2'),
                  p('Sponsor a governed evaluation of AI-assisted SQL review. Authorize an offline '
                    'pilot using synthetic evidence first. Production access and AI data disclosure '
                    'require separate organizational approval and verified deployment controls.')])
    section('Assurance status: NOT ASSESSED',
            'No company, tenant or database has been assessed by this briefing. No live connection '
            'was made. This is a proposal and responsibility guide, not an audit opinion, legal '
            'determination or certification. All organization-specific evidence remains outstanding.')
    section('Where AI adds value',
            'Codex and Claude can coordinate Azure, SQL and compliance review through pinned AEF '
            'workflows: explain evidence, propose migration and schema plans, analyze supplied '
            'performance plans, draft control mappings and prioritize remediation. Each conclusion '
            'needs traceable evidence and human review. The graph runtime itself is deterministic; '
            'it does not call a model. AI assistance comes from the authorized agent host.')
    section('Authority stays with people',
            'Live reads default to disabled and require an exact, fresh, externally signed approval. '
            'The read broker permits bounded catalog metadata reads. Writes need separate exact signed approval. '
            'Only nullable-column additions are supported. AI cannot sign, grant itself access or promote learned '
            'candidates. Deployment isolation must be independently demonstrated.')
    section('Board oversight',
            'Assign an accountable executive, IT control owner and independent reviewer. Require '
            'evidence of data handling, identity separation, reproducible findings and recovery '
            'readiness before expanding scope. Risk acceptance needs a named owner and expiry.')
    story.append(PageBreak())
    story.extend([p('01 / Azure and customer responsibility', 'Heading1'),
                  p('Baseline: Azure SQL Database PaaS in the public cloud. Provider responsibility '
                    'is a candidate for inherited assurance, never an automatic PASS. Match provider '
                    'attestations to service, region, period, exclusions and customer obligations. '
                    'Managed Instance and SQL Server on VMs require separate scope reviews. [1-3]')])
    data = [[p(x, 'CellIQ') for x in ('Control area', 'Responsibility', 'Evidence / accountable owner')]]
    data += [[p(x, 'CellIQ') for x in row] for row in RESPONSIBILITIES]
    table = Table(data, colWidths=[143, 115, 270], repeatRows=1, hAlign='LEFT')
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#dceee9')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), .4, colors.HexColor('#ccd7dd')),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.extend([table, Spacer(1, 10), p('Inheritance evidence: control ID, provider report reference, '
        'coverage dates, service scope, customer complementary controls, reviewer and residual gap. '
        'Keep restricted provider reports outside the public repository.')])
    story.append(PageBreak())
    story.append(p('02 / AI adoption with explicit controls', 'Heading1'))
    for title, body in [
        ('Data exposure and confidentiality', 'Start with synthetic or approved redacted inputs. '
         'Schema names, plans and error messages can be sensitive even without business rows. '
         'The host can transmit supplied context to its model provider. Local report generation '
         'does not establish that Codex or Claude is offline. IT must verify provider contracts, '
         'retention, training terms, residency and approved classifications before enabling a host.'),
        ('Accuracy and hostile input', 'Treat retrieved documents, skills and database metadata as '
         'untrusted evidence. Require source references and reproducible checks. Unsupported claims '
         'remain NOT_ASSESSED. Review model conclusions independently; prompts are not enforcement.'),
        ('Credentials and separation of duties', 'Keep reviewer keys and broker credentials outside '
         'agent reach. Demonstrate effective read-only permissions, separate identities, immutable '
         'audit export, network restrictions and tamper-resistant deployment. These production '
         'controls are requirements, not verified features of a customer installation.'),
        ('Token efficiency', 'Use code for parsing, rule inventory, hashing, validation and PDF rendering. '
         'Send only approved, minimal evidence excerpts to AI for interpretation. Reuse reviewed '
         'summaries keyed to source hashes; invalidate them on change. Set per-task token budgets '
         'and escalate reasoning only for ambiguity or high-impact changes. Never trim control '
         'coverage or approval checks to save tokens.'),
        ('Learning and supply chain', 'Pin and review imported skills. Learning uses de-identified '
         'enumerated outcomes and creates review candidates only. No model retraining, automatic '
         'policy change or unrestricted memory ingestion. Require tests and release review.'),
        ('Adoption decision gates', 'Phase 1: policy-approved synthetic offline evaluation. Phase 2: '
         'approved host and classified evidence review. Phase 3: isolated broker with independently '
         'verified controls and signed reads. Record owner, evidence and decision at each gate. '
         'If organizational policy prohibits AI, obtain a policy exception or use the deterministic '
         'CLI without an AI host; never route around the restriction.'),
    ]:
        section(title, body)
    story.append(PageBreak())
    story.append(p('03 / Evidence and assurance plan', 'Heading1'))
    section('SQL STIG and TLS', 'Select the exact official product benchmark and release. Inventory '
            'every rule and record applicability, responsibility, evidence, result and reviewer. '
            'Provider-managed rules still require an inheritance rationale. Review NIST SP 800-52 '
            'TLS requirements separately; a minimum TLS setting alone is insufficient. [4,5]')
    section('Finance applicability', 'Compliance and counsel determine applicable obligations and '
            'versions, including SOX, GLBA or PCI DSS where relevant. Finance validates segregation '
            'of duties, change approvals, retention and reconciliations. Azure attestations do not '
            'establish the company\'s compliance. No universal finance SQL checklist applies. [3]')
    section('Evidence needed before production', 'IT: effective grants, network and key controls, '
            'restore exercises and audit retention. Security: host data handling, threat review, '
            'approval identity, replay and isolation tests. Compliance: benchmark scope, provider '
            'assurance mapping and time-limited exceptions. Board: accountable owner and accepted '
            'residual risks. Status for all customer evidence: NOT ASSESSED.')
    section('Report interpretation', 'The STIG HTML command inventories every supplied XCCDF rule '
            'and displays optional operator findings. It does not run a full database scan or '
            'independently verify assertions. Missing findings remain NOT_ASSESSED. This PDF is '
            'a generic governance briefing; it contains no customer-specific findings.')
    story.append(p('Primary sources | verify currency at review time', 'Heading2'))
    for index, (label, url) in enumerate(SOURCES, 1):
        story.extend([p(f'[{index}] {label}', 'CellIQ'), p(url, 'CellIQ'), Spacer(1, 7)])

    def footer(canvas, document):
        canvas.saveState()
        canvas.drawImage(brand, 42, 733, width=36, height=36, mask='auto',
                         preserveAspectRatio=True, anchor='c')
        canvas.setFillColor(navy)
        canvas.setFont('Helvetica-Bold', 14)
        canvas.drawString(86, 746, 'SQLIQ')
        if customer:
            width, height = customer.getSize()
            scale = min(150 / width, 32 / height)
            canvas.drawImage(customer, 570 - width * scale, 740,
                             width=width * scale, height=height * scale, mask='auto')
        if company_name:
            canvas.setFont('Helvetica', 8)
            # Fit long organization names without clipping or distorting artwork.
            from reportlab.pdfbase.pdfmetrics import stringWidth
            size = min(8, 240 / max(stringWidth(company_name, 'Helvetica', 1), 1))
            canvas.setFont('Helvetica', size)
            canvas.drawRightString(570, 726, company_name)
        canvas.setStrokeColor(colors.HexColor('#ccd7dd'))
        canvas.line(42, 716, 570, 716)
        canvas.setStrokeColor(colors.HexColor('#ccd7dd'))
        canvas.line(42, 36, 570, 36)
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(navy)
        canvas.drawString(42, 24, 'SQLIQ | Public governance proposal | No customer assessment')
        canvas.drawRightString(570, 24, str(document.page))
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return out.getvalue()
