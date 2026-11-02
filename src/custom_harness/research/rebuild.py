"""Deterministic, verifiable rebuild of a task suite: freeze the manifest, then mint the data (GOAL §16.1-16.3).

    chh build-suite OUT --frozen --record-dir DIR --beacon-file FILE (--docker | --real-export FILE) [--verify]

Order (each step fails closed):
1. The MANIFEST is written: generator version and code hash, every task (id, family, source, rows, split), the
   held-out families, the SHA-256 of each real dataset the trusted exporter produced, the candidate image agent code
   runs in, and a SHA-256 commitment to the sealed beacon. No task data exist yet.
2. FREEZE: the manifest is registered in the Kernel ledger (`register_artifact`, kind `suite_manifest`) and the
   ledger is anchored to `<record-dir>/anchor.jsonl` (outside the store). `suite.require_anchored` must then pass.
3. MINT: master key = draw("heldout_mint", prereg_hash=manifest hash, beacon). Every synthetic task's features,
   labels and split, every real/repair task's split, and every canary come from independent keyed streams under it
   (suite.build_suite). The fixed public datasets stay retrospective targets: only their split is drawn here.

Secrets. The beacon is SEALED: it is held in a file outside every agent-readable root
(commitment.default_agent_readable_roots) until the suite retires. With it, anyone holding the repo and the manifest
re-derives every label, so a PUBLIC beacon value (e.g. a drand round) must never be used for a long-lived suite.
Neither the beacon, the master key nor any task key is written anywhere; suite.yaml and manifest.json carry
SHA-256 commitments only. The private record (`<record-dir>/record.json`: manifest hash, anchor, the hash of every
file, private ones included) and the real-data export (`real_export.json`: every labelled row) also stay there.

Idempotent: a second run with the same manifest and beacon re-derives the suite in a temporary directory and compares
it with OUT ("unchanged"); it never overwrites a different suite, and it adds no ledger entry and no anchor line.
`verify` re-derives the suite from the record, the beacon and the export and reports every differing file.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from ..commitment import agent_readable_root_of, as_key, default_agent_readable_roots, draw, secret_leak_scan
from ..truth_layout import truth_dir
from . import suite as S
from .suite import MintRefused, dataset_hash, require_anchored

MANIFEST_FORMAT = "chh-suite-manifest/1"
RECORD_FORMAT = "chh-suite-record/1"
MANIFEST_KIND = "suite_manifest"
TEST_FRACTION = 0.3  # suite._split keeps 70% for training (fixed in the code; recorded for the reader)

__all__ = ["MintRefused", "RebuildRefused", "dataset_hash", "manifest_hash", "rebuild", "suite_digest", "verify"]


class RebuildRefused(ValueError):
    pass


# ---------------------------------------------------------------- hashing
def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def manifest_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(_canon(manifest)).hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generator_code_hash() -> str:
    """SHA-256 of research/suite.py with line endings normalised (the code that turns the key into data)."""
    return _sha(Path(S.__file__).read_bytes().replace(b"\r\n", b"\n"))


def _normalised_suite_yaml(out: Path) -> bytes:
    doc = yaml.safe_load((out / "suite.yaml").read_text(encoding="utf-8"))
    for t in doc.get("tasks", []):
        t["dir"] = Path(t["dir"]).name  # absolute paths differ between equal suites built in different places
    return _canon(doc)


def task_digest(out: Path) -> dict[str, str]:
    """SHA-256 of every task file (public and private), keyed by path relative to `out`. A task's truth is keyed
    `<task>/private/<file>` wherever truth_layout keeps it, so a suite under `<data>/suites` (truth outside the tree)
    and the same suite built in a scratch dir (legacy in-task private/) give the same keys. An unmigrated suite under
    `<data>/suites` is refused (TruthLayoutError), never digested from its agent-readable copy."""
    from ..kerneld.client import truth_query
    from ..truth_layout import LEGACY_DIRNAME

    out = Path(out)
    d = {p.relative_to(out).as_posix(): _sha(p.read_bytes()) for p in sorted(out.rglob("*"))
         if p.is_file() and p.parent != out}
    for task in sorted(p for p in out.iterdir() if p.is_dir()) if out.is_dir() else []:
        # the truth hashes come from kerneld (GOAL 16.5.2): this host never reads the truth bytes; kerneld down raises
        for rel, h in truth_query("truth_hashes", task_dir=str(task))["hashes"].items():
            d.setdefault(f"{task.name}/{LEGACY_DIRNAME}/{rel}", h)
    return dict(sorted(d.items()))


def suite_digest(out: Path) -> dict[str, str]:
    """task_digest plus suite.yaml (with task dirs made relative) and manifest.json when present."""
    out = Path(out)
    d = task_digest(out)
    if (out / "suite.yaml").is_file():
        d["suite.yaml#normalised"] = _sha(_normalised_suite_yaml(out))
    if (out / "manifest.json").is_file():
        d["manifest.json"] = _sha((out / "manifest.json").read_bytes())
    return d


# ---------------------------------------------------------------- the manifest (everything fixed before the data)
def _stub(task_id: str, family: str, source: str | None, retrospective: bool) -> S.TaskSpecFiles:
    return S.TaskSpecFiles(task_id, "", "", "", "y", ["y"], [], [], "", family, source=source,
                           retrospective_target=retrospective)


def build_manifest(name: str, *, real_data: dict[str, dict] | None, beacon: str,
                   heldout_families: tuple[str, ...] = S.DEFAULT_HELDOUT_FAMILIES, heldout_rows: int | None = None,
                   null_families: tuple[str, ...] = ()) -> dict[str, Any]:
    from ..sandbox.docker import CANDIDATE_IMAGE, FULL_IMAGE

    probe = b"\x00" * 32  # task ids do not depend on the key; nothing built with this probe is kept
    tasks: list[dict[str, Any]] = []
    stubs = []
    for gen, seed, n in S.synthetic_plan(tuple(null_families)):
        tid = gen(seed, 1, key=probe).task_id
        fam = tid.rsplit("-s", 1)[0]
        tasks.append({"id": tid, "family": fam, "source": tid, "kind": "synthetic", "rows": n})
        stubs.append(_stub(tid, fam, None, False))
    real_hashes: dict[str, str] = {}
    if real_data is not None:
        for rname in S.REAL:
            real_hashes[rname] = dataset_hash(real_data[rname])
            tasks.append({"id": f"{rname}-real", "family": "real", "source": rname, "kind": "real",
                          "rows": len(real_data[rname]["y"])})
            stubs.append(_stub(f"{rname}-real", "real", rname, True))
        for tid, (rname, _model, _defect) in S.REPAIRS.items():
            tasks.append({"id": tid, "family": "repair", "source": rname, "kind": "repair",
                          "rows": len(real_data[rname]["y"])})
            stubs.append(_stub(tid, "repair", rname, True))
    splits = S.assign_splits(stubs, heldout_families)
    for t in tasks:
        t["split"] = splits[t["id"]]
    return {"format": MANIFEST_FORMAT, "suite": name, "generator_version": S.GENERATOR_VERSION,
            "generator_code_sha256": generator_code_hash(), "test_fraction": TEST_FRACTION,
            "heldout_families": list(heldout_families), "tasks": sorted(tasks, key=lambda t: t["id"]),
            "real_export_sha256": real_hashes, "candidate_image": CANDIDATE_IMAGE,
            "exporter_image": FULL_IMAGE if real_data is not None else None,
            "beacon_commitment": _sha(beacon.encode()), "draw_purpose": "heldout_mint",
            # frozen only when set, so a manifest built without them hashes as before
            **({"heldout_rows": heldout_rows} if heldout_rows is not None else {}),
            **({"null_families": sorted(null_families)} if null_families else {})}


def generator_block(manifest: dict[str, Any], mhash: str) -> dict[str, Any]:
    """Extra provenance for suite.yaml's generator block (commitments only)."""
    return {"key_source": "draw:heldout_mint(manifest_sha256, sealed beacon)", "manifest_sha256": mhash,
            "beacon_commitment": manifest["beacon_commitment"], "generator_code_sha256":
                manifest["generator_code_sha256"], "minted_after_suite_freeze": True}


