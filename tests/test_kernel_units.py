"""Branch-level tests of the Evidence Kernel (GOAL §5: 100% branch coverage of Kernel code).

Every failure path of every check is driven by a specific planted defect, and the reason text is
asserted, so a check cannot pass this file by failing for the wrong reason.
"""

from __future__ import annotations

import json
import math
import os
import time
import zipfile
from pathlib import Path

import pytest

from custom_harness.kernel import (
    CHECK_IDS,
    AnalysisPlan,
    ArtifactRef,
    Certificate,
    Claim,
    ClaimKind,
    DatasetRole,
    Effect,
    ExecRecord,
    KernelConfig,
    KernelError,
    Label,
    Ledger,
    MetricRef,
    Risk,
    Scope,
    Vault,
    VaultError,
    canonical_json,
    run_program,
)
from custom_harness.kernel import checks as K
from custom_harness.kernel.bundle import build_bundle, reproduce_bundle
from custom_harness.kernel.checklist import ITEMS, fill, missing, publishable, render
from custom_harness.kernel.execution import ExecutionError, SandboxReplayer, parse_predictions
from custom_harness.kernel.ledger import Entry, verify_chain
from custom_harness.kernel.predictions import calibration, score_predictions, vds
from custom_harness.records import Cost, Evidence, EvidenceKind
from custom_harness.sandbox import LocalSandbox
from custom_harness.tiers import Tier
from custom_harness.verify.stats import Interval
from kernel_world import PROGRAMS, World

os.environ.setdefault("CHH_ALLOW_UNISOLATED", "1")


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    w = World(tmp_path_factory.mktemp("units"), seed=3, n_train=120, n_test=120)
    good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
    c = w.claim(good, comp, delta=0.2, n_boot=1000)
    w.kernel.preregister(c)
    ids = w.evaluate_all(c)
    yield w, c, ids
    w.close()


def ctx(w: World, claim: Claim, ids, *, replayer="default", config: KernelConfig | None = None) -> K.Context:
    c = K.Context(claim=claim, claim_hash=claim.claim_hash(), store=w.store, vault=w.vault, ledger=w.kernel.ledger,
                  config=config or KernelConfig(), replayer=w.replayer if replayer == "default" else replayer,
                  evidence_ids=tuple(ids))
    K.k0_provenance(c)
    return c


def record(w: World, eid: str) -> ExecRecord:
    return ExecRecord.model_validate(w.store.blobs.get_json(w.store.get_evidence(eid).payload_ref))


def add(w: World, rec: ExecRecord, kind: EvidenceKind = EvidenceKind.METRIC) -> str:
    ev = Evidence(kind=kind, tier=Tier.T1_EXECUTABLE, verifier_id="x", produced_by_run=rec.run_id,
                  payload_ref=w.store.blobs.put_json(rec.model_dump(mode="json")))
    w.store.add_evidence(ev)
    return ev.id


def variant(claim: Claim, **changes) -> Claim:
    doc = claim.model_dump(mode="json")
    for path, value in changes.items():
        node = doc
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return Claim.model_validate(doc)


def reasons(res) -> str:
    return " | ".join(res.reasons)


# ------------------------------------------------------------------ the honest baseline passes everything
def test_base_claim_certifies(base):
    w, c, ids = base
    cert = w.kernel.certify(c, ids)
    assert cert.label == Label.CERTIFIED_INTERNAL and cert.failed() == []
    assert cert.tier == int(Tier.T2_STATISTICAL) and cert.resolution["outcome"] is True
    assert w.kernel.certificate(cert.cert_hash()) == cert
    assert w.kernel.preregister(c).body["claim_hash"] == c.claim_hash()  # idempotent
    assert w.kernel.registered_claim(c.claim_hash()) == c


# ------------------------------------------------------------------ K0
def test_k0_failure_paths(base):
    w, c, ids = base
    r = record(w, ids[0])
    other_claim = add(w, r.model_copy(update={"claim_hash": "0" * 64}))
    not_metric = add(w, r, kind=EvidenceKind.STAT)
    bad_preds = add(w, r.model_copy(update={"preds_ref": w.store.blobs.put_json({"not": "a list"})}))
    no_code = add(w, r.model_copy(update={"artifact_hash": "f" * 64}))
    bad_input = add(w, r.model_copy(update={"inputs": {"train": r.inputs["train"].model_copy(
        update={"dataset_id": "nope"})}}))
    bad_target = add(w, r.model_copy(update={"target": "nope"}))
    no_run = add(w, r.model_copy(update={"run_id": "run_missing"}))
    unparsable = Evidence(kind=EvidenceKind.METRIC, tier=Tier.T1_EXECUTABLE, verifier_id="x", produced_by_run=None,
                          payload_ref=w.store.blobs.put_json({"schema_version": 1}))
    w.store.add_evidence(unparsable)
    cx = K.Context(claim=c, claim_hash=c.claim_hash(), store=w.store, vault=w.vault, ledger=w.kernel.ledger,
                   config=KernelConfig(), replayer=None,
                   evidence_ids=(other_claim, not_metric, bad_preds, no_code, bad_input, bad_target, no_run,
                                 unparsable.id, "ev_does_not_exist"))
    text = reasons(K.k0_provenance(cx))
    for want in ("different claim", "not a metric record", "not a list of strings", "code blob missing",
                 "unresolvable", "target 'nope' unregistered", "run_missing", "ValidationError", "KeyError"):
        assert want in text, want
    assert cx.records == []
    empty = K.Context(claim=variant(c, subject__artifact_id="ghost"), claim_hash="h", store=w.store, vault=w.vault,
                      ledger=w.kernel.ledger, config=KernelConfig(), replayer=None, evidence_ids=())
    text = reasons(K.k0_provenance(empty))
    assert "no evidence records" in text and "'ghost' is not registered" in text


# ------------------------------------------------------------------ K1
def test_k1_every_malformation_is_named(base):
    w, c, _ids = base
    bad = variant(c, metric__id="bleu", subject=c.comparator.model_dump(), generalization_targets=["test", "test"],
                  development=["train", "unknown_ds"], analysis_plan__seeds=[1, 1, 2], analysis_plan__alpha=0.5,
                  analysis_plan__n_boot=10, analysis_plan__reproduce_tolerance=-1.0, effect__predicted_lo=1.0,
                  effect__predicted_hi=0.0, effect__probability=1.0, risk__prior_probability=1.5,
                  effect__min_effect=-0.5)
    text = reasons(K.k1_well_formed(ctx(w, bad, [])))
    for want in ("finite number > 0", "'bleu' is not registered", "same artifact", "non-empty and unique",
                 "unregistered datasets: ['unknown_ds']", "distinct seeds", "alpha must be", "n_boot >=",
                 "reproduce_tolerance", "lo <= hi", "strictly between 0 and 1", "risk prior"):
        assert want in text, want
    for field, value in (("effect__min_effect", float("nan")), ("resolve_by", float("inf"))):
        with pytest.raises(ValueError):  # non-finite numbers cannot even form a Claim (so it is always hashable)
            variant(c, **{field: value})
    flipped = variant(c, metric__higher_is_better=False)
    assert "direction does not match" in reasons(K.k1_well_formed(ctx(w, flipped, [])))


