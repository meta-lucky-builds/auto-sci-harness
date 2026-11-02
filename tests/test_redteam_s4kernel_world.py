"""s4-kernel red team (s4-kernel__redteam), end-to-end attack tests on a kernel_world.World. NOT run by the red team
(owner rule: no tests this round). Each test asserts the SAFE behaviour; its docstring says why it is expected to fail
on s4-kernel 59066ec and whether that is verified (seen in a probe) or inferred (reasoned from the code)."""

from __future__ import annotations

import random

import pytest

from custom_harness.kernel import DatasetRole, EvidenceClass, ExecRecord, InputRef, KernelConfig, Label
from custom_harness.kernel import alpha as A
from custom_harness.kernel import checks as K
from custom_harness.kernel.reasons import Code
from custom_harness.records import Run
from custom_harness.verify import metrics as M
from custom_harness.verify import tolerance as T
from kernel_world import KNN, World, _csv, _sample

FAST = {"eps_max": 0.5, "beta": 0.25, "n_boot": 1000}


# ------------------------------------------------------------------ RT-S4-3: reservation charges are not re-derived
@pytest.mark.parametrize("charges", [[0, 0, 0], [8, -7]])
def test_reservations_with_zero_or_negative_charges_do_not_hide_an_overdraft(tmp_path, charges):
    """RT-S4-3 (high, inferred). alpha.charged sums the `charge` field of alpha_reserve entries as written. A ledger
    written around preregister (the threat target_level already names) can add reservations with charge 0, or a
    negative charge that cancels a large one: more claims hold slots at alpha_T / S_T than S_T allows (FWER above
    alpha_T), yet charged <= slots so K4_ACCOUNT_OVERDRAWN never fires. Fails today. Fix: count max(1, charge) per
    reservation, require each charge to equal slot_charge re-derived as of the reservation's seq, and count every
    reservation (not only the charge) against the slots."""
    w = World(tmp_path / "w", seed=41, n_train=120, n_test=120)
    try:
        k = w.kernel
        w.register_target("acct", DatasetRole.TEST)
        k.open_alpha_account("acct", 0.06, 2, approver="test-human")
        (acct,) = A.accounts(k.ledger, {"acct"})
        c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2, targets=("acct",), **FAST)
        k.preregister(c)
        for i, charge in enumerate(charges):
            k.ledger.append("alpha_reserve", {"account": acct.seq, "dataset_id": "acct", "claim_hash": f"{i:x}" * 64,
                                              "slot": i + 2, "level": 0.03, "charge": charge})
        assert len(A.reservations(k.ledger, acct)) > acct.slots  # the attack: more slot holders than slots
        cert = k.certify(c, w.evaluate_all(c, targets=("acct",)), record=False)
        assert cert.checks["K4"].status == "fail" and Code.K4_ACCOUNT_OVERDRAWN in cert.checks["K4"].codes
    finally:
        w.close()


# ------------------------------------------------------------------ RT-S4-4: fixed terms are trusted as written
# owner decision 2026-10-28 (stage-c4): open findings are strict xfails until fixed; the attack below is unchanged
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="open red-team finding RT-S4-4 (medium): K2 does not "
                   "re-derive the stored prereg fixed terms, so terms written around preregister weaken K3")
def test_prereg_fixed_terms_written_around_preregister_cannot_weaken_k3(tmp_path):
    """RT-S4-4 (medium, inferred). checks.fixed() returns prereg.body['fixed'] verbatim when it is a dict; nothing
    compares it with fixed_terms() re-derived as of the entry's seq. A prereg entry appended directly with
    {'replays': 1, 'p0': 0.0, ...} lowers K3's zero-failure demonstration to one replay per record and p0 to 0, and
    verify() re-derives from the same entry, so it agrees. Fails today. Fix: K2 re-derives fixed_terms(AsOfLedger(seq
    - 1), ...) and fails (K2_PREREG_MISMATCH) when the stored terms differ (report STALE across config changes)."""
    w = World(tmp_path / "w", seed=42, n_train=120, n_test=120)
    try:
        k = w.kernel
        c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2, n_boot=1000)
        honest = K.fixed_terms(k.ledger, k.vault, c, k.config)
        forged = {**honest, "replays": 1, "eps_max": 0.9, "beta": 0.9, "p0": 0.0}
        k.ledger.append("prereg", {"claim_hash": c.claim_hash(), "claim": c.model_dump(mode="json"), "fixed": forged})
        cert = k.certify(c, w.evaluate_all(c), record=False)
        need = K.reliability(c, KernelConfig())[2]
        assert cert.checks["K2"].status == "fail" or any(
            f"{need} clean replay(s) needed" in o for o in cert.checks["K3"].observations), cert.checks["K3"]
    finally:
        w.close()


# ------------------------------------------------------------------ RT-S4-2: a truth copy under another dataset id
_DECODE = 'L = [r[th.index("leak")][1:] for r in trows] if "leak" in th else None\n'
STEALTH = KNN.format(k=15, pre="", post=_DECODE + "if L:\n    wrong = [i for i, (p, l) in enumerate(zip(preds, L)) if "
                                                  "p != l][:len(L) // 50]\n    for i in wrong: preds[i] = L[i]")


