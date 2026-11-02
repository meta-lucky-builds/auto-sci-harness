"""GOAL §16 Kernel items: VDS e-process (F2), alpha-accounts, as-of verify + STALE (MR7), K3 reliability
demonstration, closed reason codes, plan-code divergence, self-reported tier cap, replayed evidence, releases.

Every attack test asserts that its attack happened (the control without the defence shows the effect).
"""

from __future__ import annotations

import math
import os
import random
import time
from types import SimpleNamespace

import pytest

from custom_harness.kernel import Certificate, DatasetRole, KernelConfig, KernelError, Label
from custom_harness.kernel import checks as K
from custom_harness.kernel.claim import AnalysisPlan
from custom_harness.kernel.ledger import AsOfLedger
from custom_harness.kernel.predictions import score_predictions, skill, vds, ville_threshold
from custom_harness.kernel.reasons import Code, coded, codes_of, exception_name
from custom_harness.kernel.releases import Alphabet, exposed, transcript_bound
from custom_harness.records import StepKind, TraceStep
from custom_harness.verify.stats import Interval
from kernel_world import World

os.environ.setdefault("CHH_ALLOW_UNISOLATED", "1")


# tests that are not about K3 declare the loosest reliability requirement the Kernel allows (2 replays), for runtime
FAST = {"eps_max": 0.5, "beta": 0.25}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    w = World(tmp_path_factory.mktemp("s16"), seed=11, n_train=120, n_test=120)
    yield w
    w.close()


def _honest(w: World, target: str = "test", **kw: object):
    good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
    c = w.claim(good, comp, delta=0.2, n_boot=1000, targets=(target,), **{**FAST, **kw})  # type: ignore[arg-type]
    w.kernel.preregister(c)
    return c, w.evaluate_all(c)


def _codes(cert: Certificate, kid: str) -> set[Code]:
    return set(cert.checks[kid].codes)


# ================================================================== F2: VDS as an e-process
def test_f2_tiny_delta_q005_miss_scores_at_most_zero(tmp_path):
    """F2 regression: a hopeless tiny-delta claim registered at q = 0.05 used to earn about +0.93 bits by failing."""
    w = World(tmp_path / "f2", seed=5, n_train=60, n_test=60)
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.001, probability=0.05, n_boot=1000,
                **FAST)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    assert cert.label == Label.REJECTED and "K7" in cert.failed()  # the attack: a trivially safe claim, failed
    (s,) = score_predictions(w.kernel.ledger, time.time())
    assert s.outcome is False and s.info_gain_bits is not None
    assert s.info_gain_bits <= 0.0, f"a miss scored {s.info_gain_bits:+.3f} bits"
    assert math.isclose(s.p0, 0.05)  # fixed at registration: alpha / n_comparisons, not the post-hoc p_null
    w.close()


def test_q_below_p0_is_refused_at_registration_and_by_k1(world):
    c = world.claim(world.artifact("good"), world.artifact("baseline", "baseline"), probability=0.04, n_boot=1000,
                    **FAST)
    with pytest.raises(KernelError, match="below the claim's level p0"):
        world.kernel.preregister(c)
    res = K.k1_well_formed(K.Context(claim=c, claim_hash=c.claim_hash(), store=world.store, vault=world.vault,
                                     ledger=world.kernel.ledger, config=KernelConfig(), replayer=None,
                                     evidence_ids=()))
    assert Code.K1_PROBABILITY_BELOW_P0 in res.codes


def _fake_ledger(preregs: list[dict], certs: list[dict]) -> SimpleNamespace:
    def entries(kind: str):
        rows = preregs if kind == "prereg" else certs
        return [SimpleNamespace(body=b) for b in rows]
    return SimpleNamespace(entries=entries)


def test_vds_is_an_e_process_under_the_global_null():
    """Adversarial q >= p0, chosen from past outcomes; outcomes certified with probability exactly p0 (the worst case
    the Kernel allows). P(sup VDS >= log2 20) must stay <= 0.05 + 3 SE. The old post-hoc scoring is the control."""
    rng = random.Random(7)
    reps, k, hits, old_hits = 1500, 60, 0, 0
    for _ in range(reps):
        total = old = best = best_old = 0.0
        for _t in range(k):
            p0 = 0.05
            q = p0 if total < 0 else rng.choice([p0, 0.5, 0.9])  # adaptive, never below p0
            ok = rng.random() < p0
            total += math.log2(q / p0) if ok else math.log2((1 - q) / (1 - p0))
            old_q, p_null = 0.05, 0.5  # F2: q = 0.05 tiny-delta claims, p0 measured afterwards (p_null ~ 0.5)
            old += math.log2(old_q / p_null) if ok else math.log2((1 - old_q) / (1 - p_null))
            best, best_old = max(best, total), max(best_old, old)
        hits += best >= ville_threshold(0.05)
        old_hits += best_old >= ville_threshold(0.05)
    se = math.sqrt(0.05 * 0.95 / reps)
    assert hits / reps <= 0.05 + 3 * se, hits / reps
    assert old_hits / reps > 0.9  # the control: post-hoc p0 lets failures accumulate VDS


