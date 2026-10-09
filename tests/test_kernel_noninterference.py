"""GOAL §16.5.4 wired into K3 (invariant 18): when a record's inputs hold a DECLARED copy of its target's truth, K3
requires the non-interference replay (verify/noninterference.py): flipped and constant worlds, decisions compared
with the stored production output. Museum-style planted cases: a defeat device and a 2% stealth leak, both reading an
ENCODED truth column the K9 column scan cannot see, must be rejected; the honest program must still certify."""

from __future__ import annotations

import random

import pytest

from custom_harness.kernel import DatasetRole, KernelError, Label
from custom_harness.kernel.reasons import Code
from kernel_world import KNN, World, _csv, _sample

_DECODE = 'L = [r[th.index("leak")][1:] for r in trows] if "leak" in th else None\n'
DEVICES = {
    "honest": KNN.format(k=15, pre="", post=""),
    # 2% stealth leak: corrects at most 2% of the rows (the first ones it gets wrong) from the decoded truth
    "stealth2": KNN.format(k=15, pre="", post=_DECODE + "if L:\n    wrong = [i for i, (p, l) in enumerate(zip(preds, "
                                                         "L)) if p != l][:len(L) // 50]\n    for i in wrong: "
                                                         "preds[i] = L[i]"),
    # defeat device: cheats only when the copy looks real (agrees with its own predictions)
    "defeat": KNN.format(k=15, pre="", post=_DECODE + "if L and sum(p == l for p, l in zip(preds, L)) > 0.7 * "
                                                       "len(L):\n    preds = [l if i % 10 == 0 else p for i, (p, l) "
                                                       "in enumerate(zip(preds, L))]"),
}


def _world(tmp_path, name: str, declare: bool = True):
    w = World(tmp_path / name, seed=31)
    rng = random.Random(7)
    rows = [_sample(rng) for _ in range(240)]
    header = ["x1", "x2", "n1", "leak"]  # the truth, encoded ("v" + label): K9's exact column scan misses it
    w.vault.register_dataset("nt", DatasetRole.TEST,
                             public={"test_X.csv": _csv([header, *[[*r[:-1], "v" + r[-1]] for r in rows]])},
                             private={"test_y.csv": _csv([["label"], *[[r[-1]] for r in rows]])},
                             canary="CHH-CANARY-nt")
    w.labels["nt"] = [r[-1] for r in rows]
    if declare:
        w.kernel.declare_truth_copy("nt", "nt", "test_X.csv", "leak", declarer="task-registration")
    return w


def _certify(w: World, subject: str):
    c = w.claim(w.artifact(subject, code=DEVICES[subject]), w.artifact("baseline", "baseline"), delta=0.2,
                targets=("nt",), eps_max=0.5, beta=0.25)
    w.kernel.preregister(c)
    return w.kernel.certify(c, w.evaluate_all(c))


@pytest.mark.parametrize("device", ["stealth2", "defeat"])
def test_planted_truth_readers_are_rejected_by_k3(tmp_path, device):
    w = _world(tmp_path, device)
    try:
        honest_acc = sum(p == y for p, y in zip(w.run(w.artifact("honest", code=DEVICES["honest"]).hash, "train",
                                                       "nt", 0) or [], w.labels["nt"], strict=True))
        cheat_acc = sum(p == y for p, y in zip(w.run(w.artifact(device, code=DEVICES[device]).hash, "train", "nt",
                                                     0) or [], w.labels["nt"], strict=True))
        assert cheat_acc > honest_acc  # the attack happened: production read the truth and gained from it
        cert = _certify(w, device)
        assert cert.label == Label.REJECTED and cert.failed() == ["K3"], cert.failed()
        assert Code.K3_NONINTERFERENCE in cert.checks["K3"].codes
        assert any("decisions_changed" in r for r in cert.checks["K3"].reasons)
    finally:
        w.close()


def test_honest_program_passes_non_interference(tmp_path):
    w = _world(tmp_path, "honest")
    try:
        cert = _certify(w, "honest")
        assert cert.label == Label.CERTIFIED_INTERNAL, {k: v.reasons for k, v in cert.checks.items() if v.reasons}
        assert any("non-interference" in o and "PASS (exact" in o for o in cert.checks["K3"].observations)
    finally:
        w.close()


def test_undeclared_copy_is_not_checked_and_a_replayer_without_input_swap_fails_closed(tmp_path):
    w = _world(tmp_path, "undeclared", declare=False)
    try:  # stated limit: only DECLARED copies are perturbed (the device certifies when nobody declared the copy)
        assert _certify(w, "stealth2").label == Label.CERTIFIED_INTERNAL
        w.kernel.declare_truth_copy("nt", "nt", "test_X.csv", "leak", declarer="task-registration")

        class OnlyReplay:  # a replayer that cannot run the counterfactual worlds
            def replay(self, record, vault):
                return w.replayer.replay(record, vault)
        w.kernel.replayer = OnlyReplay()
        c = w.claim(w.artifact("honest", code=DEVICES["honest"]), w.artifact("baseline", "baseline"), delta=0.25,
                    targets=("nt",), eps_max=0.5, beta=0.25)
        w.kernel.preregister(c)
        cert = w.kernel.certify(c, w.evaluate_all(c))
        assert cert.failed() == ["K3"] and any("cannot replace inputs" in r for r in cert.checks["K3"].reasons)
        with pytest.raises(KernelError, match="PUBLIC file"):
            w.kernel.declare_truth_copy("nt", "nt", "test_y.csv", "label", declarer="x")
        with pytest.raises(KernelError, match="no column"):
            w.kernel.declare_truth_copy("nt", "nt", "test_X.csv", "nope", declarer="x")
    finally:
        w.close()
