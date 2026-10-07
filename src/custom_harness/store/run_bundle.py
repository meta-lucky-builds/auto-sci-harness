"""Per-run Reproducibility Bundle (GOAL §1.3, invariant 9: every run, whatever its outcome).

A zip with manifest.json + blobs/<sha256>: the run record and final status, the exact harness spec, the initial
inputs and seed, the full trace (already scrubbed of private values), the cost record, the sandbox environment,
the final workspace snapshot (all code the run produced) and the hashes of the task's files. It never contains
private bytes: task label files appear only as hashes, and any blob identical to a private task file is refused.
`chh rerun BUNDLE` re-runs it with one command after checking that the task files still match the recorded hashes.
Claim-level bundles (kernel/bundle.py) are separate: they carry the held-out labels needed to re-check a
certificate and are never agent-visible.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

from .evidence import EvidenceStore

FORMAT = "chh-run-bundle/1"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def task_file_hashes(task_dir: str | Path) -> dict[str, str]:
    """sha256 of every file under a task directory (relative posix path -> hash)."""
    root = Path(task_dir)
    return {p.relative_to(root).as_posix(): _sha(p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()}


def build_run_bundle(store: EvidenceStore, run_id: str, out_path: Path, *, environment: dict[str, str] | None,
                     workspace_ref: str | None) -> str:
    run = store.get_run(run_id)
    status, detail = store.run_status(run_id)
    inputs = store.blobs.get_json(run.inputs_ref) if run.inputs_ref else None
    task_dir = (inputs or {}).get("input", {}).get("task_dir") if isinstance(inputs, dict) else None
    task_files = task_file_hashes(task_dir) if task_dir and Path(task_dir).is_dir() else {}
    private = {h for rel, h in task_files.items() if rel.startswith("private/")}
    blobs: dict[str, bytes] = {}
    refused: list[str] = []
    snapshot = None
    if workspace_ref:
        snapshot = store.blobs.get_json(workspace_ref)
        for rel, entry in (snapshot.get("files") or {}).items():
            data = store.blobs.get(entry["blob"])
            if _sha(data) in private:  # defence in depth: the workspace must never hold private bytes
                refused.append(rel)
                continue
            blobs[entry["blob"]] = data
    manifest: dict[str, Any] = {
        "format": FORMAT, "run": run.model_dump(mode="json"), "status": status.value, "detail": detail,
        "spec": store.harness_spec(run.harness_hash), "inputs": inputs, "seed": run.seed,
        "trace": [s.model_dump(mode="json") for s in store.trace(run_id)],
        "cost": store.run_cost(run_id).model_dump(mode="json"), "environment": environment or {},
        "task_dir": task_dir, "task_files": task_files, "workspace_snapshot": snapshot,
        "refused_private_blobs": refused, "blobs": sorted(blobs)}
    data = json.dumps(manifest, sort_keys=True, default=str).encode()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", data)
        for h, b in sorted(blobs.items()):
            z.writestr(f"blobs/{h}", b)
    return _sha(data)


def read_run_bundle(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as z:
        manifest: dict[str, Any] = json.loads(z.read("manifest.json"))
    if manifest.get("format") != FORMAT:
        raise ValueError(f"not a run bundle: format {manifest.get('format')!r}")
    return manifest


def check_task_files(manifest: dict[str, Any], task_dir: str | Path) -> list[str]:
    """Differences between the task files recorded in the bundle and the files on disk now."""
    now, then = task_file_hashes(task_dir), manifest.get("task_files") or {}
    return sorted([f"changed: {k}" for k in then if k in now and now[k] != then[k]]
                  + [f"missing: {k}" for k in then if k not in now] + [f"added: {k}" for k in now if k not in then])
