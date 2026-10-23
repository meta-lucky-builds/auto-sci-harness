"""Emergence rule (GOAL §16.10): "Novelty may steer budget, never acceptance. Generated task families enter the
evolve split only."

Static half: custom_harness.emergence.check_novelty_isolation, on the real tree and on planted attacks in a COPY of
the source tree (every attack test first asserts the planted text is really in the copy). Dynamic half:
research/suite.py (assign_splits, check_split_isolation, build_suite) and evolve/driver.load_suite.
"""

from __future__ import annotations

import ast
import dataclasses
import shutil
from pathlib import Path

import pytest
import yaml

from custom_harness.emergence import ALLOWLIST, BUDGET_PATHS, check_novelty_isolation
from custom_harness.evolve.driver import load_suite
from custom_harness.research import suite as suite_mod
from custom_harness.research.suite import (
    TaskSpecFiles,
    assign_splits,
    build_suite,
    check_split_isolation,
    imbalanced,
    rings,
    spirals,
    write_task,
    xor,
)

SRC = Path(__file__).resolve().parents[1] / "src"


# ---------------------------------------------------------------- static check: novelty never in acceptance
def test_real_tree_passes_novelty_isolation():
    # not vacuous: a stale-entry check is part of the result, so [] also means every ALLOWLIST token was found
    assert ALLOWLIST
    assert check_novelty_isolation(SRC) == []


@pytest.fixture
def tree(tmp_path) -> Path:
    """A copy of src/custom_harness (the check reads files only; nothing is imported from the copy)."""
    shutil.copytree(SRC / "custom_harness", tmp_path / "src" / "custom_harness",
                    ignore=shutil.ignore_patterns("__pycache__"))
    root = tmp_path / "src"
    assert check_novelty_isolation(root) == []  # the unmodified copy is clean, so a problem below is the plant's
    return root


def _plant_in_function(path: Path, func: str, stmt: str) -> None:
    """Insert `stmt` as the first statement of top-level function `func`."""
    text = path.read_text(encoding="utf-8")
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == func)
    first = node.body[0]
    lines = text.splitlines(keepends=True)
    lines.insert(first.lineno - 1, " " * first.col_offset + stmt + "\n")
    path.write_text("".join(lines), encoding="utf-8", newline="")
    ast.parse(path.read_text(encoding="utf-8"))  # still valid Python


def _append(path: Path, code: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + "\n" + code + "\n", encoding="utf-8", newline="")


def test_novelty_score_in_the_acceptance_gate_is_flagged(tree):
    f = tree / "custom_harness" / "evolve" / "selection.py"
    _plant_in_function(f, "gate", "novelty_score = 0.0  # planted")
    assert "novelty_score = 0.0  # planted" in f.read_text(encoding="utf-8")  # the attack happened
    problems = check_novelty_isolation(tree)
    assert any("evolve/selection.py" in p and "'novelty_score'" in p for p in problems), problems


def test_interestingness_in_the_kernel_is_flagged(tree):
    f = tree / "custom_harness" / "kernel" / "kernel.py"
    _append(f, "def _rank(c):\n    return c.interestingness")
    assert "c.interestingness" in f.read_text(encoding="utf-8")
    problems = check_novelty_isolation(tree)
    assert any("kernel/kernel.py" in p and "'interestingness'" in p for p in problems), problems


@pytest.mark.parametrize("code, token", [
    ('def _accept(scores):\n    return scores["Novelty"] > 0.5', "novelty"),        # a string key
    ("def _accept(x, *, is_novel=True):\n    return is_novel", "is_novel"),          # an argument name
    ("from custom_harness.verify import stats as interesting_stats", "interesting_stats"),  # an import alias
])
def test_other_token_shapes_in_an_acceptance_module_are_flagged(tree, code, token):
    f = tree / "custom_harness" / "evolve" / "loop.py"  # loop.py has allowlisted 'rejected_novelty', not these
    _append(f, code)
    assert code in f.read_text(encoding="utf-8")
    problems = check_novelty_isolation(tree)
    assert any("evolve/loop.py" in p and repr(token) in p for p in problems), problems


def test_comments_and_docstrings_are_not_flagged(tree):
    f = tree / "custom_harness" / "evolve" / "selection.py"
    _append(f, 'def _doc():\n    """novelty_score is discussed here only."""\n    return 1  # novelty_score')
    assert "novelty_score is discussed" in f.read_text(encoding="utf-8")
    assert check_novelty_isolation(tree) == []


def _budget_module(tree: Path) -> dict:
    """A fake allocation module holding a 'budget' novelty token, and the config that declares it: the REAL config
    plus this module. (int18: BUDGET_PATHS is no longer empty; replacing it instead of extending it un-declared the
    station archive's real budget token, so every exact-equality assertion below saw that unrelated problem.)"""
    (tree / "custom_harness" / "allocate.py").write_text("novelty_bonus = 1.0\n", encoding="utf-8", newline="")
    allow = {**ALLOWLIST, ("allocate.py", "novelty_bonus"): ("budget", "allocation weight (test)")}
    return {"budget_paths": (*BUDGET_PATHS, "allocate.py"), "allowlist": allow}


