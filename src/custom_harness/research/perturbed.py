"""A real perturbed evaluator for ML skills (GOAL §5 L3/L5 exploit quarantine; memory/exploit.py).

`PerturbedTaskEvaluator` is an `Evaluator` for the memory package's `PerturbedEvaluatorCheck`: called with
(entry, perturbed) it returns the skill's gain over its baseline, re-measured from scratch by running the two
programs of the skill's certified claim (subject and comparator, code from the evidence store) under k-fold
cross-validation on the claim's PUBLIC development data. It never reads held-out data: only
`Vault.read_public` on the claim's first development dataset, which must not have a held-out role.

Nominal evaluator: the contract the skill was certified under. The claim's seeds, fold-split seed 0, the
standard input names and layout (`train.csv`, `test_X.csv`, the same argv as the held-out runs), original row
and column order.

Perturbed but equivalent evaluator: what an honest program must not care about. A disjoint seed set (derived
from the certificate hash, never overlapping the claim's seeds), a different fold split, randomised input file
names, the test rows shuffled (the predictions are un-shuffled before scoring) and the column order permuted
(the CLI contract names columns: programs must read by header name). A skill whose gain disappears under this
evaluator was exploiting the grader (e.g. good only for the claim's seeds, or only when the test file is named
`test_X.csv`).

Gain = mean over seeds of (metric(subject OOF predictions) - metric(comparator OOF predictions)), sign-adjusted
so that positive is better. Anything unexpected (no certified ML certificate, a dataset without a declared target
column, a failing program run, a wrong number of predictions) raises, so the check fails closed.

Wiring: this module may not import the memory package (only memory/* and cli.py may, see
test_memory_package_has_no_model_path_and_only_trusted_code_writes), so trusted wiring code composes
`PerturbedEvaluatorCheck(PerturbedTaskEvaluator(...), min_retained=...)`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import itertools
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..kernel import DEFAULT_ARGV, Kernel, Label, run_program
from ..kernel.vault import HELD_OUT_ROLES
from ..sandbox.base import Sandbox
from ..verify import metrics as M

# Certificates of data (ML) claims; exact/simulated/formal certificates have no development data to re-score on.
ML_CERTIFIED = frozenset({Label.CERTIFIED_VALIDATED, Label.CERTIFIED_INTERNAL})
TRAIN_FILE = "train.csv"  # the standard public layout of a development dataset (research/task.py, kernel_world)
NOMINAL_TEST_FILE = "test_X.csv"

DEFAULT_K = 3  # UNCALIBRATED (owner decision, GOAL §13.8): folds of the cross-validation
DEFAULT_PERTURBED_SEEDS = 3  # UNCALIBRATED (owner decision, GOAL §13.8): seeds of the perturbed evaluator
DEFAULT_TIMEOUT_S = 120.0  # UNCALIBRATED (owner decision, GOAL §13.8): per program run
PERTURBED_SEED_RANGE = (10_000, 2**31 - 1)  # UNCALIBRATED (owner decision, GOAL §13.8): perturbed seeds drawn here


class PerturbedEvalError(RuntimeError):
    """The evaluator cannot produce a gain; the exploit check treats it as a failure (fail closed)."""


@dataclass(frozen=True)
class Condition:
    """One evaluator configuration: seeds, fold split, input names and row/column permutations."""

    seeds: tuple[int, ...]
    split_seed: int
    train_name: str
    test_name: str
    permute: bool  # permute columns and shuffle test rows (with `rng_seed`)
    rng_seed: int


def _table(data: bytes) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    if not rows or not rows[0]:
        raise PerturbedEvalError("development data has no header")
    width = len(rows[0])
    if any(len(r) != width for r in rows[1:]):
        raise PerturbedEvalError("development data is not rectangular")
    return rows[0], rows[1:]


def _csv(rows: Sequence[Sequence[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode()


class PerturbedTaskEvaluator:
    """`Evaluator` (entry, perturbed) -> gain. `targets` maps a development dataset id to its target column; it
    is supplied by trusted code (an unknown dataset raises). `spawn(name)` returns a fresh sandbox."""

    def __init__(self, kernel: Kernel, spawn: Callable[[str], Sandbox], targets: Mapping[str, str], *,
                 k: int = DEFAULT_K, timeout_s: float = DEFAULT_TIMEOUT_S,
                 n_perturbed_seeds: int = DEFAULT_PERTURBED_SEEDS):
        if k < 2:
            raise ValueError("k must be at least 2")
        if n_perturbed_seeds < 1:
            raise ValueError("n_perturbed_seeds must be at least 1")
        self.kernel, self.spawn, self.targets = kernel, spawn, dict(targets)
        self.k, self.timeout_s, self.n_perturbed_seeds = k, timeout_s, n_perturbed_seeds
        self._runs = itertools.count()

    # ------------------------------------------------------------------ conditions
    def condition(self, cert_hash: str, claim_seeds: tuple[int, ...], perturbed: bool) -> Condition:
        if not perturbed:
            return Condition(claim_seeds, 0, TRAIN_FILE, NOMINAL_TEST_FILE, False, 0)
        rng = random.Random(hashlib.sha256(f"chh-perturbed|{cert_hash}".encode()).digest())
        lo, hi = PERTURBED_SEED_RANGE
        seeds: list[int] = []
        while len(seeds) < self.n_perturbed_seeds:
            s = rng.randint(lo, hi)
            if s not in claim_seeds and s not in seeds:
                seeds.append(s)
        split_seed = rng.randint(1, hi)
        token = f"{rng.getrandbits(48):012x}"
        return Condition(tuple(seeds), split_seed, f"d{token}.csv", f"q{token[::-1]}.csv", True, rng.getrandbits(64))

    # ------------------------------------------------------------------ evaluation
    def __call__(self, entry: Any, perturbed: bool) -> float:
        cert_hash, claim = self._certified_claim(entry)
        try:
            subject, comparator = claim["subject"]["hash"], claim["comparator"]["hash"]
            metric, higher = claim["metric"]["id"], bool(claim["metric"]["higher_is_better"])
            development = claim["development"]
            claim_seeds = tuple(int(s) for s in claim["analysis_plan"]["seeds"])
        except (KeyError, TypeError, ValueError) as e:
            raise PerturbedEvalError(f"certificate {cert_hash[:12]}: malformed claim ({e})") from e
        if not development:
            raise PerturbedEvalError(f"certificate {cert_hash[:12]}: the claim names no development dataset")
        if not claim_seeds:
            raise PerturbedEvalError(f"certificate {cert_hash[:12]}: the claim names no seeds")
        dataset = development[0]
        if dataset not in self.targets:
            raise PerturbedEvalError(f"no target column declared for development dataset {dataset!r}")
        if self.kernel.vault.dataset(dataset).role in HELD_OUT_ROLES:
            raise PerturbedEvalError(f"development dataset {dataset!r} has a held-out role")
        header, rows = _table(self.kernel.vault.read_public(dataset, TRAIN_FILE))  # public bytes, never labels
        target = self.targets[dataset]
        if target not in header:
            raise PerturbedEvalError(f"target column {target!r} is not in {dataset!r}")
        if len(rows) < self.k:
            raise PerturbedEvalError(f"{dataset!r} has fewer rows than folds")
        cond = self.condition(cert_hash, claim_seeds, perturbed)
        code_s, code_c = self.kernel.store.blobs.get(subject), self.kernel.store.blobs.get(comparator)
        labels = [r[header.index(target)] for r in rows]
        gains = []
        for seed in cond.seeds:
            s = M.score(metric, labels, self._oof(code_s, header, rows, target, seed, cond))
            c = M.score(metric, labels, self._oof(code_c, header, rows, target, seed, cond))
            gains.append(s - c if higher else c - s)
        return sum(gains) / len(gains)

    def _certified_claim(self, entry: Any) -> tuple[str, dict[str, Any]]:
        for h in entry.evidence:
            cert = self.kernel.certificate(h)
            if cert.label in ML_CERTIFIED:
                return h, cert.claim
        raise PerturbedEvalError("the skill cites no certified data (ML) certificate to re-evaluate")

    def _oof(self, code: bytes, header: list[str], rows: list[list[str]], target: str, seed: int,
             cond: Condition) -> list[str]:
        """Out-of-fold predictions of one program for one seed, in the original row order."""
        idx = list(range(len(rows)))
        random.Random(cond.split_seed).shuffle(idx)
        folds = [idx[f::self.k] for f in range(self.k)]
        cols = list(range(len(header)))
        prng = random.Random(cond.rng_seed + seed)  # used only when cond.permute
        if cond.permute and len(cols) > 1:
            while cols == sorted(cols):  # a real permutation, never the identity
                prng.shuffle(cols)
        tcol = header.index(target)
        feat = [c for c in cols if c != tcol]
        out: list[str | None] = [None] * len(rows)
        for f, fold_idx in enumerate(folds):
            train_idx = sorted(i for g, fold in enumerate(folds) if g != f for i in fold)
            test_idx = sorted(fold_idx)
            if cond.permute:
                prng.shuffle(test_idx)  # predictions come back in this order and are un-shuffled below
            train = _csv([[header[c] for c in cols], *[[rows[i][c] for c in cols] for i in train_idx]])
            test = _csv([[header[c] for c in feat], *[[rows[i][c] for c in feat] for i in test_idx]])
            sandbox = self.spawn(f"perturbed-{next(self._runs)}")
            preds, diag = run_program(sandbox, code=code, entrypoint="solution.py", argv=DEFAULT_ARGV,
                                      inputs={"train": (cond.train_name, train), "test": (cond.test_name, test)},
                                      seed=seed, timeout_s=self.timeout_s)
            if preds is None:
                raise PerturbedEvalError(f"a program run failed under the {'perturbed' if cond.permute else 'nominal'}"
                                         f" evaluator: {diag}")
            if len(preds) != len(test_idx):
                raise PerturbedEvalError(f"a program returned {len(preds)} predictions for {len(test_idx)} rows")
            for i, p in zip(test_idx, preds, strict=True):
                out[i] = p
        if any(p is None for p in out):
            raise PerturbedEvalError("the folds did not cover every row")  # pragma: no cover  (folds partition idx)
        return [p for p in out if p is not None]