# ------------------------------------------------------------------ K2
def test_k2_missing_and_tampered_registration(base, tmp_path):
    w, c, _ids = base
    never = variant(c, effect__min_effect=0.123)
    assert "never pre-registered" in reasons(K.k2_preregistered(ctx(w, never, [])))
    # a ledger whose prereg body does not hash to its claim (written by a buggy/hostile writer)
    w2 = World(tmp_path / "w2", seed=4, n_train=60, n_test=60)
    c2 = w2.claim(w2.artifact("good"), w2.artifact("baseline", "baseline"))
    w2.kernel.ledger.append("prereg", {"claim_hash": c2.claim_hash(), "claim": variant(c2, effect__min_effect=0.3)
                                       .model_dump(mode="json")})
    assert "does not match its hash" in reasons(K.k2_preregistered(ctx(w2, c2, [])))
    c3 = w2.claim(w2.artifact("good"), w2.artifact("baseline", "baseline"), delta=0.31)
    w2.kernel.ledger.append("prereg", {"claim_hash": c3.claim_hash()})
    assert "body unreadable" in reasons(K.k2_preregistered(ctx(w2, c3, [])))
    w2.close()


# ------------------------------------------------------------------ K3
class FixedReplayer:
    def __init__(self, out):
        self.out = out

    def replay(self, record, vault):
        if isinstance(self.out, Exception):
            raise self.out
        return self.out(record) if callable(self.out) else self.out


def test_k3_paths(base):
    w, c, ids = base
    assert "no usable evaluation records" in reasons(K.k3_reproduced(ctx(w, c, [])))
    assert "no replayer configured" in reasons(K.k3_reproduced(ctx(w, c, ids, replayer=None)))
    assert "failed: boom" in reasons(K.k3_reproduced(ctx(w, c, ids, replayer=FixedReplayer(ExecutionError("boom")))))
    assert "produced no predictions" in reasons(K.k3_reproduced(ctx(w, c, ids, replayer=FixedReplayer(None))))
    tol = variant(c, analysis_plan__reproduce_tolerance=0.02)
    w.kernel.preregister(tol)
    tol_ids = [add(w, record(w, e).model_copy(update={"claim_hash": tol.claim_hash()})) for e in ids]
    stored = {e: w.store.blobs.get_json(record(w, e).preds_ref) for e in tol_ids}
    same = FixedReplayer(lambda rec: next(p for e, p in stored.items() if record(w, e) == rec))
    assert K.k3_reproduced(ctx(w, tol, tol_ids, replayer=same)).status == "pass"
    assert "not scorable" in reasons(K.k3_reproduced(ctx(w, tol, tol_ids, replayer=FixedReplayer(["0"]))))
    flipped = FixedReplayer(lambda rec: ["9"] * 120)
    assert "tolerance 0.02" in reasons(K.k3_reproduced(ctx(w, tol, tol_ids, replayer=flipped)))
    odd = variant(c, analysis_plan__seeds=[7, 8, 9])
    w.kernel.preregister(odd)
    odd_ids = [add(w, record(w, ids[0]).model_copy(update={"claim_hash": odd.claim_hash()}))]
    assert "no record selected" in reasons(K.k3_reproduced(ctx(w, odd, odd_ids)))


# ------------------------------------------------------------------ K4
def test_k4_paths(base):
    w, c, ids = base
    assert "no usable evaluation records" in reasons(K.k4_statistics(ctx(w, c, [])))
    dup = ids + [add(w, record(w, ids[0]))]
    assert "duplicate records" in reasons(K.k4_statistics(ctx(w, c, dup)))
    other = variant(c, effect__min_effect=0.21)
    w.kernel.preregister(other)
    forged = [add(w, record(w, e).model_copy(update={"claim_hash": other.claim_hash()})) for e in ids]
    assert "no logged label read" in reasons(K.k4_statistics(ctx(w, other, forged)))


def test_criterion_rules():
    c_imp = Claim.model_construct(kind=ClaimKind.IMPROVEMENT, effect=Effect(min_effect=0.1, predicted_lo=0,
                                                                              predicted_hi=1, probability=0.5))
    flat = Interval(0.5, 0.5, 0.5, 3, 0.1, "t", degenerate=True)
    good = Interval(0.5, 0.3, 0.7, 50, 0.1, "bca")
    assert K._criterion(c_imp, flat, good)
    assert not K._criterion(c_imp, flat, Interval(0.5, 0.5, 0.5, 50, 0.1, "bca", degenerate=True))
    assert not K._criterion(c_imp, Interval(0.5, 0.05, 0.9, 3, 0.1, "t"), good)  # seed bound below delta
    other = c_imp.model_copy(update={"kind": ClaimKind.REDISCOVERY})
    assert not K._criterion(other, flat, good)


# ------------------------------------------------------------------ K5
def test_k5_metric_mismatch(base):
    w, c, ids = base
    wrong = [add(w, record(w, e).model_copy(update={"metric": "f1_macro"})) for e in ids]
    assert "the claim's metric is 'accuracy'" in reasons(K.k5_comparator(ctx(w, c, wrong)))


# ------------------------------------------------------------------ K6
def test_k6_roles_and_registration(base, tmp_path):
    w, c, _ids = base
    w.vault.register_dataset("devlike", DatasetRole.DEV, public={"test_X.csv": b"x1\n1\n"},
                             private={"test_y.csv": b"label\n1\n"})
    cx = ctx(w, variant(c, generalization_targets=["devlike", "ghost"]), [])
    text = reasons(K.k6_held_out(cx))
    assert "'dev' is not a held-out role" in text and "ghost: unregistered" in text
    assert not K.is_external(cx)


# ------------------------------------------------------------------ K7
def test_k7_degenerate_spread_and_certain_success(base):
    w, c, ids = base
    cx = ctx(w, c, ids)
    cx.stats["test"] = K.TargetStats(Interval(1, 1, 1, 3, 0.1, "t", True), Interval(1, 1, 1, 9, 0.1, "bca", True),
                                     0.05, True)
    assert "noise model is undefined" in reasons(K.k7_risk(cx))
    cx.stats["test"] = K.TargetStats(Interval(1, 1, 1, 3, 0.1, "t", True), Interval(0.5, 0.49, 0.51, 9, 0.1, "bca"),
                                     0.05, True)
    res = K.k7_risk(cx)  # min effect 0.2 is ~40 standard errors away: P(success | null) underflows to 0
    assert res.status == "pass" and "inf bits" in res.observations[0]
    neg = ctx(w, variant(c, kind="negative_result"), ids)
    res = K.k7_risk(neg)  # fail closed: no risk model for this kind in v1 (an "n/a" would have passed)
    assert res.status == "fail" and "no risk model for negative_result claims" in reasons(res)
    assert "no statistics" in reasons(K.k7_risk(ctx(w, c, [])))


# ------------------------------------------------------------------ K8
def test_k8_every_invariant(base):
    w, c, ids = base
    r = record(w, ids[0])
    blob = w.vault.dataset("test").files["test_y.csv"].blob
    recs = [
        r.model_copy(update={"score": 1.7}),
        r.model_copy(update={"preds_ref": w.store.blobs.put_json(["1"] * 5)}),
        r.model_copy(update={"preds_ref": w.store.blobs.put_json([" "] * 120)}),
        r.model_copy(update={"label_file": "test_X.csv"}),
        r.model_copy(update={"inputs": {"train": r.inputs["train"].model_copy(update={"blob": blob})}}),
        r.model_copy(update={"inputs": {"leak": r.inputs["test"].model_copy(update={"file": "test_y.csv",
                                                                                     "blob": blob})}}),
        r.model_copy(update={"metric": "custom_unbounded"}),
    ]
    text = reasons(K.k8_background(ctx(w, c, [add(w, x) for x in recs])))
    for want in ("outside the range", "5 predictions for 120 labels", "empty prediction values",
                 "is not a private file", "not the registered bytes", "PRIVATE file"):
        assert want in text, want


