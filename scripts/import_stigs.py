"""Reproduce the reviewed DISA import from an exact, locally supplied archive.

This maintainer tool never connects to Azure/SQL or executes benchmark instructions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ARCHIVE_SHA256 = "e46bfae4b5e2ad7a068bef2539f6f98467c9cdfa8a012f01f7868bf4e07a5e64"
ARCHIVE_URL = (
    "https://dl.dod.cyber.mil/wp-content/uploads/stigs/zip/"
    "U_MS_SQL_Server_2022_Y26M04_STIG.zip"
)
BENCHMARKS = {
    "U_MS_SQL_Server_2022_Instance_V1R4_Manual_STIG/"
    "U_MS_SQL_Server_2022_Instance_STIG_V1R4_Manual-xccdf.xml": {
        "benchmark_id": "MS_SQL_Server_2022_Instance_STIG",
        "version": "1", "release": "4", "rules": 79,
    },
    "U_MS_SQL_Server_2022_Database_V1R3_Manual_STIG/"
    "U_MS_SQL_Server_2022_Database_STIG_V1R3_Manual-xccdf.xml": {
        "benchmark_id": "MS_SQL_Server_2022_Database_STIG",
        "version": "1", "release": "3", "rules": 23,
    },
}
NOTICES = (
    "U_MS_SQL_Server_2022_STIG_V1_Release_Memo.pdf",
    "U_MS_SQL_Server_2022_Revision_History.pdf",
    "U_MS_SQL_Server_2022_Overview.pdf",
    "U_Readme_SRG_and_STIG.pdf",
)


def import_archive(archive: Path, destination: Path) -> dict:
    """Verify bytes before parsing; extract only fixed reviewed members, without paths."""
    if archive.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Archive exceeds pinned import size limit")
    raw = archive.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("DISA archive SHA-256 mismatch")
    selected = {}
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.namelist()) != len(set(bundle.namelist())):
            raise ValueError("Duplicate ZIP members")
        for name in (*BENCHMARKS, *NOTICES):
            member = bundle.getinfo(name)
            if member.file_size > 2 * 1024 * 1024:
                raise ValueError("Unexpected member size")
            selected[Path(name).name] = bundle.read(member)
    # Create only after every member has been checked; never overwrite a previous import.
    destination.mkdir(parents=True, exist_ok=False)
    files = []
    for name, contents in selected.items():
        (destination / name).write_bytes(contents)
        entry = {"path": name, "sha256": hashlib.sha256(contents).hexdigest()}
        original = next((key for key in BENCHMARKS if Path(key).name == name), None)
        if original:
            entry.update(BENCHMARKS[original])
            entry["archive_member"] = original
        files.append(entry)
    manifest = {
        "publisher": "Defense Information Systems Agency (DISA)",
        "publication": "Microsoft SQL Server 2022 STIG, April 2026 package",
        "archive_url": ARCHIVE_URL, "archive_sha256": ARCHIVE_SHA256,
        "hash_reference": "https://ncp.nist.gov/checklist/1292/download/18495",
        "retrieved_at": "2026-10-06",
        "benchmark_date": "2026-04-01", "files": files,
        "total_rules": 102,
        "provenance": "Archive bytes match SHA-256 published by NIST NCP; original DISA XML",
        "license_notice": "Publisher notices retained in original XML and bundled PDFs. "
        "External publisher content is not relicensed under the SQLIQ code license. "
        "DISA logos and stylesheet are not redistributed; no endorsement implied.",
        "excluded": "Supplemental executable SQL, stylesheet and DISA logo image",
        "latest_release_claim": False,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--destination", type=Path,
                        default=Path("compliance/sources/stig"))
    args = parser.parse_args()
    print(json.dumps(import_archive(args.archive, args.destination), indent=2))


if __name__ == "__main__":
    main()