def test_scoring_reads_registered_terms_and_skill(world):
    now = time.time()
    claim = world.claim(world.artifact("good"), world.artifact("baseline", "baseline")).model_dump(mode="json")
    new = {"claim_hash": "a", "claim": {**claim, "resolve_by": now + 1e6},
           "fixed": {"p0": 0.05, "p_ref": 0.25}}
    legacy_low = {"claim_hash": "b", "claim": {**claim, "resolve_by": now - 1,
                                                "effect": {**claim["effect"], "probability": 0.01}}}
    legacy_other = {"claim_hash": "c", "claim": {**claim, "evidence_class": "formal", "resolve_by": now + 1e6}}
    pending = {"claim_hash": "d", "claim": {**claim, "resolve_by": now + 1e6}}
    led = _fake_ledger([new, legacy_low, legacy_other, pending],
                       [{"claim_hash": "a", "resolution": {"outcome": True}},
                        {"claim_hash": "c", "resolution": {"outcome": False}}])
    s = {x.claim_hash: x for x in score_predictions(led, now)}  # type: ignore[arg-type]
    assert math.isclose(s["a"].info_gain_bits, math.log2(0.8 / 0.05))
    assert math.isclose(s["a"].skill_bits, math.log2(0.8 / 0.25))
    assert s["b"].expired and s["b"].info_gain_bits == 0.0  # legacy q < p0: LR = 1, never positive
    assert s["c"].p0 == 0.5 and s["d"].outcome is None
    assert math.isclose(vds(list(s.values())), sum(x.info_gain_bits or 0 for x in s.values()))
    assert math.isclose(skill(list(s.values())), sum(x.skill_bits or 0 for x in s.values()))


def test_malformed_ledger_entries_fail_closed_per_claim_not_for_the_whole_store(world):
    """studio-to-kstats-02: a certificate entry without `resolution.outcome` (e.g. the self-consistent forgery of
    test_e4b) made score_predictions raise KeyError, so every prediction reader crashed for the whole store. Now the
    affected claim alone is marked (closed reason) and scored as a miss (no credit); other claims score normally."""
    from custom_harness.kernel import predictions as P

    now = time.time()
    claim = world.claim(world.artifact("good"), world.artifact("baseline", "baseline")).model_dump(mode="json")
    pre = [{"claim_hash": h, "claim": {**claim, "resolve_by": now + 1e6}, "fixed": {"p0": 0.05, "p_ref": 0.25}}
           for h in ("ok", "noout", "nores", "strout")]
    broken_prereg = {"claim_hash": "bad", "claim": {"effect": {}}}  # unreadable registration terms
    led = _fake_ledger([*pre, broken_prereg, {"no": "hash"}],
                       [{"claim_hash": "ok", "resolution": {"outcome": True}},
                        {"claim_hash": "noout", "resolution": {}},
                        {"claim_hash": "nores"},
                        {"claim_hash": "strout", "resolution": {"outcome": "yes"}},
                        {"resolution": {"outcome": True}}])
    s = {x.claim_hash: x for x in score_predictions(led, now)}  # type: ignore[arg-type]
    assert set(s) == {"ok", "noout", "nores", "strout", "bad"}
    assert s["ok"].outcome is True and s["ok"].invalid is None
    for h in ("noout", "nores", "strout"):  # fail closed: a miss with a closed reason, never a crash or credit
        assert s[h].outcome is False and s[h].invalid == P.INVALID_RESOLUTION and s[h].info_gain_bits <= 0, s[h]
    assert s["bad"].invalid == P.INVALID_PREREG and s["bad"].info_gain_bits is None and s["bad"].outcome is None


def test_a_malformed_certificate_entry_breaks_no_other_lookup(tmp_path):
    """Same class (ledger readers indexing a missing key): one certificate or retraction entry without `cert_hash`
    made every certificate lookup, status and verify raise KeyError for the whole store."""
    w = World(tmp_path / "mal", seed=23, n_train=60, n_test=60)
    c, ids = _honest(w)
    cert = w.kernel.certify(c, ids)
    w.kernel.ledger.append("certificate", {"claim_hash": "x", "resolution": {}})  # malformed (no cert_hash)
    w.kernel.ledger.append("retraction", {"reason": "no hash"})
    first = w.kernel.ledger.entries("certificate")[0]
    assert first.body.get("cert_hash") == cert.cert_hash()  # the real one precedes; the malformed one is later
    assert w.kernel.certificate(cert.cert_hash()) == cert and w.kernel.status(cert.cert_hash()) == "active"
    assert w.kernel.verify_status(cert.cert_hash()).status in ("valid", "stale")
    with pytest.raises(KernelError):
        w.kernel.certificate("0" * 64)  # a lookup past the malformed entry: refused cleanly, not a KeyError
    (sc,) = [x for x in score_predictions(w.kernel.ledger, time.time()) if x.claim_hash == c.claim_hash()]
    assert sc.invalid is None
    w.close()


