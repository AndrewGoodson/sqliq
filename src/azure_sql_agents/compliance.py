"""Offline, complete XCCDF rule inventory. Never evaluates or executes checks."""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024
NAMESPACES = {"http://checklists.nist.gov/xccdf/1.1", "http://checklists.nist.gov/xccdf/1.2"}
DISA_DESCRIPTION_LABELS = {
    "VulnDiscussion": "Discussion",
    "FalsePositives": "False positives",
    "FalseNegatives": "False negatives",
    "Documentable": "Documentable",
    "Mitigations": "Mitigations",
    "SeverityOverrideGuidance": "Severity override guidance",
    "PotentialImpacts": "Potential impacts",
    "ThirdPartyTools": "Third-party tools",
    "MitigationControl": "Mitigation control",
    "Responsibility": "Responsibility",
    "IAControls": "IA controls",
}


def stig_register(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Benchmark too large")
    # UTF-8 only: reject DTDs/entities before handing bytes to the XML parser.
    text = raw.decode("utf-8-sig")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper() or "\x00" in text:
        raise ValueError("Unsafe XML")
    root = ET.fromstring(text)
    namespace = root.tag.partition("}")[0].removeprefix("{")
    if namespace not in NAMESPACES or root.tag != f"{{{namespace}}}Benchmark":
        raise ValueError("Expected an XCCDF benchmark")
    ns = {"x": namespace}
    benchmark_id = root.get("id")
    version = root.findtext("x:version", namespaces=ns)
    if not benchmark_id or not version:
        raise ValueError("Benchmark identity/version required")
    rules = root.findall(".//x:Rule", ns)
    if not rules or len(rules) > 10000:
        raise ValueError("Invalid rule count")
    seen = set()
    controls = []
    for rule in rules:
        identity = rule.get("id")
        if not identity or identity in seen:
            raise ValueError("Missing or duplicate rule ID")
        seen.add(identity)
        controls.append({
            "rule_id": identity, "version": rule.findtext("x:version", namespaces=ns),
            "title": rule.findtext("x:title", namespaces=ns), "severity": rule.get("severity"),
            "requirement": " ".join(rule.find("x:description", ns).itertext())
            if rule.find("x:description", ns) is not None else "Not supplied by benchmark",
            "checks": [" ".join(item.itertext()) + " " + " ".join(
                f"Check reference: {ref.get('href', '')} {ref.get('name', '')}"
                for ref in item.findall("x:check-content-ref", ns))
                for item in rule.findall(".//x:check", ns)],
            "fixes": [" ".join(item.itertext()) for item in rule.findall("x:fixtext", ns)],
            "references": [{"href": item.get("href", ""),
                            "text": " ".join(item.itertext())}
                           for item in rule.findall("x:reference", ns)],
            "identifiers": [{"system": item.get("system"), "value": item.text}
                            for item in rule.findall("x:ident", ns)],
            "status": "NOT_ASSESSED", "applicability": "UNDETERMINED",
            "responsibility": "UNDETERMINED", "evidence": [], "owner": None,
            "reviewer": None, "rationale": None, "remediation_proposal": None,
        })
    return {
        "mode": "offline_stig_inventory", "benchmark_id": benchmark_id, "version": version,
        "benchmark_sha256": hashlib.sha256(raw).hexdigest(),
        "release": root.findtext("x:plain-text[@id='release-info']", namespaces=ns),
        "provenance": "UNVERIFIED: reviewer must verify official DISA origin and release",
        "scope": "All Rule elements, including profile-unselected rules; no filtering",
        "total_rules": len(rules), "assessed_rules": 0, "compliance_claim": False,
        "controls": controls,
    }


def compliance_review() -> dict:
    return {
        "selected_skill": "skills/local/sql-compliance-review/SKILL.md",
        "status": "NOT_ASSESSED", "compliance_claim": False,
        "required": ["exact DISA product benchmark and release; every rule accounted for",
                     "Azure service applicability and shared responsibility",
                     "AI governance, token efficiency and IT/board briefing",
                     "NIST SP 800-52 Rev. 2 TLS evidence beyond a minimum TLS setting",
                     "finance framework scope approved by control owners",
                     "evidence, exceptions, remediation, owner and independent review"],
        "live_collection": "disabled; existing approved catalog reads are insufficient",
    }


def assessment(register: dict, evidence_path: Path | None = None) -> tuple:
    """Render untrusted, operator-supplied findings; never infer a passing check."""
    import json
    from datetime import datetime

    findings = {}
    scope = "No database evidence supplied"
    if evidence_path is not None:
        with evidence_path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Evidence too large")
        document = json.loads(raw)
        if not isinstance(document, dict) or set(document) != {"benchmark_sha256", "target", "findings"}:
            raise ValueError("Invalid evidence document")
        if document["benchmark_sha256"] != register["benchmark_sha256"]:
            raise ValueError("Evidence benchmark mismatch")
        scope = document["target"]
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("Target required")
        if not isinstance(document["findings"], list) or len(document["findings"]) > 10000:
            raise ValueError("Invalid findings")
        known = {row["rule_id"] for row in register["controls"]}
        required = {"rule_id", "status", "evidence", "observed_at", "reviewer", "rationale",
                    "remediation"}
        for item in document["findings"]:
            if not isinstance(item, dict) or set(item) != required:
                raise ValueError("Invalid finding fields")
            if any(not isinstance(value, str) or not value.strip() for value in item.values()):
                raise ValueError("Finding fields must be nonempty strings")
            if item["rule_id"] not in known or item["rule_id"] in findings:
                raise ValueError("Unknown or duplicate finding")
            if item["status"] not in {"PASS", "FAIL", "NOT_APPLICABLE", "NOT_ASSESSED"}:
                raise ValueError("Invalid status")
            if datetime.fromisoformat(item["observed_at"]).tzinfo is None:
                raise ValueError("Evidence timestamp requires timezone")
            findings[item["rule_id"]] = item
    return scope, findings


def compliance_html(register: dict, evidence_path: Path | None = None) -> str:
    import html

    escape = lambda value: html.escape(str(value), quote=True)  # noqa: E731
    scope, findings = assessment(register, evidence_path)
    tls_heading = ("NIST SP 800-52 TLS review scope" if
                   register.get("mode") == "offline_tls_review_profile" else
                   "NIST SP 800-52 TLS review — NOT_ASSESSED")
    counts = dict.fromkeys(["PASS", "FAIL", "NOT_APPLICABLE", "NOT_ASSESSED"], 0)
    rows = []
    for control in register["controls"]:
        finding = findings.get(control["rule_id"], {})
        status = finding.get("status", "NOT_ASSESSED")
        counts[status] += 1
        cells = [control["rule_id"], control["title"], control["severity"], status,
                 finding.get("evidence", "Missing"), finding.get("observed_at", "Unknown"),
                 finding.get("reviewer", "Unassigned"), finding.get("rationale", "Review required"),
                 finding.get("remediation", "Determine applicability and collect evidence")]
        rows.append('<tr>' + ''.join(f'<td>{escape(cell)}</td>' for cell in cells) + '</tr>')
    details = []
    for control in register["controls"]:
        details.append("<section><h2>" + escape(control["rule_id"]) + "</h2>" +
                       "".join("<p><strong>" + escape(label) + ":</strong> "
                               + escape(value).replace("\n", "<br>")
                               + "</p>" for label, value in control_details(control,
                                   findings.get(control["rule_id"], {}))) + "</section>")
    summary = ' · '.join(f'{key}: {value}' for key, value in counts.items())
    return '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>SQLIQ SQL compliance review</title><style>
body{font:16px system-ui;color:#17313d;background:#f5f7f8;margin:3vw;overflow-wrap:anywhere}
h1{font-size:2.3rem}h2{margin-top:2rem}.table{overflow:auto}table{border-collapse:collapse;width:100%;background:white}
th,td{padding:12px;border:1px solid #ccd7dd;text-align:left;vertical-align:top;min-width:110px;overflow-wrap:anywhere}
th{background:#17313d;color:white}p{max-width:95ch;line-height:1.6}
</style><h1>SQLIQ | SQL compliance review</h1>''' + f'''
<p><strong>Target:</strong> {escape(scope)}</p>
<p><strong>Benchmark:</strong> {escape(register['benchmark_id'])} / {escape(register['version'])}<br>
<strong>SHA-256:</strong> {escape(register['benchmark_sha256'])}</p>
<p><strong>Assessment coverage:</strong> {escape(register.get('scope', 'Verify source scope'))}<br>
<strong>Provenance:</strong> {escape(register.get('provenance', 'Verify source origin'))}</p>
<p><strong>{register['total_rules']} rules inventoried.</strong> {escape(summary)}</p>
<p>Offline report. No database connection or automated control checks performed.
Statuses are operator-supplied assertions, not independently verified conclusions.
Verify publisher provenance, scope, evidence freshness and reviewer authority.
Missing rules remain NOT_ASSESSED. No compliance certification or overall pass is issued.</p>
<h2>Control findings and evidence gaps</h2><div class="table"><table><thead><tr>
<th>Rule</th><th>Title</th><th>Severity</th><th>Reported status</th><th>Evidence reference</th>
<th>Observed at</th><th>Reviewer</th><th>Rationale</th><th>Remediation proposal</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
{''.join(details)}
<h2>{tls_heading}</h2>
<p>Review protocol support and negotiation, cipher suites, certificates, validation,
cryptographic modules and client/server applicability. A minimum TLS setting does
not establish compliance. STIG findings alone do not evaluate every TLS requirement.</p>
<h2>Financial controls — NOT_ASSESSED</h2>
<p>Control owners must determine applicable SOX, GLBA, PCI DSS and other obligations,
framework versions and evidence requirements. No universal financial SQL checklist
applies to every database. Provider attestations do not prove customer compliance.</p>
<p>Remediation is a proposal only. Live access requires exact externally signed approval;
writes require separate exact signed approval.</p></html>'''


def requirement_display_text(requirement: str) -> str:
    """Label flat DISA description fields for display; leave source data untouched.

    Unexpected fragments remain literal text for the HTML/PDF renderers to escape.
    Do not discard unknown fields, nested content, attributes or parser directives.
    """
    if (len(requirement) > MAX_BYTES or "<!" in requirement or "<?" in requirement
            or "\x00" in requirement):
        return requirement
    try:
        root = ET.fromstring("<description>" + requirement + "</description>")
    except ET.ParseError:
        return requirement
    if not len(root) or (root.text and root.text.strip()):
        return requirement
    for field in root:
        if (field.tag not in DISA_DESCRIPTION_LABELS or field.attrib or len(field)
                or (field.tail and field.tail.strip())):
            return requirement
    return "\n\n".join(
        f"{DISA_DESCRIPTION_LABELS[field.tag]}: {field.text.strip()}"
        for field in root if field.text and field.text.strip()
    ) or "Not supplied by benchmark"


def control_details(control: dict, finding: dict) -> list[tuple[str, str]]:
    """Preserve source check/fix text without executing benchmark content."""
    references = [f"{i.get('system')}: {i.get('value')}" for i in control['identifiers']]
    references += [f"{i['href']} {i['text']}" for i in control['references']]
    details = [
        ('Rule version', control.get('version') or 'Not supplied'),
        ('Severity', control.get('severity') or 'Unknown'),
        ('Reported status', finding.get('status', 'NOT_ASSESSED')),
        ('Requirement', requirement_display_text(control['requirement'])),
        ('References', '; '.join(references) or 'Rule ID in hashed benchmark'),
        ('Check instructions', '\n'.join(control['checks']) or 'Not supplied by benchmark'),
        ('Evidence', finding.get('evidence', 'Missing')),
        ('Observed at', finding.get('observed_at', 'Unknown')),
        ('Reviewer', finding.get('reviewer', 'Unassigned')),
        ('Rationale', finding.get('rationale', 'Determine applicability; collect current evidence')),
        ('Responsibility', 'UNDETERMINED; validate Azure service and provider assurance per rule'),
        ('Recommendation', finding.get('remediation',
            'Review applicability and source fix; collect evidence before assigning a result')),
        ('Benchmark remediation guidance', '\n'.join(control['fixes']) or 'Not supplied by benchmark'),
    ]

    if "source_benchmark" in control:
        details += [("Source benchmark", control["source_benchmark"]),
                    ("Source release", control["source_release"] or "Unknown")]
    if "responsibility_guidance" in control:
        details.append(("Responsibility starting point", control["responsibility_guidance"]))
    if "azure_guidance" in control:
        import json
        guidance = control["azure_guidance"]
        details += [("Source disposition", "WITHDRAWN; review replacement links" if
                     control["withdrawn"] else "Active catalog entry"),
                    ("Organization-defined parameters (unassigned)",
                     json.dumps(control["parameters"], ensure_ascii=False)),
                    ("Azure implementation starting point", guidance["implementation"]),
                    ("Required evidence starting point", guidance["evidence"]),
                    ("Suggested accountable role", guidance["owner"]),
                    ("Azure SQL Database responsibility", guidance["azure_sql_database"]),
                    ("Managed Instance responsibility", guidance["managed_instance"]),
                    ("SQL Server VM responsibility", guidance["sql_server_vm"])]
    return details