# ------------------------------------------------------------------ K9
def test_k9_paths(base):
    w, c, ids = base
    label_blob = w.vault.dataset("test").files["test_y.csv"].blob
    w.vault.register_artifact("labels_as_code", w.store.blobs.get(label_blob), "method")
    sneaky = variant(c, subject={"artifact_id": "labels_as_code", "hash": label_blob},
                     generalization_targets=["test", "ghost"])
    w.kernel.preregister(sneaky)
    bad_argv = [add(w, record(w, ids[0]).model_copy(update={"argv": ("{python}", "/etc/passwd"),
                                                             "claim_hash": sneaky.claim_hash()}))]
    text = reasons(K.k9_leaks(ctx(w, sneaky, bad_argv)))
    assert "subject code IS a private file" in text and "outside the workspace" in text
    gone = variant(c, subject={"artifact_id": "x", "hash": "e" * 64}, generalization_targets=["test2"])
    assert K.k9_leaks(ctx(w, gone, [])).status == "pass"  # unreadable code is K0's finding, not K9's


# ------------------------------------------------------------------ Kernel API
def test_kernel_api_errors_and_retraction(base, monkeypatch):
    w, c, ids = base
    k = w.kernel
    with pytest.raises(KernelError):
        k.registered_claim("0" * 64)
    with pytest.raises(KernelError):
        k.certificate("0" * 64)
    cert = k.certify(c, ids)
    h = cert.cert_hash()
    assert k.status(h) == "active"
    with pytest.raises(KernelError):
        k.retract(h, "  ")
    k.retract(h, "the held-out labels were found to be corrupted")
    assert k.status(h) == "retracted"

    def boom(ctx):
        raise RuntimeError("bug inside a check")
    monkeypatch.setitem(K.ALL_CHECKS, "K8", boom)
    crashed = k.certify(c, ids)
    assert crashed.label == Label.REJECTED and "raised RuntimeError" in crashed.checks["K8"].reasons[0]


# ------------------------------------------------------------------ ledger
def test_ledger_rules(tmp_path):
    from custom_harness.store import EvidenceStore
    s = EvidenceStore(tmp_path / "s")
    lg = Ledger(s)
    assert lg.head() == "genesis" and lg.verify() == []
    with pytest.raises(ValueError):
        lg.append("anything", {})
    a = lg.append("bundle", {"x": 1})
    b = lg.append("bundle", {"x": 2})
    assert lg.head() == b.hash and b.prev == a.hash and lg.verify() == []
    forged = Entry(b.seq, b.kind, {"x": 3}, b.prev, b.hash, b.at)
    assert "does not match its hash" in verify_chain([a, forged], full=True)[0]
    assert any("out of order" in p for p in verify_chain([b, a], full=False))
    gap = Entry(a.seq + 5, a.kind, a.body, a.prev, a.hash, a.at)
    assert any("chain broken" in p for p in verify_chain([gap], full=True))
    s.close()


# ------------------------------------------------------------------ vault
def test_vault_rules(tmp_path):
    from custom_harness.store import EvidenceStore
    s = EvidenceStore(tmp_path / "s")
    v = Vault(s)
    with pytest.raises(VaultError, match="both public and private"):
        v.register_dataset("d", DatasetRole.TEST, public={"a": b"1"}, private={"a": b"2"})
    rec = v.register_dataset("d", DatasetRole.TEST, public={"x": b"1"}, private={"y": b"2"})
    assert v.register_dataset("d", DatasetRole.TEST, public={"x": b"1"}, private={"y": b"2"}).hash == rec.hash
    with pytest.raises(VaultError, match="different content"):
        v.register_dataset("d", DatasetRole.TEST, public={"x": b"changed"}, private={"y": b"2"})
    art = v.register_artifact("m", b"code", "method")
    assert v.register_artifact("m", b"code", "method").hash == art.hash
    with pytest.raises(VaultError, match="different content"):
        v.register_artifact("m", b"other", "method")
    with pytest.raises(VaultError, match="unregistered"):
        v.dataset("nope")
    with pytest.raises(VaultError, match="is private"):
        v.read_public("d", "y")
    with pytest.raises(VaultError, match="must name"):
        v.read("d", "y", purpose="evaluate")
    with pytest.raises(VaultError, match="has no file"):
        v.read("d", "z", purpose="audit")
    assert v.canary("d") is None
    assert v.read("d", "y", purpose="audit") == b"2" and len(v.accesses()) == 1
    s.close()


# ------------------------------------------------------------------ execution
def test_execution_paths(tmp_path, base):
    w, _c, ids = base
    with pytest.raises(ExecutionError):
        parse_predictions(b"wrong\n1\n")
    with pytest.raises(ExecutionError):
        parse_predictions(b"prediction\n1,2\n")
    sb = LocalSandbox(tmp_path / "sb", i_understand_no_isolation=True)
    with pytest.raises(ExecutionError, match="placeholder"):
        run_program(sb, code=b"", entrypoint="s.py", argv=("{nope}",), inputs={}, seed=0, timeout_s=30)
    preds, diag = run_program(sb, code=b"import sys; sys.exit(3)", entrypoint="s.py", argv=("{python}", "{entrypoint}"),
                              inputs={}, seed=0, timeout_s=30)
    assert preds is None and "rc=3" in diag
    preds, diag = run_program(sb, code=b"open('out/preds.csv','wb').write(b'\\xff\\xfe')", entrypoint="s.py",
                              argv=("{python}", "{entrypoint}"), inputs={}, seed=0, timeout_s=30)
    assert preds is None and "unreadable predictions" in diag
    rec = record(w, ids[0])
    moved = rec.model_copy(update={"inputs": {"train": rec.inputs["train"].model_copy(update={"blob": "0" * 64})}})
    rep = SandboxReplayer(lambda key: LocalSandbox(tmp_path / key, i_understand_no_isolation=True))
    with pytest.raises(ExecutionError, match="no longer matches"):
        rep.replay(moved, w.vault)


# ------------------------------------------------------------------ predictions ledger
def _claim_for(w: World, q: float, resolve_by: float, prior=None) -> Claim:
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), probability=q)
    return c.model_copy(update={"resolve_by": resolve_by, "risk": Risk(prior_probability=prior)})


