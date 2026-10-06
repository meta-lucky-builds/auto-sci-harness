"""A small, deterministic world for exercising the Evidence Kernel end to end.

One World = its own store/ledger/vault + a synthetic 3-class "rings" task + trusted stdlib programs that
are really executed in a (local, trusted-code) sandbox. Scenarios then build honest or broken claims on
top of it. Used by test_kernel_suite.py (the M1 exit suite) and test_kernel_units.py.
"""

from __future__ import annotations

import csv
import io
import math
import random
import time
from pathlib import Path

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
    MetricRef,
    Risk,
    SandboxReplayer,
    Scope,
    run_program,
)
from custom_harness.records import Run, StepKind, TraceStep
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from custom_harness.verify import metrics as M

_READ = '''
import argparse, csv, math, random
p = argparse.ArgumentParser()
for k in ("--train", "--test", "--out"): p.add_argument(k)
p.add_argument("--seed", type=int, default=0)
a = p.parse_args()
def load(path):
    with open(path, newline="") as f:
        r = list(csv.reader(f))
    return r[0], r[1:]
h, rows = load(a.train); th, trows = load(a.test)
def write(preds):
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["prediction"])
        for x in preds: w.writerow([x])
'''

KNN = _READ + '''
K = {k}
{pre}
rad = lambda r, hh: math.hypot(float(r[hh.index("x1")]), float(r[hh.index("x2")]))
pts = sorted((rad(r, h), r[h.index("label")]) for r in rows)
def knn(x):
    near = sorted(pts, key=lambda t: (abs(t[0] - x), t[1]))[:K]
    votes = {{}}
    for _, y in near: votes[y] = votes.get(y, 0) + 1
    return max(sorted(votes), key=votes.get)
preds = [knn(rad(r, th)) for r in trows]
{post}
write(preds)
'''

PROGRAMS = {
    "baseline": _READ + '''
from collections import Counter
maj = Counter(r[h.index("label")] for r in rows).most_common(1)[0][0]
write([maj for _ in trows])
''',
    "weak": _READ + "write(['0' for _ in trows])\n",
    "good": KNN.format(k=15, pre="", post=""),
    "good_k17": KNN.format(k=17, pre="", post=""),
    # genuinely seed-dependent: each seed trains on a different 70% subsample
    "good_seeded": KNN.format(k=7, pre="rows = random.Random(a.seed).sample(rows, int(0.7 * len(rows)))", post=""),
    "nondet": KNN.format(k=15, pre="", post="rng = random.SystemRandom()\npreds = [p if rng.random() > 0.15 else "
                                     "rng.choice(['0', '1', '2']) for p in preds]"),
    "ablated": _READ + '''
xs = sorted((float(r[h.index("x1")]), r[h.index("label")]) for r in rows)
def nn(x):
    near = sorted(xs, key=lambda t: (abs(t[0] - x), t[1]))[:15]
    votes = {}
    for _, y in near: votes[y] = votes.get(y, 0) + 1
    return max(sorted(votes), key=votes.get)
write([nn(float(r[th.index("x1")])) for r in trows])
''',
    "noisy_majority": _READ + '''
from collections import Counter
maj = Counter(r[h.index("label")] for r in rows).most_common(1)[0][0]
rng = random.Random(a.seed)
write([maj if rng.random() > 0.05 else rng.choice(["0", "1", "2"]) for _ in trows])
''',
}


def _sample(rng: random.Random) -> list[str]:
    label = rng.randrange(3)
    radius = 1.0 + 1.2 * label + rng.gauss(0, 0.55)
    angle = rng.uniform(0, 2 * math.pi)
    return [f"{radius * math.cos(angle):.5f}", f"{radius * math.sin(angle):.5f}", f"{rng.gauss(0, 1):.5f}", str(label)]


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode()


