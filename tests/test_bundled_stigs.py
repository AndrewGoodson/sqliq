"""Verify complete imported DISA rule coverage against byte-pinned original sources."""
import hashlib
import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from azure_sql_agents.compliance import stig_register

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "compliance/sources/stig"
MANIFEST = json.loads((SOURCES / "manifest.json").read_text())
BENCHMARKS = [item for item in MANIFEST["files"] if "benchmark_id" in item]


def test_publisher_files_match_reviewed_hashes():
    assert MANIFEST["archive_sha256"] == (
        "e46bfae4b5e2ad7a068bef2539f6f98467c9cdfa8a012f01f7868bf4e07a5e64"
    )
    for item in MANIFEST["files"]:
        assert hashlib.sha256((SOURCES / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    assert sum(item["rules"] for item in BENCHMARKS) == 102
    assert len(BENCHMARKS) == 2


@pytest.mark.parametrize("item", BENCHMARKS, ids=lambda item: item["benchmark_id"])
def test_every_source_rule_reference_check_and_fix_is_preserved(item):
    path = SOURCES / item["path"]
    source = ET.fromstring(path.read_bytes())
    ns = {"x": "http://checklists.nist.gov/xccdf/1.1"}
    rules = source.findall(".//x:Rule", ns)
    register = stig_register(path)
    assert register["benchmark_id"] == item["benchmark_id"]
    assert register["version"] == item["version"]
    release = source.find("x:plain-text[@id='release-info']", ns).text
    assert release == f"Release: {item['release']} Benchmark Date: 01 Apr 2026"
    assert register["total_rules"] == item["rules"] == len(rules)
    assert register["assessed_rules"] == 0
    assert register["compliance_claim"] is False
    assert {c["rule_id"] for c in register["controls"]} == {r.get("id") for r in rules}
    for original, control in zip(rules, register["controls"], strict=True):
        assert control["version"] == original.findtext("x:version", namespaces=ns)
        assert control["title"] == original.findtext("x:title", namespaces=ns)
        assert control["severity"] == original.get("severity")
        assert control["requirement"] == " ".join(original.find("x:description", ns).itertext())
        assert control["fixes"] == [
            " ".join(fix.itertext()) for fix in original.findall("x:fixtext", ns)
        ]
        assert control["checks"] and control["fixes"]
        assert len(control["checks"]) == len(original.findall(".//x:check", ns))
        for check, preserved in zip(original.findall(".//x:check", ns),
                                    control["checks"], strict=True):
            assert " ".join(check.itertext()) in preserved
        assert control["identifiers"] == [
            {"system": ident.get("system"), "value": ident.text}
            for ident in original.findall("x:ident", ns)
        ]
        assert control["references"] == [
            {"href": ref.get("href", ""), "text": " ".join(ref.itertext())}
            for ref in original.findall("x:reference", ns)
        ]
        assert control["status"] == "NOT_ASSESSED"
        assert control["applicability"] == control["responsibility"] == "UNDETERMINED"
        assert control["evidence"] == []


def test_import_rejects_unpinned_archive_before_creating_files(tmp_path):
    spec = importlib.util.spec_from_file_location("import_stigs", ROOT / "scripts/import_stigs.py")
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    archive = tmp_path / "bad.zip"
    archive.write_bytes(b"not the reviewed publisher archive")
    destination = tmp_path / "extracted"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        importer.import_archive(archive, destination)
    assert not destination.exists()