def test_prediction_scoring(tmp_path):
    w = World(tmp_path / "p", seed=5, n_train=60, n_test=60)
    k = w.kernel
    now = time.time()
    hit = _claim_for(w, 0.9, now + 1e6)
    miss = _claim_for(w, 0.8, now + 1e6, prior=0.4)
    expired = _claim_for(w, 0.7, now - 1)
    pending = _claim_for(w, 0.6, now + 1e6, prior=0.3)
    for cl in (hit, miss, expired, pending):
        k.preregister(cl)
    k.preregister(hit)  # duplicates are counted once
    for cl, ok, p_null in ((hit, True, 0.1), (miss, False, None)):
        k.ledger.append("certificate", {"cert_hash": cl.claim_hash(), "claim_hash": cl.claim_hash(),
                                        "label": "x", "blob": "", "resolution": {"outcome": ok, "p_null": p_null}})
    scores = {s.claim_hash: s for s in score_predictions(k.ledger, now)}
    assert len(scores) == 4
    s_hit, s_miss = scores[hit.claim_hash()], scores[miss.claim_hash()]
    assert math.isclose(s_hit.info_gain_bits, math.log2(0.9 / 0.1))
    assert math.isclose(s_miss.info_gain_bits, math.log2(0.2 / 0.6))  # p0 = declared prior 0.4
    s_exp = scores[expired.claim_hash()]
    assert s_exp.expired and s_exp.outcome is False and math.isclose(s_exp.info_gain_bits, math.log2(0.3 / 0.5))
    assert scores[pending.claim_hash()].outcome is None and scores[pending.claim_hash()].p0 == 0.3
    total = vds(list(scores.values()))
    assert math.isclose(total, sum(s.info_gain_bits for s in scores.values() if s.info_gain_bits is not None))
    cal = calibration(list(scores.values()), bins=5)
    assert cal["n"] == 3 and 0 <= cal["brier"] <= 1 and sum(b["n"] for b in cal["bins"]) == 3
    assert calibration([]) == {"n": 0, "brier": None, "bins": []}
    w.close()


# ------------------------------------------------------------------ checklist
def test_checklist_blocks_missing_items(base):
    w, c, ids = base
    cert = w.kernel.certify(c, ids)
    costs = {r: Cost(prompt_tokens=10, wall_s=1.0) for r in {record(w, e).run_id for e in ids}}
    full = fill(cert, run_costs=costs, models=["codex (byos)"], bundle_hash="b" * 64,
                selection_policy="best of 5 hypotheses on dev; confirmed on held-out",
                human_in_the_loop="none", novelty="not performed (literature module not built)")
    assert missing(full) == [] and publishable(cert, full) == (True, [])
    assert "Reporting checklist" in render(full) and len(full) == len(ITEMS)
    gaps = fill(cert, run_costs={}, models=[], bundle_hash=None, selection_policy=None, human_in_the_loop=None,
                novelty=None)
    assert set(missing(gaps)) == {"selection_policy", "traces", "reproducibility_bundle", "cost",
                                  "human_in_the_loop", "novelty_verification"}
    assert "MISSING" in render(gaps) and gaps["models"].startswith("none")
    rejected = cert.model_copy(update={"label": Label.REJECTED})
    ok, why = publishable(rejected, full)
    assert not ok and "not certified" in why[0]


# ------------------------------------------------------------------ bundle edge paths
def test_bundle_edge_paths(tmp_path, base):
    w, c, ids = base
    r = record(w, ids[0])
    junk = Evidence(kind=EvidenceKind.METRIC, tier=Tier.T1_EXECUTABLE, verifier_id="x", produced_by_run=None,
                    payload_ref=w.store.blobs.put_json({"not": "a record"}))
    w.store.add_evidence(junk)
    ghost_run = add(w, r.model_copy(update={"run_id": "run_gone"}))
    cert = w.kernel.certify(c, [*ids, "ev_missing", junk.id, ghost_run])
    assert cert.label == Label.REJECTED
    lost = w.store.blobs.put(b"will be deleted")
    w.store.blobs._path(lost).unlink()
    bundle = tmp_path / "edge.zip"
    build_bundle(w.kernel, cert.cert_hash(), bundle)
    fresh = lambda: SandboxReplayer(lambda key: LocalSandbox(tmp_path / "rp" / key, i_understand_no_isolation=True))  # noqa: E731
    rep = reproduce_bundle(bundle, fresh)
    assert rep.reproduced_label == "REJECTED"
    with zipfile.ZipFile(bundle) as zin:
        m = json.loads(zin.read("manifest.json"))
    cert_blob = next(row for row in m["ledger"] if row[1] == "certificate")[2]
    cert_blob = json.loads(cert_blob)["blob"]
    for name, drop in (("holed.zip", next(b for b in m["blobs"] if b != cert_blob)), ("nocertblob.zip", cert_blob)):
        with zipfile.ZipFile(bundle) as zin, zipfile.ZipFile(tmp_path / name, "w") as zout:
            for n in zin.namelist():
                if n != f"blobs/{drop}":
                    zout.writestr(n, zin.read(n))
        rep = reproduce_bundle(tmp_path / name, fresh)  # must report, never crash, whichever blob is missing
        assert not rep.reproduced and any("missing from the bundle" in p for p in rep.problems), rep.problems
    with zipfile.ZipFile(bundle) as zin, zipfile.ZipFile(tmp_path / "nocert.zip", "w") as zout:
        m = json.loads(zin.read("manifest.json"))
        m["ledger"] = [row for row in m["ledger"] if row[1] != "certificate"]
        for n in zin.namelist():
            zout.writestr(n, json.dumps(m).encode() if n == "manifest.json" else zin.read(n))
    rep = reproduce_bundle(tmp_path / "nocert.zip", fresh)
    assert not rep.reproduced and "certificate entry missing" in rep.problems[-1]


def test_check_ids_are_complete():
    assert tuple(K.ALL_CHECKS) == CHECK_IDS
    assert {f.name for f in Path(K.__file__).parent.glob("*.py")} >= {"claim.py", "checks.py", "ledger.py"}
    assert isinstance(PROGRAMS, dict) and AnalysisPlan and ArtifactRef and MetricRef and Scope and Effect


def test_stale_predictions_are_never_inherited(tmp_path):
    """A program that exits 0 without writing output must not be credited with an earlier run's file."""
    sb = LocalSandbox(tmp_path / "sb", i_understand_no_isolation=True)
    writer = b"open('out/preds.csv', 'w').write('prediction' + chr(10) + '1' + chr(10))"
    preds, _ = run_program(sb, code=writer, entrypoint="s.py", argv=("{python}", "{entrypoint}"), inputs={},
                           seed=0, timeout_s=30)
    assert preds == ["1"]
    lazy = b"pass  # writes nothing"
    preds, diag = run_program(sb, code=lazy, entrypoint="s.py", argv=("{python}", "{entrypoint}"), inputs={},
                              seed=0, timeout_s=30)
    assert preds is None and "unreadable predictions" in diag


def test_duplicate_prereg_entries_count_once(tmp_path):
    w = World(tmp_path / "d", seed=6, n_train=60, n_test=60)
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"))
    body = {"claim_hash": c.claim_hash(), "claim": c.model_dump(mode="json")}
    w.kernel.ledger.append("prereg", body)
    w.kernel.ledger.append("prereg", body)  # e.g. merged from two imported ledgers
    assert len(score_predictions(w.kernel.ledger, time.time())) == 1
    w.close()


