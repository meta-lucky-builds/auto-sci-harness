"""Certifiability preflight (GOAL §16.8; invariant 21: a cheap proxy may only DENY, never grant).

Before a claim is pre-registered, the Kernel's REAL K4/K6/K7 check functions (`kernel.checks.k4_statistics`,
`k6_held_out`, `k7_risk`) are run on outcomes SIMULATED from public numbers only:

- n_test: the number of held-out rows (public: the row count of the target's public feature file);
- the paired dev outcome of the candidate and the comparator (out-of-fold predictions on the PUBLIC training data,
  scored against the public training labels): counts of (both right, only the candidate right, only the comparator
  right, both wrong);
- the claim's planned minimum effect, alpha, seeds, n_boot and kind, and the multiplicity family size m
  (max(declared n_comparisons, claims already registered on the target + 1); the prereg ledger is public).

Each simulation draws paired cell probabilities from a Dirichlet posterior over the dev counts (pseudo-count
`prior` per cell), draws n_test paired held-out outcomes from them, builds a SCRATCH store/vault/ledger in a
temporary directory (simulated labels, never the real ones), pre-registers a copy of the claim there, logs the
simulated evaluations there and runs the three checks unchanged. The real store is never opened by the simulation:
no held-out label is read and nothing is written to the real ledger (tests/test_preflight.py checks the real vault
log).

The estimate P(K4, K6 and K7 all pass) is compared with theta (UNCALIBRATED). Below theta the claim is NOT
registered (deny). At or above theta the preflight raises no objection, which grants nothing: the claim still goes
through pre-registration, held-out confirmation and every Kernel check.

Model limits (stated, not solved):
- programs are simulated as seed-invariant (identical predictions on every seed), so the seed interval is
  degenerate and only the example interval decides (as for the deterministic programs of both live runs);
- only metrics whose per-example value is a 0/1 correctness (accuracy) are simulated; any other metric returns
  verdict "unsupported", which never denies;
- the dev counts are treated as an unbiased sample of held-out behaviour: dev selection (winner's curse) and a
  shift between dev and held-out (live run A: baseline 0.917 on dev, 0.704 held-out) make the estimate optimistic.
"""

from __future__ import annotations

import random
import shutil
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..kernel import AnalysisPlan, ArtifactRef, Claim, ClaimKind, DatasetRole, Effect, MetricRef, Scope
from ..kernel import checks as kchecks
from ..kernel.checks import Context, KernelConfig
from ..kernel.execution import ExecRecord, InputRef
from ..kernel.ledger import Ledger
from ..kernel.vault import Vault
from ..store.evidence import EvidenceStore

SIMULATED_METRICS = frozenset({"accuracy"})


@dataclass(frozen=True)
class PreflightConfig:
    """The preflight's parameters, in ONE place. The owner has not set them (GOAL §13.8)."""

    theta: float = 0.5  # UNCALIBRATED (owner decision, GOAL §13.8): deny registration when P(certify) < theta
    n_sims: int = 200  # UNCALIBRATED (owner decision, GOAL §13.8): simulations per claim (precision, not policy)
    prior: float = 1.0  # UNCALIBRATED (owner decision, GOAL §13.8): Dirichlet pseudo-count per paired dev cell
    seed: int = 0  # simulation seed (reproducible estimates)


DEFAULT_CONFIG = PreflightConfig()


@dataclass(frozen=True)
class PublicInputs:
    """Everything the preflight may use. Every field is public before pre-registration."""

    metric: str
    higher_is_better: bool
    n_test: int
    dev_pairs: tuple[int, int, int, int]  # (both right, only candidate right, only comparator right, both wrong)
    min_effect: float
    alpha: float = 0.05
    family_size: int = 1
    seeds: tuple[int, ...] = (0, 1, 2)
    n_boot: int = 2000
    kind: ClaimKind = ClaimKind.IMPROVEMENT


@dataclass
class PreflightResult:
    verdict: str  # "deny" | "no objection" | "unsupported"
    p_certify: float | None  # simulated P(K4, K6 and K7 all pass)
    p_k4: float | None
    p_k6: float | None
    p_k7: float | None
    p_k7_given_k6: float | None  # P(K7 pass | K6 pass): is a claim that holds also risky enough?
    n_sims: int
    theta: float
    reasons: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def denied(self) -> bool:
        return self.verdict == "deny"

    def as_record(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "p_certify": self.p_certify, "p_k4": self.p_k4, "p_k6": self.p_k6,
                "p_k7": self.p_k7, "p_k7_given_k6": self.p_k7_given_k6, "n_sims": self.n_sims, "theta": self.theta,
                "reasons": list(self.reasons),
                "inputs": self.inputs}


