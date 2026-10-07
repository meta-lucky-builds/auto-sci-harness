"""Safe snapshots and host-escape guards (GOAL L0, CR P0-3).

Done-when (GOAL L0): a corrupted blob mid-restore leaves the original workspace intact; a planted
host-escape test fails.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import custom_harness.research  # noqa: F401
from custom_harness.sandbox import restore_snapshot, take_snapshot
from custom_harness.sandbox.base import WorkspaceEscape, safe_path
from custom_harness.sandbox.docker import DockerSandbox, docker_available
from custom_harness.sandbox.snapshot import SnapshotError

ROOT = Path(__file__).resolve().parents[1]


def _can_symlink(tmp_path) -> bool:
    try:
        os.symlink("x", tmp_path / "_probe")
        os.remove(tmp_path / "_probe")
        return True
    except OSError:
        return False


def _make_junction(link: Path, target: Path) -> None:
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


def _tree(ws: Path) -> dict[str, bytes]:
    return {p.relative_to(ws).as_posix(): p.read_bytes() for p in sorted(ws.rglob("*")) if p.is_file()}


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    (w / "a").mkdir(parents=True)
    (w / "a" / "x.bin").write_bytes(bytes(range(256)))
    (w / "top.txt").write_text("hello")
    (w / "empty").mkdir()
    return w


def test_roundtrip_keeps_bytes_and_empty_dirs(store, ws):
    ref = take_snapshot(ws, store.blobs)
    before = _tree(ws)
    (ws / "top.txt").write_text("changed")
    (ws / "junk.txt").write_text("junk")
    os.rmdir(ws / "empty")
    restore_snapshot(ref, ws, store.blobs)
    assert _tree(ws) == before and (ws / "empty").is_dir()
    assert not list(ws.parent.glob(".ws.*"))  # no staging/archive left behind


def test_corrupted_blob_leaves_workspace_intact(store, ws):
    ref = take_snapshot(ws, store.blobs)
    (ws / "top.txt").write_text("live state")
    live = _tree(ws)
    blob = store.blobs.get_json(ref)["files"]["a/x.bin"]["blob"]
    store.blobs._path(blob).write_bytes(b"bit rot")
    with pytest.raises(SnapshotError, match="missing or corrupted"):
        restore_snapshot(ref, ws, store.blobs)
    assert _tree(ws) == live


def test_missing_blob_leaves_workspace_intact(store, ws):
    ref = take_snapshot(ws, store.blobs)
    live = _tree(ws)
    store.blobs._path(store.blobs.get_json(ref)["files"]["top.txt"]["blob"]).unlink()
    with pytest.raises(SnapshotError):
        restore_snapshot(ref, ws, store.blobs)
    assert _tree(ws) == live


def test_failure_during_swap_puts_original_back(store, ws, monkeypatch):
    ref = take_snapshot(ws, store.blobs)
    (ws / "top.txt").write_text("live state")
    live = _tree(ws)
    real_replace, calls = os.replace, {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # archive succeeded, staging -> workspace fails
            raise OSError("disk full")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(SnapshotError, match="original workspace put back"):
        restore_snapshot(ref, ws, store.blobs)
    monkeypatch.undo()
    assert _tree(ws) == live
    assert not list(ws.parent.glob(".ws.*"))


def test_tampered_manifest_is_rejected_in_preflight(store, ws):
    live = _tree(ws)
    for manifest in ({"kind": "workspace_snapshot", "files": {"../escape.txt": store.blobs.put(b"x")}},
                     {"kind": "workspace_snapshot", "version": 2, "files": {},
                      "symlinks": {"l": "../../etc/passwd"}, "dirs": []},
                     {"kind": "workspace_snapshot", "version": 2, "files": {},
                      "symlinks": {"l": "/etc/passwd"}, "dirs": []}):
        with pytest.raises(SnapshotError):
            restore_snapshot(store.blobs.put_json(manifest), ws, store.blobs)
    assert _tree(ws) == live


def test_v1_manifest_still_restores(store, ws):
    ref = store.blobs.put_json({"kind": "workspace_snapshot", "files": {"only.txt": store.blobs.put(b"v1")}})
    restore_snapshot(ref, ws, store.blobs)
    assert _tree(ws) == {"only.txt": b"v1"}


def test_permission_bits_roundtrip(store, ws):
    f = ws / "top.txt"
    os.chmod(f, stat.S_IREAD)  # read-only works on Windows too
    want = stat.S_IMODE(f.stat().st_mode)
    ref = take_snapshot(ws, store.blobs)
    os.chmod(f, stat.S_IREAD | stat.S_IWRITE)
    restore_snapshot(ref, ws, store.blobs)
    assert stat.S_IMODE((ws / "top.txt").stat().st_mode) == want
    os.chmod(ws / "top.txt", stat.S_IREAD | stat.S_IWRITE)


def test_fingerprint_reuses_unchanged_files_and_sees_changes(store, ws, monkeypatch):
    first = take_snapshot(ws, store.blobs)
    reads = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",  # count workspace reads only (not the store's own blobs)
                        lambda self: (reads.append(self.name) if ws in self.parents else None) or real(self))
    second = take_snapshot(ws, store.blobs, previous=first)
    assert reads == [] and second == first  # nothing re-read, identical manifest
    (ws / "top.txt").write_text("a different length")
    third = take_snapshot(ws, store.blobs, previous=second)
    assert reads == ["top.txt"] and third != second


def test_safe_path_refuses_escapes(tmp_path, ws):
    assert safe_path(ws, "a/x.bin") == ws.resolve() / "a" / "x.bin"
    assert safe_path(ws, "not/yet/created.csv").name == "created.csv"
    for bad in ("../outside.txt", "a/../../outside.txt"):
        with pytest.raises(WorkspaceEscape):
            safe_path(ws, bad)
    with pytest.raises(WorkspaceEscape):
        safe_path(ws, str((tmp_path / "abs.txt").resolve()))


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_junction_planted_in_workspace_is_refused_and_not_snapshotted(store, tmp_path, ws):
    secret = tmp_path / "host_secret"
    secret.mkdir()
    (secret / "labels.csv").write_text("TOP SECRET")
    _make_junction(ws / "out", secret)
    with pytest.raises(WorkspaceEscape):
        safe_path(ws, "out/labels.csv")
    m = store.blobs.get_json(take_snapshot(ws, store.blobs))
    assert "out" in m["dropped_symlinks"] and not any(k.startswith("out/") for k in m["files"])
    assert b"TOP SECRET" not in b"".join(store.blobs.get(e["blob"]) for e in m["files"].values())


def test_symlink_planted_in_workspace_is_refused(store, tmp_path, ws):
    if not _can_symlink(tmp_path):
        pytest.skip("this OS account cannot create symlinks (covered by the junction and Docker tests)")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    os.symlink(secret, ws / "preds.csv")
    os.symlink("top.txt", ws / "inner_link")
    with pytest.raises(WorkspaceEscape):
        safe_path(ws, "preds.csv")
    m = store.blobs.get_json(take_snapshot(ws, store.blobs))
    assert "preds.csv" in m["dropped_symlinks"] and m["symlinks"] == {"inner_link": "top.txt"}


@pytest.mark.skipif(not docker_available(), reason="Docker daemon not running")
def test_planted_host_escape_from_container_fails(store, tmp_path):
    """Agent code inside the real sandbox replaces its predictions file with a link to a host file
    (the private labels). The harness must refuse to read it, record a policy failure, and keep
    running; snapshots must neither crash nor capture the target."""
    task = tmp_path / "task"
    subprocess.run([sys.executable, str(ROOT / "examples/tasks/make_rings_task.py"), str(task),
                    "--n-train", "200", "--n-test", "100", "--seed", "5"], check=True, capture_output=True)
    labels = (task / "private" / "test_y.csv").resolve()
    evil = (
        "import argparse, os\n"
        "p = argparse.ArgumentParser()\n"
        "for a in ('--train', '--test', '--out', '--seed'): p.add_argument(a)\n"
        "a = p.parse_args()\n"
        f"os.symlink({labels.as_posix()!r}, a.out)\n"   # absolute host path to the hidden labels
    )
    from custom_harness.research.modules import _run_solution
    sb = DockerSandbox(tmp_path / "ws")
    (sb.workspace / "evil.py").write_text(evil)
    preds, info = _run_solution(sb, "evil.py", "train.csv", "valid_X.csv", 0, 120)
    assert preds is None and "escape_attempt" in info  # refused, recorded as the agent's failure
    assert "label" not in json.dumps(info)
    m = store.blobs.get_json(take_snapshot(sb.workspace, store.blobs))  # must not crash
    assert m["dropped_symlinks"] and b"label" not in b"".join(
        store.blobs.get(e["blob"]) for e in m["files"].values())