def test_bundle_is_a_snapshot_and_reports_missing_bytes(tmp_path, base):
    w, c, ids = base
    cert = w.kernel.certify(c, ids)
    first = build_bundle(w.kernel, cert.cert_hash(), tmp_path / "one.zip")
    second = build_bundle(w.kernel, cert.cert_hash(), tmp_path / "two.zip")  # later ledger entries are excluded
    assert first == second
    rec = record(w, ids[-1])
    w.store.blobs._path(rec.preds_ref).unlink()  # bytes lost before bundling
    build_bundle(w.kernel, cert.cert_hash(), tmp_path / "three.zip")
    fresh = lambda: SandboxReplayer(lambda key: LocalSandbox(tmp_path / "rp" / key, i_understand_no_isolation=True))  # noqa: E731
    rep = reproduce_bundle(tmp_path / "three.zip", fresh)
    assert not rep.reproduced and any("missing from the bundle" in p for p in rep.problems)
    payload = w.store.get_evidence(ids[0]).payload_ref
    with zipfile.ZipFile(tmp_path / "one.zip") as zin, zipfile.ZipFile(tmp_path / "nopayload.zip", "w") as zout:
        for n in zin.namelist():
            if n != f"blobs/{payload}":
                zout.writestr(n, zin.read(n))
    rep = reproduce_bundle(tmp_path / "nopayload.zip", fresh)
    assert any("not importable" in p for p in rep.problems)
    w.store.blobs.put_json(w.store.blobs.get_json(payload))  # leave the shared fixture intact


def test_external_target_earns_validated_label(tmp_path):
    """K6 on an EXTERNAL (or FUTURE) dataset -> CERTIFIED-VALIDATED; internal held-out -> CERTIFIED-INTERNAL."""
    w = World(tmp_path / "ext", seed=8, n_train=120, n_test=120)
    w.register_target("external_lab", DatasetRole.EXTERNAL, n=120)
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2, targets=("external_lab",),
                n_boot=1000)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    assert cert.label == Label.CERTIFIED_VALIDATED, {k: v.reasons for k, v in cert.checks.items() if v.reasons}
    assert "external" in cert.vg_ladder
    w.close()


def test_relabelled_internal_test_set_cannot_launder_validation(tmp_path):
    """Re-registering the internal test set under a new id with role EXTERNAL must not earn VALIDATED."""
    w = World(tmp_path / "launder", seed=9, n_train=120, n_test=120)
    test = w.vault.dataset("test")
    w.vault.register_dataset("totally_external", DatasetRole.EXTERNAL,
                             public={"test_X.csv": w.store.blobs.get(test.files["test_X.csv"].blob)},
                             private={"test_y.csv": w.store.blobs.get(test.files["test_y.csv"].blob)})
    w.labels["totally_external"] = w.labels["test"]
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2, targets=("totally_external",),
                n_boot=1000)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    assert cert.label == Label.REJECTED
    assert "identical content registered under other id" in " ".join(cert.checks["K6"].reasons)
    w.close()


def test_test_set_reuse_is_multiplicity_not_peeking(tmp_path):
    """A second, independently registered claim on the same target is NOT a K2 violation (the earlier reads
    served another claim); it is charged as multiplicity: Bonferroni over both registered claims."""
    w = World(tmp_path / "reuse", seed=12, n_train=120, n_test=120)
    first = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2, n_boot=1000)
    w.kernel.preregister(first)
    assert w.kernel.certify(first, w.evaluate_all(first)).label == Label.CERTIFIED_INTERNAL
    second = w.claim(w.artifact("good_k17"), w.artifact("baseline", "baseline"), delta=0.2, n_boot=1000)
    w.kernel.preregister(second)
    cert = w.kernel.certify(second, w.evaluate_all(second))
    assert cert.label == Label.CERTIFIED_INTERNAL, {k: v.reasons for k, v in cert.checks.items() if v.reasons}
    assert any("2 claim(s) tested on this target" in o for o in cert.checks["K4"].observations)
    # but an inspection read of the labels before registering a third claim IS peeking
    w.vault.read("test", "test_y.csv", purpose="audit")
    third = w.claim(w.artifact("good_seeded"), w.artifact("baseline", "baseline"), delta=0.2, n_boot=1000)
    w.kernel.preregister(third)
    assert "K2" in w.kernel.certify(third, w.evaluate_all(third)).failed()
    w.close()


def test_k6_and_k4_see_held_out_examples_shared_under_other_dataset_ids(tmp_path):
    """Found 2026-10-07 (live suite): every held-out example of a real-data task was public, WITH its label, in its
    repair variant's training file, and 16 were in the repair task's own held-out set. Identity by dataset id and
    exact bytes saw none of it. K6 now refuses a target whose examples appear in a public file of any other dataset
    that has more columns than the target's features (a label can be there); K4 counts claims on any target that
    shares held-out examples into the multiplicity family."""
    w = World(tmp_path / "w", seed=5, n_train=120, n_test=120)
    try:
        lines = w.vault.read_public("test", "test_X.csv").decode().splitlines()
        header, rows, labels = lines[0], lines[1:], w.labels["test"]
        leaky = "\n".join([f"{header},label", *[f"{r},{y}" for r, y in zip(rows[:10], labels[:10], strict=True)]])
        w.vault.register_dataset("other_train", DatasetRole.DEV, public={"train.csv": (leaky + "\n").encode()},
                                 private={})
        # no target columns / empty file: ignored
        w.vault.register_dataset("misc", DatasetRole.DEV, public={"notes.csv": b"a,b\n1,2\n", "empty.csv": b""},
                                 private={})
        # another held-out target re-using 15 of the same examples (features only: no label exposed, but reuse)
        w.vault.register_dataset("test_reuse", DatasetRole.TEST,
                                 public={"test_X.csv": ("\n".join([header, *rows[5:20]]) + "\n").encode()},
                                 private={"test_y.csv": ("label\n" + "\n".join(labels[5:20]) + "\n").encode()})
        other = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2, targets=("test_reuse",))
        w.kernel.preregister(other)
        c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2, n_boot=1000)
        w.kernel.preregister(c)
        cert = w.kernel.certify(c, w.evaluate_all(c))
    finally:
        w.close()
    k6 = reasons(cert.checks["K6"])
    assert "10 held-out example(s) of 'test' appear in public file other_train/train.csv" in k6, k6
    assert "test_reuse" not in k6 and "misc" not in k6
    assert any("2 claim(s) tested on this target" in o for o in cert.checks["K4"].observations), cert.checks["K4"]


def test_k1_requires_a_human_approved_unchanged_adapter_for_the_evidence_class(tmp_path):
    """GOAL §4.7 / invariant 13: a claim is checked only by a human-approved domain adapter whose content is unchanged
    since approval; a claim of a class no approved adapter serves (here: simulated) is never certified."""
    w = World(tmp_path / "w", seed=6, n_train=120, n_test=120, approve_adapters=False)
    try:
        c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2, n_boot=1000)
        w.kernel.preregister(c)
        ids = w.evaluate_all(c)
        cert = w.kernel.certify(c, ids)
        assert cert.failed() == ["K1"] and "no human approval" in reasons(cert.checks["K1"]) and cert.adapters == ()
        entry = w.kernel.approve_adapter("ml", approver="alice")
        cert = w.kernel.certify(c, ids)
        assert cert.label == Label.CERTIFIED_INTERNAL, cert.failed()
        (used,) = cert.adapters
        assert used["name"] == "ml" and used["approval_seq"] == entry.seq and used["approver"] == "alice"
        sim = variant(c, evidence_class="simulated")
        w.kernel.preregister(sim)
        k1 = reasons(w.kernel.certify(sim, []).checks["K1"])
        assert "no registered adapter serves evidence class 'simulated'" in k1
    finally:
        w.close()


