"""Phase-4 criterion 6: **canary rollout stratified by tenant tag, gated on
percentiles, previous version kept warm for rollback.**

Four requirements in one sentence, and each is load-bearing:

- **stratified by tenant tag** — assignment is per tenant, not per request.
  A tenant that lands on the candidate for one request and the incumbent for
  the next sees inconsistent behaviour *and* contributes samples to both arms,
  which is how a difference between versions gets averaged into invisibility.
  Assignment here is a hash of `(graph_id, version, tenant_tag)`: stable for a
  tenant, and re-shuffled per version so the same tenants are not always the
  guinea pigs.
- **gated on percentiles** — not on means. A regression that doubles the worst
  1% while improving the median is exactly the shape a mean hides, and it is
  the shape users notice.
- **previous version kept warm** — a rollback target that has to be rebuilt is
  not a rollback, it is an outage with a plan.
- **rollout** — stages, which is what `monitor` never had: it rolls back, but
  it has nothing staged to roll back *to* partway.

`monitor` already rolls back on a regression. This is the ladder it was
missing.
"""

from __future__ import annotations

import secrets
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import blake2b, pbkdf2_hmac
from pathlib import Path

# The ladder. Each rung must survive its own observation window before the next
# is entered, and 100 is a stage rather than an implicit end state so "fully
# promoted" is something the ledger records rather than something inferred from
# the absence of a next stage.
DEFAULT_LADDER: tuple[int, ...] = (1, 5, 25, 50, 100)

# Percentiles compared against the incumbent. p50 catches a broad regression;
# p95 and p99 catch the tail a mean would bury.
DEFAULT_PERCENTILES: tuple[int, ...] = (50, 95, 99)

# How much worse a percentile may be before the stage fails, as a ratio.
# 1.0 would fail on measurement noise; a value this loose is deliberate,
# because a canary's job is to catch a REGRESSION, and a gate that fires on
# jitter gets disabled by whoever is on call.
DEFAULT_TOLERANCE = 1.20

# Below this, a percentile is computed from too few samples to mean anything.
# A p99 over 20 samples is the maximum with extra steps.
MIN_SAMPLES = 100


# blake2b's own bounds. 32 is the floor for the same reason a signing key has
# one: the assignment function is public, so a short salt is brute-forcible
# offline against a single observed assignment.
MIN_SALT_BYTES = 32

# Domain separation for the fingerprint, so a fingerprint can never be
# mistaken for — or replayed as — any other digest of the same salt.
FINGERPRINT_DOMAIN = b"aef.canary.salt.fingerprint.v1"
MAX_SALT_BYTES = blake2b.MAX_KEY_SIZE


class CanaryError(RuntimeError):
    pass


class UnkeyedCanaryError(CanaryError):
    """Assignment asked to run without a salt, without saying so."""


