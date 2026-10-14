"""Seeded random histories through the PUBLIC Kernel API, for the differential test (docs/kernel_differential.md).

Each history is one fresh `kernel_world.World` (own store, ledger, vault; programs really executed in a local trusted
sandbox) plus a randomly drawn sequence of Kernel actions of one kind: honest, p-hacked/peeking, leaked,
irreproducible, strawman, trivial/malformed, negative/equivalence, alpha-accounts, releases, minted targets, external
targets, verify-after-a-later-event, and the cheap non-data classes (exact LP certificates, simulations). Formal (Lean)
claims are NOT generated (they need the pinned-toolchain monkeypatch of tests/test_kernel_formal.py; the suite covers
them).

Everything is drawn from `random.Random(f"{seed}:{index}")`, so history `i` is the same sequence of API calls for OLD
and NEW. Nondeterminism is removed AT THE SOURCE where the generator controls it: every claim gets a fixed
`resolve_by` and the Kernel a counting clock (so claim hashes and `issued_at` are reproducible); what remains (uuid
evidence/run ids, the programs that are random on purpose) is handled by the diff tool's proven normalisation rules.

Only the public API is used (custom_harness.kernel exports, Kernel methods, Vault methods, kernel.sim for the
simulation result bytes): a refactor of checks.py must not need a change here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import tempfile
import time
import traceback
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import kernel_recorder

from custom_harness.kernel import (
    AnalysisPlan,
    ArtifactRef,
    Claim,
    ClaimKind,
    DatasetRole,
    Effect,
    KernelError,
    MetricRef,
    Scope,
    VaultError,
    canonical_json,
)
from kernel_world import PROGRAMS, World

FAR = 4102444800.0  # fixed resolve_by (2100-01-01): claim hashes do not depend on the wall clock
FAST = {"eps_max": 0.5, "beta": 0.25}  # the loosest K3 reliability requirement (2 replays), as in test_kernel_s16.py


class Ctx:
    def __init__(self, w: World, rng: random.Random):
        self.w, self.rng, self.k = w, rng, w.kernel
        self.certs: list[Any] = []

    # -------------------------------------------------------------- claims
    def plan(self) -> dict[str, Any]:
        r = self.rng
        kw: dict[str, Any] = {"n_boot": r.choice([500, 1000, 2000])}
        if r.random() < 0.85:
            kw |= FAST
        if r.random() < 0.2:
            kw["reproduce"] = "all"
        if r.random() < 0.3:
            kw["alpha"] = r.choice([0.01, 0.025, 0.05, 0.1])
        if r.random() < 0.1:
            kw["reproduce_tolerance"] = r.choice([0.0, 0.01, 0.05])
        return kw

    def claim(self, subject: str | ArtifactRef = "good", comparator: str = "baseline", ckind: str = "baseline", *,
              delta: float | None = None, seeds: tuple[int, ...] | None = None, **kw: Any) -> Claim:
        r = self.rng
        delta = round(r.uniform(0.05, 0.45), 3) if delta is None else delta
        seeds = seeds or tuple(range(r.choice([2, 3, 3, 4])))
        prob = kw.pop("probability", round(r.uniform(0.55, 0.95), 2))
        args = {**self.plan(), **kw}
        subj = subject if isinstance(subject, ArtifactRef) else self.w.artifact(subject)
        c = self.w.claim(subj, self.w.artifact(comparator, ckind), delta=delta, seeds=seeds,
                         probability=prob, **args)
        return c.model_copy(update={"resolve_by": FAR})

    def prereg(self, c: Claim) -> bool:
        try:
            self.k.preregister(c)
            return True
        except KernelError:
            return False  # recorded by the recorder (type + message)

    def certify(self, c: Claim, ids: list[str], **kw: Any) -> Any:
        cert = self.k.certify(c, ids, **kw)
        if kw.get("record", True):
            self.certs.append(cert)
        return cert

    def honest(self, c: Claim, **kw: Any) -> Any:
        self.prereg(c)
        return self.certify(c, self.w.evaluate_all(c, **kw))


# ------------------------------------------------------------------ history kinds
def h_honest(x: Ctx) -> None:
    targets = ("test", "test2") if x.rng.random() < 0.3 else ("test",)
    kind = x.rng.choice([ClaimKind.IMPROVEMENT, ClaimKind.IMPROVEMENT, ClaimKind.ABLATION_EFFECT])
    comp, ckind = ("ablated", "method") if kind == ClaimKind.ABLATION_EFFECT else ("baseline", "baseline")
    subj = x.rng.choice(["good", "good", "good_seeded", "good_k17"])
    x.honest(x.claim(subj, comp, ckind, kind=kind, targets=targets))


def h_negative_equivalence(x: Ctx) -> None:
    if x.rng.random() < 0.5:
        c = x.claim("noisy_majority", kind=ClaimKind.NEGATIVE_RESULT, delta=round(x.rng.uniform(0.02, 0.4), 3),
                    probability=round(x.rng.uniform(0.5, 0.9), 2))
    else:
        c = x.claim("good_k17", "good", "method", kind=ClaimKind.EQUIVALENCE, delta=round(x.rng.uniform(0.02, 0.2), 3))
    x.honest(c)


def h_peek(x: Ctx) -> None:
    c = x.claim()
    x.w.vault.read(x.rng.choice(["test", "test2"]) if x.rng.random() < 0.3 else "test", "test_y.csv",
                   purpose=x.rng.choice(["develop", "audit"]))
    x.honest(c)


def h_edited(x: Ctx) -> None:
    c = x.claim(delta=0.2)
    x.prereg(c)
    edited = c.model_copy(update={"effect": c.effect.model_copy(update={"min_effect": x.rng.choice([0.15, 0.25, 0.3]),
                                                                        "predicted_lo": 0.2})})
    x.certify(edited, x.w.evaluate_all(edited, log_read=False))


def h_selective(x: Ctx) -> None:
    """Ran more seeds than registered and reported only the registered ones (K4: unreported runs)."""
    c = x.claim("good_seeded", seeds=(0, 1, 2))
    x.prereg(c)
    ids = []
    for seed in range(3 + x.rng.choice([0, 1, 3])):  # 0 extra seeds = the honest control
        for arm in ("subject", "comparator"):
            eid = x.w.evaluate(c, arm, seed=seed)
            if seed in c.analysis_plan.seeds:
                ids.append(eid)
    x.certify(c, ids)


def h_swapped(x: Ctx) -> None:
    c = x.claim()
    x.prereg(c)
    other = x.w.artifact(x.rng.choice(["good_k17", "good_seeded"])).hash
    ids = []
    for seed in c.analysis_plan.seeds:
        ids.append(x.w.evaluate(c, "subject", seed=seed, code_hash=other))
        ids.append(x.w.evaluate(c, "comparator", seed=seed))
    x.certify(c, ids)


def h_leaked(x: Ctx) -> None:
    w, r = x.w, x.rng
    mode = r.choice(["labels", "canary", "canary_other", "trace", "develop"])
    if mode == "labels":
        sep = r.choice([",", " ", "\n"])
        code = PROGRAMS["good"] + '\nHIDDEN = """' + sep.join(w.labels["test"]) + '"""\n'
        x.honest(x.claim(w.artifact("good_leaky", code=code)))
    elif mode in ("canary", "canary_other"):
        code = PROGRAMS["good"] + f"\n# {w.vault.canary('test' if mode == 'canary' else 'test2')}\n"
        x.honest(x.claim(w.artifact("good_canary", code=code)))
    elif mode == "trace":
        c = x.claim()
        x.prereg(c)
        bad = r.choice(c.analysis_plan.seeds)
        ids = []
        for seed in c.analysis_plan.seeds:
            ids.append(w.evaluate(c, "subject", seed=seed,
                                  trace_text=f"prompt: {w.vault.canary('test')}" if seed == bad else ""))
            ids.append(w.evaluate(c, "comparator", seed=seed))
        x.certify(c, ids)
    else:
        c = x.claim()
        x.prereg(c)
        w.vault.read("test", "test_y.csv", purpose="develop")
        x.certify(c, w.evaluate_all(c))


def h_irreproducible(x: Ctx) -> None:
    w, r = x.w, x.rng
    mode = r.choice(["nondet", "fabricated", "preds_lost", "code_corrupted"])
    if mode == "nondet":
        x.honest(x.claim("nondet"))
    elif mode == "fabricated":
        c = x.claim()
        x.prereg(c)
        off = r.choice([1e-6, 1e-3, 0.01, 0.03])
        ids = []
        for seed in c.analysis_plan.seeds:
            ids.append(w.evaluate(c, "subject", seed=seed, score_offset=off))
            ids.append(w.evaluate(c, "comparator", seed=seed))
        x.certify(c, ids)
    else:
        c = x.claim()
        x.prereg(c)
        ids = w.evaluate_all(c)
        if mode == "preds_lost":
            rec = w.store.blobs.get_json(w.store.get_evidence(ids[r.randrange(len(ids))]).payload_ref)
            w.store.blobs._path(rec["preds_ref"]).unlink()
        else:
            w.store.blobs._path(c.subject.hash).write_bytes(b"print('not the code that ran')")
        x.certify(c, ids)


def h_strawman(x: Ctx) -> None:
    w, r = x.w, x.rng
    mode = r.choice(["train_small", "weak_code", "ablated_code", "limits", "argv", "seeds", "weak_called_baseline"])
    if mode == "weak_called_baseline":
        x.honest(x.claim("good", "weak", "method"))
        return
    if mode == "seeds":
        c = x.claim("good_seeded", seeds=(0, 1, 2))
        x.prereg(c)
        ids = [w.evaluate(c, "subject", seed=s) for s in (0, 1, 2)]
        ids += [w.evaluate(c, "comparator", seed=s) for s in r.sample(range(3, 12), 3)]
        x.certify(c, ids)
        return
    kw: dict[str, Any] = {"train_small": {"train_ds": "train_small"},
                          "weak_code": {"code_hash": w.artifact("weak").hash},
                          "ablated_code": {"code_hash": w.artifact("ablated").hash},
                          "limits": {"limits": {"timeout_s": r.choice([5.0, 30.0, 60.0])}},
                          "argv": {"argv_extra": (r.choice(["--fast", "--k=3"]),)}}[mode]
    c = x.claim()
    x.prereg(c)
    ids = []
    for seed in c.analysis_plan.seeds:
        ids.append(w.evaluate(c, "subject", seed=seed))
        ids.append(w.evaluate(c, "comparator", seed=seed, **kw))
    x.certify(c, ids)


def h_trivial(x: Ctx) -> None:
    r = x.rng
    mode = r.choice(["tiny", "tiny", "zero", "negative", "unsupported", "target_dev", "low_probability"])
    if mode == "tiny":
        x.honest(x.claim(delta=r.choice([0.001, 0.003, 0.005, 0.008, 0.012, 0.02])))
    elif mode == "zero":
        x.honest(x.claim(delta=0.0))
    elif mode == "negative":
        x.honest(x.claim(delta=-round(r.uniform(0.01, 0.2), 3)))
    elif mode == "unsupported":
        x.honest(x.claim(kind=r.choice([ClaimKind.SCALING_LAW, ClaimKind.REDISCOVERY])))
    elif mode == "target_dev":
        x.honest(x.claim(development=("train", "test")))
    else:  # a forecast below p0: refused at registration (and by K1)
        x.honest(x.claim(probability=round(r.uniform(0.01, 0.2), 2)))


def h_family(x: Ctx) -> None:
    """Several claims on one target, with an optional alpha-account, releases and mints; certify all, then verify the
    first certificate after the later events (valid / stale)."""
    w, k, r = x.w, x.k, x.rng
    target = "fam"
    w.register_target(target, DatasetRole.TEST, n=r.choice([60, 90, 120]))
    if r.random() < 0.6:
        try:
            k.open_alpha_account(target, r.choice([0.02, 0.05, 0.06, 0.1]), r.choice([1, 2, 3, 4]),
                                 approver=r.choice(["owner", "owner", " "]))
        except KernelError:
            pass
    claims = []
    for j in range(r.choice([2, 3, 4])):
        c = x.claim(delta=round(0.1 + 0.01 * j + r.uniform(0, 0.2), 3), targets=(target,))
        if r.random() < 0.4:
            try:
                k.record_release(target, r.choice(["prompt", "export", "memory", "evolution_score", "report", "tv"]),
                                 r.choice(["verdict_bit", "count", "verdict_bit", "continuous", "real"]),
                                 r.choice(["report.verdict", "evolve.score:verdict", "Bad Note"]))
            except KernelError:
                pass
        if r.random() < 0.2:
            try:
                k.record_mint(target, r.choice(["f" * 64, "e" * 64]), f"beacon-{j}", "task-a")
            except KernelError:
                pass
        if x.prereg(c):
            claims.append((c, w.evaluate_all(c)))
    for c, ids in claims:
        x.certify(c, ids, record=r.random() < 0.7)
    if r.random() < 0.3:
        try:
            k.retire_alpha_account(target, approver="owner")
        except KernelError:
            pass
        x.prereg(x.claim(delta=0.33, targets=(target,)))
    recorded = [c for c in x.certs]
    if recorded:
        if r.random() < 0.5:
            w.vault.read(target, "test_y.csv", purpose="develop")  # a later event
        k.verify_status(recorded[0].cert_hash())


def h_external(x: Ctx) -> None:
    w, r = x.w, x.rng
    role = r.choice([DatasetRole.EXTERNAL, DatasetRole.TEST])
    w.register_target("ext", role, n=r.choice([60, 120]))
    if r.random() < 0.5:
        with contextlib.suppress(KernelError, VaultError):  # refused for a non-external role; the certificate shows it
            w.kernel.approve_role("ext", approver="owner")
    x.honest(x.claim(targets=("ext",)))


LP_PROBLEM = {"format": "chh-lp/1", "sense": "max", "c": ["1", "1"], "A": [["2", "1"], ["1", "3"]], "b": ["4", "5"]}
LP_CERT = {"format": "chh-lp-certificate/1", "x": ["7/5", "6/5"], "y": ["2/5", "1/5"]}


def _exact_claim(x: Ctx, data: bytes, aid: str, metric: str, value: str | None, evidence_class: str,
                 conditions: dict[str, str] | None = None) -> Claim:
    ref = ArtifactRef(artifact_id=aid, hash=x.w.vault.register_artifact(aid, data, "problem").hash)
    doc = Claim(kind=ClaimKind.IMPROVEMENT, statement=f"the value of {aid} is {value}", subject=ref, comparator=ref,
                metric=MetricRef(id=metric, higher_is_better=True),
                effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                scope=Scope(datasets=(), conditions=conditions or {}), development=(), generalization_targets=(),
                analysis_plan=AnalysisPlan(seeds=()), resolve_by=FAR).model_dump(mode="json")
    doc |= {"kind": "computed_value", "evidence_class": evidence_class, "value": value}
    return Claim.model_validate(doc)


def _exact_certify(x: Ctx, c: Claim, data: bytes, kind: str) -> None:
    x.prereg(c)
    aid = f"{kind}-{c.claim_hash()[:10]}-{hashlib.sha256(data).hexdigest()[:10]}"
    x.w.vault.register_artifact(aid, data, kind)
    cert = x.certify(c, [x.k.record_certificate(c, aid)])
    if x.rng.random() < 0.3:
        x.k.verify_status(cert.cert_hash())


def h_lp(x: Ctx) -> None:
    r = x.rng
    if r.random() < 0.85:
        x.k.approve_adapter("lp", approver="test-world")
    value = r.choice(["13/5", "13/5", "13/5", "3", "2.6", "26/10", None])
    cert = dict(LP_CERT)
    fault = r.choice([None, None, "x", "y", "gap", "float", "dims"])
    if fault == "x":
        cert["x"] = ["2", "1"]
    elif fault == "y":
        cert["y"] = ["1/5", "1/5"]
    elif fault == "gap":
        cert["x"] = ["1", "1"]
    elif fault == "float":
        cert["x"] = [1.4, 1.2]  # type: ignore[list-item]
    elif fault == "dims":
        cert["x"] = ["7/5", "6/5", "0"]
    c = _exact_claim(x, canonical_json(LP_PROBLEM), "lp-instance", "lp_optimum", value, "computational")
    _exact_certify(x, c, canonical_json(cert), "certificate")


def h_sim(x: Ctx) -> None:
    from custom_harness.kernel import sim

    r = x.rng
    if r.random() < 0.85:
        x.k.approve_adapter("sim", approver="test-world")
    cfg = {"format": "chh-sim/1", "simulator": "chh-ode-rk4", "version": "1", "model": "damped_oscillator",
           "params": {"omega": "1", "zeta": r.choice(["0", "1/10", "1/4"])}, "x0": ["1", "0"],
           "t_end": r.choice(["1", "2"]), "steps": r.choice([20, 50, 100]), "component": 0,
           "tolerance": r.choice(["1/10000", "1/1000", "1/100"])}
    data = canonical_json(cfg)
    parsed = sim.parse_config(data)
    vals = [repr(sim.quantity(parsed, parsed.steps * k)) for k in sim.REFINEMENTS]
    true = Fraction(vals[-1]).limit_denominator(10000)
    value = r.choice([str(true), str(true), str(true + Fraction(1, 10)), "abc"])
    if r.random() < 0.2:
        vals[0] = repr(float(vals[0]) + 1e-3)  # a reported number the re-run does not reproduce
    result = canonical_json({"format": "chh-sim-result/1", "config_sha256": hashlib.sha256(data).hexdigest(),
                             "values": vals})
    aid = f"sim-{hashlib.sha256(data).hexdigest()[:10]}"
    ref = ArtifactRef(artifact_id=aid, hash=x.w.vault.register_artifact(aid, data, "simulation config").hash)
    try:
        statement = sim.scope_phrase(data, Fraction(value)) if value != "abc" else "unreadable"
    except ValueError:
        statement = "unreadable"
    cond = {"simulator": "chh-ode-rk4", "version": "1", "config": ref.hash}
    if r.random() < 0.15:
        cond["simulator"] = "reality"
    c = _exact_claim(x, data, aid, "sim_value", value, "simulated", cond)
    c = c.model_copy(update={"statement": statement})
    _exact_certify(x, c, result, "simulation result")


KINDS: dict[str, Callable[[Ctx], None]] = {
    "honest": h_honest, "negative_equivalence": h_negative_equivalence, "peek": h_peek, "edited": h_edited,
    "selective": h_selective, "swapped": h_swapped, "leaked": h_leaked, "irreproducible": h_irreproducible,
    "strawman": h_strawman, "trivial": h_trivial, "family": h_family, "external": h_external, "lp": h_lp,
    "sim": h_sim,
}


def main(out: Path, n: int, seed: int, head: str, only: list[str] | None = None) -> int:
    kinds = [k for k in KINDS if not only or k in only]
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="kdiff-", dir=out.resolve().parent))  # left in place (small); never deleted
    os.environ["CHH_DATA_DIR"] = str(tmp / "data")  # as tests/conftest.py: never the real data dir
    os.environ["CHH_ALLOW_UNISOLATED"] = "1"  # LocalSandbox runs the world's trusted stdlib programs (test mode)
    with open(out, "w", encoding="utf-8") as f:
        kernel_recorder.install(f)
        kernel_recorder.write_header({"mode": "histories", "n": n, "seed": seed, "head": head, "kinds": kinds,
                                      "deterministic_ids": kernel_recorder.deterministic_ids()})
        for i in range(n):
            r = random.Random(f"{seed}:{i}")
            kind = kinds[i % len(kinds)]  # round-robin: every kind appears, parameters are random
            scope = f"h{i:04d}:{kind}"
            kernel_recorder.set_scope(scope)
            w = World(tmp / f"h{i}", seed=r.randrange(1000), n_train=r.choice([60, 90, 120]),
                      n_test=r.choice([60, 90, 120]))
            ticks = iter(range(1, 10**9))
            w.kernel.clock = lambda t=ticks: float(next(t))  # type: ignore[misc]  # reproducible issued_at
            x = Ctx(w, r)
            outcome: dict[str, Any] = {"t": "history", "scope": scope, "status": "ok"}
            try:
                KINDS[kind](x)
            except Exception as e:
                outcome |= {"status": "error", "type": type(e).__name__, "message": str(e),
                            "where": traceback.extract_tb(e.__traceback__)[-1].name}
            finally:
                w.close()
            f.write(json.dumps(outcome, sort_keys=True) + "\n")
            f.flush()
            print(f"{scope}: {outcome['status']} ({time.time() - t0:.0f}s)", flush=True)
    print(f"worlds left in {tmp}", flush=True)
    return 0
