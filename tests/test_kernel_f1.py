"""Evidence-economics F1 reproduced against the REAL K4 and K6 (GOAL §16.5.3, invariant 18).

Held-out target: 500 random binary labels (no real effect exists). The adversary registers 49 probe claims (random
classifiers) and then a final claim: the majority vote of the probes it believes are above chance, against a constant
baseline. The family is 50 claims, so K4 tests at Bonferroni alpha 0.05 / 50.
- embargo: the adversary sees nothing from the target, so it picks probes blindly. False certification <= 0.065.
- continuous, unlogged: it sees each probe's held-out accuracy (a continuous value) and keeps those above 0.5. This
  is the attack; with nothing ledgered, Bonferroni over registered claims does not hold: false certification >= 0.30.
- continuous, logged as a release: K6 marks the target adaptively exposed and the final claim is never certified.
"""

from __future__ import annotations

import csv
import io
import random
import time
from pathlib import Path

import pytest

from custom_harness.kernel import (
    AnalysisPlan,
    ArtifactRef,
    Claim,
    ClaimKind,
    DatasetRole,
    Effect,
    ExecRecord,
    InputRef,
    Kernel,
    KernelConfig,
    MetricRef,
    Scope,
)
from custom_harness.kernel import checks as K
from custom_harness.kernel.reasons import Code
from custom_harness.records import Run
from custom_harness.store import EvidenceStore

# One seed: the classifiers are deterministic, so 3 identical seeds give the same example-level interval (and a
# degenerate seed interval, unused) at 3x the bootstrap cost. K1's minimum-seed rule is not under test here.
N, PROBES, SEEDS = 500, 49, (0,)


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode()


def _claim(subject: ArtifactRef, comparator: ArtifactRef, i: int) -> Claim:
    return Claim(kind=ClaimKind.IMPROVEMENT, statement=f"claim {i}", subject=subject, comparator=comparator,
                 metric=MetricRef(id="accuracy", higher_is_better=True),
                 effect=Effect(min_effect=0.001, predicted_lo=0.01, predicted_hi=0.2, probability=0.5),
                 scope=Scope(datasets=("t",)), development=(), generalization_targets=("t",),
                 analysis_plan=AnalysisPlan(seeds=SEEDS, n_boot=1000), resolve_by=time.time() + 1e6)


def one_campaign(root: Path, rng: random.Random, policy: str) -> tuple[bool, set[Code]]:
    store = EvidenceStore(root)
    store._db.execute("PRAGMA synchronous=OFF")  # test speed only: durability is not under test
    try:
        kernel = Kernel(store)
        labels = [str(rng.randrange(2)) for _ in range(N)]
        kernel.vault.register_dataset("t", DatasetRole.TEST, public={"x.csv": _csv([["i"], *[[str(i)] for i in
                                                                                              range(N)]])},
                                      private={"y.csv": _csv([["label"], *[[y] for y in labels]])})
        comp_rec = kernel.vault.register_artifact("baseline", b"constant 0", "baseline")
        comparator = ArtifactRef(artifact_id="baseline", hash=comp_rec.hash)
        probes = [[str(rng.randrange(2)) for _ in range(N)] for _ in range(PROBES)]
        for i in range(PROBES):
            art = kernel.vault.register_artifact(f"probe{i}", f"probe {i}".encode(), "method")
            kernel.preregister(_claim(ArtifactRef(artifact_id=f"probe{i}", hash=art.hash), comparator, i))
        if policy == "embargo":  # no feedback: the chosen half is independent of the labels
            chosen = rng.sample(probes, PROBES // 2)
        else:  # continuous feedback: each probe's held-out accuracy reached the adversary, who keeps the probes above
            # chance and flips those below (a flipped below-chance classifier is above chance)
            chosen = [p if sum(a == y for a, y in zip(p, labels, strict=True)) / N > 0.5 else
                      [str(1 - int(a)) for a in p] for p in probes]
            if policy == "continuous_logged":
                kernel.record_release("t", "evolution_score", "continuous", "evolve.score:continuous")
        vote = [str(int(sum(p[j] == "1" for p in chosen) * 2 > len(chosen))) for j in range(N)]
        art = kernel.vault.register_artifact("final", b"majority vote", "method")
        final = _claim(ArtifactRef(artifact_id="final", hash=art.hash), comparator, PROBES)
        kernel.preregister(final)
        ids = []
        for arm, preds in (("subject", vote), ("comparator", ["0"] * N)):
            for seed in SEEDS:
                kernel.vault.read("t", "y.csv", purpose="evaluate", claim_hash=final.claim_hash(), arm=arm, seed=seed)
                run = Run(harness_id="f1", harness_hash="f1", task_id="t", seed=seed)
                store.add_run(run)
                rec = ExecRecord(claim_hash=final.claim_hash(), arm=arm,  # type: ignore[arg-type]
                                 artifact_hash=final.subject.hash if arm == "subject" else comparator.hash,
                                 inputs={"test": InputRef(dataset_id="t", file="x.csv",
                                                          blob=kernel.vault.dataset("t").files["x.csv"].blob)},
                                 target="t", label_file="y.csv", seed=seed, env={}, limits={},
                                 preds_ref=store.blobs.put_json(preds), metric="accuracy",
                                 score=sum(a == y for a, y in zip(preds, labels, strict=True)) / N, run_id=run.id)
                ids.append(kernel.record_evaluation(rec))
        ctx = K.Context(claim=final, claim_hash=final.claim_hash(), store=store, vault=kernel.vault,
                        ledger=kernel.ledger, config=KernelConfig(), replayer=None, evidence_ids=tuple(ids))
        assert K.k0_provenance(ctx).status == "pass" and K.k2_preregistered(ctx).status == "pass"
        k4, k6 = K.k4_statistics(ctx), K.k6_held_out(ctx)
        # the family really is 50 claims (an adaptively exposed claim cannot be certified, so it is not counted)
        family = "49 claim(s)" if policy == "continuous_logged" else "50 claim(s)"
        assert any(family in o for o in k4.observations), k4.observations
        return k4.status == "pass" and k6.status == "pass", set(k6.codes)
    finally:
        store.close()


@pytest.mark.parametrize("policy", ["embargo", "continuous_unlogged", "continuous_logged"])
def test_f1_false_certification_by_release_policy(tmp_path, policy):
    reps = 5 if policy == "continuous_logged" else 40  # runtime: about 3 s per campaign
    rng = random.Random({"embargo": 1, "continuous_unlogged": 2, "continuous_logged": 3}[policy])
    outcomes = [one_campaign(tmp_path / f"{policy}{r}", rng, policy) for r in range(reps)]
    rate = sum(ok for ok, _ in outcomes) / reps
    if policy == "embargo":
        assert rate <= 0.065, rate
    elif policy == "continuous_unlogged":
        assert rate >= 0.30, rate  # the attack happened: Bonferroni over claims alone does not stop it
    else:
        assert rate == 0.0 and all(Code.K6_ADAPTIVELY_EXPOSED in codes for _, codes in outcomes)