def test_prereg_freezes_terms(world):
    c, _ = _honest(world, eps_max=0.3, beta=0.2)
    entry = K.registration(world.kernel.ledger, c.claim_hash())
    f = entry.body["fixed"]
    assert f["p0"] == 0.05 and f["eps_max"] == 0.3 and f["beta"] == 0.2 and f["replays"] == 5
    assert 0 < f["p_ref"] < 1 and f["reference"] == "laplace-base-rate/1"
    assert K.reliability(c.model_copy(update={"analysis_plan": AnalysisPlan(seeds=(0, 1, 2), eps_max=0.1,
                                                                           beta=0.05)}), KernelConfig())[2] == 29
    assert K.reliability(c.model_copy(update={"analysis_plan": AnalysisPlan(seeds=(0, 1, 2), eps_max=1.0)}),
                         KernelConfig())[2] == 0
    # a legacy pre-registration (no `fixed`) gets the terms from its registered claim, as of its registration
    legacy = world.claim(world.artifact("good"), world.artifact("baseline", "baseline"), delta=0.21)
    world.kernel.ledger.append("prereg", {"claim_hash": legacy.claim_hash(), "claim": legacy.model_dump(mode="json")})
    ctx = K.Context(claim=legacy, claim_hash=legacy.claim_hash(), store=world.store, vault=world.vault,
                    ledger=world.kernel.ledger, config=KernelConfig(), replayer=None, evidence_ids=())
    assert K.fixed(ctx)["p0"] == 0.05 and K.fixed(ctx)["replays"] == 11


def test_k1_refuses_loose_reliability(world):
    c = world.claim(world.artifact("good"), world.artifact("baseline", "baseline"), eps_max=0.9, n_boot=1000)
    res = K.k1_well_formed(K.Context(claim=c, claim_hash=c.claim_hash(), store=world.store, vault=world.vault,
                                     ledger=world.kernel.ledger, config=KernelConfig(), replayer=None,
                                     evidence_ids=()))
    assert Code.K1_RELIABILITY in res.codes


# ================================================================== alpha-accounts (R2-2)
def test_alpha_account_fixes_the_level_whatever_the_order(tmp_path):
    w = World(tmp_path / "acct", seed=2, n_train=120, n_test=120)
    k = w.kernel
    w.register_target("fresh", DatasetRole.TEST)
    w.register_target("control", DatasetRole.TEST)
    with pytest.raises(KernelError, match="human"):
        k.open_alpha_account("fresh", 0.05, 2, approver=" ")
    with pytest.raises(KernelError, match="1 slot"):
        k.open_alpha_account("fresh", 0.05, 0, approver="owner")
    with pytest.raises(KernelError, match="only held-out"):
        k.open_alpha_account("train", 0.05, 2, approver="owner")
    k.open_alpha_account("fresh", 0.05, 2, approver="owner")
    with pytest.raises(KernelError, match="already has an account"):
        k.open_alpha_account("fresh", 0.05, 2, approver="owner")

    def level(c, ids) -> str:
        return next(o for o in k.certify(c, ids, record=False).checks["K4"].observations if "Bonferroni" in o)

    good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
    c1 = w.claim(good, comp, delta=0.2, n_boot=1000, **FAST, targets=("fresh",))
    c2 = w.claim(good, comp, delta=0.21, n_boot=1000, **FAST, targets=("fresh",))
    k.preregister(c1)
    ids1 = w.evaluate_all(c1)
    before = level(c1, ids1)
    k.preregister(c2)  # the last slot: the target retires
    after = level(c1, ids1)
    assert before == after and "alpha 0.025" in after  # fixed at alpha_T / S_T, independent of order
    c3 = w.claim(good, comp, delta=0.22, n_boot=1000, **FAST, targets=("fresh",))
    with pytest.raises(KernelError, match="retired"):
        k.preregister(c3)
    k.preregister(w.claim(good, comp, delta=0.2, n_boot=1000, **FAST))  # a claim on "test"
    with pytest.raises(KernelError, match="claims are already registered"):
        k.open_alpha_account("test", 0.05, 2, approver="owner")
    # control (R2-2 as it is without an account): the second registration changes the first claim's level
    d1 = w.claim(good, comp, delta=0.2, n_boot=1000, **FAST, targets=("control",))
    k.preregister(d1)
    idsd = w.evaluate_all(d1)
    b = level(d1, idsd)
    k.preregister(w.claim(good, comp, delta=0.21, n_boot=1000, **FAST, targets=("control",)))
    assert level(d1, idsd) != b
    w.close()