def test_adapter_content_change_after_approval_invalidates_it(tmp_path, monkeypatch):
    import sys

    from custom_harness.kernel import adapters as A
    pkg = tmp_path / "adapter_mod_x"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("VALUE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = A.AdapterSpec("ml", "1", frozenset({A.EvidenceClass.DATA}), ("adapter_mod_x",), frozenset({"accuracy"}))
    monkeypatch.setitem(A.REGISTRY, "ml", (spec, (("trivially true", lambda: True),)))
    w = World(tmp_path / "w", approve_adapters=False)
    try:
        w.kernel.approve_adapter("ml", approver="alice")
        assert A.resolve(w.kernel.ledger, A.EvidenceClass.DATA, "accuracy")[0] is not None
        (pkg / "__init__.py").write_text("VALUE = 2  # an unapproved change\n")
        sys.modules.pop("adapter_mod_x", None)
        adapter, why = A.resolve(w.kernel.ledger, A.EvidenceClass.DATA, "accuracy")
        assert adapter is None and "changed since approval" in why[0]
    finally:
        w.close()


def test_adapter_approval_refuses_unknown_anonymous_or_failing_adapters(tmp_path, monkeypatch):
    from custom_harness.kernel import AdapterError
    from custom_harness.kernel import adapters as A
    assert A.validate(A.ML_CHECKS) == []  # the real ML adapter passes its own fault-injection validation
    assert A._raises(lambda: None) is False  # a "must refuse" check whose target does NOT refuse fails
    w = World(tmp_path / "w", approve_adapters=False)
    try:
        with pytest.raises(AdapterError, match="unknown adapter"):
            w.kernel.approve_adapter("lean", approver="alice")
        with pytest.raises(AdapterError, match="name the human approver"):
            w.kernel.approve_adapter("ml", approver="  ")

        def boom() -> bool:
            raise RuntimeError("crash")

        broken = (("ok", lambda: True), ("wrong answer", lambda: False), ("crashes", boom))
        monkeypatch.setitem(A.REGISTRY, "ml", (A.ML, broken))
        with pytest.raises(AdapterError, match=r"failed its validation: \['wrong answer', 'crashes'\]"):
            w.kernel.approve_adapter("ml", approver="alice")
        assert w.kernel.ledger.entries("adapter_approval") == []
    finally:
        w.close()


def test_evidence_class_and_adapters_keep_old_hashes_stable(base):
    w, c, ids = base
    assert b"evidence_class" not in c.canonical()  # the default is omitted: pre-existing claim hashes unchanged
    assert b'"evidence_class":"formal"' in variant(c, evidence_class="formal").canonical()
    cert = w.kernel.certify(c, ids, record=False)
    legacy = Certificate.model_validate({k: v for k, v in cert.model_dump(mode="json").items() if k != "adapters"})
    assert legacy.adapters == () and cert.adapters != ()
    import hashlib
    old_rule = {k: v for k, v in legacy.model_dump(mode="json").items() if k != "adapters"}  # how v1.0 hashed it
    assert legacy.cert_hash() == hashlib.sha256(canonical_json(old_rule)).hexdigest()
    assert legacy.cert_hash() != cert.cert_hash()  # adapters are part of a new certificate's identity


def test_infrastructure_outage_during_certification_issues_no_certificate(tmp_path, monkeypatch):
    """Found 2026-10-07: a check that raises is a FAILED check (fail closed), so a Docker outage during K3's
    re-execution turned into an immutable REJECTED certificate for an honest claim: an infrastructure failure
    scored as a policy failure (GOAL L1). An outage must issue no certificate at all; certification is then
    retried and judges the claim on its merits."""
    from custom_harness.errors import InfraError
    from custom_harness.kernel import Kernel

    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    w = World(tmp_path / "w")
    try:
        claim = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.3)
        w.kernel.preregister(claim)
        ids = w.evaluate_all(claim)

        class DaemonDown:
            def replay(self, record, vault):
                raise InfraError("Cannot connect to the Docker daemon", transient=True)

        before = len(w.kernel.ledger.entries("certificate"))
        with pytest.raises(InfraError):
            Kernel(w.store, replayer=DaemonDown()).certify(claim, ids)
        assert len(w.kernel.ledger.entries("certificate")) == before  # nothing recorded
        assert w.kernel.certify(claim, ids).label == Label.CERTIFIED_INTERNAL  # the retry judges the claim
    finally:
        w.close()


# ------------------------------------------------------------------ M3: computational evidence class (exact LP adapter)
# maximise x1 + x2  s.t.  2 x1 + x2 <= 4,  x1 + 3 x2 <= 5,  x >= 0: optimum 13/5 at x = (7/5, 6/5), dual y = (2/5, 1/5)
LP_PROBLEM = {"format": "chh-lp/1", "sense": "max", "c": ["1", "1"], "A": [["2", "1"], ["1", "3"]], "b": ["4", "5"]}
LP_CERT = {"format": "chh-lp-certificate/1", "x": ["7/5", "6/5"], "y": ["2/5", "1/5"]}


@pytest.fixture()
def lpw(tmp_path):
    w = World(tmp_path / "lp", seed=11, n_train=30, n_test=30)
    w.kernel.approve_adapter("lp", approver="test-world")
    yield w
    w.close()


def lp_claim(w: World, *, value: str | None = "13/5", problem: dict | None = None, artifact_id: str = "lp-instance",
             **changes) -> Claim:
    data = canonical_json(problem or LP_PROBLEM)
    ref = ArtifactRef(artifact_id=artifact_id, hash=w.vault.register_artifact(artifact_id, data, "problem").hash)
    doc = Claim(kind=ClaimKind.IMPROVEMENT, statement=f"the optimum of {artifact_id} is {value}", subject=ref,
                comparator=ref, metric=MetricRef(id="lp_optimum", higher_is_better=True),
                effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                scope=Scope(datasets=()), development=(), generalization_targets=(),
                analysis_plan=AnalysisPlan(seeds=()), resolve_by=time.time() + 86400).model_dump(mode="json")
    doc |= {"kind": "computed_value", "evidence_class": "computational", "value": value}
    claim = Claim.model_validate(doc)
    return variant(claim, **changes) if changes else claim


def lp_cert_artifact(w: World, claim: Claim, cert: dict | None = None, raw: bytes | None = None) -> str:
    import hashlib
    data = raw if raw is not None else canonical_json(LP_CERT if cert is None else {**LP_CERT, **cert})
    aid = f"cert-{claim.claim_hash()[:10]}-{hashlib.sha256(data).hexdigest()[:10]}"
    return w.vault.register_artifact(aid, data, "certificate").artifact_id


def lp_certify(w: World, claim: Claim, cert: dict | None = None, raw: bytes | None = None) -> Certificate:
    w.kernel.preregister(claim)
    eid = w.kernel.record_certificate(claim, lp_cert_artifact(w, claim, cert, raw))
    return w.kernel.certify(claim, [eid])


def test_lp_claim_certifies_at_t1_with_the_lp_adapter_and_table_reasons(lpw):
    from custom_harness.kernel import EvidenceClass
    claim = lp_claim(lpw)
    cert = lp_certify(lpw, claim)
    assert cert.label == Label.CERTIFIED_EXACT, {k: v.reasons for k, v in cert.checks.items() if v.status != "pass"}
    assert cert.tier == int(Tier.T1_EXECUTABLE) and "exact" in cert.vg_ladder
    assert [k for k in CHECK_IDS if cert.checks[k].status == "pass"] == ["K0", "K1", "K2", "K3", "K8", "K9"]
    table = K.CHECK_APPLICABILITY[EvidenceClass.COMPUTATIONAL]
    for kid in ("K4", "K5", "K6", "K7"):  # n/a, with the reason taken from the human-owned table
        assert cert.checks[kid].status == "n/a" and cert.checks[kid].reasons == (table[kid],)
    assert "exact certificate replaces statistics" in table["K4"]
    assert "triviality/importance are not judged by the Kernel (GOAL §4.6)" in table["K7"]
    (used,) = cert.adapters
    assert used["name"] == "lp" and used["approver"] == "test-world"
    assert "exact:lp_optimum" in cert.verifier_ids
    assert any(a.tested_by is None and "instance" in a.text for a in cert.assumptions)
    assert lpw.kernel.verify(cert.cert_hash()) == []
    assert b'"value":"13/5"' in claim.canonical() and b'"evidence_class":"computational"' in claim.canonical()


