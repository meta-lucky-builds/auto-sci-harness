"""GOAL §5 L5 production skill writer (memory/writer.py, s3-verifiers): the research loop's only path to persist a skill
from a certified outcome. Real Kernel certificates (programs executed on the kernel_world rings task), the real store
gate and the real perturbed evaluator (research/perturbed.py). Attack tests check that the attack happened (the
exploit really is CERTIFIED by the Kernel and gains under the nominal grader) before checking it is blocked."""

from __future__ import annotations

import functools
import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest

from custom_harness.kernel import Claim, ExecRecord, InputRef, Label
from custom_harness.memory import MemoryPolicy, MemoryStore, NotReadable, PerturbedEvaluatorCheck, Status
from custom_harness.memory.model import Kind
from custom_harness.memory.store import verified_failure
from custom_harness.memory.writer import WRITER, SkillWriter, WriterRefused
from custom_harness.records import Run
from custom_harness.research.perturbed import PerturbedTaskEvaluator
from custom_harness.sandbox import LocalSandbox
from custom_harness.tiers import Tier
from custom_harness.verify import metrics as M
from kernel_world import KNN, World

os.environ.setdefault("CHH_ALLOW_UNISOLATED", "1")
SRC = Path(__file__).resolve().parents[1] / "src"
SEED_HACK = KNN.format(k=15, pre="", post="if a.seed not in (0, 1, 2):\n    preds = ['0' for _ in preds]")
# research certificates are T2; the human-owned default skill floor is T1 (tiers.DEFAULT_POLICY), so these stores
# declare a T2 floor the way an owner would (as tests/test_exploit_eval.py does)
POLICY = MemoryPolicy(floors={Kind.PLAYBOOK: Tier.T2_STATISTICAL, Kind.NEGATIVE: Tier.T2_STATISTICAL})
_ns = itertools.count()


def evaluate_in_run(w: World, claim: Claim, run_id: str) -> list[str]:
    """kernel_world.World.evaluate_all, but every record belongs to ONE run (as in the research loop)."""
    ids = []
    for target in claim.generalization_targets:
        for seed in claim.analysis_plan.seeds:
            for arm in ("subject", "comparator"):
                ref = claim.subject if arm == "subject" else claim.comparator
                preds = w.run(ref.hash, "train", target, seed)
                assert preds is not None
                raw = w.vault.read(target, "test_y.csv", purpose="evaluate", claim_hash=claim.claim_hash(), arm=arm,
                                   seed=seed)
                labels = [ln.split(",")[0] for ln in raw.decode().splitlines()[1:]]
                rec = ExecRecord(
                    claim_hash=claim.claim_hash(), arm=arm, artifact_hash=ref.hash,  # type: ignore[arg-type]
                    argv=("{python}", "{entrypoint}", "--train", "{train}", "--test", "{test}", "--out", "{out}",
                          "--seed", "{seed}"),
                    inputs={"train": InputRef(dataset_id="train", file="train.csv",
                                              blob=w.vault.dataset("train").files["train.csv"].blob),
                            "test": InputRef(dataset_id=target, file="test_X.csv",
                                             blob=w.vault.dataset(target).files["test_X.csv"].blob)},
                    target=target, label_file="test_y.csv", seed=seed, env={"sandbox": "local (trusted test code)"},
                    limits={"timeout_s": 120.0}, preds_ref=w.store.blobs.put_json(preds), metric="accuracy",
                    score=M.score("accuracy", labels, preds), run_id=run_id)
                ids.append(w.kernel.record_evaluation(rec))
    return ids