def test_alpha_account_human_retire_and_unslotted_claims(tmp_path):
    w = World(tmp_path / "acct2", seed=4, n_train=120, n_test=120)
    k = w.kernel
    w.register_target("t", DatasetRole.TEST)
    good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
    # a claim registered BEFORE an account entry exists (only possible by writing the ledger directly): K4 says it
    # holds no slot and falls back to Bonferroni over the family
    c = w.claim(good, comp, delta=0.2, n_boot=1000, **FAST, targets=("t",))
    k.preregister(c)
    ids = w.evaluate_all(c)
    k.ledger.append("alpha_open", {"dataset_id": "t", "dataset_hash": "x", "alpha": 0.04, "slots": 4, "approver": "o"})
    cert = k.certify(c, ids, record=False)
    assert Code.K4_NO_SLOT in cert.checks["K4"].codes
    with pytest.raises(KernelError, match="no alpha-account"):
        k.retire_alpha_account("test", approver="owner")
    k.retire_alpha_account("t", approver="owner")
    with pytest.raises(KernelError, match="retired"):
        k.preregister(w.claim(good, comp, delta=0.23, n_boot=1000, **FAST, targets=("t",)))
    w.close()


# ================================================================== as-of verify and STALE (MR7)
def test_mr7_as_of_verify_reports_stale_not_forged(tmp_path):
    """MR7: verify(cert) evaluated on the ledger prefix up to the cert's own entry equals the issued certificate;
    a later event makes it STALE, never 'forged'."""
    w = World(tmp_path / "mr7", seed=3, n_train=120, n_test=120)
    c, ids = _honest(w)
    cert = w.kernel.certify(c, ids)
    assert cert.label == Label.CERTIFIED_INTERNAL
    assert w.kernel.verify_status(cert.cert_hash()).status == "valid"
    w.vault.read("test", "test_y.csv", purpose="develop")  # a later event: the target is used for development
    res = w.kernel.verify_status(cert.cert_hash())
    assert res.forged == () and res.status == "stale"
    assert any(p.startswith("K6: pass recorded, fail") for p in res.stale)  # the event really changes today's verdict
    assert all(p.startswith("stale: ") for p in w.kernel.verify(cert.cert_hash()))  # strict form still non-empty
    # a forged certificate (a REJECTED verdict relabelled) re-derives differently even as of its own prefix
    bad = cert.model_copy(update={"label": Label.CERTIFIED_VALIDATED})
    w.kernel.ledger.append("certificate", {"cert_hash": bad.cert_hash(), "claim_hash": bad.claim_hash,
                                           "label": bad.label.value,
                                           "blob": w.store.blobs.put_json(bad.model_dump(mode="json")),
                                           "resolution": bad.resolution})
    forged = w.kernel.verify_status(bad.cert_hash())
    assert forged.status == "forged" and forged.forged
    # issued under other rules: STALE with that reason, never valid
    other = cert.model_copy(update={"kernel_version": "0.9", "label": Label.REJECTED})
    w.kernel.ledger.append("certificate", {"cert_hash": other.cert_hash(), "claim_hash": other.claim_hash,
                                           "label": other.label.value,
                                           "blob": w.store.blobs.put_json(other.model_dump(mode="json")),
                                           "resolution": other.resolution})
    assert w.kernel.verify_status(other.cert_hash()).status == "stale"
    asof = AsOfLedger(w.store, 1)
    assert [e.seq for e in asof.entries()] == [1]
    w.close()


# ================================================================== K3 reliability demonstration
class _Flaky:
    def __init__(self, inner, p: float, rng: random.Random):
        self.inner, self.p, self.rng = inner, p, rng

    def replay(self, record, vault):
        out = self.inner(record)
        return out if self.rng.random() >= self.p else [*out[:-1], "flipped"]


def test_k3_zero_failure_demonstration(world):
    """A 50%-flaky program passed one replay per arm 25% of the time; with the frozen requirement (eps 0.2, beta 0.1:
    11 consecutive clean replays) it passes <= ~0.05%. An almost-reliable program still passes."""
    c, ids = _honest(world, target="test2", eps_max=0.2, beta=0.1)
    base = K.Context(claim=c, claim_hash=c.claim_hash(), store=world.store, vault=world.vault,
                     ledger=world.kernel.ledger, config=KernelConfig(), replayer=None, evidence_ids=tuple(ids))
    K.k0_provenance(base)
    preds = dict(base.preds)
    by_ref = {r.preds_ref: preds[e] for e, r in base.records}
    rng = random.Random(1)

    def rate(p: float, n: int) -> float:
        passed = 0
        for _ in range(n):
            ctx = K.Context(claim=c, claim_hash=c.claim_hash(), store=world.store, vault=world.vault,
                            ledger=world.kernel.ledger, config=KernelConfig(),
                            replayer=_Flaky(lambda r: list(by_ref[r.preds_ref]), p, rng), evidence_ids=tuple(ids),
                            records=base.records, preds=preds, labels=base.labels)
            K.k2_preregistered(ctx)
            passed += K.k3_reproduced(ctx).status == "pass"
        return passed / n

    assert rate(0.5, 300) <= 0.02
    assert rate(0.001, 60) >= 0.9
    old_style = (1 - 0.5) ** 2  # the control: one replay per arm
    assert old_style == 0.25