class World:
    def __init__(self, root: Path, seed: int = 0, n_train: int = 240, n_test: int = 240):
        self.root = Path(root)
        self.store = EvidenceStore(self.root / "store")
        self.replayer = SandboxReplayer(lambda key: LocalSandbox(self.root / "replay" / key,
                                                                 i_understand_no_isolation=True))
        self.kernel = Kernel(self.store, replayer=self.replayer)
        self.vault = self.kernel.vault
        rng = random.Random(seed)
        header = ["x1", "x2", "n1", "label"]
        train = [_sample(rng) for _ in range(n_train)]
        self.train = _csv([header, *train])
        self.vault.register_dataset("train", DatasetRole.DEV, public={"train.csv": self.train}, private={})
        small = [header, *train[: n_train // 6]]
        self.vault.register_dataset("train_small", DatasetRole.DEV, public={"train.csv": _csv(small)}, private={})
        self.labels: dict[str, list[str]] = {}
        for i, name in enumerate(("test", "test2")):
            test = [_sample(rng) for _ in range(n_test)]
            self.labels[name] = [r[-1] for r in test]
            self.vault.register_dataset(
                name, DatasetRole.TEST, public={"test_X.csv": _csv([header[:-1], *[r[:-1] for r in test]])},
                private={"test_y.csv": _csv([["label"], *[[r[-1]] for r in test]])},
                canary=f"CHH-CANARY-world{seed}-{i}")
        self._rng, self._header = rng, header
        self.code: dict[str, bytes] = {}
        self._cache: dict[tuple[str, str, int], list[str] | None] = {}
        self._n = 0

    def register_target(self, dataset_id: str, role: DatasetRole, n: int = 240) -> None:
        """A fresh held-out dataset (new samples) with its own canary."""
        rows = [_sample(self._rng) for _ in range(n)]
        self.labels[dataset_id] = [r[-1] for r in rows]
        self.vault.register_dataset(
            dataset_id, role, public={"test_X.csv": _csv([self._header[:-1], *[r[:-1] for r in rows]])},
            private={"test_y.csv": _csv([["label"], *[[r[-1]] for r in rows]])}, canary=f"CHH-CANARY-{dataset_id}")

    # ---------------------------------------------------------------- artifacts and claims
    def artifact(self, name: str, kind: str = "method", code: str | None = None) -> ArtifactRef:
        data = (code if code is not None else PROGRAMS[name]).encode()
        rec = self.vault.register_artifact(name, data, kind)
        self.code[rec.hash] = data
        return ArtifactRef(artifact_id=name, hash=rec.hash)

    def claim(self, subject: ArtifactRef, comparator: ArtifactRef, *, kind: ClaimKind = ClaimKind.IMPROVEMENT,
              delta: float = 0.2, seeds: tuple[int, ...] = (0, 1, 2), targets: tuple[str, ...] = ("test",),
              development: tuple[str, ...] = ("train",), probability: float = 0.8, **plan: object) -> Claim:
        return Claim(kind=kind, statement=f"{subject.artifact_id} vs {comparator.artifact_id}", subject=subject,
                     comparator=comparator, metric=MetricRef(id="accuracy", higher_is_better=True),
                     effect=Effect(min_effect=delta, predicted_lo=delta, predicted_hi=0.8, probability=probability),
                     scope=Scope(datasets=("train", *targets)), development=development,
                     generalization_targets=targets,
                     analysis_plan=AnalysisPlan(seeds=seeds, **plan), risk=Risk(), resolve_by=time.time() + 86400)

    # ---------------------------------------------------------------- execution
    def run(self, code_hash: str, train_ds: str, target: str, seed: int) -> list[str] | None:
        key = (code_hash, train_ds + target, seed)
        if key not in self._cache:
            self._n += 1
            sb = LocalSandbox(self.root / "runs" / f"r{self._n}", i_understand_no_isolation=True)
            train = self.vault.read_public(train_ds, "train.csv")
            test = self.vault.read_public(target, "test_X.csv")
            preds, _ = run_program(sb, code=self.store.blobs.get(code_hash), entrypoint="solution.py",
                                   argv=("{python}", "{entrypoint}", "--train", "{train}", "--test", "{test}",
                                         "--out", "{out}", "--seed", "{seed}"),
                                   inputs={"train": ("train.csv", train), "test": ("test_X.csv", test)},
                                   seed=seed, timeout_s=120)
            self._cache[key] = preds
        return self._cache[key]

    def evaluate(self, claim: Claim, arm: str, *, seed: int, target: str = "test", code_hash: str | None = None,
                 train_ds: str = "train", log_read: bool = True, score_offset: float = 0.0,
                 limits: dict[str, float] | None = None, argv_extra: tuple[str, ...] = (),
                 trace_text: str = "") -> str:
        """Run one arm honestly (unless told otherwise) and record it as evidence; returns the evidence id."""
        ref = claim.subject if arm == "subject" else claim.comparator
        code_hash = code_hash or ref.hash
        preds = self.run(code_hash, train_ds, target, seed)
        assert preds is not None, "a world program failed"
        if log_read:
            raw = self.vault.read(target, "test_y.csv", purpose="evaluate", claim_hash=claim.claim_hash(), arm=arm,
                                  seed=seed)
            labels = [ln.split(",")[0] for ln in raw.decode().splitlines()[1:]]
        else:
            labels = self.labels[target]
        score = M.score("accuracy", labels, preds) + score_offset
        run = Run(harness_id="kernel-world", harness_hash="world", task_id=target, seed=seed)
        self.store.add_run(run)
        now = time.time()
        self.store.add_step(TraceStep(run_id=run.id, index=0, node_id="evaluate", kind=StepKind.TOOL,
                                      input={"arm": arm}, output={"note": trace_text or "evaluated"},
                                      started_at=now, ended_at=now))
        rec = ExecRecord(
            claim_hash=claim.claim_hash(), arm=arm, artifact_hash=code_hash,  # type: ignore[arg-type]
            argv=("{python}", "{entrypoint}", "--train", "{train}", "--test", "{test}", "--out", "{out}", "--seed",
                  "{seed}", *argv_extra),
            inputs={"train": InputRef(dataset_id=train_ds, file="train.csv",
                                      blob=self.vault.dataset(train_ds).files["train.csv"].blob),
                    "test": InputRef(dataset_id=target, file="test_X.csv",
                                     blob=self.vault.dataset(target).files["test_X.csv"].blob)},
            target=target, label_file="test_y.csv", seed=seed, env={"sandbox": "local (trusted test code)"},
            limits=limits or {"timeout_s": 120.0}, preds_ref=self.store.blobs.put_json(preds), metric="accuracy",
            score=score, run_id=run.id)
        return self.kernel.record_evaluation(rec)

    def evaluate_all(self, claim: Claim, *, targets: tuple[str, ...] | None = None, **kw: object) -> list[str]:
        ids = []
        for target in targets or claim.generalization_targets:
            for seed in claim.analysis_plan.seeds:
                for arm in ("subject", "comparator"):
                    ids.append(self.evaluate(claim, arm, seed=seed, target=target, **kw))  # type: ignore[arg-type]
        return ids

    def close(self) -> None:
        self.store.close()