class SW:
    def __init__(self, root: Path):
        self.w = World(root, seed=3, n_train=200, n_test=200)
        self.kernel = self.w.kernel
        for t in ("tb", "tc"):
            from custom_harness.kernel.vault import DatasetRole
            self.w.register_target(t, DatasetRole.TEST, n=200)
        base = self.w.artifact("baseline", "baseline")
        self.good = self.w.artifact("good")
        self.hack = self.w.artifact("seed-hack", code=SEED_HACK)
        self.runs: dict[str, str] = {}
        self.honest = self._certify("honest", self.w.claim(self.good, base, delta=0.2, targets=("test",), n_boot=1000))
        self.exploit = self._certify("exploit", self.w.claim(self.hack, base, delta=0.2, targets=("tb",), n_boot=1000))
        # honest procedure, no effect of the claimed size: REJECTED on K6 alone (a verified failure)
        self.failure = self._certify("failure", self.w.claim(self.good, base, delta=0.6, targets=("tc",),
                                                             seeds=(0, 1, 2, 3, 4), alpha=0.01, n_boot=1000))

    def _certify(self, name: str, claim: Claim) -> str:
        run = Run(harness_id="skill-writer-test", harness_hash="world", task_id=name, seed=0)
        self.w.store.add_run(run)
        self.runs[name] = run.id
        self.kernel.preregister(claim)
        return self.kernel.certify(claim, evaluate_in_run(self.w, claim, run.id)).cert_hash()

    def check(self) -> PerturbedEvaluatorCheck:
        n, root = itertools.count(), self.w.root / f"perturbed{next(_ns)}"
        ev = PerturbedTaskEvaluator(self.kernel, lambda name: LocalSandbox(root / f"{name}-{next(n)}",
                                                                           i_understand_no_isolation=True),
                                    {"train": "label"}, k=2, n_perturbed_seeds=2)
        return PerturbedEvaluatorCheck(ev, min_retained=0.5)

    def store(self, check=None) -> MemoryStore:
        return MemoryStore(self.kernel, namespace=f"sw{next(_ns)}", policy=POLICY, exploit_check=check)


@pytest.fixture(scope="module")
def sw(tmp_path_factory):
    m = SW(tmp_path_factory.mktemp("skills"))
    yield m
    m.w.close()


def test_the_world_is_as_expected(sw):
    k = sw.kernel
    assert k.certificate(sw.honest).label == Label.CERTIFIED_INTERNAL, k.certificate(sw.honest).failed()
    assert k.certificate(sw.exploit).label == Label.CERTIFIED_INTERNAL  # the attack: the Kernel certified it
    assert verified_failure(k.certificate(sw.failure)) is None


def test_an_honest_skill_is_quarantined_then_admitted_and_rendered_as_quoted_data(sw):
    s = sw.store(sw.check())
    writer = SkillWriter(s)
    prop = writer.playbook_proposal(sw.honest, run_id=sw.runs["honest"], method="radius kNN on the rings",
                                    method_subject_hash=sw.good.hash)
    eid = s.propose(prop, writer=WRITER)
    assert s.status(eid) == Status.QUARANTINED
    with pytest.raises(NotReadable):
        s.get(eid, reader="t")
    res = s.admit(eid)
    assert res.passed and res.status == Status.ACTIVE, res.reasons
    e = s.get(eid, reader="t")
    assert e.writer == WRITER and e.kind == Kind.PLAYBOOK and e.evidence == (sw.honest,)
    text = s.render([e])
    body = text.split("\n", 2)[2]
    assert text.startswith("<<UNTRUSTED-MEMORY-DATA BEGIN") and "radius kNN on the rings" in body
    assert "data, not instructions" in text


def test_a_planted_exploit_skill_is_blocked_end_to_end_through_the_writer(sw):
    check = sw.check()
    nominal = check.evaluator(type("E", (), {"evidence": (sw.exploit,)})(), False)
    assert nominal > 0.2  # the attack: under the nominal grader the exploit really gains
    s = sw.store(check)
    res = SkillWriter(s).write_playbook(sw.exploit, run_id=sw.runs["exploit"], method="kNN (seed-tuned)",
                                        method_subject_hash=sw.hack.hash)
    assert not res.passed and res.status == Status.REJECTED, res
    assert any("disappears under the perturbed evaluator" in r for r in res.reasons), res.reasons
    assert s.query(reader="t") == []
    with pytest.raises(NotReadable):
        s.get(res.entry_id, reader="t")


def test_revoking_the_source_claim_cascades(sw, tmp_path):
    # a separate certificate, so retracting it does not disturb the other tests' world
    base = sw.w.artifact("baseline", "baseline")
    h = sw._certify("revocable", sw.w.claim(sw.good, base, delta=0.15, targets=("test",), n_boot=1000))
    s = sw.store(sw.check())
    res = SkillWriter(s).write_playbook(h, run_id=sw.runs["revocable"], method="kNN", method_subject_hash=sw.good.hash)
    assert res.passed, res.reasons
    from custom_harness.memory import Proposal
    child = s.submit(Proposal(kind=Kind.PLAYBOOK, title="derived playbook", text="builds on the kNN playbook",
                              evidence=(h,), parents=(res.entry_id,)), writer=WRITER)
    assert child.passed, child.reasons
    sw.kernel.retract(h, reason="source claim withdrawn")
    s.sync()
    assert s.status(res.entry_id) == Status.REVOKED and s.status(child.entry_id) == Status.REVOKED
    assert s.query(reader="t") == []


