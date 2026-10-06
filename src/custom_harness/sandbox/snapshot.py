"""Content-addressed workspace snapshots (PLAN B4, GOAL L0, CR P0-3).

State-exact, not process-exact: files, empty directories, permission bits and in-workspace symlinks
are restored exactly; running processes are not.

Manifest v2: {"kind": "workspace_snapshot", "version": 2,
              "files": {rel: {"blob", "mode", "size", "mtime_ns", "ctime_ns"}},
              "symlinks": {rel: target}, "dirs": [rel, ...]}
v1 manifests ({"files": {rel: blob}}) still restore.

Restore never leaves a half-restored workspace (ScienceFlow pattern):
  1. preflight: every path is a safe relative path, every blob exists and matches its hash, every
     symlink target stays inside the workspace -- before anything on disk is touched;
  2. the new tree is built in a sibling staging directory;
  3. the live workspace is archived by renaming it aside;
  4. staging is renamed into place; on any failure the archive is renamed back (rollback);
  5. the archive is deleted.

Symlinks are recorded, never followed: a snapshot can never pull host files into the evidence store.
"""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from pathlib import Path, PurePosixPath

from ..store.blobs import BlobStore
from .base import is_link_like


class SnapshotError(RuntimeError):
    pass


def _safe_rel(rel: str) -> PurePosixPath:
    p = PurePosixPath(rel)
    if not rel or p.is_absolute() or ".." in p.parts or any(":" in part for part in p.parts):
        raise SnapshotError(f"unsafe path in snapshot manifest: {rel!r}")
    return p


def _target_inside(rel: str, target: str) -> bool:
    if os.path.isabs(target) or target.startswith(("/", "\\")):
        return False
    depth = 0
    for part in (PurePosixPath(rel).parent / target.replace("\\", "/")).parts:
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        elif part not in (".", ""):
            depth += 1
    return True


def take_snapshot(workspace: Path, blobs: BlobStore, previous: str | None = None) -> str:
    """Store the workspace; return the manifest hash.

    `previous` (a manifest hash) enables the fingerprint shortcut: a file whose (size, mtime_ns, ctime_ns)
    match the previous snapshot reuses its blob without being re-read. This trusts filesystem metadata;
    pass previous=None where byte-exactness must not depend on it (e.g. reproducibility bundles)."""
    workspace = Path(workspace)
    prev_files: dict = {}
    if previous is not None:
        try:
            pm = blobs.get_json(previous)
            if pm.get("version") == 2:
                prev_files = pm["files"]
        except (OSError, ValueError, KeyError):
            prev_files = {}
    files: dict[str, dict] = {}
    symlinks: dict[str, str] = {}
    # links pointing outside the workspace, and entries the host cannot read (e.g. a symlink made inside a
    # Linux container, seen from Windows): recorded with a reason, never restored, never followed
    dropped: dict[str, str] = {}
    dirs: list[str] = []
    for root, dirnames, filenames in os.walk(workspace, followlinks=False):
        root_p = Path(root)
        for d in list(dirnames):
            full = root_p / d
            rel = full.relative_to(workspace).as_posix()
            if is_link_like(full):
                dirnames.remove(d)  # never descend into a link
                _record_link(full, rel, symlinks, dropped)
            elif not any(full.iterdir()):
                dirs.append(rel)
        for f in filenames:
            full = root_p / f
            rel = full.relative_to(workspace).as_posix()
            if is_link_like(full):
                _record_link(full, rel, symlinks, dropped)
                continue
            st = full.lstat()
            if not stat.S_ISREG(st.st_mode):
                continue  # sockets, fifos, devices: not state we restore
            fp = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns}
            old = prev_files.get(rel)
            if old and all(old.get(k) == v for k, v in fp.items()) and blobs.has(old["blob"]):
                digest = old["blob"]
            else:
                digest = blobs.put(full.read_bytes())
            files[rel] = {"blob": digest, "mode": stat.S_IMODE(st.st_mode), **fp}
    return blobs.put_json({"kind": "workspace_snapshot", "version": 2, "files": files,
                           "symlinks": symlinks, "dropped_symlinks": dropped, "dirs": sorted(dirs)})


def _record_link(full: Path, rel: str, symlinks: dict[str, str], dropped: dict[str, str]) -> None:
    try:
        target = os.readlink(full)
    except (OSError, ValueError) as e:  # ValueError: a WSL reparse point Windows cannot read as a link
        dropped[rel] = f"<unreadable: {type(e).__name__}>"
        return
    if _target_inside(rel, target) and not os.path.isabs(target):
        symlinks[rel] = target
    else:
        dropped[rel] = target


def _load_manifest(snapshot_ref: str, blobs: BlobStore) -> tuple[dict[str, dict], dict[str, str], list[str]]:
    try:
        manifest = blobs.get_json(snapshot_ref)
    except (OSError, ValueError) as e:
        raise SnapshotError(f"snapshot {snapshot_ref} unreadable: {e}") from e
    if manifest.get("kind") != "workspace_snapshot":
        raise SnapshotError(f"{snapshot_ref} is not a workspace snapshot")
    raw = manifest.get("files", {})
    files = {rel: (v if isinstance(v, dict) else {"blob": v, "mode": None}) for rel, v in raw.items()}
    return files, dict(manifest.get("symlinks", {})), list(manifest.get("dirs", []))


def restore_snapshot(snapshot_ref: str, workspace: Path, blobs: BlobStore) -> None:
    workspace = Path(workspace)
    files, symlinks, dirs = _load_manifest(snapshot_ref, blobs)

    # 1. preflight: nothing on disk is touched until every check has passed
    for rel in [*files, *symlinks, *dirs]:
        _safe_rel(rel)
    for rel, target in symlinks.items():
        if not _target_inside(rel, target):
            raise SnapshotError(f"symlink {rel!r} -> {target!r} points outside the workspace")
    for rel, entry in files.items():
        try:
            blobs.get(entry["blob"])  # existence + hash check
        except (OSError, KeyError) as e:
            raise SnapshotError(f"blob for {rel!r} missing or corrupted: {e}") from e

    # 2. build in staging
    tag = uuid.uuid4().hex[:10]
    staging = workspace.parent / f".{workspace.name}.restore-{tag}"
    archive = workspace.parent / f".{workspace.name}.prev-{tag}"
    staging.mkdir(parents=True)
    try:
        for rel in dirs:
            (staging / rel).mkdir(parents=True, exist_ok=True)
        for rel, entry in files.items():
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blobs.get(entry["blob"]))
            if entry.get("mode") is not None:
                os.chmod(target, entry["mode"])
        for rel, link_target in symlinks.items():
            link = staging / rel
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(link_target, link)
    except BaseException as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise SnapshotError(f"restore failed while staging; workspace untouched: {e}") from e

    # 3-5. archive live -> swap -> rollback on failure -> drop archive
    had_live = workspace.exists()
    if had_live:
        os.replace(workspace, archive)
    try:
        os.replace(staging, workspace)
    except BaseException as e:
        if had_live:
            os.replace(archive, workspace)
        shutil.rmtree(staging, ignore_errors=True)
        raise SnapshotError(f"restore failed during swap; original workspace put back: {e}") from e
    if had_live:
        shutil.rmtree(archive, ignore_errors=True)
