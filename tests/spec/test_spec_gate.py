"""Tests of the gate itself (GOAL §16.6 derived Kernel test list, §16.7 two-speed rule, findings registry)."""

from __future__ import annotations

import datetime as dt
import importlib.util
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