def test_a_budget_token_in_a_budget_module_is_allowed(tree):
    """Control for the two attacks below: the same token is fine where it may steer budget."""
    assert check_novelty_isolation(tree, **_budget_module(tree)) == []


def test_an_allowlisted_budget_token_moved_into_an_acceptance_module_is_flagged(tree):
    cfg = _budget_module(tree)
    f = tree / "custom_harness" / "evolve" / "selection.py"
    _plant_in_function(f, "gate", "novelty_bonus = 1.0  # planted")
    assert "novelty_bonus = 1.0  # planted" in f.read_text(encoding="utf-8")
    # not allowlisted for selection.py: flagged as an unallowlisted occurrence
    problems = check_novelty_isolation(tree, **cfg)
    assert any("evolve/selection.py" in p and "not allowlisted" in p for p in problems), problems
    # and allowlisting it there as "budget" does not help: the entry itself and the occurrence are flagged
    cfg["allowlist"][("evolve/selection.py", "novelty_bonus")] = ("budget", "smuggled")
    problems = check_novelty_isolation(tree, **cfg)
    assert any("(evolve/selection.py, novelty_bonus)" in p and "only in a BUDGET_PATHS" in p for p in problems)
    assert any("budget token 'novelty_bonus' inside an acceptance module" in p for p in problems), problems


@pytest.mark.parametrize("stmt", ["from .. import allocate", "import custom_harness.allocate"])
def test_an_acceptance_module_importing_a_budget_module_is_flagged(tree, stmt):
    cfg = _budget_module(tree)
    f = tree / "custom_harness" / "evolve" / "selection.py"
    _plant_in_function(f, "gate", stmt + "  # planted")
    assert stmt + "  # planted" in f.read_text(encoding="utf-8")
    problems = check_novelty_isolation(tree, **cfg)
    assert problems == ["evolve/selection.py: acceptance module imports budget module allocate.py"], problems


def test_the_configuration_cannot_rot_silently(tree):
    (tree / "custom_harness" / "research" / "tree.py").rename(tree / "custom_harness" / "research" / "tree2.py")
    assert not (tree / "custom_harness" / "research" / "tree.py").exists()
    problems = check_novelty_isolation(tree)
    assert "ACCEPTANCE_PATHS pattern 'research/tree.py' matches no file" in problems
    # a stale allowlist entry, an unknown category, a file in both path sets
    allow = {**ALLOWLIST, ("verify/stats.py", "novelty_gain"): ("budget", "x"),
             ("evolve/loop.py", "rejected_novelty"): ("score", "x")}
    problems = check_novelty_isolation(tree, budget_paths=("verify/stats.py",), allowlist=allow)
    assert "allowlist (verify/stats.py, novelty_gain) has no occurrence (stale entry)" in problems
    assert "allowlist (evolve/loop.py, rejected_novelty): unknown category 'score'" in problems
    assert "verify/stats.py is in both ACCEPTANCE_PATHS and BUDGET_PATHS" in problems


SWITCH_ENTRIES = [("ablation.py", "novelty"), ("ablation.py", "rejected_novelty"),
                  ("ablation.py", "test_novelty_gate_switch"), ("audit/checklist.py", "novelty")]


def test_the_acceptance_loop_carries_no_novelty_switch_token():
    # evolve/loop.py (an acceptance path) names the duplicate gate's switch through ablation.DUPLICATE_GATE_SWITCH:
    # no allowlist entry for the bare token there, so a new `novelty` identifier in the loop is still flagged
    assert ("evolve/loop.py", "novelty") not in ALLOWLIST


@pytest.mark.parametrize(("rel", "tok"), SWITCH_ENTRIES)
def test_switch_name_allowlist_entries_are_pinned_and_narrow(tree, rel, tok):
    # fails before the entries exist (KeyError); each is a not-a-score, non-budget entry with a stated reason
    cat, reason = ALLOWLIST[(rel, tok)]
    assert cat == "not-a-score" and len(reason) > 20
    # dropping exactly this entry makes exactly this (file, token) flagged again: the entry is load-bearing, not slack
    allow = {k: v for k, v in ALLOWLIST.items() if k != (rel, tok)}
    problems = check_novelty_isolation(tree, allowlist=allow)
    assert problems and all(f"{rel}:" in p and repr(tok) in p for p in problems), problems
    # and it exempts only that token: a planted score in the same file is still flagged
    _append(tree / "custom_harness" / rel, "def _plant():\n    novelty_score = 0.0\n    return novelty_score")
    assert any(p.startswith(f"{rel}:") and "'novelty_score'" in p for p in check_novelty_isolation(tree))


def test_no_package_or_an_unparsable_file_fails_closed(tree, tmp_path):
    assert check_novelty_isolation(tmp_path / "nowhere") != []
    _append(tree / "custom_harness" / "evolve" / "selection.py", "def broken(:")
    assert any("evolve/selection.py: cannot be parsed" in p for p in check_novelty_isolation(tree))


