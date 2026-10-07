"""GOAL §5 L2 "Large artifacts": a per-run Reproducibility Bundle lists every artifact it does not carry (weights,
optimizer states, datasets) with path, bytes, sha256 and status (bundled / referenced / available_locally);
`chh rerun` REPORTS missing artifacts and fails closed when a required one is missing or any hash differs; the
bundle reader is bounded (zip bombs, STATUS §D)."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import custom_harness.research  # noqa: F401
from custom_harness.cli import cmd_rerun
from custom_harness.gateway import MockBackend
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox, take_snapshot

ANSWER = json.dumps({"answer": "a", "confidence": 0.5})
SPEC = {"id": "art", "entry": "answer", "max_steps": 3,
        "nodes": {"answer": {"kind": "llm", "prompt": "Q: {{input.q}}", "output_key": "answer",
                             "output_schema": {"type": "object", "required": ["answer", "confidence"]}}},
        "edges": []}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def task(tmp_path):
    t = tmp_path / "task"
    (t / "public").mkdir(parents=True)
    (t / "public" / "train.csv").write_bytes(b"x,y\n1,0\n")
    return t


def _run(store, tmp_path, task, artifacts):
    eng = Engine(HarnessSpec.model_validate(SPEC), backends={"executor": MockBackend(lambda r: ANSWER)}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True))
    res = eng.run("t", {"q": "x", "task_dir": str(task), "artifacts": artifacts})
    rec = [r for r in store.records("run_bundle") if r["run_id"] == res.run_id]
    assert rec and "error" not in rec[-1], rec
    return res, Path(rec[-1]["path"])


def _args(path, **kw):
    return argparse.Namespace(bundle=str(path), role=["executor=mock"], allow_same_family=True, task_dir=None,
                              docker=False, dev_sandbox=True, artifact_root=kw.get("roots", []))


def _rewrite_manifest(path: Path, edit) -> None:
    with zipfile.ZipFile(path) as z:
        members = {n: z.read(n) for n in z.namelist()}
    m = json.loads(members["manifest.json"])
    edit(m)
    members["manifest.json"] = json.dumps(m, sort_keys=True).encode()
    with zipfile.ZipFile(path, "w") as z:
        for n, b in members.items():
            z.writestr(n, b)


# ------------------------------------------------------------------ the manifest
def test_bundle_lists_task_data_and_declared_artifacts_with_path_bytes_sha_status(store, tmp_path, task):
    from custom_harness.store.run_bundle import read_run_bundle
    weights = tmp_path / "pretrained.pt"
    weights.write_bytes(b"W" * 1000)
    _, path = _run(store, tmp_path, task, [{"path": str(weights), "kind": "weights", "required": True}])
    arts = {a["path"]: a for a in read_run_bundle(path)["artifacts"]}
    w = arts[str(weights)]
    assert (w["bytes"], w["sha256"], w["status"], w["kind"], w["required"]) == (
        1000, _sha(b"W" * 1000), "available_locally", "weights", True)
    d = arts["task/public/train.csv"]
    assert (d["kind"], d["status"], d["required"], d["sha256"]) == (
        "dataset", "available_locally", True, _sha(b"x,y\n1,0\n"))
    with zipfile.ZipFile(path) as z:  # a declared artifact is referenced, never copied in by default
        assert f"blobs/{w['sha256']}" not in z.namelist()


def test_a_large_workspace_file_is_listed_not_bundled(store, tmp_path, task, monkeypatch):
    from custom_harness import config
    from custom_harness.store.run_bundle import build_run_bundle, read_run_bundle
    res, _ = _run(store, tmp_path, task, [])
    monkeypatch.setattr(config, "LARGE_ARTIFACT_BYTES", 4096)
    ws = tmp_path / "out"
    ws.mkdir()
    big, small = b"\x01" * 5000, b"print(1)\n"
    (ws / "model.safetensors").write_bytes(big)
    (ws / "optimizer.pt").write_bytes(big + b"o")
    (ws / "solution.py").write_bytes(small)
    out = tmp_path / "b.zip"
    build_run_bundle(store, res.run_id, out, environment={}, workspace_ref=take_snapshot(ws, store.blobs))
    arts = {a["path"]: a for a in read_run_bundle(out)["artifacts"]}
    assert arts["workspace/model.safetensors"]["kind"] == "weights"
    assert arts["workspace/optimizer.pt"]["kind"] == "optimizer_state"
    assert arts["workspace/model.safetensors"]["status"] == "available_locally"
    assert arts["workspace/solution.py"]["status"] == "bundled"
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert f"blobs/{_sha(big)}" not in names and f"blobs/{_sha(small)}" in names


# ------------------------------------------------------------------ rerun reports, and fails closed
def test_rerun_reports_artifacts_and_runs_when_all_are_present(store, tmp_path, task, capsys):
    weights = tmp_path / "pretrained.pt"
    weights.write_bytes(b"W" * 100)
    _, path = _run(store, tmp_path, task, [{"path": str(weights), "kind": "weights", "required": True}])
    capsys.readouterr()
    assert cmd_rerun(_args(path), store) in (0, 1)
    out = json.loads(capsys.readouterr().out)
    rows = {r["path"]: r for r in out["artifacts"]["rows"]}
    assert rows[str(weights)]["result"] == "present" and out["artifacts"]["fatal"] == []
    assert out["manifest_anchor"] == "matches the store's record"


def test_rerun_fails_closed_when_a_required_artifact_is_missing(store, tmp_path, task, capsys):
    weights = tmp_path / "pretrained.pt"
    weights.write_bytes(b"W" * 100)
    _, path = _run(store, tmp_path, task, [{"path": str(weights), "kind": "weights", "required": True}])
    n_runs = len(store.list_runs(limit=50))
    weights.unlink()
    with pytest.raises(SystemExit, match=r"required artifact missing: .*pretrained\.pt"):
        cmd_rerun(_args(path), store)
    assert len(store.list_runs(limit=50)) == n_runs  # refused BEFORE anything ran


def test_rerun_reports_a_missing_optional_artifact_and_still_runs(store, tmp_path, task, capsys):
    extra = tmp_path / "eval_cache.bin"
    extra.write_bytes(b"c" * 10)
    _, path = _run(store, tmp_path, task, [{"path": str(extra), "kind": "other", "required": False}])
    extra.unlink()
    capsys.readouterr()
    assert cmd_rerun(_args(path), store) in (0, 1)
    out = json.loads(capsys.readouterr().out)
    rows = {r["path"]: r for r in out["artifacts"]["rows"]}
    assert rows[str(extra)]["result"] == "missing" and out["artifacts"]["fatal"] == []
    assert any("missing (optional)" in p for p in out["artifacts"]["problems"])


def test_rerun_finds_a_moved_artifact_under_an_artifact_root(store, tmp_path, task, capsys):
    weights = tmp_path / "pretrained.pt"
    weights.write_bytes(b"W" * 100)
    _, path = _run(store, tmp_path, task, [{"path": str(weights), "kind": "weights", "required": True}])
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    weights.rename(moved / "pretrained.pt")
    capsys.readouterr()
    assert cmd_rerun(_args(path, roots=[str(moved)]), store) in (0, 1)
    out = json.loads(capsys.readouterr().out)
    row = {r["path"]: r for r in out["artifacts"]["rows"]}[str(weights)]
    assert row["result"] == "present" and row["found_at"] == str(moved / "pretrained.pt")


# ------------------------------------------------------------------ attacks
def test_attack_swapped_artifact_same_size_is_refused(store, tmp_path, task):
    weights = tmp_path / "pretrained.pt"
    weights.write_bytes(b"W" * 100)
    _, path = _run(store, tmp_path, task, [{"path": str(weights), "kind": "weights", "required": False}])
    weights.write_bytes(b"X" * 100)  # the attack: other weights, same name and size
    assert _sha(weights.read_bytes()) != _sha(b"W" * 100)
    with pytest.raises(SystemExit, match=r"hash differs: .*pretrained\.pt"):
        cmd_rerun(_args(path), store)  # fails closed even for an optional artifact


def test_attack_manifest_lies_about_a_hash(store, tmp_path, task):
    """The manifest is rewritten so it names the hash of swapped weights: the store's anchor catches the rewrite,
    and the artifact check alone still flags the lie against the bytes it describes."""
    from custom_harness.store.run_bundle import check_artifacts, read_run_bundle
    weights = tmp_path / "pretrained.pt"
    weights.write_bytes(b"W" * 100)
    _, path = _run(store, tmp_path, task, [{"path": str(weights), "kind": "weights", "required": True}])

    def lie(m):
        for a in m["artifacts"]:
            if a["path"] == str(weights):
                a["sha256"] = _sha(b"X" * 100)
    _rewrite_manifest(path, lie)
    m = read_run_bundle(path)
    assert any(a["sha256"] == _sha(b"X" * 100) for a in m["artifacts"])  # the lie is in place
    rep = check_artifacts(m, path, blobs=store.blobs)
    assert any("hash differs" in f and "pretrained.pt" in f for f in rep.fatal)
    with pytest.raises(SystemExit, match=r"manifest.json differs from the one recorded"):
        cmd_rerun(_args(path), store)


def test_attack_tampered_bundled_blob_is_refused(store, tmp_path, task):
    from custom_harness.store.run_bundle import check_artifacts, read_run_bundle
    cfg = tmp_path / "config.json"
    cfg.write_bytes(b'{"lr": 0.1}')
    _, path = _run(store, tmp_path, task, [{"path": str(cfg), "kind": "other", "required": True, "bundle": True}])
    m = read_run_bundle(path)
    bundled = [a for a in m["artifacts"] if a["status"] == "bundled"]
    assert [a["path"] for a in bundled] == [str(cfg)]
    assert check_artifacts(m, path, blobs=None).fatal == []  # intact before the attack
    victim = bundled[0]["sha256"]
    with zipfile.ZipFile(path) as z:
        members = {n: z.read(n) for n in z.namelist()}
    members[f"blobs/{victim}"] = b"tampered" + members[f"blobs/{victim}"]
    with zipfile.ZipFile(path, "w") as z:
        for n, b in members.items():
            z.writestr(n, b)
    rep = check_artifacts(m, path, blobs=None)
    assert any("hash differs" in f for f in rep.fatal), rep


# ------------------------------------------------------------------ bounded reads (zip bomb class)
def _bomb(path: Path, name: str, size: int, manifest: bytes | None = None) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        if manifest is not None:
            z.writestr("manifest.json", manifest)
        with z.open(name, "w", force_zip64=True) as f:
            chunk = b" " * 2**20
            for _ in range(size // 2**20):
                f.write(chunk)


def test_manifest_read_is_bounded(tmp_path, monkeypatch):
    from custom_harness import config
    from custom_harness.store.run_bundle import read_run_bundle
    monkeypatch.setattr(config, "RUN_BUNDLE_MANIFEST_BYTES", 4 * 2**20, raising=False)
    bomb = tmp_path / "bomb.zip"
    _bomb(bomb, "manifest.json", 6 * 2**20)
    assert zipfile.ZipFile(bomb).getinfo("manifest.json").file_size == 6 * 2**20  # the attack is in place
    with pytest.raises(ValueError, match=r"manifest.json declares .* over the cap"):  # refused BEFORE reading it
        read_run_bundle(bomb)


def test_bundled_blob_reads_are_bounded(store, tmp_path, monkeypatch):
    from custom_harness import config
    from custom_harness.store.run_bundle import RunBundleError, check_artifacts
    monkeypatch.setattr(config, "RUN_BUNDLE_RATIO", 10.0)
    bomb = tmp_path / "bomb.zip"
    h = "a" * 64
    _bomb(bomb, f"blobs/{h}", 16 * 2**20, manifest=b"{}")
    info = zipfile.ZipFile(bomb).getinfo(f"blobs/{h}")
    assert info.file_size > 10 * info.compress_size  # the attack is in place
    m = {"format": "chh-run-bundle/1", "artifacts": [
        {"path": "workspace/x", "bytes": 16 * 2**20, "sha256": h, "status": "bundled", "kind": "output",
         "required": True}]}
    with pytest.raises(RunBundleError, match="compression ratio"):
        check_artifacts(m, bomb, blobs=None)


def test_member_count_is_bounded(tmp_path, monkeypatch):
    from custom_harness import config
    from custom_harness.store.run_bundle import read_run_bundle
    monkeypatch.setattr(config, "RUN_BUNDLE_MEMBERS", 5, raising=False)
    p = tmp_path / "many.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": "chh-run-bundle/1"}))
        for i in range(10):
            z.writestr(f"blobs/{i}", b"")
    with pytest.raises(ValueError, match="members"):
        read_run_bundle(p)


# ------------------------------------------------------------------ chh reproduce (claim bundles)
def _claim_bundle(tmp_path, monkeypatch):
    """A real Kernel claim bundle. Its manifest gets an `artifacts` list added by hand: kernel/bundle.py does not
    write one yet (requested from kstats), so this checks the CLI side of the contract only."""
    from custom_harness.kernel.bundle import build_bundle
    from kernel_world import World
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    w = World(tmp_path / "world", seed=11)
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    path = tmp_path / "claim.zip"
    build_bundle(w.kernel, cert.cert_hash(), path)
    w.close()
    return path


def _reproduce(path, store, capsys):
    from custom_harness.cli import cmd_reproduce
    capsys.readouterr()
    code = cmd_reproduce(argparse.Namespace(bundle=str(path), docker=False, dev_sandbox=True, artifact_root=[]),
                         store)
    return code, json.loads(capsys.readouterr().out)


def test_reproduce_fails_closed_on_a_missing_required_artifact(store, tmp_path, monkeypatch, capsys):
    path = _claim_bundle(tmp_path, monkeypatch)
    weights = {"path": str(tmp_path / "gone.safetensors"), "bytes": 10, "sha256": "b" * 64, "status": "referenced",
               "kind": "weights", "required": True}
    _rewrite_manifest(path, lambda m: m.__setitem__("artifacts", [weights]))
    code, out = _reproduce(path, store, capsys)
    assert code == 1 and out["reproduced"] is False
    assert "required artifact missing" in out["refused"] and "gone.safetensors" in out["refused"]


def test_reproduce_reports_artifacts_when_present(store, tmp_path, monkeypatch, capsys):
    path = _claim_bundle(tmp_path, monkeypatch)
    f = tmp_path / "w.bin"
    f.write_bytes(b"w" * 10)
    entry = {"path": str(f), "bytes": 10, "sha256": _sha(b"w" * 10), "status": "available_locally",
             "kind": "weights", "required": True}
    _rewrite_manifest(path, lambda m: m.__setitem__("artifacts", [entry]))
    code, out = _reproduce(path, store, capsys)
    assert code == 0 and out["reproduced"] is True, out
    assert out["artifacts"]["rows"][0]["result"] == "present"


def test_attack_bundled_blob_removed_from_the_zip_is_fatal_even_if_optional(store, tmp_path, task):
    from custom_harness.store.run_bundle import check_artifacts, read_run_bundle
    cfg = tmp_path / "notes.txt"
    cfg.write_bytes(b"n")
    _, path = _run(store, tmp_path, task, [{"path": str(cfg), "kind": "other", "required": False, "bundle": True}])
    cfg.unlink()
    m = read_run_bundle(path)
    with zipfile.ZipFile(path) as z:
        members = {n: z.read(n) for n in z.namelist() if n != f"blobs/{_sha(b'n')}"}
    with zipfile.ZipFile(path, "w") as z:
        for n, b in members.items():
            z.writestr(n, b)
    assert f"blobs/{_sha(b'n')}" not in zipfile.ZipFile(path).namelist()  # the attack is in place
    assert any("absent from the bundle" in f for f in check_artifacts(m, path, blobs=None).fatal)
