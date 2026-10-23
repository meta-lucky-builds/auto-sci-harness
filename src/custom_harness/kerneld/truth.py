"""Truth queries, run INSIDE the kerneld process (GOAL §16.5.2; s4-kerneld-review finding 3): host code that needs a
FACT about a task's held-out truth (its vault target id, the hashes of its truth files, how well a guess agrees with
the labels, whether exported files leak it) asks kerneld, which reads the truth (truth_layout) and answers with ids,
hashes, counts or location strings. Truth bytes never leave this process.

Kinds (server op `truth_query`, client `RunHandle.truth_query` / `kerneld.client.truth_query`):
  target_id        {task_dir}            -> {"target": <vault id of the task's held-out data>}   (evolve/multiplicity)
  truth_hashes     {task_dir}            -> {"hashes": {<rel path in the truth dir>: sha256}}     (research/rebuild)
  label_agreement  {task_dir, guess}     -> {"present", "n", "agree", "exact"}                    (research/rebuild)
  export_scan      {tasks, root}         -> {"problems": [location strings]}                      (evolve/export)
                   tasks = {task_id: task_dir}

Stated limits: `truth_hashes` returns sha256 of truth files (the ledger already holds the same digests for
registered private files: the offline-oracle limit, docs/kerneld_boundary.md); `label_agreement` is a scoring answer
for a caller-supplied guess (an attack-regression tool, trusted CLI only; not rate-limited). Each query appends a
`kerneld_truth_query` store record (kind, tasks, run), so its use is auditable.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

RECORD_KIND = "kerneld_truth_query"
MAX_PRIVATE_SCAN = 1024 * 1024  # bytes: a larger truth file is fingerprinted by hash only (evolve/export.py's rule)
MAX_GUESS = 1_000_000  # labels in one label_agreement guess


class TruthQueryRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def target_id(task_dir: str) -> dict[str, Any]:
    """The vault id research.setup registers for a task's held-out data (content-based; kerneld/ingest.research_task
    computes the same digest): sha256 over the per-file sha256 of train, test features and test labels."""
    from ..research.task import load_task
    from ..truth_layout import truth_file
    from .ingest import _read
    d = Path(task_dir)
    try:
        task = load_task(d)
    except (FileNotFoundError, ValueError):
        raise TruthQueryRefused("not_found") from None
    parts = [_read(d / "public" / task.train_file), _read(d / "public" / task.test_features_file),
             _read(truth_file(d, task.test_labels_file))]
    digest = hashlib.sha256(b"".join(hashlib.sha256(b).digest() for b in parts)).hexdigest()
    return {"target": task.dataset_ids(digest, digest)["test"]}


def truth_hashes(task_dir: str) -> dict[str, Any]:
    from ..truth_layout import truth_files
    return {"hashes": {rel: _sha(p.read_bytes()) for rel, p in truth_files(Path(task_dir)).items()}}


def label_agreement(task_dir: str, guess: list[str], labels_file: str = "test_y.csv") -> dict[str, Any]:
    from ..truth_layout import truth_file
    if len(guess) > MAX_GUESS:
        raise TruthQueryRefused("bad_request")
    p = truth_file(Path(task_dir), labels_file)
    if not p.is_file():
        return {"present": False, "n": 0, "agree": 0, "exact": False}
    truth = [ln for ln in p.read_text(encoding="utf-8").splitlines()[1:] if ln]
    agree = sum(a == b for a, b in zip(guess, truth, strict=False))
    return {"present": True, "n": len(truth), "agree": agree, "exact": guess == truth}


def _fingerprints(tasks: dict[str, str]) -> dict[str, Any]:
    """evolve/export.py's truth fingerprints (moved here, 2026-10-15): hashes of every truth file, canary tokens and
    verbatim contents of small ones, label windows of a CSV, and every spelling of the truth directories' paths."""
    from ..truth_layout import legacy_truth_dir, truth_dir, truth_files
    from ..verify.leak import CANARY_PREFIX, label_windows
    hashes: dict[str, str] = {}
    contents: list[str] = []
    windows: list[str] = []
    paths: list[str] = []
    canaries: list[str] = []
    for task_id, td in sorted(tasks.items()):
        d = Path(td)
        files = truth_files(d)  # refuses an unmigrated suite task (TruthLayoutError) instead of fingerprinting nothing
        if not files:
            continue
        for pdir in {truth_dir(d), legacy_truth_dir(d)}:
            for variant in {str(pdir), pdir.as_posix(), str(pdir.resolve()), pdir.resolve().as_posix()}:
                paths.append(variant.replace("\\", "/").lower())
        for rel, p in files.items():
            data = p.read_bytes()
            hashes[_sha(data)] = f"{task_id}/private/{rel}"
            if len(data) > MAX_PRIVATE_SCAN:
                continue
            text = data.decode("utf-8", errors="ignore")
            canaries += [tok for tok in text.split() if tok.startswith(CANARY_PREFIX)]
            if len(text.strip()) >= 16:
                contents.append(text.strip())
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            windows += label_windows(lines[1:])  # a truth CSV: header, then one label per line
    return {"hashes": hashes, "contents": contents, "windows": windows, "paths": sorted(set(paths)),
            "canaries": sorted(set(canaries))}