def _nt_world(tmp_path, seed: int) -> World:
    w = World(tmp_path / "w", seed=seed)
    rng = random.Random(7)
    rows = [_sample(rng) for _ in range(240)]
    public = _csv([["x1", "x2", "n1", "leak"], *[[*r[:-1], "v" + r[-1]] for r in rows]])
    w.vault.register_dataset("nt", DatasetRole.TEST, public={"test_X.csv": public, "copy.csv": public},
                             private={"test_y.csv": _csv([["label"], *[[r[-1]] for r in rows]])},
                             canary="CHH-CANARY-nt")
    w.vault.register_dataset("nt_twin", DatasetRole.DEV, public={"test_X.csv": public}, private={})
    w.labels["nt"] = [r[-1] for r in rows]
    w.kernel.declare_truth_copy("nt", "nt", "test_X.csv", "leak", declarer="task-registration")
    return w


def _evaluate_via(w: World, claim, arm: str, seed: int, test_ds: str, test_file: str) -> str:
    """World.evaluate, except the program's test input is the same bytes registered as `test_ds/test_file`."""
    ref = claim.subject if arm == "subject" else claim.comparator
    preds = w.run(ref.hash, "train", "nt", seed)
    assert preds is not None
    raw = w.vault.read("nt", "test_y.csv", purpose="evaluate", claim_hash=claim.claim_hash(), arm=arm, seed=seed)
    labels = [ln.split(",")[0] for ln in raw.decode().splitlines()[1:]]
    run = Run(harness_id="kernel-world", harness_hash="world", task_id="nt", seed=seed)
    w.store.add_run(run)
    rec = ExecRecord(
        claim_hash=claim.claim_hash(), arm=arm, artifact_hash=ref.hash,
        inputs={"train": InputRef(dataset_id="train", file="train.csv",
                                  blob=w.vault.dataset("train").files["train.csv"].blob),
                "test": InputRef(dataset_id=test_ds, file=test_file,
                                 blob=w.vault.dataset(test_ds).files[test_file].blob)},
        target="nt", label_file="test_y.csv", seed=seed, env={"sandbox": "local (trusted test code)"},
        limits={"timeout_s": 120.0}, preds_ref=w.store.blobs.put_json(preds), metric="accuracy",
        score=M.score("accuracy", labels, preds), run_id=run.id)
    return w.kernel.record_evaluation(rec)


@pytest.mark.parametrize(("test_ds", "test_file"), [("nt_twin", "test_X.csv"), ("nt", "copy.csv")])
def test_a_declared_truth_copy_given_under_another_name_is_still_checked(tmp_path, test_ds, test_file):
    """RT-S4-2 (high, inferred). checks.truth_copies keys declarations by (dataset id, file NAME) and _noninterference
    matches the record's InputRef by that key. The same bytes registered under another dataset id (a public twin) or
    another file name pass K8 (registered bytes) and K6 (no extra column, so no public-labels finding), and K3 then
    runs no non-interference replay: the 2% stealth reader certifies. Fails today. Fix: match declared copies by blob
    hash (every public file whose blob equals a declared file's blob carries the copy), in _noninterference and in
    declare_truth_copy's idempotence key."""
    w = _nt_world(tmp_path, seed=43)
    try:
        c = w.claim(w.artifact("stealth", code=STEALTH), w.artifact("baseline", "baseline"), delta=0.2,
                    targets=("nt",), eps_max=0.5, beta=0.25)
        w.kernel.preregister(c)
        ids = [_evaluate_via(w, c, arm, s, test_ds, test_file) for s in c.analysis_plan.seeds
               for arm in ("subject", "comparator")]
        cert = w.kernel.certify(c, ids)
        assert cert.label == Label.REJECTED and Code.K3_NONINTERFERENCE in cert.checks["K3"].codes, cert.failed()
    finally:
        w.close()


# ------------------------------------------------------------------ RT-S4-10: noise floor not bound to the claim
# owner decision 2026-10-28 (stage-c4): open findings are strict xfails until fixed; the attack below is unchanged
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="open red-team finding RT-S4-10 (medium, needs owner "
                   "decision): a noise-floor record is not bound to the claim's program, metric or data, so K1 accepts "
                   "any stored floor")
def test_a_noise_floor_record_must_be_bound_to_the_claims_program(tmp_path):
    """RT-S4-10 (medium, inferred; NEEDS OWNER DECISION). A chh-noise-floor/1 record holds only numbers: nothing names
    the program, metric or data it was measured on, so a claim may cite ANY stored floor (another, noisier
    candidate's) and K1 accepts a reproduce_tolerance > 0, which switches K3 from identical predictions to a score
    window. Fails today (and would also fail the research loop's current records): the fix adds the subject hash,
    metric and dev dataset to the record and has K1 require they match the claim."""
    w = World(tmp_path / "w", seed=44, n_train=60, n_test=60)
    try:
        floor = T.NoiseFloor(0.8, tuple(0.8 + 0.001 * i for i in range(1, 10)),
                             tuple(0.8 - 0.0005 * i for i in range(1, 10)), (), ())
        got = T.claim_tolerance(EvidenceClass.DATA, floor).value
        assert got is not None and got > 0
        ref = w.kernel.record_noise_floor(floor)  # measured for some other program: nothing in it says which
        c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), n_boot=1000, reproduce_tolerance=got,
                    tolerance_evidence=ref)
        cx = K.Context(claim=c, claim_hash=c.claim_hash(), store=w.store, vault=w.vault, ledger=w.kernel.ledger,
                       config=KernelConfig(), replayer=None, evidence_ids=())
        assert Code.K1_TOLERANCE in K.k1_well_formed(cx).codes
    finally:
        w.close()
