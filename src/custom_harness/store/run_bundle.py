"""Per-run Reproducibility Bundle (GOAL §1.3, invariant 9: every run, whatever its outcome).

A zip with manifest.json + blobs/<sha256>: the run record and final status, the exact harness spec, the initial
inputs and seed, the full trace (already scrubbed of private values), the cost record (`cost`: the units as recorded;
`cost_record`: priced through the gateway cost table, its usd None and marked unknown or UNCALIBRATED unless every rate
that applied is a real, sourced one), the sandbox environment,
the final workspace snapshot (all code the run produced) and the hashes of the task's files. It never contains
private bytes: task label files appear only as hashes, and any blob identical to a private task file is refused.
`chh rerun BUNDLE` re-runs it with one command after checking that the task files still match the recorded hashes.
Claim-level bundles (kernel/bundle.py) are separate: they carry the held-out labels needed to re-check a
certificate and are never agent-visible.

Large artifacts (GOAL §5 L2, after the OpenResearch demo run-manifest): `manifest["artifacts"]` lists EVERY artifact
of the run, whether the zip carries it or not: the task's data files (kind `dataset`), the artifacts the run's input
declares (`input.artifacts`: [{path, kind, required, bundle?}], e.g. pretrained weights it starts from) and every
file of the final workspace (weights / optimizer_state / output, by name). Each entry: path, bytes, sha256, kind,
required, status:
  bundled            the bytes are in the zip (blobs/<sha256>)
  available_locally  not in the zip; present on the building machine (at `path`, or in its blob store)
  referenced         not in the zip and not on the building machine (only a declared reference)
A workspace file over `config.LARGE_ARTIFACT_BYTES` is listed, not bundled (multi-GB weights stay out of the zip).
`check_artifacts` resolves every entry (zip, blob store, recorded path, then each artifact root), compares bytes and
sha256 (streamed: a large file is never held in memory), REPORTS every missing one and is fatal (fail closed) when a
required one is missing or ANY found artifact's size or hash differs from the manifest.

Reading is bounded (zip bombs, STATUS §D): member count, per-member, manifest and total uncompressed sizes and the
compression ratio are capped (`config.RUN_BUNDLE_*`); a bundle over a cap is refused with a RunBundleError naming it.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .. import config
from ..gateway.costs import run_cost_record
from .blobs import BlobStore
from .evidence import EvidenceStore

FORMAT = "chh-run-bundle/1"
STATUSES = ("bundled", "referenced", "available_locally")
KINDS = ("weights", "optimizer_state", "dataset", "output", "other")
_WEIGHT_SUFFIXES = (".pt", ".pth", ".ckpt", ".safetensors", ".bin", ".h5", ".onnx", ".pkl", ".joblib", ".npz")
_CHUNK = 2**20


class RunBundleError(ValueError):
    """A run bundle refused before it was (fully) read: over a read cap, or malformed."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> tuple[int, str]:
    """(bytes, sha256) of a file, streamed."""
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
            n += len(chunk)
    return n, h.hexdigest()