# ================================================================== §16.5.8 closed reason codes
def test_reasons_are_coded_and_carry_no_exception_text(world, monkeypatch):
    c, ids = _honest(world)
    cert = world.kernel.certify(c, ids, record=False)
    for kid, r in cert.checks.items():
        assert len(r.codes) == len(r.reasons), kid
    secret = "SECRET-LABELS-0,1,1,0"

    def boom(ctx):
        raise ValueError(secret)

    impl = dict(K.ALL_CHECKS)
    impl["K5"] = boom
    impl["K8"] = lambda ctx: K.CheckResult(status="fail", reasons=("plain text, no code",))
    monkeypatch.setattr("custom_harness.kernel.kernel.checks_for", lambda _cls: impl)
    bad = world.kernel.certify(c, ids, record=False)
    assert bad.checks["K5"].codes == (Code.CHECK_RAISED,) and secret not in " ".join(bad.checks["K5"].reasons)
    assert bad.checks["K8"].codes == (Code.CHECK_UNCODED,)
    assert exception_name(ValueError()) == "ValueError" and exception_name(type("Leak42", (Exception,), {})()) == \
        "Exception"
    with pytest.raises(TypeError):
        codes_of(["uncoded"])
    assert codes_of(coded(Code.K9_LEAK, ["a"])) == (Code.K9_LEAK,)
    # one code per reason is enforced where results are made (certify -> CHECK_UNCODED above), not by the model:
    # stored certificates must stay loadable whatever was filed (verify then reports them)
    assert K.CheckResult(status="fail", reasons=("a", "b"), codes=(Code.K9_LEAK,)).codes == (Code.K9_LEAK,)
    legacy = K.CheckResult(status="pass")
    assert "codes" not in legacy.model_dump()


# ================================================================== §16.6 plan-code divergence
def test_k4_plan_code_divergence(world, monkeypatch):
    c, ids = _honest(world)
    assert world.kernel.certify(c, ids, record=False).checks["K4"].status == "pass"
    from custom_harness.kernel import ml as ML  # the registered test is the ML adapter's procedure since the split
    real = ML.paired_example_interval

    def percentile_instead(*a, **kw):  # a code change that silently runs another test than the registered one
        iv = real(*a, **kw)
        return Interval(iv.estimate, iv.lo, iv.hi, iv.n, iv.alpha, "percentile", iv.degenerate)

    monkeypatch.setattr(ML, "paired_example_interval", percentile_instead)
    cert = world.kernel.certify(c, ids, record=False)
    assert Code.K4_PLAN_CODE_DIVERGENCE in cert.checks["K4"].codes and cert.label == Label.REJECTED
    monkeypatch.setattr(ML, "paired_example_interval", real)
    monkeypatch.setattr(ML, "REGISTERED_TESTS", {})
    assert Code.K4_PLAN_CODE_DIVERGENCE in world.kernel.certify(c, ids, record=False).checks["K4"].codes
    ctx = K.Context(claim=c, claim_hash=c.claim_hash(), store=world.store, vault=world.vault,
                    ledger=world.kernel.ledger, config=KernelConfig(), replayer=None, evidence_ids=())
    iv = Interval(0, 0, 0, 1, 0.1, "t")
    assert K.plan_code_divergence(ctx, "test", 0.05, iv, iv, 1)[0].code == Code.K4_PLAN_CODE_DIVERGENCE
    ctx.prereg = SimpleNamespace(body={})  # type: ignore[assignment]
    assert K.plan_code_divergence(ctx, "test", 0.05, iv, iv, 1)


# ================================================================== §16.7 self-reported fields cap the tier
def test_unapproved_external_role_caps_the_tier(tmp_path):
    w = World(tmp_path / "ext", seed=6, n_train=120, n_test=120)
    w.register_target("ext", DatasetRole.EXTERNAL)
    c, ids = _honest(w, target="ext")
    cert = w.kernel.certify(c, ids, record=False)
    assert cert.label == Label.CERTIFIED_INTERNAL and cert.tier == 4 and cert.self_reported
    w.kernel.approve_role("ext", approver="owner")
    approved = w.kernel.certify(c, ids, record=False)
    assert approved.label == Label.CERTIFIED_VALIDATED and approved.tier == 2 and approved.self_reported == ()
    w.close()