@dataclass(frozen=True)
class CanarySalt:
    """The owner-held key that makes tenant assignment unpredictable.

    Without it, `assigned_to_candidate` is a public deterministic function of
    a string the tenant supplies, so a tenant who controls its own tag can
    compute which arm any tag lands in and pick one. Demonstrated in the trust
    case §2.3: one in roughly two hundred tried tags landed on the incumbent
    at 99% exposure, found by searching offline.

    **What keying buys, precisely.** It removes OFFLINE computation: a tenant
    without the salt cannot evaluate the function at all, so it cannot sift
    candidate tags before using them. It does NOT remove online probing — a
    tenant that can observe which arm it landed in can still re-register under
    new tags until it lands where it wants. That is slower, one tag at a time,
    and visible in whatever issues tenant tags. Stated rather than implied,
    because "unpredictable" would overclaim.

    **Not the signing key.** Loaded separately and deliberately so: reusing
    one secret for two purposes means a compromise of either leaks both, and
    the release key is held by a person while this one is read by a running
    service.
    """

    material: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not MIN_SALT_BYTES <= len(self.material) <= MAX_SALT_BYTES:
            raise CanaryError(
                f"canary salt is {len(self.material)} bytes; blake2b accepts a key of "
                f"{MIN_SALT_BYTES}..{MAX_SALT_BYTES}. The assignment function is public, so "
                f"a short salt is brute-forcible offline against one observed assignment."
            )

    def fingerprint(self) -> str:
        """A comparable, deliberately EXPENSIVE digest of this salt.

        A fingerprint is persisted next to a rollout, so it ends up in state
        files and logs — and **any deterministic function of the salt is a
        verification oracle**: an attacker guesses a salt, computes the
        fingerprint, and compares. That is unavoidable if restarts are to
        prove they kept the same population, so the answer is to make each
        guess cost rather than to pretend the oracle is not there.

        The first version used a plain `blake2b` of the salt — microseconds
        per guess, found by attacking it. `pbkdf2_hmac` at 200k iterations
        costs milliseconds instead, which is ~1000x on an offline search and
        nothing at all on the once-per-rollout real use.

        **This does not make a guessable salt safe.** It buys time against a
        weak one; the actual defence is that the salt is random, which is what
        `generate()` is for.
        """
        return pbkdf2_hmac("sha256", self.material, FINGERPRINT_DOMAIN, 200_000, 8).hex()

    @classmethod
    def generate(cls) -> CanarySalt:
        """A random salt. The documented way to get one.

        `secrets`, not `random`: a salt from a seeded PRNG is a salt an
        attacker who learns the seed can reproduce, and "it looked random" is
        how that gets missed.
        """
        return cls(material=secrets.token_bytes(MIN_SALT_BYTES))

    @classmethod
    def from_file(cls, path: str | Path) -> CanarySalt:
        file = Path(path)
        if not file.is_file():
            raise CanaryError(f"no canary salt at {file}")
        mode = file.stat().st_mode & 0o077
        if mode:
            raise CanaryError(
                f"canary salt {file} is group/world accessible (mode {mode:03o}); "
                f"`chmod 600` it. A salt any local process can read is a salt a tenant's "
                f"code could read if it ever ran on the same host."
            )
        return cls(material=file.read_bytes().strip())


@dataclass(frozen=True)
class CanaryVerdict:
    stage_percent: int
    passed: bool
    reason: str
    measurements: tuple[str, ...] = ()