# ---------------------------------------------------------------- generated task families: evolve split only
def _human() -> list[TaskSpecFiles]:
    return [g(seed, 60) for g in (rings, xor, spirals, imbalanced) for seed in (1, 2)]


def _gen(task_id: str, seed: int = 9, **kw) -> TaskSpecFiles:
    return dataclasses.replace(rings(seed, 60), task_id=task_id, family=kw.pop("family", "gen-rings"),
                               origin="generated", **kw)


def test_assign_splits_sends_every_generated_task_to_evolve_and_moves_no_human_task():
    human = _human()
    gens = [_gen("gen-a", 9), _gen("gen-b", 10), _gen("gen-c", 11, family="gen-xor")]
    assert {g.origin for g in gens} == {"generated"} and {h.origin for h in human} == {"human"}
    splits = assign_splits(human + gens)
    assert {splits[g.task_id] for g in gens} == {"evolve"}
    assert {k: v for k, v in splits.items() if not k.startswith("gen-")} == assign_splits(human)


def test_assign_splits_refuses_a_generated_held_out_family():
    gens = [_gen("gen-a")]
    with pytest.raises(ValueError, match="generated task"):
        assign_splits(_human() + gens, heldout_families=("gen-rings",))
    # also a human family that has a generated task in it (it would have an evolve task: not held out)
    with pytest.raises(ValueError, match="generated task"):
        assign_splits(_human() + [_gen("gen-s", family="spirals")], heldout_families=("spirals",))


def test_assign_splits_refuses_a_generated_task_sharing_a_base_dataset_with_a_human_task():
    human = _human()
    copy = _gen("gen-copy", source="rings-s1")
    assert copy.data_source == "rings-s1" and "rings-s1" in {h.data_source for h in human}  # the attack happened
    with pytest.raises(ValueError, match="shared by a generated and a human task"):
        assign_splits(human + [copy])


def test_unknown_origin_is_refused():
    with pytest.raises(ValueError, match="unknown origin"):
        dataclasses.replace(rings(1, 60), origin="llm")
    spec = rings(1, 60)
    object.__setattr__(spec, "origin", "llm")  # bypassing the dataclass check: assign_splits still refuses
    assert spec.origin == "llm"
    with pytest.raises(ValueError, match="unknown origin"):
        assign_splits([spec, *_human()[1:]])


def _entries(tmp_path: Path, rows: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rows):
        spec = rings(i + 1, 60)
        d = write_task(spec, tmp_path)
        out.append({"id": spec.task_id, "dir": str(d), "source": spec.task_id, **r})
    return out


BASE = [{"split": "evolve", "family": "rings"}, {"split": "gate", "family": "spirals"},
        {"split": "sealed", "family": "xor"}]  # no `origin`: written before the field existed = human


@pytest.mark.parametrize("split", ["gate", "sealed"])
def test_check_split_isolation_refuses_a_generated_task_outside_evolve(tmp_path, split):
    entries = _entries(tmp_path, [*BASE, {"split": split, "family": "gen", "origin": "generated"}])
    assert entries[-1]["origin"] == "generated" and entries[-1]["split"] == split
    problems = check_split_isolation(entries)
    assert any("generated task" in p and repr(split) in p for p in problems), problems


def test_check_split_isolation_origin_rules(tmp_path):
    ok = _entries(tmp_path, [*BASE, {"split": "evolve", "family": "gen", "origin": "generated"}])
    assert check_split_isolation(ok) == []  # backwards compatible (no origin) + generated in evolve
    bad = [*ok[:3], {**ok[3], "origin": "llm"}]
    assert any("unknown origin 'llm'" in p for p in check_split_isolation(bad))
    assert any("unknown origin None" in p for p in check_split_isolation([*ok[:3], {**ok[3], "origin": None}]))


def _write(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "suite.yaml"
    p.write_text(yaml.safe_dump({"tasks": entries}), encoding="utf-8")
    return p


@pytest.mark.parametrize("split", ["gate", "sealed"])
def test_load_suite_refuses_a_generated_task_in_gate_or_sealed(tmp_path, split):
    p = _write(tmp_path, _entries(tmp_path, [*BASE, {"split": split, "family": "gen", "origin": "generated"}]))
    last = yaml.safe_load(p.read_text(encoding="utf-8"))["tasks"][-1]
    assert (last["split"], last["origin"]) == (split, "generated")  # the attack is in the file
    with pytest.raises(ValueError, match="generated task"):
        load_suite(p)


def test_load_suite_accepts_generated_in_evolve_and_old_suites(tmp_path):
    p = _write(tmp_path, _entries(tmp_path, [*BASE, {"split": "evolve", "family": "gen", "origin": "generated"}]))
    assert [t.split for t in load_suite(p)] == ["evolve", "gate", "sealed", "evolve"]


def test_build_suite_writes_origin(tmp_path):
    doc = yaml.safe_load(build_suite(tmp_path, real_data=None).read_text(encoding="utf-8"))
    assert doc["tasks"] and {t["origin"] for t in doc["tasks"]} == {"human"}
    assert suite_mod.ORIGINS == ("human", "generated")