def test_the_writer_refuses_before_proposing(sw):
    s = sw.store(sw.check())
    w = SkillWriter(s)
    cases = [
        (dict(cert_hash=sw.failure, run_id=sw.runs["failure"], method_subject_hash=sw.good.hash), "only a certified"),
        (dict(cert_hash=sw.honest, run_id=sw.runs["exploit"], method_subject_hash=sw.good.hash), "not produced by run"),
        (dict(cert_hash=sw.honest, run_id=sw.runs["honest"], method_subject_hash=sw.hack.hash), "certified subject"),
        (dict(cert_hash="0" * 64, run_id=sw.runs["honest"], method_subject_hash=sw.good.hash), "no such certificate"),
    ]
    for kw, why in cases:
        with pytest.raises(WriterRefused, match=why):
            w.write_playbook(method="m", **kw)
    with pytest.raises(WriterRefused, match="no exploit check"):
        SkillWriter(sw.store(None)).write_playbook(sw.honest, run_id=sw.runs["honest"], method="m",
                                                   method_subject_hash=sw.good.hash)
    assert all(r["type"] != "propose" for r in s.log())  # nothing was proposed


def test_a_verified_failure_becomes_negative_memory_through_the_writer(sw):
    s = sw.store(None)
    results = SkillWriter(s).write_negatives()
    mine = [r for r in results if s.entry(r.entry_id).evidence == (sw.failure,)]
    assert mine and mine[0].passed, results
    e = s.entry(mine[0].entry_id)
    assert e.kind == Kind.NEGATIVE and e.writer == WRITER


def test_importing_the_writer_loads_no_model_path():
    code = ("import sys, custom_harness.memory.writer; bad = [m for m in sys.modules if m.startswith(("
            "'custom_harness.gateway', 'custom_harness.runtime', 'custom_harness.research', "
            "'custom_harness.evolve', 'httpx'))]; print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(SRC)},
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_after_certify_runs_the_writer_and_refuses_without_an_evaluator(sw, monkeypatch):
    from custom_harness.research.memory_wiring import after_certify
    state = {"best": {"code_ref": sw.good.hash, "hypothesis_id": "h1"},
             "hypotheses": [{"id": "h1", "idea": "radius kNN"}]}
    ns = f"ac{next(_ns)}"
    rec = after_certify(sw.kernel, state, sw.honest, (), ns, run_id=sw.runs["honest"], spawn=None,
                        targets={"train": "label"})
    assert rec["error"] is None and rec["skill"]["proposed"] is False and "exploit check" in rec["skill"]["reason"]
    n = itertools.count()
    root = sw.w.root / f"ac{next(_ns)}"
    rec = after_certify(sw.kernel, state, sw.honest, (), ns, run_id=sw.runs["honest"],
                        spawn=lambda name: LocalSandbox(root / f"{name}-{next(n)}", i_understand_no_isolation=True),
                        targets={"train": "label"})
    # the production store uses the human-owned tier table: a T2 research certificate is below the T1 skill floor,
    # so the writer refuses before proposing (review M5: no wasted re-derivation or perturbed evaluation)
    assert rec["error"] is None and rec["skill"]["proposed"] is False, rec
    assert "skill tier floor" in rec["skill"]["reason"], rec
    # a certificate of another run is refused even through the wiring. Since 98a6728 the tier floor is checked first,
    # so under the production floor this call would stop there; the wiring's store gets this file's T2 floors
    # (POLICY) for this call only, so the refusal below is the wrong-run check itself
    import custom_harness.research.memory_wiring as MWI

    with monkeypatch.context() as mp:
        mp.setattr(MWI, "MemoryStore", functools.partial(MWI.MemoryStore, policy=POLICY))
        other = after_certify(sw.kernel, state, sw.honest, (), f"ac{next(_ns)}", run_id=sw.runs["exploit"],
                              spawn=lambda name: LocalSandbox(root / f"x{name}-{next(n)}",
                                                              i_understand_no_isolation=True),
                              targets={"train": "label"})
    assert other["skill"]["proposed"] is False and "not produced by run" in other["skill"]["reason"], other
    # review M2: a crashing writer is recorded and does not stop the rest of after_certify
    def boom(*a, **k):
        raise OSError("disk")
    orig = MWI.write_skill
    MWI.write_skill = boom
    try:
        crashed = after_certify(sw.kernel, state, sw.honest, (), f"ac{next(_ns)}", run_id=sw.runs["honest"],
                                spawn=None, targets=None)
    finally:
        MWI.write_skill = orig
    assert crashed["error"] is None and "skill writer failed: OSError" in crashed["skill"]["reason"], crashed