def percentile(samples: list[float], p: int) -> float:
    """Nearest-rank percentile. No interpolation.

    Interpolating invents a value between two observations and then gates on
    it; nearest-rank returns a number that actually happened, which is what an
    operator asked to explain a rollback needs to be able to point at.
    """
    if not samples:
        raise CanaryError(f"cannot take p{p} of an empty sample")
    if not 0 < p <= 100:
        raise CanaryError(f"percentile must be within (0, 100]; got {p}")
    ordered = sorted(samples)
    rank = max(1, -(-p * len(ordered) // 100))
    return ordered[rank - 1]


@dataclass(frozen=True)
class CanaryPolicy:
    ladder: tuple[int, ...] = DEFAULT_LADDER
    percentiles: tuple[int, ...] = DEFAULT_PERCENTILES
    tolerance: float = DEFAULT_TOLERANCE
    min_samples: int = MIN_SAMPLES

    def __post_init__(self) -> None:
        if not self.ladder:
            raise CanaryError("a canary needs at least one stage")
        if list(self.ladder) != sorted(set(self.ladder)):
            raise CanaryError(
                f"ladder {self.ladder} must be strictly increasing and unique — a stage that "
                f"narrows exposure reads as progress while reducing the evidence"
            )
        if self.ladder[-1] != 100:
            raise CanaryError(
                f"the last stage must be 100, not {self.ladder[-1]}; otherwise 'fully "
                f"promoted' is inferred from running out of stages rather than recorded"
            )
        if not 0 < self.ladder[0]:
            raise CanaryError("the first stage must expose someone")
        if self.tolerance < 1.0:
            raise CanaryError(
                f"tolerance {self.tolerance} below 1.0 demands the candidate be BETTER than "
                f"the incumbent at every percentile to proceed, which fails on noise alone"
            )
        if self.min_samples < 1:
            raise CanaryError("min_samples must be positive")


def assigned_to_candidate(
    tenant_tag: str,
    *,
    graph_id: str,
    version: int,
    percent: int,
    salt: CanarySalt | None = None,
) -> bool:
    """Is this tenant in the candidate arm at `percent` exposure?

    Deterministic, so a tenant's arm does not change between requests, and
    **monotone in `percent`**: a tenant admitted at 5% is still admitted at 25.
    Without that, advancing a stage would reshuffle the population and throw
    away every sample gathered so far — the ladder would restart its evidence
    at every rung while appearing to accumulate it.
    """
    if not 0 <= percent <= 100:
        raise CanaryError(f"percent must be within [0, 100]; got {percent}")
    if not tenant_tag:
        raise CanaryError(
            "a canary stratified by tenant tag cannot assign an empty tag; an unlabelled "
            "request has no arm, and defaulting it to the incumbent would silently exempt "
            "whoever forgot the tag"
        )
    # NFC first. REPRODUCED: "café" in NFC and NFD are different byte
    # sequences, so one tenant sending each from two clients landed in
    # different arms 52% of the time — it sees inconsistent behaviour AND
    # contributes samples to both arms, which is exactly what stratifying by
    # tenant exists to prevent. Normalising is not cosmetic here.
    seed = f"{graph_id}:{version}:{unicodedata.normalize('NFC', tenant_tag)}".encode()
    digest = (
        blake2b(seed, digest_size=8, key=salt.material)
        if salt is not None
        else blake2b(seed, digest_size=8)
    )
    bucket = int.from_bytes(digest.digest(), "big") % 100
    return bucket < percent


@dataclass(frozen=True)
class CanaryState:
    """Where a rollout is, and what it can fall back to.

    `warm_version` is the requirement's "previous version kept warm". It is
    carried in the state rather than looked up, because a rollback that has to
    resolve its own target is a rollback that can fail at the moment it is
    needed.
    """

    graph_id: str
    candidate_version: int
    warm_version: int
    stage_index: int = 0
    policy: CanaryPolicy = field(default_factory=CanaryPolicy)
    history: tuple[str, ...] = ()
    # The owner-held key that makes assignment unpredictable to the tenant
    # being assigned. Required unless `unkeyed=True` says otherwise.
    salt: CanarySalt | None = None
    # The explicit opt-out, named for what it costs.
    unkeyed: bool = False
    # Set by `rollback` and never cleared. Found by this milestone's
    # adversarial round: without it a rolled-back rollout climbed the ladder
    # again on the next passing verdict, so a candidate with a real regression
    # cycled advance -> regress -> rollback -> advance forever, re-exposing
    # users on every lap. A rollback is a VERDICT ON THIS CANDIDATE, not a
    # reset — what comes next is a new version, not a retry of this one.
    rolled_back: bool = False

    def __post_init__(self) -> None:
        # KEYED BY DEFAULT, the same shape used for shadow containment and for
        # network isolation: refuse unless the control is really there, and
        # make running without it something a caller states rather than
        # inherits. Trust case §2.3 demonstrated the cost of the unkeyed
        # version — one in roughly two hundred tried tags landed on the
        # incumbent at 99% exposure, found by searching offline.
        if self.salt is None and not self.unkeyed:
            raise UnkeyedCanaryError(
                "tenant assignment without a salt is a public deterministic function of a "
                "string the tenant supplies, so a tenant controlling its own tag can compute "
                "which arm any tag lands in and choose one — biasing exactly the evidence "
                "promotion is read from. Pass salt=CanarySalt.from_file(...), or unkeyed=True "
                "to state that this rollout's population is self-selectable."
            )
        if self.salt is not None and self.unkeyed:
            raise CanaryError(
                "unkeyed=True was passed alongside a salt; one of the two is a mistake, and "
                "guessing which would mislabel the population"
            )
        if self.warm_version == self.candidate_version:
            raise CanaryError(
                f"warm_version equals candidate_version ({self.warm_version}); there is "
                f"nothing to roll back TO, so this rollout has no rollback"
            )
        if not 0 <= self.stage_index < len(self.policy.ladder):
            raise CanaryError(f"stage_index {self.stage_index} is outside the ladder")

    @property
    def percent(self) -> int:
        return self.policy.ladder[self.stage_index]

    @property
    def complete(self) -> bool:
        return self.percent == 100

    @property
    def keyed(self) -> bool:
        return self.salt is not None

    @property
    def salt_fingerprint(self) -> str:
        """A short digest of the salt — never the salt.

        Persisted alongside a rollout so a restart can prove it is still
        assigning the SAME population. Changing the salt mid-ladder reshuffles
        every tenant, which silently discards every sample gathered so far
        while the stage index goes on claiming they accumulated — the same
        failure the monotonicity requirement exists to prevent.

        See `CanarySalt.fingerprint` for why this is deliberately slow.
        """
        if self.salt is None:
            return "unkeyed"
        return self.salt.fingerprint()

    def assert_same_population(self, fingerprint: str) -> None:
        if fingerprint != self.salt_fingerprint:
            raise CanaryError(
                f"this rollout was assigning under salt {fingerprint} and is now under "
                f"{self.salt_fingerprint}: every tenant has been reshuffled, so the samples "
                f"gathered at earlier stages describe a different population. Restore the "
                f"salt, or start the rollout over."
            )

    def serves_candidate(self, tenant_tag: str) -> bool:
        return assigned_to_candidate(
            tenant_tag,
            graph_id=self.graph_id,
            version=self.candidate_version,
            percent=self.percent,
            salt=self.salt,
        )

    def evaluate(
        self, *, candidate_samples: list[float], incumbent_samples: list[float]
    ) -> CanaryVerdict:
        """Compare this stage's arms at every configured percentile.

        Lower is better — these are latencies, error counts, costs. A metric
        where higher is better must be negated by the caller, and that is
        stated here because the alternative is a `higher_is_better` flag whose
        default silently decides the direction of every gate.
        """
        stage = self.percent
        for label, samples in (("candidate", candidate_samples), ("incumbent", incumbent_samples)):
            if len(samples) < self.policy.min_samples:
                return CanaryVerdict(
                    stage_percent=stage,
                    passed=False,
                    reason=(
                        f"{len(samples)} {label} sample(s), below the floor of "
                        f"{self.policy.min_samples}. A percentile over too few observations is "
                        f"a number without a claim; this is 'keep watching', not 'regressed'"
                    ),
                )

        measurements: list[str] = []
        for p in self.policy.percentiles:
            cand = percentile(candidate_samples, p)
            base = percentile(incumbent_samples, p)
            ceiling = base * self.policy.tolerance
            measurements.append(f"p{p}: candidate {cand:.4g} vs incumbent {base:.4g}")
            if cand > ceiling:
                return CanaryVerdict(
                    stage_percent=stage,
                    passed=False,
                    reason=(
                        f"p{p} regressed: candidate {cand:.4g} exceeds {ceiling:.4g} "
                        f"({self.policy.tolerance:.2f}x incumbent {base:.4g})"
                    ),
                    measurements=tuple(measurements),
                )
        return CanaryVerdict(
            stage_percent=stage,
            passed=True,
            reason=f"all percentiles within {self.policy.tolerance:.2f}x of the incumbent",
            measurements=tuple(measurements),
        )

    def advance(self, verdict: CanaryVerdict, *, at: datetime) -> CanaryState:
        """One rung, on a passing verdict for THIS stage."""
        if not verdict.passed:
            raise CanaryError(
                f"cannot advance on a failing verdict: {verdict.reason}. Advancing past a "
                f"failure is not a rollout, it is a schedule"
            )
        if verdict.stage_percent != self.percent:
            # A verdict from a different exposure level describes a different
            # population. Accepting it would let a 1% stage's evidence promote
            # a 50% one.
            raise CanaryError(
                f"verdict describes stage {verdict.stage_percent}% but this rollout is at "
                f"{self.percent}%; that evidence is about a different population"
            )
        if self.rolled_back:
            raise CanaryError(
                f"this rollout was rolled back; v{self.candidate_version} does not climb the "
                f"ladder again. Retrying the same candidate re-exposes users to the same "
                f"regression on every lap — promote a NEW version, whose evidence is about "
                f"the fix"
            )
        if self.complete:
            raise CanaryError("already at 100%; there is no further stage to advance to")
        nxt = self.policy.ladder[self.stage_index + 1]
        entry = f"{at.isoformat()} advance {self.percent}% -> {nxt}%"
        return replace(self, stage_index=self.stage_index + 1, history=(*self.history, entry))

    def rollback(self, verdict: CanaryVerdict, *, at: datetime) -> CanaryState:
        """Back to the warm version, and TERMINAL for this candidate."""
        entry = (
            f"{at.isoformat()} rollback from {self.percent}% to v{self.warm_version}: "
            f"{verdict.reason}"
        )
        return replace(self, stage_index=0, rolled_back=True, history=(*self.history, entry))