# ================================================================== invariant 21: replayed output is never evidence
def test_replayed_evidence_is_refused(tmp_path):
    w = World(tmp_path / "rep", seed=8, n_train=120, n_test=120)
    c, ids = _honest(w)
    assert w.kernel.certify(c, ids, record=False).label == Label.CERTIFIED_INTERNAL  # the honest control
    run_id = w.store.get_evidence(ids[0]).produced_by_run
    now = time.time()
    w.store.add_step(TraceStep(run_id=run_id, index=1, node_id="codex", kind=StepKind.LLM, input={},
                               output={"_module_llm_calls": [{"provenance": {"replayed": True}}]},
                               started_at=now, ended_at=now))
    cert = w.kernel.certify(c, ids, record=False)
    assert Code.K0_REPLAYED_EVIDENCE in cert.checks["K0"].codes and cert.label == Label.REJECTED
    rec = K.ExecRecord.model_validate(w.store.blobs.get_json(w.store.get_evidence(ids[1]).payload_ref))
    assert "replayed" not in rec.model_dump()
    marked = rec.model_copy(update={"replayed": True})
    assert marked.model_dump()["replayed"] is True
    ctx = K.Context(claim=c, claim_hash=c.claim_hash(), store=w.store, vault=w.vault, ledger=w.kernel.ledger,
                    config=KernelConfig(), replayer=None,
                    evidence_ids=(w.kernel.record_evaluation(marked),))
    assert Code.K0_REPLAYED_EVIDENCE in K.k0_provenance(ctx).codes
    assert K.replayed_marks(w.store, run_id) == 1
    w.store.add_step(TraceStep(run_id=run_id, index=2, node_id="x", kind=StepKind.TOOL, input={"replayed": True},
                               output={"_module_llm_calls": "not a list"}, started_at=now, ended_at=now))
    assert K.replayed_marks(w.store, run_id) == 2
    w.close()


# ================================================================== §16.5.3 release ledger
def test_transcript_bound_and_exposure():
    assert transcript_bound([5, 9], [], 16) == 2  # embargo: N = k
    assert transcript_bound([5, 9], [(7, Alphabet.VERDICT_BIT)], 16) == 1 + 2
    assert transcript_bound([5, 9], [(3, Alphabet.COUNT), (7, Alphabet.VERDICT_BIT)], 16) == 16 + 32
    assert transcript_bound([5, 9], [(7, Alphabet.CONTINUOUS)], 16) == 1  # the exposed claim adds nothing
    assert exposed(9, [(7, Alphabet.CONTINUOUS)]) and not exposed(5, [(7, Alphabet.CONTINUOUS)])


def test_record_release_and_k4_k6_use_it(tmp_path):
    w = World(tmp_path / "rel", seed=9, n_train=120, n_test=120)
    k = w.kernel
    for bad in (("nope", "prompt", "verdict_bit", "x"), ("test", "tv", "verdict_bit", "x"),
                ("test", "prompt", "real", "x"), ("test", "prompt", "verdict_bit", "Accuracy=0.93!")):
        with pytest.raises(KernelError):
            k.record_release(*bad)
    c, ids = _honest(w)
    before = k.certify(c, ids, record=False)
    k.record_release("test", "evolution_score", "verdict_bit", "evolve.score:verdict")
    good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
    c2 = w.claim(good, comp, delta=0.21, n_boot=1000, **FAST)
    k.preregister(c2)  # registered after one verdict bit: 1 + 2 = 3 transcripts
    obs = next(o for o in k.certify(c, ids, record=False).checks["K4"].observations if "Bonferroni" in o)
    assert obs.startswith("test: 3 claim(s)")
    assert [e.body["note"] for e in k.releases("test")] == ["evolve.score:verdict"]
    k.record_release("test", "export", "continuous", "export.scores")
    c3 = w.claim(good, comp, delta=0.22, n_boot=1000, **FAST)
    k.preregister(c3)
    ids3 = w.evaluate_all(c3)
    cert3 = k.certify(c3, ids3, record=False)
    assert Code.K6_ADAPTIVELY_EXPOSED in cert3.checks["K6"].codes and cert3.label == Label.REJECTED
    assert before.label == Label.CERTIFIED_INTERNAL
    w.close()


def test_release_with_alpha_account_is_paid_in_slots_not_in_level(tmp_path):
    """R2-2 (2026-10-09, docs/alpha_accounts.md): this test used to assert that a release divided the slot level by
    N/k ("alpha 0.01333" = 0.02 * 2/3), which made the level of an earlier claim depend on later claims and releases.
    The release is now paid in slots at the later claim's registration; the level stays alpha_T / S_T."""
    w = World(tmp_path / "rel2", seed=10, n_train=120, n_test=120)
    k = w.kernel
    w.register_target("acct", DatasetRole.TEST)
    k.open_alpha_account("acct", 0.06, 3, approver="owner")
    good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
    c1 = w.claim(good, comp, delta=0.2, n_boot=1000, **FAST, targets=("acct",))
    k.preregister(c1)
    ids = w.evaluate_all(c1)
    k.record_release("acct", "report", "verdict_bit", "report.verdict")
    k.preregister(w.claim(good, comp, delta=0.21, n_boot=1000, **FAST, targets=("acct",)))
    obs = next(o for o in k.certify(c1, ids, record=False).checks["K4"].observations if "Bonferroni" in o)
    assert "alpha 0.02" in obs  # alpha_T / S_T, whatever came later
    assert [e.body["charge"] for e in k.ledger.entries("alpha_reserve")] == [1, 2]  # the release cost a slot
    assert k.ledger.entries("alpha_retire")  # 1 + 2 = 3 slots: the target retired
    w.close()