def dev_pairs(labels: Sequence[str], candidate: Sequence[str], comparator: Sequence[str]) -> tuple[int, int, int, int]:
    """Paired correctness counts on dev (public training labels and out-of-fold predictions)."""
    if not len(labels) == len(candidate) == len(comparator):
        raise ValueError("dev labels and predictions differ in length")
    cells = [0, 0, 0, 0]
    for y, a, b in zip(labels, candidate, comparator, strict=True):
        ca, cb = a == y, b == y
        cells[0 if ca and cb else 1 if ca else 2 if cb else 3] += 1
    return cells[0], cells[1], cells[2], cells[3]


def _dirichlet(rng: random.Random, alphas: Sequence[float]) -> list[float]:
    g = [rng.gammavariate(a, 1.0) for a in alphas]
    s = sum(g)
    return [x / s for x in g]


def _csv(rows: list[list[str]]) -> bytes:
    return "".join(",".join(r) + "\n" for r in rows).encode()


class _ScratchWorld:
    """A scratch store/vault/ledger in a temporary directory with a copy of the claim pre-registered and one logged
    (scratch) 'evaluate' read per planned arm and seed. Each simulation then runs the REAL K4, K6 and K7 on a fresh
    Context holding that simulation's predictions and labels, so the logged reads always equal the reported ones."""

    def __init__(self, inp: PublicInputs, root: Path):
        self.inp = inp
        self.store = EvidenceStore(root / "scratch")
        self.ledger = Ledger(self.store)
        self.vault = Vault(self.store, self.ledger)
        n = inp.n_test
        dev_id, self.target = "preflight-sim/dev", "preflight-sim/test"
        self.vault.register_dataset(dev_id, DatasetRole.DEV, public={"train.csv": _csv(
            [["dev_row", "y"], *[[f"d{i}", "y"] for i in range(3)]])}, private={})
        self.vault.register_dataset(self.target, DatasetRole.TEST,
                                    public={"test_X.csv": _csv([["sim_row"], *[[f"s{i}"] for i in range(n)]])},
                                    private={"test_y.csv": _csv([["label"], *[["y"] for _ in range(n)]])})
        subj = self.vault.register_artifact("preflight/subject", b"# simulated subject\n", "method")
        comp = self.vault.register_artifact("preflight/comparator", b"# simulated comparator\n", "baseline")
        self.claim = Claim(
            kind=inp.kind, statement="preflight simulation",
            subject=ArtifactRef(artifact_id=subj.artifact_id, hash=subj.hash),
            comparator=ArtifactRef(artifact_id=comp.artifact_id, hash=comp.hash),
            metric=MetricRef(id=inp.metric, higher_is_better=inp.higher_is_better),
            effect=Effect(min_effect=inp.min_effect, predicted_lo=0.0, predicted_hi=0.0, probability=0.5),
            scope=Scope(datasets=(dev_id, self.target)), development=(dev_id,), generalization_targets=(self.target,),
            analysis_plan=AnalysisPlan(seeds=inp.seeds, alpha=inp.alpha, n_comparisons=inp.family_size,
                                       n_boot=inp.n_boot),
            resolve_by=time.time() + 86400)
        self.h = self.claim.claim_hash()
        self.prereg = self.ledger.append("prereg", {"claim_hash": self.h, "claim": self.claim.model_dump(mode="json")})
        blob = self.vault.dataset(self.target).files["test_X.csv"].blob
        self.records: list[tuple[str, ExecRecord]] = []
        for seed in inp.seeds:
            for arm in ("subject", "comparator"):
                # a SCRATCH read of SIMULATED labels: K4 compares logged with reported evaluations
                self.vault.read(self.target, "test_y.csv", purpose="evaluate", claim_hash=self.h, arm=arm, seed=seed)
                ref = self.claim.subject if arm == "subject" else self.claim.comparator
                self.records.append((f"sim-{arm}-{seed}", ExecRecord(
                    claim_hash=self.h, arm=arm, artifact_hash=ref.hash,  # type: ignore[arg-type]
                    inputs={"test": InputRef(dataset_id=self.target, file="test_X.csv", blob=blob)},
                    target=self.target, label_file="test_y.csv", seed=seed, env={"sandbox": "preflight simulation"},
                    limits={"timeout_s": 1.0}, preds_ref="simulated", metric=inp.metric, score=0.0,
                    run_id="preflight")))

    def checks(self, correct_s: list[bool], correct_c: list[bool]) -> dict[str, bool]:
        preds = {"subject": ["y" if c else "n" for c in correct_s],
                 "comparator": ["y" if c else "n" for c in correct_c]}
        n = len(correct_s)
        records = [(eid, r.model_copy(update={"score": sum(x == "y" for x in preds[r.arm]) / n}))
                   for eid, r in self.records]
        ctx = Context(claim=self.claim, claim_hash=self.h, store=self.store, vault=self.vault, ledger=self.ledger,
                      config=KernelConfig(), replayer=None, evidence_ids=tuple(e for e, _ in records),
                      records=records, preds={eid: preds[r.arm] for eid, r in records},
                      labels={self.target: ["y"] * n}, prereg=self.prereg)
        out = {}
        # looked up on the Kernel module at call time: the preflight always runs the Kernel's current checks
        for name, fn in (("K4", kchecks.k4_statistics), ("K6", kchecks.k6_held_out), ("K7", kchecks.k7_risk)):
            try:
                out[name] = fn(ctx).status == "pass"
            except Exception:  # noqa: BLE001  a crashing check is a failed check (as in Kernel.certify)
                out[name] = False
        return out

    def close(self) -> None:
        self.store.close()