def test_lp_planted_faults_are_rejected_by_the_right_check(lpw):
    faults = {  # certificate change -> (failing checks, reason)
        "infeasible primal": ({"x": ["2", "1"]}, ["K3"], "primal infeasible"),
        "infeasible dual": ({"y": ["1/5", "1/5"]}, ["K3"], "dual infeasible"),
        "duality gap": ({"x": ["1", "1"]}, ["K3"], "duality gap"),
        "float-encoded value": ({"x": [1.4, 1.2]}, ["K3", "K8"], "exact rational string"),
        "wrong dimensions": ({"x": ["7/5", "6/5", "0"]}, ["K3", "K8"], "dimension"),
    }
    for name, (change, failed, why) in faults.items():
        cert = lp_certify(lpw, lp_claim(lpw), change)
        assert cert.label == Label.REJECTED and cert.failed() == failed, (name, cert.failed())
        assert why in reasons(cert.checks["K3"]), (name, cert.checks["K3"])
    wrong = lp_certify(lpw, lp_claim(lpw, value="3"))  # a correct certificate, but not of the claimed value
    assert wrong.failed() == ["K3"] and "differs from the claimed value 3" in reasons(wrong.checks["K3"])
    for bad in ("2.6", "26/10", "-0", "1/0", None):  # float-encoded or non-canonical claimed values
        cert = lp_certify(lpw, lp_claim(lpw, value=bad))
        assert cert.label == Label.REJECTED and "K1" in cert.failed(), bad
        assert "exact rational" in reasons(cert.checks["K1"]), (bad, cert.checks["K1"])
        assert "see K1" in reasons(cert.checks["K3"])
    huge = {**LP_PROBLEM, "A": [*LP_PROBLEM["A"], ["1", "0"]], "b": [*LP_PROBLEM["b"], "1" + "0" * 120]}
    cert = lp_certify(lpw, lp_claim(lpw, problem=huge, artifact_id="lp-huge"), {"y": ["2/5", "1/5", "0"]})
    assert cert.failed() == ["K8"] and "exceed 100 digits" in reasons(cert.checks["K8"])


def test_lp_provenance_order_and_leak_faults(lpw):
    claim = lp_claim(lpw)
    lpw.kernel.preregister(claim)
    assert lpw.kernel.certify(claim, []).failed() == ["K0", "K3", "K8"]  # no certificate at all
    early = lp_claim(lpw, statement="registered after its certificate")
    aid = lp_cert_artifact(lpw, early)  # the certificate exists BEFORE the claim is pre-registered
    lpw.kernel.preregister(early)
    cert = lpw.kernel.certify(early, [lpw.kernel.record_certificate(early, aid)])
    assert cert.failed() == ["K2"] and "registered before the pre-registration" in reasons(cert.checks["K2"])
    other = lp_claim(lpw, statement="another claim")
    foreign = lpw.kernel.record_certificate(other, lp_cert_artifact(lpw, other))
    cert = lpw.kernel.certify(claim, [foreign])
    assert "K0" in cert.failed() and "different claim" in reasons(cert.checks["K0"])
    ok_id = lpw.kernel.record_certificate(claim, lp_cert_artifact(lpw, claim))
    assert "exactly one certificate" in reasons(lpw.kernel.certify(claim, [ok_id, ok_id]).checks["K0"])
    ghost = Evidence(kind=EvidenceKind.PROOF, tier=Tier.T1_EXECUTABLE, verifier_id="x", produced_by_run=None,
                     payload_ref=lpw.store.blobs.put_json({"claim_hash": claim.claim_hash(), "artifact_id": "nope",
                                                           "hash": "0" * 64}))
    lpw.store.add_evidence(ghost)
    metric = Evidence(kind=EvidenceKind.METRIC, tier=Tier.T1_EXECUTABLE, verifier_id="x", produced_by_run=None,
                      payload_ref=lpw.store.blobs.put_json({}))
    lpw.store.add_evidence(metric)
    for eid, why in ((ghost.id, "is not registered"), (metric.id, "not a certificate record"), ("ev_none", "KeyError")):
        assert why in reasons(lpw.kernel.certify(claim, [eid]).checks["K0"]), why
    with pytest.raises(KernelError, match="not registered"):
        lpw.kernel.record_certificate(claim, "no-such-artifact")
    lost = lp_claim(lpw, statement="its certificate bytes are lost")
    lpw.kernel.preregister(lost)
    lost_aid = lp_cert_artifact(lpw, lost, {"comment": "lost"})
    lost_id = lpw.kernel.record_certificate(lost, lost_aid)
    lpw.store.blobs._path(lpw.vault.artifacts()[lost_aid].hash).unlink()
    assert "missing or corrupted" in reasons(lpw.kernel.certify(lost, [lost_id]).checks["K0"])
    bogus = ArtifactRef(artifact_id="lp-instance", hash="0" * 64).model_dump()
    unknown = variant(claim, subject=bogus, comparator=bogus)
    lpw.kernel.preregister(unknown)
    k0 = reasons(lpw.kernel.certify(unknown, [ok_id]).checks["K0"])
    assert "problem instance 'lp-instance' is not registered with hash 000000000000" in k0
    gone = lp_claim(lpw, problem={**LP_PROBLEM, "comment": "gone"}, artifact_id="lp-gone")
    gone_id = lpw.kernel.record_certificate(gone, lp_cert_artifact(lpw, gone))
    lpw.store.blobs._path(gone.subject.hash).unlink()
    lpw.kernel.preregister(gone)
    assert "problem instance bytes missing or corrupted" in reasons(lpw.kernel.certify(gone, [gone_id]).checks["K0"])
    canary = lpw.vault.canary("test")
    leaky = lp_certify(lpw, lp_claim(lpw, statement="leaky"), {"comment": f"solved with {canary}"})
    assert leaky.failed() == ["K9"] and "canary token found in certificate" in reasons(leaky.checks["K9"])
    labels = lpw.store.blobs.get(lpw.vault.dataset("test").files["test_y.csv"].blob)
    stolen = lp_certify(lpw, lp_claim(lpw, statement="stolen"), raw=labels)
    assert "certificate IS a private file (test/test_y.csv)" in reasons(stolen.checks["K9"])
    unregistered = lp_claim(lpw, statement="never registered")
    cert = lpw.kernel.certify(unregistered, [lpw.kernel.record_certificate(unregistered, aid)])
    assert cert.failed() == ["K2", "K3", "K8", "K9"] and cert.checks["K4"].status == "n/a"