# ================================================================== §16.2 minted targets (mint's request)
def test_mint_record_and_delta_floor(tmp_path):
    w = World(tmp_path / "mint", seed=12, n_train=60, n_test=60)
    k = w.kernel
    k.record_mint("test", "f" * 64, "beacon-1", "task-a")
    with pytest.raises(KernelError, match="beacon shopping"):
        k.record_mint("test", "f" * 64, "beacon-2", "task-a")
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.01, n_boot=1000)
    res = K.k1_well_formed(K.Context(claim=c, claim_hash=c.claim_hash(), store=w.store, vault=w.vault,
                                     ledger=k.ledger, config=KernelConfig(), replayer=None, evidence_ids=()))
    assert Code.K1_DELTA_FLOOR in res.codes
    ok = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.02, n_boot=1000)
    res = K.k1_well_formed(K.Context(claim=ok, claim_hash=ok.claim_hash(), store=w.store, vault=w.vault,
                                     ledger=k.ledger, config=KernelConfig(), replayer=None, evidence_ids=()))
    assert Code.K1_DELTA_FLOOR not in res.codes
    w.close()


# ================================================================== assure's differential findings KD1, KD2
def test_kd1_development_copy_of_held_out_examples_fails_k6(tmp_path):
    w = World(tmp_path / "kd1", seed=13, n_train=120, n_test=120)
    test = w.vault.dataset("test")
    x = w.store.blobs.get(test.files["test_X.csv"].blob)
    y = w.store.blobs.get(test.files["test_y.csv"].blob)
    w.vault.register_dataset("devcopy", DatasetRole.DEV, public={"features.csv": x}, private={"labels.csv": y})
    good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
    c = w.claim(good, comp, delta=0.2, n_boot=1000, development=("train", "devcopy"), **FAST)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c), record=False)
    assert Code.K6_DEVELOP_READ in cert.checks["K6"].codes and cert.label == Label.REJECTED
    assert "devcopy" in " ".join(cert.checks["K6"].reasons)
    w.close()


def _kd3_world(tmp_path, with_label_column: bool):
    """KD3 (assure, 2026-10-07): a held-out target whose own PUBLIC file (the program's input) carries its true
    labels as a column. The control registers the same rows without that column."""
    from kernel_world import _csv, _sample

    w = World(tmp_path, seed=17, n_train=120, n_test=120)
    rng = random.Random(99)
    rows = [_sample(rng) for _ in range(120)]
    header = ["x1", "x2", "n1", *(["label"] if with_label_column else [])]
    pub = _csv([header, *[r if with_label_column else r[:-1] for r in rows]])
    w.vault.register_dataset("lab", DatasetRole.TEST, public={"test_X.csv": pub},
                             private={"test_y.csv": _csv([["label"], *[[r[-1]] for r in rows]])})
    w.labels["lab"] = [r[-1] for r in rows]
    good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
    c = w.claim(good, comp, delta=0.2, n_boot=1000, targets=("lab",), **FAST)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c), record=False)
    w.close()
    return cert


def test_kd3_target_public_input_carrying_its_labels_fails_k9(tmp_path):
    cert = _kd3_world(tmp_path / "kd3", with_label_column=True)
    assert cert.checks["K9"].status == "fail" and Code.K9_LEAK in cert.checks["K9"].codes, cert.checks["K9"]
    assert "column 'label'" in " ".join(cert.checks["K9"].reasons) and cert.label == Label.REJECTED


def test_kd3_control_the_same_rows_without_the_label_column_pass_k9(tmp_path):
    cert = _kd3_world(tmp_path / "kd3c", with_label_column=False)
    assert cert.checks["K9"].status == "pass", cert.checks["K9"]


def test_label_columns_matches_only_an_exact_informative_column():
    lab = ["0", "1", "2", "1"]
    data = b"a,b,c\n0,5,0\n1,5,1\n2,5,2\n1,5,1\n"
    assert K.label_columns(data, lab) == ["a", "c"]
    assert K.label_columns(data, ["0", "1", "2"]) == []  # another number of rows: never aligned
    assert K.label_columns(b"a\n1\n1\n", ["1", "1"]) == []  # single-class labels carry no information
    assert K.label_columns(b"a,b\n5,0\n5\n", ["0", "1"]) == []  # a short row never matches
    assert K.label_columns(data, []) == []