# ---------------------------------------------------------------- secrets and locations
def read_beacon(path: Path) -> str:
    b = Path(path).read_text(encoding="utf-8").strip()
    if len(b) < 32:
        raise RebuildRefused(f"the beacon in {path} is shorter than 32 characters: too guessable")
    return b


def _inside(p: Path, roots: Iterable[Path]) -> Path | None:
    """Alias-safe (pathsafe): a `\\\\?\\`, admin-share, 8.3 or other-case spelling of a path in a root is in it."""
    return agent_readable_root_of(p, roots)


def require_private_locations(record_dir: Path, beacon_file: Path) -> None:
    """Refuse (RebuildRefused) a record directory or beacon file inside an agent-readable root."""
    _require_private(**{"record directory": Path(record_dir), "beacon file": Path(beacon_file)})


def _require_private(**paths: Path) -> None:
    roots = default_agent_readable_roots(existing_only=False)
    for what, p in paths.items():
        r = _inside(p, roots)
        if r is not None:
            raise RebuildRefused(f"the {what} {p} is inside agent-readable root {r}: secret or labelled material "
                                 f"must live outside every agent-readable root")


def _store_real_export(record_dir: Path, real_data: dict[str, dict] | None) -> None:
    if real_data is None:
        return
    p = record_dir / "real_export.json"
    data = _canon({k: real_data[k] for k in sorted(real_data)})
    if p.is_file():
        if p.read_bytes() != data:
            raise RebuildRefused(f"the real-data export differs from the one recorded in {p}: a rebuild must use the "
                                 f"recorded export (archive the record to start a new suite)")
        return
    p.write_bytes(data)


