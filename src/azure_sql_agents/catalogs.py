"""Pinned offline catalogs. Coverage is not an assessment or authorization."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .compliance import MAX_BYTES, stig_register

CATALOGS = ("nist-800-53", "sql-2022-stigs", "all")


def _walk(node: dict, key: str):
    for child in node.get(key, []):
        yield child
        yield from _walk(child, key)


def _prose(parts: list[dict]) -> str:
    rows = []
    for part in parts:
        if part.get("prose"):
            rows.append(f"{part.get('id', part['name'])}: {part['prose']}")
        nested = _prose(part.get("parts", []))
        if nested:
            rows.append(nested)
    return "\n".join(rows)


def nist_register(root: Path) -> dict:
    path = root / "compliance/sources/nist/800-53-rev5.json"
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise ValueError("Catalog too large")
    catalog = json.loads(raw)["catalog"]
    families = json.loads((root / "compliance/azure-family-guidance.json").read_bytes())
    resources = {r["uuid"]: r for r in catalog.get("back-matter", {}).get("resources", [])}
    controls = []
    for group in catalog["groups"]:
        guidance = families[group["id"]]
        for control in _walk(group, "controls"):
            parts = control.get("parts", [])
            withdrawn = any(p["name"] == "status" and p["value"] == "withdrawn"
                            for p in control.get("props", []))
            references = []
            for link in control.get("links", []):
                resource = resources.get(link["href"].removeprefix("#"), {})
                references.append({"href": link["href"], "text": json.dumps(resource)
                                   if resource else link.get("rel", "reference")})
            references += [{"href": url, "text": "Microsoft implementation context"}
                           for url in guidance["sources"]]
            references.append({"href": "https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final",
                               "text": f"NIST SP 800-53 {control['id']}"})
            controls.append({
                "rule_id": control["id"], "title": control["title"],
                "version": catalog["metadata"]["version"], "severity": "Not ranked by NIST",
                "requirement": _prose([p for p in parts if p["name"] == "statement"])
                or "Withdrawn or no statement supplied; review source disposition and links.",
                "checks": [_prose([p for p in parts if p["name"].startswith("assessment-")])],
                "fixes": [_prose([p for p in parts if p["name"] == "guidance"])],
                "references": references, "identifiers": [{"system": "NIST SP 800-53",
                                                             "value": control["id"]}],
                "status": "NOT_ASSESSED", "applicability": "UNDETERMINED",
                "responsibility": "UNDETERMINED", "withdrawn": withdrawn,
                "parameters": control.get("params", []), "source_control": control,
                "azure_guidance": guidance, "evidence": [], "owner": None,
                "reviewer": None, "rationale": None, "remediation_proposal": None,
            })
    if not controls or len({c["rule_id"] for c in controls}) != len(controls):
        raise ValueError("Invalid catalog IDs")
    return {"mode": "offline_nist_inventory", "benchmark_id": catalog["metadata"]["title"],
            "version": catalog["metadata"]["version"],
            "benchmark_sha256": hashlib.sha256(raw).hexdigest(),
            "provenance": "NIST OSCAL v1.5.0; see compliance/sources/nist/provenance.json",
            "scope": "Every control and enhancement, including withdrawn entries; no baseline filter",
            "total_rules": len(controls), "assessed_rules": 0, "compliance_claim": False,
            "controls": controls}


def bundled_register(root: Path, name: str) -> dict:
    if name not in CATALOGS:
        raise ValueError("Unknown catalog")
    registers = []
    if name in {"nist-800-53", "all"}:
        registers.append(nist_register(root))
    if name in {"sql-2022-stigs", "all"}:
        paths = sorted((root / "compliance/sources/stig").glob("*.xml"))
        if len(paths) != 2:
            raise ValueError("Missing SQL Server database/instance benchmark")
        for path in paths:
            register = stig_register(path)
            register["provenance"] = "Official DISA source; see compliance/sources/stig/manifest.json"
            for control in register["controls"]:
                control["source_benchmark"] = register["benchmark_id"]
                control["source_release"] = register.get("release", "Unknown")
            registers.append(register)
    controls = [control for register in registers for control in register["controls"]]
    if len({c["rule_id"] for c in controls}) != len(controls):
        raise ValueError("Duplicate control IDs across catalogs")
    # Bind evidence to catalog bytes AND the implementation guidance used in the report.
    inventory = [{key: r[key] for key in ("benchmark_id", "version", "benchmark_sha256")}
                 for r in registers]
    guide = (root / "compliance/azure-family-guidance.json").read_bytes()
    bundle_hash = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode() + guide).hexdigest()
    return {"mode": "offline_bundled_inventory", "benchmark_id": f"SQLIQ {name}",
            "version": "; ".join(r["version"] for r in registers),
            "benchmark_sha256": bundle_hash, "source_benchmarks": inventory,
            "provenance": "Pinned publisher sources, source integrity checked by CLI",
            "scope": "Full selected catalogs; no baseline or applicability filtering",
            "total_rules": len(controls), "assessed_rules": 0, "compliance_claim": False,
            "controls": controls}
