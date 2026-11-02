"""Held-out truth outside every agent-readable root (GOAL invariant 2; s4-integrity item 1).

Fail-first reasoning (NOT run: owner rule, tests deferred): at 82719e1 (origin/int17) custom_harness.truth_layout did
not exist (ImportError on every test here), research/suite.write_task wrote `<task>/private/` inside the task dir (so
a suite under `<data>/suites` held its labels in an agent-readable root), register_task read
`task_dir/private/test_y.csv` directly, and no sandbox check protected the truth location before a task registered it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_harness import truth_layout as TL
from custom_harness.research.task import load_task


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("CHH_DATA_DIR", str(d))
    monkeypatch.delenv("CHH_TRUTH_ROOT", raising=False)
    monkeypatch.delenv("CHH_AGENT_READABLE", raising=False)
    return d


def _task(d: Path, *, legacy: bool = False, truth: Path | None = None) -> Path:
    (d / "public").mkdir(parents=True)
    (d / "public" / "train.csv").write_text("x,label\n1,0\n2,1\n", encoding="utf-8")
    (d / "public" / "test_X.csv").write_text("x\n3\n", encoding="utf-8")
    (d / "baseline.py").write_text("print(1)\n", encoding="utf-8")
    (d / "task.yaml").write_text("id: t\ntitle: t\ndescription: t\nmetric: accuracy\ntarget_column: label\n",
                                 encoding="utf-8")
    t = d / "private" if legacy else (truth if truth is not None else TL.truth_dir(d))
    t.mkdir(parents=True, exist_ok=True)
    (t / "test_y.csv").write_text("label\n1\n", encoding="utf-8")
    (t / "canary.txt").write_text("CHH-CANARY-x\n", encoding="utf-8")
    return d


def test_suite_task_truth_maps_outside_the_suite_tree(data: Path) -> None:
    td = data / "suites" / "m2" / "rings-s1"
    assert TL.truth_dir(td) == data / "private" / "suites" / "m2" / "rings-s1"
    assert TL.truth_dir(data / "tasks" / "rings-v1") == data / "private" / "tasks" / "rings-v1"
    _task(td)
    assert not (td / "private").exists()
    assert load_task(td).id == "t"
    assert TL.truth_file(td, "test_y.csv").read_text(encoding="utf-8").startswith("label")


def test_no_agent_readable_root_holds_the_truth_root(data: Path) -> None:
    assert TL.truth_layout_problems() == []
    from custom_harness.commitment import default_agent_readable_roots

    roots = default_agent_readable_roots(existing_only=False)
    assert data / "suites" in roots and data / "tasks" in roots  # roots listed even before they exist
    for t in [TL.truth_root(), *TL.protected_truth_dirs()]:
        assert not any(t == r or r in t.parents or t in r.parents for r in roots)


def test_static_check_flags_a_truth_root_inside_a_root(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHH_TRUTH_ROOT", str(data / "suites" / "_truth"))
    assert any("overlaps agent-readable root" in p for p in TL.truth_layout_problems())


def test_static_scan_finds_legacy_truth_in_a_root(data: Path) -> None:
    _task(data / "tasks" / "rings-v1", legacy=True)
    problems = TL.truth_layout_problems(scan=True)
    assert any("rings-v1" in p and "test_y.csv" in p for p in problems)


def test_unmigrated_suite_task_is_refused_never_read(data: Path) -> None:
    td = _task(data / "suites" / "m2" / "xor-s1", legacy=True)
    with pytest.raises(TL.TruthLayoutError, match="migrate_truth"):
        TL.truth_file(td, "test_y.csv")
    with pytest.raises(TL.TruthLayoutError):
        load_task(td)
    with pytest.raises(TL.TruthLayoutError):
        TL.truth_files(td)


def test_legacy_layout_only_outside_agent_readable_roots(data: Path, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    outside = _task(tmp_path / "scratch" / "t1", legacy=True)
    assert TL.truth_dir(outside) == outside / "private"
    assert load_task(outside).id == "t"
    agent = tmp_path / "agent"
    monkeypatch.setenv("CHH_AGENT_READABLE", str(agent))
    inside = _task(agent / "t2", legacy=True)
    with pytest.raises(TL.TruthLayoutError, match="agent-readable"):
        TL.truth_dir(inside)


def test_sandbox_may_not_mount_the_truth_dirs_before_any_task_registers(data: Path) -> None:
    from custom_harness.sandbox.base import ProtectedMount, check_mount

    for ws in (data / "private" / "suites", data / "private" / "suites" / "m2" / "ws", data / "private" / "tasks",
               data):
        with pytest.raises(ProtectedMount):
            check_mount(ws)
    check_mount(data / "private" / "suite-m2" / "_export")  # the trusted exporter's workspace stays allowed


def test_run_bundle_records_truth_files_under_their_old_keys(data: Path) -> None:
    from custom_harness.store.run_bundle import task_file_hashes

    td = _task(data / "suites" / "m2" / "spirals-s1")
    keys = set(task_file_hashes(td))
    assert {"private/test_y.csv", "private/canary.txt", "public/train.csv", "task.yaml"} <= keys


def test_register_task_reads_truth_from_the_truth_dir(data: Path) -> None:
    """The Kernel vault gets the labels from the truth dir (and the canary with them)."""
    from custom_harness.research.claims import register_task
    from custom_harness.store import EvidenceStore

    td = _task(data / "suites" / "m2" / "iris-real")

    class Ctx:  # register_task ingests through kerneld (s4-kerneld): it names the run and finds the store root
        run_id = "r"
        store = EvidenceStore(data / "store")

        def kernel(self):
            from custom_harness.kernel import Kernel
            return Kernel(self.store)

    ids = register_task(Ctx(), load_task(td), td)  # type: ignore[arg-type]
    v = Ctx().kernel().vault
    assert v.canary(ids["test"]) == "CHH-CANARY-x"


def test_no_source_joins_a_private_truth_path_by_hand() -> None:
    """Generalised: only truth_layout.py builds the truth location. Fails first at 82719e1 (claims.py,
    multiplicity.py, modules.py, export.py, task.py, suite.py all joined `task_dir / "private"`)."""
    src = Path(__file__).resolve().parents[1] / "src" / "custom_harness"
    offenders = []
    for f in src.rglob("*.py"):
        rel = f.relative_to(src).as_posix()
        if rel in ("truth_layout.py",) or rel.startswith(("kernel/", "kerneld/")):
            continue
        text = f.read_text(encoding="utf-8")
        if '/ "private"' in text or "f\"private/{" in text:
            offenders.append(rel)
    assert offenders == []
