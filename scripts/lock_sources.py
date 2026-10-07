#!/usr/bin/env python3
"""Maintainer-only: regenerate after reviewing upstream diff, never during runtime."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
files = {}
for prefix in ("skills", "vendor/aef-core", "compliance"):
    for path in sorted((root / prefix).rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
manifest = {"version": 1, "sources": {
    "aef-core": {"url": "https://github.com/AndrewGoodson/aef-core",
                 "commit": "07b291198cfdeee9dc82095a9366931cf14b2a92"},
    "azure-skills": {"url": "https://github.com/microsoft/azure-skills",
                     "commit": "4190e7d253e59ee1557bcae9ed13a5d8c06263a9"},
    "microsoft-sql": {"url": "https://github.com/microsoft/microsoft-sql",
                      "commit": "eeb1c6867c2d128763516a1aad41671c593cc189"},
    "local": {"source": "our azure365-claude-design-ingest skill",
              "adaptation": "auth, read-only, approval, audit; bypass mode excluded"}}, "files": files}
(root / "sources.lock.json").write_text(json.dumps(manifest, indent=2) + "\n")
