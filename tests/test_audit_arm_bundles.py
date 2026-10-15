"""Invariant 9 for claim-audit arm runs (s3-report finding; s4-integrity item 7): every arm run gets a Reproducibility
Bundle and a `run_bundle` record, like engine runs.

Fail-first reasoning (NOT run: owner rule): at 82719e1 store.run_bundle.write_run_bundle did not exist (ImportError) and
audit/claim_audit._evaluate_arms created runs (store.add_run) with no bundle.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from custom_harness.records import Run, StepKind, TraceStep
from custom_harness.store import EvidenceStore
from custom_harness.store.run_bundle import read_run_bundle_bytes, write_run_bundle

ROOT = Path(__file__).resolve().parents[1]


def test_write_run_bundle_writes_zip_and_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    store = EvidenceStore(tmp_path / "data" / "store")
    run = Run(harness_id="chh-audit", harness_hash="h" * 64, task_id="ds", seed=0)
    store.add_run(run)
    now = time.time()
    store.add_step(TraceStep(run_id=run.id, index=0, node_id="audit_arm", kind=StepKind.TOOL, input={}, output={},
                             started_at=now, ended_at=now))
    digest = write_run_bundle(store, run.id, environment={"sandbox": "test"})
    assert digest is not None
    recs = [r for r in store.records("run_bundle") if r["run_id"] == run.id]
    assert len(recs) == 1 and recs[0]["manifest_sha256"] == digest
    assert read_run_bundle_bytes(Path(recs[0]["path"]))[1] == digest


def test_audit_arm_runs_are_bundled() -> None:
    """Every function of audit/claim_audit.py that adds a run also writes its bundle."""
    tree = ast.parse((ROOT / "src/custom_harness/audit/claim_audit.py").read_text(encoding="utf-8"))
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        calls = {c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", "")
                 for c in ast.walk(fn) if isinstance(c, ast.Call)}
        if "add_run" in calls:
            assert "write_run_bundle" in calls, fn.name
