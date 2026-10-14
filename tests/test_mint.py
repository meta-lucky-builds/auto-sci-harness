"""GOAL §16.1 COUPLED-PRNG and §16.2 freeze-then-mint (research/suite.py).

COUPLED-PRNG: every synthetic generator used to draw features, labels and the split from ONE random.Random stream
seeded with the small integers published in suite.SYNTHETIC. Anyone who could read the repo regenerated every
held-out label (verified 2026-10-07: 12/12 synthetic tasks of the live suites m2 and m2-v1 matched exactly, by
scratchpad/build16/mint_regen_check.py, read-only). The fix: independent HMAC-keyed streams per purpose under a
master key that is fresh entropy (or a draw() result), never a constant in the code.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from pathlib import Path

import pytest
import yaml

from custom_harness.research import suite as S


def _labels(p: Path) -> list[str]:
    return [r[0] for r in list(csv.reader(io.StringIO(p.read_text(encoding="utf-8"))))[1:]]


def _regenerate(spec: S.TaskSpecFiles) -> list[str]:
    """What write_task would put in private/test_y.csv for `spec` (test rows de-duplicated against train)."""
    ti = spec.header.index(spec.target)
    seen = {tuple(v for j, v in enumerate(r) if j != ti) for r in spec.train}
    return [r[ti] for r in spec.test if tuple(v for j, v in enumerate(r) if j != ti) not in seen]


def _attack(suite_dir: Path) -> dict[str, tuple[int, int]]:
    """The attacker: reads only the repo (suite.SYNTHETIC's generators and published seeds) and regenerates each
    synthetic task's held-out labels. Returns {task_id: (labels agreeing, held-out labels)}."""
    out = {}
    for gen, seed, n in S.SYNTHETIC:
        spec = gen(seed, n)
        guess = _regenerate(spec)
        truth = _labels(suite_dir / spec.task_id / "private" / "test_y.csv")
        out[spec.task_id] = (sum(a == b for a, b in zip(guess, truth, strict=True)) if len(guess) == len(truth) else 0,
                             len(truth))
    return out


def test_heldout_labels_cannot_be_regenerated_from_repo_code_and_published_seeds(tmp_path):
    build_suite_path = S.build_suite(tmp_path / "s", real_data=None)
    res = _attack(build_suite_path.parent)
    assert len(res) == 12 and all(n > 0 for _, n in res.values())  # the attack ran on every synthetic task
    exact = sorted(t for t, (ok, n) in res.items() if ok == n)
    assert exact == [], f"held-out labels regenerated exactly from repo code + published seeds: {exact}"


def test_the_regeneration_attack_works_when_the_key_is_known(tmp_path):
    """Positive control for the test above: the same attacker, given the master key, regenerates every held-out
    label exactly. So the refusal above comes from the key's secrecy, not from a broken attacker."""
    master = b"k" * 32
    path = S.build_suite(tmp_path / "s", real_data=None, key=master)
    res = {}
    for gen, seed, n in S.SYNTHETIC:
        spec = gen(seed, n, key=S.task_key(master, gen(seed, 8).task_id))
        truth = _labels(path.parent / spec.task_id / "private" / "test_y.csv")
        res[spec.task_id] = _regenerate(spec) == truth
    assert len(res) == 12 and all(res.values()), res


def test_labels_draw_from_their_own_stream(monkeypatch):
    """COUPLED-PRNG structure: replacing ONLY the labels stream leaves every feature value and the split unchanged
    (with one shared stream, any change to the label draws shifted every later feature and the split)."""
    key = b"m" * 32
    for gen, seed, _n in S.SYNTHETIC:
        base = gen(seed, 200, key=key)
        real_stream = S.stream

        def swapped(master, label, _real=real_stream):
            return _real(b"other-labels-key" * 2 if label == "labels" else master, label)

        monkeypatch.setattr(S, "stream", swapped)
        alt = gen(seed, 200, key=key)
        monkeypatch.setattr(S, "stream", real_stream)
        ti = base.header.index(base.target)

        def feats(rows, ti=ti):
            return [[v for j, v in enumerate(r) if j != ti] for r in rows]

        assert [r[ti] for r in base.train + base.test] != [r[ti] for r in alt.train + alt.test], base.task_id
        if base.family in ("rings", "spirals", "imbalanced"):
            continue  # class-conditional families: the class picks the feature distribution, so features follow y
        assert feats(base.train) == feats(alt.train) and feats(base.test) == feats(alt.test), base.task_id


def _untemper(y: int) -> int:
    y ^= y >> 18
    y ^= (y << 15) & 0xEFC60000
    t = y
    for _ in range(5):
        t = y ^ ((t << 7) & 0x9D2C5680)
    y = t
    t = y
    for _ in range(3):
        t = y ^ (t >> 11)
    return t & 0xFFFFFFFF


def _clone_attack(rng: random.Random) -> tuple[bool, float]:
    """evalA's toy (scratchpad/evalA/mint_prng.py): features are 32-bit words of the stream, labels add noise from the
    same stream; the attacker sees features only, clones the Mersenne-Twister state, predicts the noise."""
    feats = [rng.getrandbits(32) for _ in range(700)]
    labels = [1 if (feats[i] % 1000) / 1000 + (rng.random() - 0.5) * 0.6 > 0.5 else 0 for i in range(200)]
    clone = random.Random()
    clone.setstate((3, tuple([_untemper(v) for v in feats[:624]] + [624]), None))
    cloned = all(clone.getrandbits(32) == v for v in feats[624:])
    pred = [1 if (feats[i] % 1000) / 1000 + (clone.random() - 0.5) * 0.6 > 0.5 else 0 for i in range(200)]
    return cloned, sum(a == b for a, b in zip(pred, labels, strict=True)) / len(labels)


def test_mt_state_clone_attack_kills_mersenne_twister_but_not_keyed_streams():
    from custom_harness.commitment import KeyedStream

    cloned, acc = _clone_attack(random.Random(int(hashlib.sha256(b"beacon").hexdigest(), 16)))
    assert cloned and acc == 1.0  # the attack happened: the stream was cloned and every label predicted
    cloned, acc = _clone_attack(KeyedStream(b"s" * 32))
    assert not cloned and acc < 0.9
    with pytest.raises(TypeError):
        KeyedStream(b"s" * 32).getstate()
    with pytest.raises(TypeError):
        KeyedStream(b"s" * 32).seed(1)


def test_synthetic_generation_is_deterministic_under_a_key_and_fresh_without_one():
    a, b = S.rings(1, 300, key=b"x" * 32), S.rings(1, 300, key=b"x" * 32)
    assert a.train == b.train and a.test == b.test
    c, d = S.rings(1, 300), S.rings(1, 300)
    assert c.test != d.test and c.task_id == "rings-s1"


def test_suite_marks_fixed_public_datasets_as_retrospective_targets(tmp_path):
    from custom_harness.commitment import secret_leak_scan
    from test_suite_builder import _fake_real_data

    master = bytes(range(32))
    doc = yaml.safe_load(S.build_suite(tmp_path / "s", real_data=_fake_real_data(), key=master).read_text())
    retro = {t["id"]: t["retrospective_target"] for t in doc["tasks"]}
    assert {k for k, v in retro.items() if v} == {f"{n}-real" for n in S.REAL} | set(S.REPAIRS)
    assert all(t["minted"] is False for t in doc["tasks"])  # built before any freeze: not minted (§16.2)
    assert doc["generator"]["key_commitment"] == hashlib.sha256(master).hexdigest()
    assert secret_leak_scan(master, [tmp_path / "s"]) == []  # the master key itself is written nowhere


# ---------------------------------------------------------------- freeze, then mint (§16.2)
FROZEN = hashlib.sha256(b"frozen candidate program").hexdigest()


def _anchored_ledger(tmp_path, *, anchor_after: bool = True, body_hash: str = FROZEN):
    from custom_harness.kernel.ledger import Ledger, write_anchor
    from custom_harness.store import EvidenceStore

    store = EvidenceStore(tmp_path / "store")
    led = Ledger(store)
    led.append("register_artifact", {"hash": "a" * 64, "kind": "other"})
    anchor = tmp_path / "anchors" / "anchor.jsonl"
    if not anchor_after:
        write_anchor(led, anchor, tmp_path / "store")
    led.append("register_artifact", {"hash": body_hash, "kind": "frozen_program"})
    if anchor_after:
        write_anchor(led, anchor, tmp_path / "store")
    return store, led, anchor


def test_mint_refuses_a_hash_that_is_not_anchored(tmp_path):
    store, led, anchor = _anchored_ledger(tmp_path, anchor_after=False)  # recorded, but after the last anchor
    with pytest.raises(S.MintRefused, match="not anchored"):
        S.mint_heldout("xor", frozen_hash=FROZEN, beacon="drand:123", ledger_entries=led.entries(),
                       anchor_file=anchor, out=tmp_path / "m", n_train=50, n_test=200)
    with pytest.raises(S.MintRefused, match="no ledger entry"):
        S.mint_heldout("xor", frozen_hash="b" * 64, beacon="drand:123", ledger_entries=led.entries(),
                       anchor_file=anchor, out=tmp_path / "m", n_train=50, n_test=200)
    store.close()
    assert not (tmp_path / "m").exists()  # nothing minted


def test_mint_refuses_a_rewritten_ledger(tmp_path):
    store, led, anchor = _anchored_ledger(tmp_path)
    entries = led.entries()
    forged = [*entries[:-1], type(entries[-1])(entries[-1].seq, entries[-1].kind, entries[-1].body, entries[-1].prev,
                                               "f" * 64, entries[-1].at)]
    with pytest.raises(S.MintRefused, match="ledger"):
        S.mint_heldout("xor", frozen_hash=FROZEN, beacon="drand:123", ledger_entries=forged, anchor_file=anchor,
                       out=tmp_path / "m", n_train=50, n_test=200)
    store.close()


def test_mint_after_anchor_writes_large_fresh_truth_that_anyone_can_rerun(tmp_path):
    store, led, anchor = _anchored_ledger(tmp_path)
    d = S.mint_heldout("xor", frozen_hash=FROZEN, beacon="drand:123", ledger_entries=led.entries(),
                       anchor_file=anchor, out=tmp_path / "m", n_train=50, n_test=2000)
    y = _labels(d / "private" / "test_y.csv")
    assert 1990 <= len(y) <= 2000 and len((d / "public" / "train.csv").read_text().splitlines()) == 51
    rec = json.loads((d / "private" / "mint.json").read_text())  # truth-equivalent (beacon + hash): private only
    assert rec["purpose"] == "heldout_mint" and rec["frozen_hash"] == FROZEN and rec["retrospective_target"] is False
    assert rec["minted"] is True and rec["anchor"]["length"] == len(led.entries())
    assert not list((d / "public").glob("mint*"))
    assert S.verify_mint(d) == []  # re-run from the record alone: same truth
    (d / "private" / "test_y.csv").write_text("label\n" + "\n".join("0" for _ in y) + "\n")
    assert S.verify_mint(d) != []  # and a tampered truth is caught
    other = S.mint_heldout("xor", frozen_hash=FROZEN, beacon="drand:124", ledger_entries=led.entries(),
                           anchor_file=anchor, out=tmp_path / "m2", n_train=50, n_test=2000)
    assert _labels(other / "private" / "test_y.csv") != y
    store.close()


def test_mint_defaults_are_large_and_marked_uncalibrated():
    src = Path(S.__file__).read_text(encoding="utf-8")
    for name in ("MINT_TEST_ROWS", "MINT_TRAIN_ROWS", "DELTA_FLOOR"):
        line = next(ln for ln in src.splitlines() if ln.startswith(f"{name} ="))
        assert "UNCALIBRATED (owner decision, GOAL §13.8)" in line, name
    assert S.MINT_TEST_ROWS >= 20_000 and S.MINT_TRAIN_ROWS < S.MINT_TEST_ROWS and S.DELTA_FLOOR > 0
