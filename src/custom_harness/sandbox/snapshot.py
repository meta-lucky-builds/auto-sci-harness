"""Content-addressed workspace snapshots (PLAN B4).

State-exact, not process-exact: files are restored byte-identically; running processes are not.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..store.blobs import BlobStore


def take_snapshot(workspace: Path, blobs: BlobStore) -> str:
    """Store every file as a blob; return the hash of the manifest {relative_path: blob_hash}."""
    workspace = Path(workspace)
    manifest: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if path.is_file() and not path.is_symlink():
            manifest[path.relative_to(workspace).as_posix()] = blobs.put(path.read_bytes())
    return blobs.put_json({"kind": "workspace_snapshot", "files": manifest})


def restore_snapshot(snapshot_ref: str, workspace: Path, blobs: BlobStore) -> None:
    manifest = blobs.get_json(snapshot_ref)
    if manifest.get("kind") != "workspace_snapshot":
        raise ValueError(f"{snapshot_ref} is not a workspace snapshot")
    workspace = Path(workspace)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    for rel, digest in manifest["files"].items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blobs.get(digest))
