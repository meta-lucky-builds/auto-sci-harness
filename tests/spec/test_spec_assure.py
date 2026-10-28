"""The honest corpus (GOAL §16.6, invariant 20), its look ledger, and resweep (GOAL §16.10).

A small corpus (5 spec-world pairs; pairs 3 and 4 fall in the sealed half) is built once per module by the real
builder (scripts/honest_corpus.py) into tmp_path; the resweep tests use their own spec-world stores.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import gen
import pytest
from history import Artifact, Certify, ClaimSpec, Dataset, Prereg, Read
from real_kernel import RealWorld

from custom_harness.assure import corpus, looks, resweep
from custom_harness.errors import InfraError
from custom_harness.kernel import Kernel
from custom_harness.store import EvidenceStore

ROOT = Path(__file__).resolve().parents[2]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(f"_assure_{name}", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


hc = _script("honest_corpus")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("CHH_SPEC_FAST", "1")
    try:
        root = tmp_path_factory.mktemp("corpus")
        man_path = root / "manifest.json"
        man = hc.build(root / "data", 5, 0, False, man_path)
        yield root / "data", man_path, man
    finally:
        mp.undo()


# ------------------------------------------------------------------ look ledger
def test_a_look_needs_a_name_and_a_reason_and_an_unledgered_receipt_is_refused(tmp_path):
    led = tmp_path / "looks.jsonl"
    with pytest.raises(looks.LookError):
        looks.open_look(led, "honest-corpus", ["a"], by="", reason="x")
    look = looks.open_look(led, "honest-corpus", ["a", "b"], by="owner", reason="milestone")
    looks.require(look)  # recorded: fine
    with pytest.raises(looks.LookError, match="not cover"):
        looks.require(look, ["a", "c"])
    forged = looks.Look(led, "honest-corpus", ("a",), "0" * 64)
    with pytest.raises(looks.LookError, match="unledgered"):
        looks.require(forged)
    looks.record_result(look, ran=True)
    raw = led.read_bytes()  # byte-exact edits: a text-mode rewrite could change line endings on Windows

    def edit(old: bytes, new: bytes) -> None:
        first, rest = raw.split(b"\n", 1)
        assert old in first
        led.write_bytes(first.replace(old, new) + b"\n" + rest)
    edit(b"milestone", b"milestone")
    looks.require(look)  # control: the same rewrite without a change keeps the chain intact
    edit(b"milestone", b"nothing")
    with pytest.raises(looks.LookError, match="not intact"):
        looks.require(look)  # an edited earlier look breaks the chain: every later read fails closed


# ------------------------------------------------------------------ the corpus
def test_corpus_pairs_share_a_half_and_sealed_labels_are_not_in_the_manifest(built):
    _root, _man_path, man = built
    items = man["items"]
    assert len(items) == 10
    halves = {e["half"] for e in items}
    assert halves == {"dev", "sealed"}
    by_pair: dict[str, set[str]] = {}
    for e in items:
        by_pair.setdefault(e["pair"], set()).add(e["half"])
        assert e["half"] == looks.half_of(corpus.SET_NAME, e["pair"])
        if e["half"] == "sealed":
            assert set(e) == {"id", "pair", "half", "digest", "commitment"}  # no role, no label, no recipe
        else:
            assert e["expected"]["role"] in ("honest", "fault")
    assert all(len(h) == 1 for h in by_pair.values())
    assert all(e["id"].startswith("hc-") and e["pair"].startswith("hp-") for e in items)


def test_dev_half_rechecks_honest_certified_and_faults_rejected(built):
    root, man_path, _man = built
    items = corpus.dev_items(root, corpus.manifest(man_path))
    res = corpus.check_all(items)
    s = corpus.summary(res)
    assert s["honest"] >= 2 and s["faults"] >= 2
    assert s["uncheckable"] == s["honest_flips"] == s["fault_escapes"] == s["label_changes"] == []
    faults = {it.expected["fault"] for it in items if it.expected["role"] == "fault"}
    assert len(faults) >= 2  # several planted-fault kinds, not one
    assert all(it.expected["model_label"] == it.expected["label"] for it in items)  # model and Kernel agreed


def test_sealed_half_reads_fail_closed_without_a_ledgered_look(built, tmp_path):
    root, _man_path, man = built
    led = tmp_path / "looks.jsonl"
    other = looks.open_look(led, "museum", ["x"], by="owner", reason="r")
    with pytest.raises(looks.LookError):
        corpus.sealed_items(other, root, man)  # a look at another set does not open this one
    partial = looks.open_look(led, corpus.SET_NAME, [man["items"][0]["id"]], by="owner", reason="r")
    with pytest.raises(looks.LookError):
        corpus.sealed_items(partial, root, man)  # a look must cover every sealed item read
    look = corpus.open_sealed("owner", "test", ledger=led, man=man)
    items = corpus.sealed_items(look, root, man)
    assert items and all(it.half == "sealed" for it in items)
    assert json.loads(led.read_text(encoding="utf-8").splitlines()[-1])["purpose"] == "check"


def test_a_tampered_sealed_expected_record_is_refused(built, tmp_path):
    root, _man_path, man = built
    sealed = next(e for e in man["items"] if e["half"] == "sealed")
    path = root / man["version"] / "sealed" / sealed["id"] / "expected.json"
    original = path.read_bytes()  # restored byte-exact: the corpus is shared by the module's other tests
    try:
        doc = json.loads(original)
        look = corpus.open_sealed("owner", "test", ledger=tmp_path / "l.jsonl", man=man)
        path.write_bytes(json.dumps(doc).encode("utf-8"))  # control: the same record re-serialized, label unchanged
        assert corpus.sealed_items(look, root, man)
        doc["label"] = "REJECTED" if doc["label"] != "REJECTED" else "CERTIFIED-INTERNAL"
        path.write_bytes(json.dumps(doc).encode("utf-8"))
        with pytest.raises(corpus.CorpusError, match="committed hash"):
            corpus.sealed_items(look, root, man)
    finally:
        path.write_bytes(original)
    assert path.read_bytes() == original


def test_the_build_is_deterministic(built, tmp_path, monkeypatch):
    _root, _man_path, man = built
    monkeypatch.setenv("CHH_SPEC_FAST", "1")
    again = hc.build(tmp_path / "data", 2, 0, False, tmp_path / "m.json")
    first = {e["id"]: e["digest"] for e in man["items"]}
    assert again["items"] and all(first[e["id"]] == e["digest"] for e in again["items"])


# ------------------------------------------------------------------ resweep
EX = frozenset(range(40))


def _store(tmp_path: Path, alpha: float = 0.05, seeds: tuple[int, ...] = gen.SEEDS, later: list | None = None):
    """One honest spec-world certificate (CERTIFIED-INTERNAL unless the plan breaks a rule); returns its root."""
    world = RealWorld(tmp_path / "w")
    c = ClaimSpec(key="c", kind="improvement", subject="m1", comparator="b1", development=(), targets=("t",),
                  seeds=seeds, alpha=alpha, n_comparisons=1, min_effect=0.1, strong=True)
    try:
        for ev in [Dataset("t", "test", "features", EX, "A"), Artifact("m1", "method"), Artifact("m2", "method"),
                   Artifact("b1", "baseline"), Prereg(c), *gen.eval_reads(c), Certify(c, gen.honest_records(c)),
                   *(later or [])]:
            world.apply(ev)
        label = world.last_cert.label.value
    finally:
        world.close()
    return tmp_path / "w" / "store", label


def _classify(store_root: Path, rule: resweep.Rule, replayer=corpus.StoredPredictionsReplayer):
    store = EvidenceStore(store_root)
    try:
        [h] = resweep.certificates(store)
        return resweep.classify(store, h, rule, replayer)
    finally:
        store.close()


def test_resweep_valid_and_stale_new_data(tmp_path):
    root, label = _store(tmp_path / "a")
    assert label == "CERTIFIED-INTERNAL"
    r = _classify(root, resweep.Rule("today"))
    assert (r.status, r.grade) == ("VALID", "none"), r
    root2, _ = _store(tmp_path / "b", later=[Read("t", "develop")])  # a later event on the target
    r2 = _classify(root2, resweep.Rule("today"))
    assert (r2.status, r2.grade) == ("STALE-new-data", "flag"), r2


def test_resweep_stale_under_a_new_rule_is_graded_by_its_effect(tmp_path):
    root, _ = _store(tmp_path / "a", alpha=0.2)
    suspended = _classify(root, resweep.Rule("alpha01", {"max_alpha": 0.1}))
    assert (suspended.status, suspended.grade) == ("STALE-under-new-rule", "suspend"), suspended
    assert suspended.new and suspended.new["label"] == "REJECTED"
    same = _classify(root, resweep.Rule("alpha03", {"max_alpha": 0.3}))
    assert (same.status, same.grade) == ("VALID", "none"), same
    rej, label = _store(tmp_path / "b", seeds=(0, 1))
    assert label == "REJECTED"  # 2 seeds < min_seeds 3
    up = _classify(rej, resweep.Rule("seeds2", {"min_seeds": 2}))
    assert (up.status, up.grade) == ("STALE-under-new-rule", "no-upgrade"), up  # never granted by a resweep


def test_resweep_integrity_failure_and_uncheckable(tmp_path):
    root, _ = _store(tmp_path / "a")
    store = EvidenceStore(root)
    try:
        (body,) = store._db.execute("SELECT body FROM ledger WHERE seq = 2").fetchone()
        doc = json.loads(body)
        doc["tampered"] = True  # a real content change (whitespace alone re-canonicalizes to the same hash)
        store._db.execute("UPDATE ledger SET body = ? WHERE seq = 2", (json.dumps(doc, sort_keys=True),))
        store._db.commit()
    finally:
        store.close()
    r = _classify(root, resweep.Rule("today"))
    assert r.status == "INTEGRITY-FAILURE", r

    class Down:
        def replay(self, record, vault):
            raise InfraError("docker is down")

    root2, _ = _store(tmp_path / "b")
    r2 = _classify(root2, resweep.Rule("today"), replayer=Down)
    assert (r2.status, r2.grade) == ("UNCHECKABLE", "hold"), r2


def test_apply_needs_a_passing_screen_and_clawback_is_reversible_with_releases(built, tmp_path):
    root_c, _man_path, man = built
    led = tmp_path / "looks.jsonl"
    store_root, _ = _store(tmp_path / "s", alpha=0.2)
    tight = resweep.Rule("alpha01", {"max_alpha": 0.1})
    store = EvidenceStore(store_root)
    try:
        with pytest.raises(resweep.ResweepError, match="no passing screen"):
            resweep.apply(store, tight, corpus.StoredPredictionsReplayer, ledger=led)
        # a rule that flips honest corpus items does not pass its screen, so it can never be applied
        bad = resweep.Rule("seeds4", {"min_seeds": 4})
        out = resweep.screen(bad, "owner", "test", ledger=led, root=root_c, man=man)
        assert not out["passed"] and out["honest_flips"] >= 1 and out["honest"] >= 1, out
        with pytest.raises(resweep.ResweepError):
            resweep.apply(store, bad, corpus.StoredPredictionsReplayer, ledger=led)
        out = resweep.screen(tight, "owner", "test", ledger=led, root=root_c, man=man)
        assert out["passed"] and out["honest_flips"] == 0 and out["fault_escapes"] == 0, out
        assert out["earlier_screen_looks"] == 1  # wear of the sealed half is counted
        rep = resweep.apply(store, tight, corpus.StoredPredictionsReplayer, ledger=led)
        assert rep["by_grade"]["suspend"] == 1 and rep["vds"]["clawed_back"] > 0 and rep["releases"] == 1
        assert any(e.body["note"] == "resweep.alpha01" and e.body["alphabet"] == "count"
                   for e in Kernel(store).releases("t"))
        path = resweep.clawback_path(store)
        assert set(resweep.current_grades(path).values()) == {"suspend"}
        kernel = Kernel(store)
        assert kernel.ledger.entries("retraction") == []  # a clawback is never a (permanent) Kernel retraction
        rollback = resweep.Rule("rollback")  # the rule is withdrawn: the certificate's own rules again
        assert resweep.screen(rollback, "owner", "test", ledger=led, root=root_c, man=man)["passed"]
        rep2 = resweep.apply(store, rollback, corpus.StoredPredictionsReplayer, ledger=led)
        assert rep2["by_grade"]["none"] == 1 and set(resweep.current_grades(path).values()) == {"none"}
        assert any(json.loads(x)["kind"] == "restore" for x in path.read_text(encoding="utf-8").splitlines())
        assert rep2["vds"]["clawed_back"] == 0 and rep2["vds"]["provisional"] > 0 and rep2["vds"]["seasoned"] == 0
        loose = resweep.Rule("alpha03", {"max_alpha": 0.3})  # survives a real rule change: credit is seasoned
        assert resweep.screen(loose, "owner", "test", ledger=led, root=root_c, man=man)["passed"]
        rep3 = resweep.apply(store, loose, corpus.StoredPredictionsReplayer, ledger=led)
        assert rep3["vds"]["seasoned"] > 0 and rep3["vds"]["provisional"] == 0
        assert looks.check_chain(path) == [] and looks.check_chain(led) == []
    finally:
        store.close()


def test_a_screen_passed_for_one_rule_does_not_license_another(built, tmp_path):
    root_c, _man_path, man = built
    led = tmp_path / "looks.jsonl"
    a = resweep.Rule("alpha03", {"max_alpha": 0.3})
    assert resweep.screen(a, "owner", "t", ledger=led, root=root_c, man=man)["passed"]
    assert resweep.screen_passed(a, led)
    assert not resweep.screen_passed(resweep.Rule("alpha03", {"max_alpha": 0.31}), led)
    lines = led.read_text(encoding="utf-8").splitlines()
    led.write_text("\n".join([lines[0].replace("owner", "other"), *lines[1:]]) + "\n", encoding="utf-8")
    assert not resweep.screen_passed(a, led)  # an edited ledger licenses nothing


def test_clopper_pearson_upper_bound():
    assert resweep.clopper_pearson_upper(0, 60) == pytest.approx(1 - 0.05 ** (1 / 60))
    assert 0.06 < resweep.clopper_pearson_upper(1, 60) < 0.09  # exact one-sided 95%: 0.0771
