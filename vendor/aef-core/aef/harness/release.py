"""Phase-4 criterion 7, second half: **signed release manifests**.

> Human-in-the-loop approval above a configurable risk threshold, signed
> release manifests

The first half exists: `PolicyConfig.require_hitl_above_risk` and the gate
pipeline's escalation. The second did not, and ADR 0087 was explicit about the
hole: the ledger is tamper-**evident**, not signed, so *a forger who rebuilds a
consistent chain replays clean*. Every hash in that chain is computed from data
the forger also controls.

A manifest closes it by binding four things a rebuilt chain cannot re-derive:
**what** was promoted, **from what evidence**, **by whom**, and **when** — under
a key the thing being judged never holds.

## What the signature proves, and what it does not

HMAC-SHA256, from the standard library. No new dependency, and the choice is
load-bearing rather than convenient — so its limits are stated rather than
implied:

- **It proves an agent cannot forge an approval.** The signing key lives with
  the owner and never enters the loop, a workflow, a container or a
  candidate's environment. This is the threat model that matters here: the
  adversary is the harness and the code it runs.
- **It does not give third-party verifiability.** HMAC is symmetric, so anyone
  who can verify can also forge. Two key-holders cannot prove which of them
  signed. Ed25519 would fix that and needs a dependency this repo does not
  have and should not add without the owner deciding to.

Saying "signed" while meaning "hashed" is what ADR 0087 found already; saying
"signed" while meaning "MAC'd with a shared secret" would be the same mistake
one notch quieter.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

# Bumped when the signed payload's SHAPE changes. A verifier that accepted a
# manifest from an older shape would be checking a different set of claims
# than the one it thinks it is checking.
MANIFEST_VERSION = 1

# Minimum key length. Not a style rule: a short shared secret is brute-forcible
# offline against any single manifest the holder can read, and every manifest
# is readable by design.
MIN_KEY_BYTES = 32


class ReleaseError(RuntimeError):
    pass


class SignatureError(ReleaseError):
    """Verification failed. Deliberately not a bool return anywhere it is
    load-bearing: `if verify(...)` reads as safe when someone forgets the
    `not`, and a missing signature must never be quieter than a wrong one."""


@dataclass(frozen=True)
class ReleaseManifest:
    """What was promoted, from what evidence, approved by whom.

    `evidence` is a mapping of gate id -> outcome, taken from the pipeline
    that actually ran. It is IN the signed payload rather than alongside it:
    a manifest that named a promotion without binding the evidence would let
    a forger keep the signature and swap the reason.
    """

    graph_id: str
    version: int
    base_sha: str
    head_sha: str
    evidence: dict[str, str]
    approved_by: str
    approved_at: datetime
    corpus_digest: str = ""
    # An owner promoting DESPITE a failing gate must say so, in the signed
    # payload. Found by this milestone's adversarial round: a manifest whose
    # evidence recorded G3 and G5 as `fail` signed and verified cleanly, so
    # the artefact that proves "promoted on this evidence" was equally happy
    # to prove a rejection had been approved. An override is a legitimate
    # owner act; an override nobody had to type is an accident waiting to be
    # cited later as intent.
    override_reason: str = ""
    notes: str = ""
    manifest_version: int = MANIFEST_VERSION

    def __post_init__(self) -> None:
        if not self.graph_id.strip():
            raise ReleaseError("a manifest must name the graph it promotes")
        if self.version < 1:
            raise ReleaseError(f"version must be >= 1; got {self.version}")
        if not self.base_sha.strip() or not self.head_sha.strip():
            raise ReleaseError(
                "a manifest must record both the base and head sha — without both, 'what was "
                "promoted' is not answerable from the manifest alone"
            )
        if self.base_sha == self.head_sha:
            raise ReleaseError(
                f"base and head are the same commit ({self.base_sha[:12]}); there is no change "
                f"to promote, and a manifest for a no-op is a signature waiting to be reused"
            )
        if not self.approved_by.strip():
            raise ReleaseError(
                "a manifest must name its approver. 'approved by whom' is one of the four "
                "things this exists to bind, and an empty string answers it falsely rather "
                "than not at all"
            )
        if not self.evidence:
            raise ReleaseError(
                "a manifest must carry the evidence it was promoted on. An unevidenced "
                "approval is exactly the signature a forger wants: valid, and about nothing"
            )
        failing = sorted(g for g, outcome in self.evidence.items() if outcome.lower() != "pass")
        if failing and not self.override_reason.strip():
            raise ReleaseError(
                f"evidence records {', '.join(failing)} as not passing. Promoting anyway is "
                f"an owner's call to make and to STATE — set override_reason, which is signed "
                f"with the rest. A manifest that signs a rejection as though it were an "
                f"approval is the artefact a forger would have had to build"
            )
        if self.approved_at.tzinfo is None:
            raise ReleaseError(
                "approved_at must be timezone-aware; a naive timestamp signs a different "
                "instant depending on where it is read"
            )

    def payload(self) -> bytes:
        """The exact bytes that get signed.

        `sort_keys` and `separators` because a signature over a
        re-serialisation must reproduce byte-for-byte — a dict whose ordering
        depends on insertion would verify on the machine that signed it and
        fail everywhere else, which reads as tampering.
        """
        body: dict[str, Any] = {
            "manifest_version": self.manifest_version,
            "graph_id": self.graph_id,
            "version": self.version,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "evidence": dict(sorted(self.evidence.items())),
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.astimezone(UTC).isoformat(),
            "corpus_digest": self.corpus_digest,
            "override_reason": self.override_reason,
            "notes": self.notes,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def digest(self) -> str:
        return sha256(self.payload()).hexdigest()


@dataclass(frozen=True)
class SigningKey:
    """A key the loop never sees.

    Loaded from a file rather than from the environment, deliberately: the
    sandbox scrubs the environment to an allowlist, so a key that lived there
    would be one allowlist edit away from reaching candidate code, and the
    edit would look like configuration.
    """

    material: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.material) < MIN_KEY_BYTES:
            raise ReleaseError(
                f"signing key is {len(self.material)} bytes; at least {MIN_KEY_BYTES} are "
                f"required. Every manifest is readable by design, so a short shared secret "
                f"is brute-forcible offline against any one of them."
            )

    @classmethod
    def from_file(cls, path: str | Path) -> SigningKey:
        file = Path(path)
        if not file.is_file():
            raise ReleaseError(f"no signing key at {file}")
        mode = file.stat().st_mode & 0o077
        if mode:
            # Checked rather than assumed. A key readable by the whole machine
            # is a key the loop can read, and the loop runs code it did not
            # write.
            raise ReleaseError(
                f"signing key {file} is group/world accessible (mode {mode:03o}); "
                f"`chmod 600` it. A key any local process can read is not a key the harness "
                f"is excluded from."
            )
        return cls(material=file.read_bytes().strip())


def sign(manifest: ReleaseManifest, key: SigningKey) -> str:
    return hmac.new(key.material, manifest.payload(), sha256).hexdigest()


def verify(manifest: ReleaseManifest, signature: str, key: SigningKey) -> None:
    """Raise unless `signature` is this manifest's, under this key.

    `compare_digest`, not `==`: string equality short-circuits on the first
    differing byte, and the timing difference is enough to recover a signature
    a byte at a time given enough attempts.
    """
    if not signature:
        raise SignatureError(
            "no signature. An unsigned manifest is not a weaker approval, it is not an "
            "approval — and treating it as one is how a forger's rebuilt chain replays clean"
        )
    expected = sign(manifest, key)
    if not hmac.compare_digest(expected, signature):
        raise SignatureError(
            f"signature does not match manifest {manifest.graph_id}@v{manifest.version} "
            f"(digest {manifest.digest()[:16]}). Either the manifest was altered after "
            f"signing, or it was signed under a different key."
        )


@dataclass(frozen=True)
class SignedRelease:
    manifest: ReleaseManifest
    signature: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "manifest": json.loads(self.manifest.payload()),
            "signature": self.signature,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SignedRelease:
        body = payload.get("manifest")
        if not isinstance(body, dict):
            raise ReleaseError("payload has no manifest")
        version = body.get("manifest_version")
        if version != MANIFEST_VERSION:
            # Refused, not migrated. A verifier that accepted an older shape
            # would be checking a different set of claims than it believes.
            raise ReleaseError(
                f"manifest_version {version!r} is not {MANIFEST_VERSION}; this verifier "
                f"checks a different set of claims than that manifest makes"
            )
        try:
            manifest = ReleaseManifest(
                graph_id=body["graph_id"],
                version=body["version"],
                base_sha=body["base_sha"],
                head_sha=body["head_sha"],
                evidence=dict(body["evidence"]),
                approved_by=body["approved_by"],
                approved_at=datetime.fromisoformat(body["approved_at"]),
                corpus_digest=body.get("corpus_digest", ""),
                override_reason=body.get("override_reason", ""),
                notes=body.get("notes", ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReleaseError(f"malformed manifest: {exc}") from exc
        return cls(manifest=manifest, signature=str(payload.get("signature", "")))