def task_file_paths(task_dir: str | Path) -> dict[str, Path]:
    """Every file of a task (relative posix path -> path on disk): the files under the task directory, plus its
    held-out truth under `private/<name>` wherever truth_layout keeps it (outside agent-readable roots for suite
    tasks), so a bundle records the same keys whichever layout the task uses."""
    from ..truth_layout import LEGACY_DIRNAME, truth_files

    root = Path(task_dir)
    out = {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*")) if p.is_file()}
    for rel, p in truth_files(root).items():
        out.setdefault(f"{LEGACY_DIRNAME}/{rel}", p)
    return out


def task_file_hashes(task_dir: str | Path) -> dict[str, str]:
    """sha256 of every file of a task (relative posix path -> hash; see task_file_paths)."""
    return {rel: _file_sha(p)[1] for rel, p in task_file_paths(task_dir).items()}


def guess_kind(name: str) -> str:
    low = PurePosixPath(name).name.lower()
    if "optim" in low:
        return "optimizer_state"
    return "weights" if low.endswith(_WEIGHT_SUFFIXES) else "output"


def _declared(inputs: Any) -> list[dict[str, Any]]:
    """Artifacts declared by the run's input (trusted: the run's caller). Malformed declarations are refused."""
    raw = ((inputs or {}).get("input") or {}).get("artifacts") if isinstance(inputs, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RunBundleError("input.artifacts must be a list")
    out = []
    for d in raw:
        if not isinstance(d, dict) or not isinstance(d.get("path"), str) or not d["path"]:
            raise RunBundleError(f"malformed artifact declaration: {d!r}")
        kind = d.get("kind", "other")
        if kind not in KINDS:
            raise RunBundleError(f"artifact {d['path']}: unknown kind {kind!r} (allowed: {', '.join(KINDS)})")
        out.append({"path": d["path"], "kind": kind, "required": bool(d.get("required", True)),
                    "bundle": bool(d.get("bundle", False)), "sha256": d.get("sha256"), "bytes": d.get("bytes")})
    return out


def build_run_bundle(store: EvidenceStore, run_id: str, out_path: Path, *, environment: dict[str, str] | None,
                     workspace_ref: str | None) -> str:
    run = store.get_run(run_id)
    status, detail = store.run_status(run_id)
    inputs = store.blobs.get_json(run.inputs_ref) if run.inputs_ref else None
    task_dir = (inputs or {}).get("input", {}).get("task_dir") if isinstance(inputs, dict) else None
    task_paths = task_file_paths(task_dir) if task_dir and Path(task_dir).is_dir() else {}
    task_files = {rel: _file_sha(p)[1] for rel, p in task_paths.items()}
    private = {h for rel, h in task_files.items() if rel.startswith("private/")}
    cap = config.LARGE_ARTIFACT_BYTES
    blobs: dict[str, bytes] = {}
    refused: list[str] = []
    artifacts: list[dict[str, Any]] = []
    for rel, h in task_files.items():
        size = task_paths[rel].stat().st_size
        artifacts.append({"path": f"task/{rel}", "bytes": size, "sha256": h, "kind": "dataset", "required": True,
                          "status": "available_locally"})
    for d in _declared(inputs):
        p = Path(d["path"])
        if p.is_file():
            size, h = _file_sha(p)
            st = "available_locally"
            if d["bundle"] and size <= cap:
                if h in private:
                    raise RunBundleError(f"declared artifact {d['path']} is a private task file: never bundled")
                blobs[h] = p.read_bytes()
                st = "bundled"
        elif isinstance(d["sha256"], str) and isinstance(d["bytes"], int):  # only a reference (e.g. an external URI)
            size, h, st = d["bytes"], d["sha256"], "referenced"
        else:
            raise RunBundleError(f"declared artifact {d['path']} is not on this machine and its sha256/bytes are not "
                                 f"declared: it cannot be recorded")
        artifacts.append({"path": d["path"], "bytes": size, "sha256": h, "kind": d["kind"], "required": d["required"],
                          "status": st})
    snapshot = None
    if workspace_ref:
        snapshot = store.blobs.get_json(workspace_ref)
        for rel, entry in (snapshot.get("files") or {}).items():
            size = entry.get("size")
            if isinstance(size, int) and size > cap:  # listed, not bundled; never read into memory here
                if entry["blob"] in private:
                    refused.append(rel)
                    continue
                artifacts.append({"path": f"workspace/{rel}", "bytes": size, "sha256": entry["blob"],
                                  "kind": guess_kind(rel), "required": False,
                                  "status": "available_locally" if store.blobs.has(entry["blob"]) else "referenced"})
                continue
            data = store.blobs.get(entry["blob"])
            if _sha(data) in private:  # defence in depth: the workspace must never hold private bytes
                refused.append(rel)
                continue
            blobs[entry["blob"]] = data
            artifacts.append({"path": f"workspace/{rel}", "bytes": len(data), "sha256": entry["blob"],
                              "kind": guess_kind(rel), "required": False, "status": "bundled"})
    manifest: dict[str, Any] = {
        "format": FORMAT, "run": run.model_dump(mode="json"), "status": status.value, "detail": detail,
        "spec": store.harness_spec(run.harness_hash), "inputs": inputs, "seed": run.seed,
        "trace": [s.model_dump(mode="json") for s in store.trace(run_id)],
        "cost": store.run_cost(run_id).model_dump(mode="json"),
        # priced through the gateway cost table: usd None (unknown / UNCALIBRATED) unless every rate is real
        "cost_record": run_cost_record(store, run_id), "environment": environment or {},
        "task_dir": task_dir, "task_files": task_files, "workspace_snapshot": snapshot,
        "refused_private_blobs": refused, "blobs": sorted(blobs), "artifacts": artifacts}
    if run.ablated:  # custom_harness/ablation.py: an ablated run's bundle says so at the top level (absent otherwise)
        manifest["ABLATED"] = {"disabled": list(run.ablated)}
    data = json.dumps(manifest, sort_keys=True, default=str).encode()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", data)
        for h, b in sorted(blobs.items()):
            z.writestr(f"blobs/{h}", b)
    return _sha(data)


# ------------------------------------------------------------------ bounded reading
class BoundedZip:
    """Reads members of an untrusted zip under the `config.RUN_BUNDLE_*` caps (checked on DECLARED sizes before a
    byte is decompressed; zipfile never returns more than the declared size and raises on a CRC/size mismatch)."""

    def __init__(self, z: zipfile.ZipFile):
        self.z, self.total = z, 0
        infos = z.infolist()
        if len(infos) > config.RUN_BUNDLE_MEMBERS:
            raise RunBundleError(f"bundle has {len(infos)} members, more than the cap of {config.RUN_BUNDLE_MEMBERS}")
        self.infos = {i.filename: i for i in infos}
        if len(self.infos) != len(infos):  # the checker and an extractor could see different copies
            raise RunBundleError("bundle has a duplicate member name: refused")

    def read(self, name: str) -> bytes:
        info = self.infos.get(name)
        if info is None:
            raise RunBundleError(f"bundle has no member {name}")
        cap = config.RUN_BUNDLE_MANIFEST_BYTES if name == "manifest.json" else config.RUN_BUNDLE_MEMBER_BYTES
        if info.file_size > cap:
            raise RunBundleError(f"bundle member {name} declares {info.file_size} bytes, over the cap of {cap}")
        if info.file_size > 8 * 2**20 and info.file_size > config.RUN_BUNDLE_RATIO * max(info.compress_size, 1):
            raise RunBundleError(f"bundle member {name} has a compression ratio over {config.RUN_BUNDLE_RATIO:g} "
                                 f"({info.file_size} bytes from {info.compress_size}): refused as a possible zip bomb")
        self.total += info.file_size
        if self.total > config.RUN_BUNDLE_TOTAL_BYTES:
            raise RunBundleError(f"bundle content exceeds the total cap of {config.RUN_BUNDLE_TOTAL_BYTES} bytes")
        try:
            return self.z.read(info)
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as e:
            raise RunBundleError(f"bundle member {name} is corrupt: {e}") from e


def write_run_bundle(store: EvidenceStore, run_id: str, *, environment: dict[str, str] | None,
                     workspace_ref: str | None = None) -> str | None:
    """Invariant 9 for runs that do not go through the engine (e.g. the claim-audit arm runs, audit/claim_audit.py):
    write `<data>/bundles/runs/<run_id>.zip` and its `run_bundle` record, as runtime/engine.Engine._write_run_bundle
    does. A failure is recorded (`run_bundle` with `error`), never silent. Returns the manifest sha256 or None."""
    path = config.data_dir() / "bundles" / "runs" / f"{run_id}.zip"
    try:
        digest = build_run_bundle(store, run_id, path, environment=environment, workspace_ref=workspace_ref)
    except (OSError, ValueError, KeyError) as e:
        store.append_record("run_bundle", {"run_id": run_id, "error": f"{type(e).__name__}: {e}"})
        return None
    store.append_record("run_bundle", {"run_id": run_id, "path": str(path), "manifest_sha256": digest})
    return digest

def read_run_bundle_bytes(path: Path) -> tuple[dict[str, Any], str]:
    """(manifest, sha256 of manifest.json), read under the caps."""
    try:
        with zipfile.ZipFile(path) as z:
            data = BoundedZip(z).read("manifest.json")
    except zipfile.BadZipFile as e:
        raise RunBundleError(f"not a zip: {e}") from e
    try:
        manifest = json.loads(data)
    except ValueError as e:
        raise RunBundleError(f"manifest.json is not JSON: {e}") from e
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        fmt = manifest.get("format") if isinstance(manifest, dict) else None
        raise RunBundleError(f"not a run bundle: format {fmt!r}")
    return manifest, _sha(data)


def read_bundle_manifest(path: Path) -> dict[str, Any]:
    """manifest.json of any chh bundle (run or claim), read under the caps; the format is not checked here."""
    try:
        with zipfile.ZipFile(path) as z:
            data = BoundedZip(z).read("manifest.json")
        manifest = json.loads(data)
    except (zipfile.BadZipFile, ValueError) as e:
        raise RunBundleError(f"bundle manifest unreadable: {e}") from e
    if not isinstance(manifest, dict):
        raise RunBundleError("bundle manifest is not an object")
    return manifest


def read_run_bundle(path: Path) -> dict[str, Any]:
    return read_run_bundle_bytes(path)[0]


def check_task_files(manifest: dict[str, Any], task_dir: str | Path) -> list[str]:
    """Differences between the task files recorded in the bundle and the files on disk now."""
    now, then = task_file_hashes(task_dir), manifest.get("task_files") or {}
    return sorted([f"changed: {k}" for k in then if k in now and now[k] != then[k]]
                  + [f"missing: {k}" for k in then if k not in now] + [f"added: {k}" for k in now if k not in then])


# ------------------------------------------------------------------ artifact check
@dataclass
class ArtifactReport:
    rows: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # everything worth telling the user, fatal or not
    fatal: list[str] = field(default_factory=list)  # reasons to refuse (fail closed)

    def to_json(self) -> dict[str, Any]:
        return {"rows": self.rows, "problems": self.problems, "fatal": self.fatal}


def _candidates(a: dict[str, Any], task_dir: str | None, roots: list[Path]) -> list[Path]:
    p = str(a["path"])
    out: list[Path] = []
    if p.startswith("task/") and task_dir:
        out.append(Path(task_dir) / p[len("task/"):])
    elif not p.startswith("workspace/"):
        out.append(Path(p))
    name = PurePosixPath(p.replace("\\", "/"))
    for r in roots:
        out += [r / name.name] + ([r / Path(*name.parts[1:])] if len(name.parts) > 1 else [])
    return out


def _consistency(manifest: dict[str, Any], raw: list[dict[str, Any]], rep: ArtifactReport) -> None:
    """Every task file and every workspace file the manifest records elsewhere must be listed, with the same hash (a
    rewritten artifact list cannot quietly drop one)."""
    listed = {a["path"]: a["sha256"] for a in raw}
    want = {f"task/{k}": v for k, v in (manifest.get("task_files") or {}).items()}
    refused = set(manifest.get("refused_private_blobs") or [])
    files = ((manifest.get("workspace_snapshot") or {}).get("files") or {})
    want |= {f"workspace/{k}": v.get("blob") for k, v in files.items() if k not in refused and isinstance(v, dict)}
    for path, h in sorted(want.items()):
        if listed.get(path) != h:
            msg = f"artifact {path} is recorded by the manifest but not listed (or listed with another hash)"
            rep.problems.append(msg)
            rep.fatal.append(msg)


def check_artifacts(manifest: dict[str, Any], bundle_path: Path | None, *, blobs: BlobStore | None,
                    roots: list[Path] | tuple[Path, ...] = (), task_dir: str | None = None) -> ArtifactReport:
    """Resolve every artifact the manifest lists; see the module docstring for the rules."""
    rep = ArtifactReport()
    raw = manifest.get("artifacts")
    if raw is None:
        rep.problems.append("bundle has no artifact manifest (built before GOAL L2 large artifacts): not checked")
        return rep
    if not isinstance(raw, list):
        raise RunBundleError("manifest artifacts must be a list")
    task_dir = task_dir or manifest.get("task_dir")
    try:
        zf = zipfile.ZipFile(bundle_path) if bundle_path is not None else None
    except (zipfile.BadZipFile, OSError) as e:
        raise RunBundleError(f"not a readable zip: {e}") from e
    try:
        reader = BoundedZip(zf) if zf is not None else None
        for a in raw:
            if (not isinstance(a, dict) or a.get("status") not in STATUSES or not isinstance(a.get("sha256"), str)
                    or not isinstance(a.get("bytes"), int) or not isinstance(a.get("path"), str)
                    or not isinstance(a.get("required"), bool) or a.get("kind") not in KINDS):
                raise RunBundleError(f"malformed artifact entry: {a!r}")
            row = {"path": a["path"], "status": a["status"], "kind": a.get("kind"), "required": bool(a["required"]),
                   "result": "missing", "found_at": None}
            got: tuple[int, str] | None = None
            member = f"blobs/{a['sha256']}"
            if a["status"] == "bundled" and reader is not None and member in reader.infos:
                data = reader.read(member)
                got, row["found_at"] = (len(data), _sha(data)), "bundle"
            elif blobs is not None and blobs.has(a["sha256"]):
                try:
                    got, row["found_at"] = _file_sha(blobs._path(a["sha256"])), "blob store"
                except OSError:
                    got = None
            if got is None:
                for c in _candidates(a, task_dir, list(roots)):
                    if c.is_file():
                        got, row["found_at"] = _file_sha(c), str(c)
                        break
            if got is None:
                what = "required artifact missing" if row["required"] else "artifact missing (optional)"
                msg = (f"{what}: {a['path']} ({a.get('kind')}, {a['bytes']} bytes, sha256 {a['sha256'][:12]}, "
                       f"{a['status']})")
                rep.problems.append(msg)
                if row["required"]:
                    rep.fatal.append(msg)
                elif a["status"] == "bundled":  # the manifest says the zip carries it: the bundle is damaged
                    rep.fatal.append(f"bundled artifact absent from the bundle: {a['path']}")
            elif got != (a["bytes"], a["sha256"]):
                row["result"] = "hash_mismatch"
                msg = (f"artifact hash differs: {a['path']} found at {row['found_at']} has {got[0]} bytes, sha256 "
                       f"{got[1][:12]}; the manifest says {a['bytes']} bytes, sha256 {a['sha256'][:12]}")
                rep.problems.append(msg)
                rep.fatal.append(msg)
            else:
                row["result"] = "present"
            rep.rows.append(row)
        _consistency(manifest, raw, rep)
    finally:
        if zf is not None:
            zf.close()
    return rep