def _strings(obj: Any, keys: list[str]) -> Any:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(str(k))
            yield str(k)
            yield from _strings(v, keys)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, keys)


def export_scan(tasks: dict[str, str], root: str) -> dict[str, Any]:
    """The TRUTH part of evolve/export.py's `_audit` over every file under `root`: a copy of a truth file (by hash), a
    canary of a truth file, a label window, verbatim truth content, a truth directory's path. Problems are location
    strings with the same wording as before; they never quote a value."""
    from ..verify.leak import scan_text
    fp = _fingerprints(tasks)
    base = Path(root)
    problems: list[str] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        data = p.read_bytes()
        h = _sha(data)
        if h in fp["hashes"]:
            problems.append(f"{rel} is a copy of private file {fp['hashes'][h]}")
        raw = data.decode("utf-8", errors="replace")
        try:
            texts = [raw, *_strings(json.loads(raw), [])]
        except json.JSONDecodeError:
            texts = [raw]  # the host's own audit reports invalid JSON (fail closed) itself
        for text in texts:
            problems += scan_text(text, rel, fp["canaries"], fp["windows"])
            if any(c in text for c in fp["contents"]):
                problems.append(f"verbatim content of a task's private file found in {rel}")
            norm = text.replace("\\", "/").lower()
            if any(pp in norm for pp in fp["paths"]):
                problems.append(f"a task's private directory path found in {rel}")
    return {"problems": list(dict.fromkeys(problems))}


def query(store: Any, run_id: str, kind: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one query (server.op_truth_query); `args` are already type-checked there. A truth location the layout
    refuses is `truth_layout`; a missing task file `not_found`."""
    from ..truth_layout import TruthLayoutError
    from .ingest import IngestRefused
    try:
        if kind == "target_id":
            res = target_id(args["task_dir"])
            tasks = [args["task_dir"]]
        elif kind == "truth_hashes":
            res = truth_hashes(args["task_dir"])
            tasks = [args["task_dir"]]
        elif kind == "label_agreement":
            res = label_agreement(args["task_dir"], args["guess"])
            tasks = [args["task_dir"]]
        elif kind == "export_scan":
            res = export_scan(args["tasks"], args["root"])
            tasks = sorted(args["tasks"].values())
        else:
            raise TruthQueryRefused("bad_request")
    except IngestRefused as e:  # the no-symlink reader: not_found
        raise TruthQueryRefused(e.code) from None
    except TruthLayoutError:  # a PermissionError (OSError): before the generic missing-file mapping
        raise TruthQueryRefused("truth_layout") from None
    except OSError:
        raise TruthQueryRefused("not_found") from None
    store.append_record(RECORD_KIND, {"run_id": run_id, "kind": kind, "tasks": tasks, "at": time.time()})
    return res