def _kd4_world(tmp_path, read_related: bool):
    """KD4 (assure, 2026-10-08): an early label read of a dataset that holds the target's held-out rows under
    another id is peeking at the target."""
    w = World(tmp_path, seed=19, n_train=120, n_test=120)
    x = w.store.blobs.get(w.vault.dataset("test").files["test_X.csv"].blob)
    w.vault.register_dataset("rows", DatasetRole.DEV, public={"features.csv": x},
                             private={"labels.csv": b"label\n" + b"0\n" * 120})
    if read_related:
        w.vault.read("rows", "labels.csv", purpose="audit")
    c, ids = _honest(w)
    cert = w.kernel.certify(c, ids, record=False)
    w.close()
    return cert


def test_kd4_early_read_of_a_related_dataset_fails_k2(tmp_path):
    cert = _kd4_world(tmp_path / "kd4", read_related=True)
    assert cert.checks["K2"].status == "fail" and Code.K2_PEEK in cert.checks["K2"].codes, cert.checks["K2"]
    assert "'rows'" in " ".join(cert.checks["K2"].reasons)


def test_kd4_control_without_the_read_passes_k2(tmp_path):
    assert _kd4_world(tmp_path / "kd4c", read_related=False).checks["K2"].status == "pass"


def test_two_replayers_never_share_a_replay_directory(world):
    """Same class as the confirm-retry gap (orcompute-to-yield-02): every certify/reproduce builds a NEW
    SandboxReplayer whose call counter restarts at 1, so a factory mapping key -> root / key handed the second
    replayer the first one's directory and files. The key now carries a per-replayer nonce."""
    from custom_harness.kernel import SandboxReplayer

    c, ids = _honest(world)
    ctx = K.Context(claim=c, claim_hash=c.claim_hash(), store=world.store, vault=world.vault,
                    ledger=world.kernel.ledger, config=KernelConfig(), replayer=None, evidence_ids=tuple(ids))
    K.k0_provenance(ctx)
    rec = ctx.records[0][1]
    keys: list[str] = []

    class Stop(Exception):
        pass

    def factory(key: str):
        keys.append(key)
        raise Stop

    for _ in range(2):
        with pytest.raises(Stop):
            SandboxReplayer(factory).replay(rec, world.vault)
    assert len(keys) == 2 and keys[0] != keys[1], keys


def test_kd2_k7_needs_statistics_on_every_target(world):
    good, comp = world.artifact("good"), world.artifact("baseline", "baseline")
    c = world.claim(good, comp, delta=0.2, n_boot=1000, targets=("test", "test2"), **FAST)
    world.kernel.preregister(c)
    ids = world.evaluate_all(c, targets=("test",))
    ctx = K.Context(claim=c, claim_hash=c.claim_hash(), store=world.store, vault=world.vault,
                    ledger=world.kernel.ledger, config=KernelConfig(), replayer=None, evidence_ids=tuple(ids))
    K.k0_provenance(ctx)
    K.k2_preregistered(ctx)
    K.k4_statistics(ctx)
    assert set(ctx.stats) == {"test"}  # the attack precondition: one target has no statistics
    res = K.k7_risk(ctx)
    assert res.status == "fail" and Code.K7_NO_STATS in res.codes and "test2" in res.reasons[0]


def test_verify_difference_direction(world):
    """Forged = the record claims more than the re-derivation; a record claiming less is stale, not forged."""
    from custom_harness.kernel.kernel import _differences

    c, ids = _honest(world)
    cert = world.kernel.certify(c, ids, record=False)
    worse = cert.model_copy(update={"label": Label.REJECTED, "tier": 6, "checks": {
        **cert.checks, "K3": cert.checks["K3"].model_copy(update={"status": "fail"})}})
    better, less = _differences(worse, cert)
    assert better == [] and len(less) == 3
    better, less = _differences(cert, worse)
    assert len(better) == 3 and less == []
    # a formal claim below the non-statistical p0 is not refused (no null level), only scored with LR = 1
    formal = c.model_copy(update={"evidence_class": "formal"})
    ctx = K.Context(claim=formal, claim_hash=formal.claim_hash(), store=world.store, vault=world.vault,
                    ledger=world.kernel.ledger, config=KernelConfig(), replayer=None, evidence_ids=())
    assert K.p0_reasons(ctx) == []


def test_ledger_accepts_the_kinds_other_workstreams_append():
    from custom_harness.kernel.ledger import KINDS

    assert {"release", "alpha_open", "alpha_reserve", "alpha_retire", "mint", "feature_ruling", "role_exam",
            "tree_event", "salt_commitment", "salt_reveal", "run_kill", "tier_policy", "verifier_review",
            "fork"} <= KINDS