def load_real_export(path: Path) -> dict[str, dict]:
    return dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------- freeze
def freeze(manifest: dict[str, Any], *, vault: Any, store_root: Path, anchor_file: Path) -> dict[str, Any]:
    """Record the manifest in the ledger (idempotent: an identical manifest is registered once) and anchor the ledger
    unless the latest anchor already covers that entry. Returns require_anchored's {"entry_seq", "length", "head"}."""
    from ..kernel.ledger import write_anchor

    h = manifest_hash(manifest)
    rec = vault.register_artifact(f"suite-manifest/{manifest['suite']}/{h}", _canon(manifest), MANIFEST_KIND)
    if rec.hash != h:  # the vault hashes the bytes it stores; they must be the manifest hash
        raise RebuildRefused("the registered manifest hash differs from the manifest hash")
    covered = 0
    if Path(anchor_file).is_file():
        lines = [json.loads(ln) for ln in Path(anchor_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
        covered = max((int(a["length"]) for a in lines), default=0)
    if covered < rec.seq:
        write_anchor(vault.ledger, Path(anchor_file), Path(store_root))
    return require_anchored(h, vault.ledger.entries(), Path(anchor_file))


def master_key(mhash: str, beacon: str) -> bytes:
    return as_key(draw("heldout_mint", prereg_hash=mhash, beacon=beacon, secret=None))


def _build(out: Path, manifest: dict[str, Any], mhash: str, master: bytes,
           real_data: dict[str, dict] | None) -> None:
    S.build_suite(out, real_data=real_data, key=master, heldout_families=tuple(manifest["heldout_families"]),
                  generator_extra=generator_block(manifest, mhash), heldout_rows=manifest.get("heldout_rows"),
                  null_families=tuple(manifest.get("null_families", ())))
    doc = yaml.safe_load((out / "suite.yaml").read_text(encoding="utf-8"))
    built = {t["id"]: t["split"] for t in doc["tasks"]}
    frozen = {t["id"]: t["split"] for t in manifest["tasks"]}
    if built != frozen:
        diff = sorted(set(built.items()) ^ set(frozen.items()))[:6]
        raise RebuildRefused(f"the built tasks/splits differ from the frozen manifest: {diff}")
    (out / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    leaks = secret_leak_scan(master, [out])
    if leaks:
        raise RebuildRefused(f"the master key was written into the suite: {leaks[:3]}")


def _digest_hash(d: dict[str, str]) -> str:
    return _sha(_canon(d))


# ---------------------------------------------------------------- rebuild / verify
def rebuild(out: Path, *, record_dir: Path, beacon_file: Path, vault: Any, store_root: Path,
            real_data: dict[str, dict] | None, name: str = "m2",
            heldout_families: tuple[str, ...] = S.DEFAULT_HELDOUT_FAMILIES, heldout_rows: int | None = None,
            null_families: tuple[str, ...] = ()) -> dict[str, Any]:
    """Freeze the manifest, then mint the suite into `out` (see the module docstring). Returns a summary with
    status "built" or "unchanged"."""
    out, record_dir = Path(out), Path(record_dir)
    require_private_locations(record_dir, Path(beacon_file))
    beacon = read_beacon(Path(beacon_file))
    record_dir.mkdir(parents=True, exist_ok=True)
    _store_real_export(record_dir, real_data)
    manifest = build_manifest(name, real_data=real_data, beacon=beacon, heldout_families=heldout_families,
                              heldout_rows=heldout_rows, null_families=null_families)
    mhash = manifest_hash(manifest)
    anchor = freeze(manifest, vault=vault, store_root=store_root, anchor_file=record_dir / "anchor.jsonl")
    master = master_key(mhash, beacon)
    with tempfile.TemporaryDirectory(dir=record_dir) as tmp:  # labelled scratch: inside the private record dir
        ref = Path(tmp) / out.name
        _build(ref, manifest, mhash, master, real_data)
        want = suite_digest(ref)
        out_truth = truth_dir(out)  # where OUT's tasks keep their truth (outside agent-readable roots)
        if not (out.exists() and any(out.iterdir())) and out_truth.is_dir() and any(out_truth.iterdir()):
            raise RebuildRefused(f"{out} is empty but its truth dir {out_truth} is not: archive that too (a rebuild "
                                 f"never overwrites held-out truth)")
        if out.exists() and any(out.iterdir()):
            have = suite_digest(out)
            if have != want:
                diff = sorted(k for k in set(have) | set(want) if have.get(k) != want.get(k))
                raise RebuildRefused(f"{out} holds a suite that differs from this rebuild ({len(diff)} file(s), e.g. "
                                     f"{diff[:3]}): archive it first; a rebuild never overwrites")
            status = "unchanged"
        else:
            status = "built"
    if status == "built":
        _build(out, manifest, mhash, master, real_data)
        have = suite_digest(out)
        if have != want:  # the same inputs must give the same bytes, wherever they are written
            raise RebuildRefused("the suite written to OUT differs from the reference build: not deterministic")
    record = {"format": RECORD_FORMAT, "suite": name, "out": str(out.resolve()), "manifest_sha256": mhash,
              "manifest": manifest, "anchor": anchor, "anchor_file": str((record_dir / "anchor.jsonl").resolve()),
              "digest": want, "digest_sha256": _digest_hash(want)}
    rp = record_dir / "record.json"
    if rp.is_file():
        old = json.loads(rp.read_text(encoding="utf-8"))
        if old.get("manifest_sha256") != mhash or old.get("digest_sha256") != record["digest_sha256"]:
            raise RebuildRefused(f"{rp} records another suite build: archive the record directory first")
    else:
        rp.write_text(json.dumps(record, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return {"status": status, "out": str(out), "manifest_sha256": mhash, "anchor": anchor,
            "digest_sha256": record["digest_sha256"], "tasks": len(manifest["tasks"])}


def verify(out: Path, *, record_dir: Path, beacon_file: Path, ledger_entries: list[Any]) -> dict[str, Any]:
    """Re-derive the suite from the private record, the beacon and the recorded export and compare every file.
    Returns {"problems": [...], ...}; empty problems = OUT is exactly what the frozen manifest mints."""
    out, record_dir = Path(out), Path(record_dir)
    problems: list[str] = []
    rec = json.loads((record_dir / "record.json").read_text(encoding="utf-8"))
    manifest = rec["manifest"]
    mhash = manifest_hash(manifest)
    if mhash != rec["manifest_sha256"]:
        problems.append("the recorded manifest does not hash to the recorded manifest hash")
    public = json.loads((out / "manifest.json").read_text(encoding="utf-8")) if (out / "manifest.json").is_file() \
        else None
    if public != manifest:
        problems.append("OUT/manifest.json differs from the recorded manifest")
    beacon = read_beacon(Path(beacon_file))
    if _sha(beacon.encode()) != manifest["beacon_commitment"]:
        problems.append("the beacon does not match the manifest's beacon commitment")
        return {"problems": problems, "manifest_sha256": mhash}
    if manifest["generator_code_sha256"] != generator_code_hash():
        problems.append("research/suite.py differs from the code the manifest froze: re-derivation uses other code")
    try:
        require_anchored(mhash, ledger_entries, Path(rec["anchor_file"]))
    except MintRefused as e:
        problems.append(f"the manifest is not anchored: {e}")
    real = None
    if manifest["real_export_sha256"]:
        real = load_real_export(record_dir / "real_export.json")
        bad = sorted(k for k, h in manifest["real_export_sha256"].items() if dataset_hash(real[k]) != h)
        if bad:
            problems.append(f"the recorded real-data export differs from the manifest for {bad}")
    with tempfile.TemporaryDirectory(dir=record_dir) as tmp:
        ref = Path(tmp) / out.name
        _build(ref, manifest, mhash, master_key(mhash, beacon), real)
        want, have = suite_digest(ref), suite_digest(out)
    problems += [f"{k} differs from the re-derived suite" for k in sorted(set(want) | set(have))
                 if want.get(k) != have.get(k)]
    if _digest_hash(want) != rec["digest_sha256"]:
        problems.append("the re-derived suite differs from the digest recorded at build time")
    return {"problems": problems, "manifest_sha256": mhash, "files": len(want), "digest_sha256": _digest_hash(have)}


# ---------------------------------------------------------------- COUPLED-PRNG regression (an attacker's view)
def load_generator_module(path: Path) -> ModuleType:
    """Load a stand-alone copy of generator code (e.g. the pre-fix suite.py from the repo history) as a module."""
    spec = importlib.util.spec_from_file_location(f"_attacker_{_sha(str(path).encode())[:8]}", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


def _test_labels(spec: Any) -> list[str]:
    ti = spec.header.index(spec.target)
    seen = {tuple(v for j, v in enumerate(r) if j != ti) for r in spec.train}
    return [r[ti] for r in spec.test if tuple(v for j, v in enumerate(r) if j != ti) not in seen]


def prng_regression(suite_dir: Path, generators: ModuleType) -> dict[str, dict[str, Any]]:
    """The attack of the COUPLED-PRNG finding (GOAL §16.1): regenerate every synthetic task from `generators`
    (code an agent can read, with its published seeds) and compare with the task's held-out labels. Reads the
    private labels, through kerneld (`label_agreement`), to SCORE the attack only.
    Returns {task_id: {"exact", "agree", "n", "train_features_match"}}."""
    from ..kerneld.client import truth_query

    out: dict[str, dict[str, Any]] = {}
    for gen, seed, n in generators.SYNTHETIC:
        spec = gen(seed, n)
        d = Path(suite_dir) / spec.task_id
        guess = _test_labels(spec)
        res = truth_query("label_agreement", task_dir=str(d), guess=guess)
        if not res["present"]:
            continue
        train_rows = (d / "public" / "train.csv").read_text(encoding="utf-8").splitlines()[1:]
        regen_train = {",".join(r) for r in spec.train}
        out[spec.task_id] = {"exact": res["exact"], "agree": res["agree"], "n": res["n"],
                             "train_features_match": sum(r in regen_train for r in train_rows)}
    return out
