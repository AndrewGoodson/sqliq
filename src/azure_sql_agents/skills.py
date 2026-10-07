from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import digest


def verify_sources(root: Path) -> str:
    manifest = json.loads((root / "sources.lock.json").read_text())
    for relative, expected in manifest["files"].items():
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Unsafe source path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Source integrity mismatch: {relative}")
    actual = {str(p.relative_to(root)) for prefix in ("skills", "vendor/aef-core", "compliance")
              for p in (root / prefix).rglob("*") if p.is_file()
              and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    if actual != set(manifest["files"]):
        raise ValueError("Source inventory changed")
    return digest(manifest)
