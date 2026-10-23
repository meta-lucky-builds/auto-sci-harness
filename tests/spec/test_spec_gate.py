"""Tests of the gate itself (GOAL §16.6 derived Kernel test list, §16.7 two-speed rule, findings registry)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_gate_{name}", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


check, governance, museum = _load("check"), _load("governance"), _load("museum")


def _tree(tmp_path: Path, *subs: str) -> Path:
    for sub in subs:
        shutil.copytree(ROOT / sub, tmp_path / sub, ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


def test_kernel_test_list_is_every_tests_test_kernel_file():
    derived = check.kernel_tests()
    on_disk = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_kernel_*.py"))
    assert derived == on_disk and len(derived) >= 8
    assert "tests/test_kernel_units.py" in check.kernel_step()


def test_a_new_kernel_test_file_joins_the_gate_and_a_conflict_marker_is_refused(tmp_path):
    root = _tree(tmp_path, "tests")
    (root / "tests" / "test_kernel_new.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    assert "tests/test_kernel_new.py" in check.kernel_tests(root)
    (root / "tests" / "test_kernel_new.py").write_text("<<<<<<< HEAD\ndef test_x():\n    pass\n", encoding="utf-8")
    with pytest.raises(check.GateError, match="merge-conflict marker"):
        check.kernel_tests(root)
    (root / "tests" / "test_kernel_new.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(check.GateError, match="no test function"):
        check.kernel_tests(root)


def test_conflict_markers_are_found_anywhere_in_code(tmp_path):
    root = _tree(tmp_path, "scripts")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    assert check.conflict_markers(root) == []
    (root / "src" / "m.py").write_text("a = 1\n=======\nb = 2\n", encoding="utf-8")
    assert check.conflict_markers(root) == ["src/m.py"]


def test_gate_self_test_goes_red_on_a_planted_failing_kernel_test():
    assert check.self_test() == 0  # self_test returns 0 only if the planted failure made the Kernel step fail


def _gov_tree(tmp_path: Path) -> Path:
    root = _tree(tmp_path, "src/custom_harness/kernel")
    shutil.copy2(ROOT / "GOAL.md", root / "GOAL.md")
    (root / "redteam").mkdir()
    return root


def _record(root: Path, **rec: object) -> None:
    rec.setdefault("fingerprint", governance.fingerprint(root))
    (root / governance.RECORDS).write_text(yaml.safe_dump({"changes": [rec]}), encoding="utf-8")


def test_two_speed_rule_core_change_needs_a_record_but_a_parameter_change_does_not(tmp_path):
    root = _gov_tree(tmp_path)
    _record(root, kind="baseline", approver="owner", recorded="2026-10-07")
    assert governance.problems(root) == []
    checks = root / "src/custom_harness/kernel/checks.py"
    text = checks.read_text(encoding="utf-8")
    checks.write_text(text.replace("min_seeds: int = 3", "min_seeds: int = 5"), encoding="utf-8")
    assert governance.problems(root) == []  # the parameter list changes on evidence
    checks.write_text(text.replace("if not 0 < plan.alpha <= cfg.max_alpha:", "if not 0 < plan.alpha:"),
                      encoding="utf-8")
    assert "with no record" in " ".join(governance.problems(root))  # a core change without a record fails
    goal = root / "GOAL.md"
    checks.write_text(text, encoding="utf-8")
    goal.write_text(goal.read_text(encoding="utf-8").replace("Every parser and gate fails closed.",
                                                             "Most parsers fail closed."), encoding="utf-8")
    assert "with no record" in " ".join(governance.problems(root))  # so does an edit of a §10 invariant


@pytest.mark.parametrize(("rec", "ok"), [
    ({"kind": "window", "proposed": "2026-10-01", "recorded": "2026-10-08", "approver": "owner"}, True),
    ({"kind": "window", "proposed": "2026-10-05", "recorded": "2026-10-08", "approver": "owner"}, False),
    ({"kind": "window", "recorded": "2026-10-08", "approver": "owner"}, False),
    ({"kind": "tighten", "recorded": "2026-10-08", "approver": "owner"}, True),
    ({"kind": "tighten", "recorded": "2026-10-08", "approver": " "}, False),
    ({"kind": "loosen", "recorded": "2026-10-08", "approver": "owner"}, False),
])
def test_two_speed_rule_cooling_off_and_kinds(tmp_path, rec, ok):
    root = _gov_tree(tmp_path)
    _record(root, **rec)
    assert (governance.problems(root, today=dt.date(2026, 10, 9)) == []) is ok


def test_the_committed_tree_satisfies_the_two_speed_rule():
    assert governance.problems(ROOT) == []


def test_findings_registry_is_consistent_and_a_dangling_test_id_fails(tmp_path):
    assert museum.check_registry(ROOT) == []
    root = _tree(tmp_path, "tests", "redteam")
    doc = yaml.safe_load((root / museum.REGISTRY).read_text(encoding="utf-8"))
    doc["findings"][0]["tests"] = ["tests/test_kernel_exploits.py::test_this_attack_test_was_deleted"]
    (root / museum.REGISTRY).write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert any("dangling test id" in p for p in museum.check_registry(root))


sealed = _load("sealed")


def test_validation_sets_split_into_stable_disjoint_halves():
    for name in ("claim-suite", "museum"):
        halves = sealed.split(name)
        assert set(halves["dev"]).isdisjoint(halves["sealed"])
        assert sorted(halves["dev"] + halves["sealed"]) == sealed.items(name)
        assert halves["dev"] and halves["sealed"]  # both halves exist
    assert len(sealed.items("claim-suite")) == 51
    # the honest corpus (scripts/honest_corpus.py): halves per PAIR, from the committed manifest. Until the v1 build
    # is run and its manifest committed, the set is empty, as before (stated in docs/assure.md)
    halves = sealed.split("honest-corpus")
    if not (ROOT / sealed.CORPUS_MANIFEST).exists():
        assert halves == {"dev": [], "sealed": []}
        return
    assert halves["dev"] and halves["sealed"] and set(halves["dev"]).isdisjoint(halves["sealed"])
    assert sorted(halves["dev"] + halves["sealed"]) == sealed.items("honest-corpus")
    man = json.loads((ROOT / sealed.CORPUS_MANIFEST).read_text(encoding="utf-8"))
    for e in man["items"]:
        assert e["half"] == sealed.half_of("honest-corpus", e["pair"])
        if e["half"] == "sealed":  # sealed labels are never committed: only a commitment hash
            assert "expected" not in e and set(e) == {"id", "pair", "half", "digest", "commitment"}


def test_every_sealed_look_is_ledgered_and_an_edited_look_breaks_the_chain(tmp_path):
    root = _tree(tmp_path, "tests", "redteam")
    (root / "redteam" / "sealed_looks.jsonl").unlink(missing_ok=True)
    assert sealed.look("museum", by="", reason="x", root=root, run=False) == 1  # anonymous looks are refused
    assert sealed.look("museum", by="owner", reason="milestone check", root=root, run=False) == 0
    lines = (root / sealed.LOOKS).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and '"kind": "look"' in lines[0] and sealed.check_chain(root) == []
    assert sorted(json.loads(lines[0])["items"]) == sealed.split("museum", root)["sealed"]
    (root / sealed.LOOKS).write_text(lines[0].replace("milestone check", "nothing to see") + "\n" + lines[1] + "\n",
                                     encoding="utf-8")
    assert any("chain broken" in p for p in sealed.check_chain(root))


def test_the_committed_look_ledger_is_intact():
    assert sealed.check_chain() == []


def _records(root: Path, *recs: dict) -> None:
    (root / governance.RECORDS).write_text(yaml.safe_dump({"changes": list(recs)}), encoding="utf-8")


def test_a_deviation_record_corrects_an_earlier_record_and_never_satisfies_the_gate(tmp_path):
    """Owner decision 2026-10-08: a record that misclassified a core change is corrected by an APPENDED `deviation`
    record (history is never rewritten). It must name the record it corrects (an earlier one), list the changes, say
    whether the 7-day cooling-off was observed (a bool) and name an approver. A deviation never stands in for the
    window/tighten record the CURRENT core needs (else it would be a way around the cooling-off)."""
    root = _gov_tree(tmp_path)
    fp = governance.fingerprint(root)
    old = {"fingerprint": "a" * 64, "kind": "tighten", "approver": "owner", "recorded": "2026-10-08"}
    cur = {"fingerprint": fp, "kind": "tighten", "approver": "owner", "recorded": "2026-10-08"}
    dev = {"fingerprint": "a" * 64, "kind": "deviation", "corrects": "a" * 64, "changes": ["STALE vs forged"],
           "cooling_off_observed": False, "basis": "GOAL §16.4/§16.6", "approver": "owner", "recorded": "2026-10-08"}
    _records(root, old, cur, dev)
    assert governance.problems(root) == []
    for bad in ({"corrects": "b" * 64}, {"changes": []}, {"cooling_off_observed": "no"}, {"approver": ""},
                {"basis": ""}):
        _records(root, old, cur, {**dev, **bad})
        assert governance.problems(root), bad
    _records(root, dev | {"corrects": "a" * 64}, old, cur)  # it must come AFTER the record it corrects
    assert governance.problems(root)
    _records(root, old, {**dev, "fingerprint": fp, "corrects": "a" * 64})  # no window/tighten for the current core
    assert "with no record" in " ".join(governance.problems(root))


_DELEGATED = {"kind": "delegated", "recorded": "2026-10-15", "approver": "orchestrator", "delegated_by": "owner",
              "basis": "GOAL §13.5", "changes": ["equivalence claims get TOST bounded-null certificates"],
              "cooling_off_observed": False}


@pytest.mark.parametrize(("change", "ok"), [
    ({}, True),
    ({"delegated_by": " "}, False), ({"basis": ""}, False), ({"changes": []}, False), ({"changes": [""]}, False),
    ({"cooling_off_observed": True}, False), ({"cooling_off_observed": "false"}, False), ({"approver": ""}, False),
])
def test_a_delegated_record_satisfies_the_gate_only_when_it_admits_the_skipped_cooling_off(tmp_path, change, ok):
    """s4-kernel (item 8): an owner-DELEGATED core change gets its own record kind instead of the tighten-plus-deviation
    pattern. Fails first on the old script: kind 'delegated' was 'neither window nor tighten' (the ok case fails)."""
    root = _gov_tree(tmp_path)
    _record(root, **{**_DELEGATED, **change})
    assert (governance.problems(root, today=dt.date(2026, 10, 15)) == []) is ok, governance.problems(root)


def test_record_delegated_appends_without_rewriting_history(tmp_path, monkeypatch):
    """--record --kind delegated appends one record and leaves the earlier bytes unchanged (the s3-domain note: the old
    --record rewrote the whole yaml). Fails first: the old CLI refused --kind delegated (argparse exit 2)."""
    root = _gov_tree(tmp_path)
    _record(root, kind="baseline", approver="owner", recorded="2026-10-07")
    path = root / governance.RECORDS
    before = path.read_bytes()
    monkeypatch.setattr(governance, "ROOT", root)
    argv = ["--record", "--kind", "delegated", "--approver", "orchestrator", "--delegated-by", "owner", "--basis",
            "GOAL §13.5", "--change", "TOST equivalence"]
    assert governance.main(argv) == 0
    assert path.read_bytes().startswith(before)
    recs = yaml.safe_load(path.read_text(encoding="utf-8"))["changes"]
    assert recs[-1]["kind"] == "delegated" and recs[-1]["cooling_off_observed"] is False
    assert recs[-1]["changes"] == ["TOST equivalence"]
    assert governance.main(argv[:-2]) == 1  # no --change: refused, nothing written
    assert len(yaml.safe_load(path.read_text(encoding="utf-8"))["changes"]) == len(recs)