def test_lp_claim_well_formedness(lpw, base):
    claim = lp_claim(lpw)
    lpw.kernel.preregister(claim)
    other = ArtifactRef(artifact_id="x", hash="0" * 64)
    cases = {
        "comparator must be the subject": {"comparator": other.model_dump()},
        "uses no datasets": {"generalization_targets": ["test"]},
        "analysis plan must be empty": {"analysis_plan__seeds": [0, 1, 2]},
        "effect fields other than the declared probability must be 0": {"effect__min_effect": 0.1},
        "declared probability": {"effect__probability": 1.0},
        "risk prior must be unset": {"risk__prior_probability": 0.5},
        "kind 'computed_value'": {"kind": "improvement"},
        "direction does not match the adapter": {"metric__higher_is_better": False},
        "no registered adapter serves evidence class 'computational' with metric 'lp_min'": {"metric__id": "lp_min"},
    }
    for why, change in cases.items():
        bad = variant(claim, **change)
        lpw.kernel.preregister(bad)
        assert why in reasons(lpw.kernel.certify(bad, []).checks["K1"]), (why, bad)
    w, c, ids = base
    k1 = reasons(w.kernel.certify(variant(c, value="1"), ids, record=False).checks["K1"])
    assert "only computed_value claims carry an exact value" in k1
    k1 = reasons(w.kernel.certify(variant(c, kind="computed_value"), ids, record=False).checks["K1"])
    assert "'computed_value' is not checkable" in k1
    assert b'"value"' not in c.canonical() and "value" not in c.model_dump(mode="json")  # data hashes unchanged


def test_computational_claim_without_an_approved_lp_adapter_fails_k1(tmp_path):
    w = World(tmp_path / "noapp", seed=12, n_train=30, n_test=30)  # approves only the ML adapter
    try:
        cert = lp_certify(w, lp_claim(w))
        assert cert.label == Label.REJECTED and "K1" in cert.failed() and cert.adapters == ()
        assert "adapter 'lp'" in reasons(cert.checks["K1"]) and "no human approval" in reasons(cert.checks["K1"])
    finally:
        w.close()


def test_n_a_outside_the_table_fails_closed(lpw, base, monkeypatch):
    from custom_harness.kernel import CheckResult, EvidenceClass
    from custom_harness.kernel import adapters as A
    assert set(K.applicability(EvidenceClass.DATA).values()) == {None}  # every check applies to data claims
    assert K.applicability(EvidenceClass.SIMULATED) == K.applicability(EvidenceClass.DATA)  # unknown: all apply
    w, c, ids = base
    na = lambda ctx: CheckResult(status="n/a", reasons=("planted",))  # noqa: E731
    monkeypatch.setitem(K.ALL_CHECKS, "K7", na)
    assert w.kernel.certify(c, ids, record=False).label == Label.REJECTED
    monkeypatch.setitem(K.COMPUTATIONAL_CHECKS, "K3", na)
    claim = lp_claim(lpw)
    assert lp_certify(lpw, claim).label == Label.REJECTED
    monkeypatch.undo()
    no_exact = A.AdapterSpec("lp", "1", frozenset({EvidenceClass.COMPUTATIONAL}), ("custom_harness.kernel.lp",),
                             frozenset({"lp_optimum"}))
    monkeypatch.setitem(A.REGISTRY, "lp", (no_exact, A.LP_CHECKS))
    lpw.kernel.approve_adapter("lp", approver="test-world")
    cert = lpw.kernel.certify(claim, [])
    assert "declares no exact certificate checker" in reasons(cert.checks["K1"])
    assert "no approved adapter with an exact certificate checker" in reasons(cert.checks["K3"])


def test_lp_adapter_validation_and_checker(lpw):
    from fractions import Fraction

    from custom_harness.kernel import adapters as A
    from custom_harness.kernel import lp
    assert A.validate(A.LP_CHECKS) == [] and len(A.LP_CHECKS) >= 8
    assert A.adapter_hash(A.ML) == "71694bb1ca60010f9ea241073c89255f5f0937bc1f215b7797e088ca8e363fca"  # unchanged
    p, cert = canonical_json(LP_PROBLEM), canonical_json(LP_CERT)
    assert lp.check(p, cert, Fraction(13, 5)) == [] and lp.bounds(p, cert, Fraction(13, 5)) == []
    no_b = {k: v for k, v in LP_PROBLEM.items() if k != "b"}
    malformed = {
        "not JSON": (b"{", cert), "not a chh-lp/1": (canonical_json([1]), cert),
        "unexpected keys": (canonical_json({**LP_PROBLEM, "extra": "1"}), cert),
        "missing keys": (canonical_json(no_b), cert),
        "sense must be 'max'": (canonical_json({**LP_PROBLEM, "sense": "min"}), cert),
        "'c' must be a list": (canonical_json({**LP_PROBLEM, "c": "1"}), cert),
        "at least one variable": (canonical_json({**LP_PROBLEM, "c": [], "A": [[], []]}), cert),
        "'A' must be a list": (canonical_json({**LP_PROBLEM, "A": {}}), cert),
        "exact rational string": (canonical_json({**LP_PROBLEM, "b": ["4", True]}), cert),
        "larger than": (b" " * (lp.MAX_BYTES + 1), cert),
        "not a chh-lp-certificate/1": (p, canonical_json({**LP_CERT, "format": "chh-lp/1"})),
    }
    for why, (prob, cer) in malformed.items():
        assert any(why in r for r in lp.check(prob, cer, Fraction(1))), why
        assert lp.bounds(prob, cer, Fraction(1)) != [], why
    assert any("x[0] = -1 < 0" in r for r in lp.check(p, canonical_json({**LP_CERT, "x": ["-1", "0"]}), Fraction(-1)))
    assert any("y[1] = -1 < 0" in r for r in lp.check(p, canonical_json({**LP_CERT, "y": ["1", "-1"]}), Fraction(-1)))
    wide = canonical_json({**LP_PROBLEM, "c": ["0"] * (lp.MAX_DIM + 1), "A": [], "b": []})
    zero = canonical_json({**LP_CERT, "x": ["0"] * (lp.MAX_DIM + 1), "y": []})
    assert "exceed the registered bound" in lp.bounds(wide, zero, Fraction(0))[0]
    assert lp.check(wide, zero, Fraction(0)) == []
    assert lp.parse_rational("-3/4") == Fraction(-3, 4)
    for bad in ("0.5", "1e3", " 1", "1/-2", "+1", "1/0", 2):
        with pytest.raises(ValueError):
            lp.parse_rational(bad)


def test_lp_certificate_bundle_reproduces_by_rechecking_the_certificate(lpw, tmp_path, monkeypatch):
    from custom_harness.kernel import lp
    cert = lp_certify(lpw, lp_claim(lpw))
    path = tmp_path / "lp.zip"
    build_bundle(lpw.kernel, cert.cert_hash(), path)
    fresh = lambda: SandboxReplayer(lambda key: LocalSandbox(tmp_path / "rp" / key, i_understand_no_isolation=True))  # noqa: E731
    rep = reproduce_bundle(path, fresh)
    assert rep.reproduced and rep.reproduced_label == "CERTIFIED-EXACT", rep.problems
    assert rep.certificate is not None and rep.certificate.checks["K3"].status == "pass"
    monkeypatch.setattr(lp, "check", lambda *a: ["planted: the checker now rejects"])  # reproduction must re-check
    rep = reproduce_bundle(path, fresh)
    assert not rep.reproduced and any(p.startswith("K3: pass originally, fail now") for p in rep.problems)