def _outcome(counts: tuple[int, int, int, int]) -> tuple[list[bool], list[bool]]:
    """Paired correctness vectors with the given cell counts, in an order fixed by the counts (simulated examples
    are exchangeable; a fixed order per count tuple makes the check results cacheable by counts)."""
    cells = [c for c, m in enumerate(counts) for _ in range(m)]
    random.Random("preflight-order|" + ",".join(map(str, counts))).shuffle(cells)
    return [c in (0, 1) for c in cells], [c in (0, 2) for c in cells]


def preflight(inp: PublicInputs, config: PreflightConfig = DEFAULT_CONFIG) -> PreflightResult:
    """Estimate P(K4, K6, K7 pass) for a candidate claim from public numbers; deny below theta."""
    record = {"metric": inp.metric, "n_test": inp.n_test, "dev_pairs": list(inp.dev_pairs),
              "min_effect": inp.min_effect, "alpha": inp.alpha, "family_size": inp.family_size,
              "seeds": list(inp.seeds), "n_boot": inp.n_boot, "kind": inp.kind.value}
    if inp.metric not in SIMULATED_METRICS:
        return PreflightResult("unsupported", None, None, None, None, None, 0, config.theta,
                               [f"metric {inp.metric!r} is not simulated (only {sorted(SIMULATED_METRICS)}); "
                                f"the preflight raises no objection (it can only deny)"], record)
    if inp.n_test < 2 or any(c < 0 for c in inp.dev_pairs) or config.n_sims < 1:
        return PreflightResult("unsupported", None, None, None, None, None, 0, config.theta,
                               ["inputs out of range (n_test < 2, a negative dev count, or no simulations)"], record)
    rng = random.Random(config.seed)
    alphas = [c + config.prior for c in inp.dev_pairs]
    passes = {"K4": 0, "K6": 0, "K7": 0, "K6K7": 0, "all": 0}
    root = Path(tempfile.mkdtemp(prefix="chh-preflight-"))
    cache: dict[tuple[int, int, int, int], dict[str, bool]] = {}
    world = _ScratchWorld(inp, root)
    try:
        for _ in range(config.n_sims):
            probs = _dirichlet(rng, alphas)
            draws = rng.choices(range(4), weights=probs, k=inp.n_test)
            counts = (draws.count(0), draws.count(1), draws.count(2), draws.count(3))
            if counts not in cache:
                cache[counts] = world.checks(*_outcome(counts))
            res = cache[counts]
            for name in ("K4", "K6", "K7"):
                passes[name] += res[name]
            passes["K6K7"] += res["K6"] and res["K7"]
            passes["all"] += all(res.values())
    finally:
        world.close()
        shutil.rmtree(root, ignore_errors=True)
    n = config.n_sims
    p = {k: v / n for k, v in passes.items()}
    verdict = "deny" if p["all"] < config.theta else "no objection"
    reasons = [f"simulated P(K4, K6 and K7 pass) = {p['all']:.3f} over {n} simulations (K4 {p['K4']:.3f}, "
               f"K6 {p['K6']:.3f}, K7 {p['K7']:.3f}); theta {config.theta} (UNCALIBRATED)"]
    if verdict == "deny":
        reasons.append("not registered: the claim is unlikely to certify at this n, effect and family size "
                       "(shrink the claim, pick another candidate, or report it uncertifiable)")
    k7_given_k6 = passes["K6K7"] / passes["K6"] if passes["K6"] else None
    return PreflightResult(verdict, p["all"], p["K4"], p["K6"], p["K7"], k7_given_k6, n, config.theta, reasons,
                           record)
