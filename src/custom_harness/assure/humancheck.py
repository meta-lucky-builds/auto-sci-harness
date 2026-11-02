"""Scaffold for the human-held check (GOAL section 10 invariant 22, section 16.7, docs/governance.md section 2).

A human drops check scripts into `<data_dir>/human/checks/` and commits their sha256 in `redteam/human_checks.lock`;
the same hashes are in `<data_dir>/human/checks/registry.json`. This module only VERIFIES hashes, RUNS verified
scripts with output discarded, and LEDGERS {id, sha256, result, at, git HEAD}. It never decodes, prints, logs or
records script content or output. With no registered check the status is "NOT MET": nothing here can satisfy
invariant 22 without a human-supplied check.

Honest limit: agents can still read files under the data dir; only a filesystem permission would stop that.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

LOCK = "redteam/human_checks.lock"
LEDGER = "redteam/human_check_runs.jsonl"
DEFAULT_TIMEOUT_S = 1800.0
NOT_MET_LINE = "human-held check: NOT MET (no check registered; invariant 22 needs the owner)"


@dataclass
class Report:
    """status: NOT MET | OK | FAIL. checks: id -> sha256 (as computed from the file bytes, when the file exists)."""

    status: str
    problems: list[str] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)


def checks_dir(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from custom_harness.config import data_dir as _dd
        data_dir = _dd()
    return Path(data_dir) / "human" / "checks"


def sha256_bytes_of(path: Path) -> str:
    """Hash of the file's raw bytes. The content is never decoded or exposed."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> object:
    return json.loads(path.read_bytes().decode("utf-8"))


def _registry(cdir: Path) -> tuple[dict[str, dict[str, str]], list[str]]:
    """id -> entry. Returns (entries, problems). A missing registry file is an empty registry."""
    reg = cdir / "registry.json"
    if not reg.is_file():
        return {}, []
    try:
        doc = _load_json(reg)
        rows = doc["checks"]  # type: ignore[index]
        out: dict[str, dict[str, str]] = {}
        problems: list[str] = []
        for r in rows:
            cid, fname, sha = str(r["id"]), str(r["file"]), str(r["sha256"]).lower()
            if cid in out:
                problems.append(f"registry: duplicate id {cid!r}")
            if not fname or Path(fname).name != fname or fname in (".", "..") or "/" in fname or "\\" in fname:
                problems.append(f"registry: {cid!r} file must be a plain file name inside the checks dir")
            out[cid] = {"file": fname, "sha256": sha}
        return out, problems
    except Exception as e:  # noqa: BLE001 - any malformed registry fails closed
        return {}, [f"registry.json unreadable or malformed ({type(e).__name__})"]


def _lock(repo_root: Path) -> tuple[dict[str, str], list[str]]:
    p = Path(repo_root) / LOCK
    if not p.is_file():
        return {}, [f"{LOCK} missing"]
    try:
        doc = _load_json(p)
        if not isinstance(doc, dict):
            raise ValueError("not an object")
        return {str(k): str(v).lower() for k, v in doc.items()}, []
    except Exception as e:  # noqa: BLE001
        return {}, [f"{LOCK} unreadable or malformed ({type(e).__name__})"]


def verify_hashes(repo_root: Path, data_dir: Path | None = None) -> Report:
    """Hash each registered file's bytes; compare to the registry AND the repository lock. Fails closed."""
    cdir = checks_dir(data_dir)
    reg, problems = _registry(cdir)
    lock, lprob = _lock(repo_root)
    # A missing lock file only matters if something is registered (an empty repo state is "no check").
    problems += [p for p in lprob if reg or lock or not p.endswith("missing")]
    if not reg and not lock and not problems:
        return Report("NOT MET")
    for cid in sorted(set(reg) ^ set(lock)):
        problems.append(f"{cid!r}: present in only one of registry / lock")
    checks: dict[str, str] = {}
    for cid, ent in sorted(reg.items()):
        f = cdir / ent["file"]
        if not f.is_file():
            problems.append(f"{cid!r}: check file missing")
            continue
        got = sha256_bytes_of(f)
        checks[cid] = got
        if got != ent["sha256"]:
            problems.append(f"{cid!r}: file hash differs from registry")
        if cid in lock and got != lock[cid]:
            problems.append(f"{cid!r}: file hash differs from lock")
        if cid in lock and ent["sha256"] != lock[cid]:
            problems.append(f"{cid!r}: registry and lock disagree")
    return Report("FAIL" if problems else "OK", problems, checks)


# ---------------------------------------------------------------------------------------------------- ledger
def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").split("\n") if ln.strip()]


def _git_head(repo_root: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"], capture_output=True, text=True,
                           check=False, timeout=30)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def append_ledger(repo_root: Path, cid: str, sha256: str | None, result: str, code: str,
                  now: dt.datetime | None = None) -> dict[str, object]:
    """Append one record: id, sha256, result (pass|fail), code, at, git HEAD, prev. No output, no content."""
    path = Path(repo_root) / LEDGER
    lines = _lines(path)
    rec: dict[str, object] = {
        "id": cid, "sha256": sha256, "result": result, "code": code,
        "at": (now or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "head": _git_head(Path(repo_root)), "prev": _line_hash(lines[-1]) if lines else "genesis",
    }
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


def check_chain(repo_root: Path) -> tuple[bool, str]:
    """The run ledger is an intact hash chain (genesis first, each prev = sha256 of the previous line)."""
    path = Path(repo_root) / LEDGER
    prev = "genesis"
    for i, ln in enumerate(_lines(path), 1):
        try:
            rec = json.loads(ln)
            ok = isinstance(rec, dict) and rec.get("prev") == prev
        except ValueError:
            ok = False
        if not ok:
            return False, f"{LEDGER}: chain broken at line {i}"
        prev = _line_hash(ln)
    return True, f"{LEDGER}: chain intact ({len(_lines(path))} records)"


# ---------------------------------------------------------------------------------------------------- running
def run_checks(repo_root: Path, data_dir: Path | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> Report:
    """Verify, then run each verified check as `python <file> <repo_root>` (cwd = fresh temp dir, output discarded)."""
    repo_root = Path(repo_root)
    rep = verify_hashes(repo_root, data_dir)
    cdir = checks_dir(data_dir)
    if rep.status == "NOT MET":
        return rep
    if rep.status == "FAIL":
        reg, _ = _registry(cdir)
        for cid in sorted(reg):  # nothing is executed; the failure itself is ledgered (no content)
            append_ledger(repo_root, cid, rep.checks.get(cid), "fail", "verify-failed")
        return rep
    reg, _ = _registry(cdir)
    problems: list[str] = []
    for cid in sorted(reg):
        f = cdir / reg[cid]["file"]
        sha = reg[cid]["sha256"]
        try:
            now_sha = sha256_bytes_of(f)  # TOCTOU: hash again immediately before executing
        except OSError:
            now_sha = ""
        if now_sha != sha:
            append_ledger(repo_root, cid, now_sha or None, "fail", "changed-before-run")
            problems.append(f"{cid!r}: file changed before run; not executed")
            continue
        result, code = "fail", "launch-error"
        with tempfile.TemporaryDirectory(prefix="humancheck-") as tmp:
            try:
                p = subprocess.run([sys.executable, str(f), str(repo_root)], cwd=tmp, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_s,
                                   check=False)
                result, code = ("pass", "exit-0") if p.returncode == 0 else ("fail", f"exit-{p.returncode}")
            except subprocess.TimeoutExpired:
                result, code = "fail", "timeout"
            except OSError:
                result, code = "fail", "launch-error"
        append_ledger(repo_root, cid, sha, result, code)
        if result != "pass":
            problems.append(f"{cid!r}: {code}")
    return Report("FAIL" if problems else "OK", problems, rep.checks)


# ---------------------------------------------------------------------------------------------------- status/gate
def latest_results(repo_root: Path) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for ln in _lines(Path(repo_root) / LEDGER):
        try:
            rec = json.loads(ln)
            out[str(rec["id"])] = rec
        except (ValueError, KeyError, TypeError):
            continue
    return out


def status(repo_root: Path, data_dir: Path | None = None) -> tuple[str, list[str]]:
    """(status, notes). NOT MET when nothing is registered; never 'pass' without a registered, verified, run check.

    Statuses: NOT MET | FAIL | REGISTERED-NOT-RUN | LAST-RUN-PASSED.
    """
    rep = verify_hashes(repo_root, data_dir)
    if rep.status == "NOT MET":
        return "NOT MET", [NOT_MET_LINE]
    if rep.status == "FAIL":
        return "FAIL", rep.problems
    ok, msg = check_chain(repo_root)
    if not ok:
        return "FAIL", [msg]
    last = latest_results(repo_root)
    notes = []
    state = "LAST-RUN-PASSED"
    for cid, sha in sorted(rep.checks.items()):
        rec = last.get(cid)
        if rec is None or rec.get("sha256") != sha:
            state = "REGISTERED-NOT-RUN" if state != "FAIL" else state
            notes.append(f"{cid!r}: no ledgered run of this exact file yet")
        elif rec.get("result") != "pass":
            state = "FAIL"
            notes.append(f"{cid!r}: last ledgered run failed ({rec.get('code')})")
    return state, notes


def gate(repo_root: Path, data_dir: Path | None = None) -> tuple[bool, list[str]]:
    """The check.py step. Non-failing when no check is registered; failing on hash mismatch, a ledgered failed
    run, or a broken ledger chain. It does not run the checks (up to 30 minutes each); `--run` does."""
    ok, msg = check_chain(repo_root)
    if not ok:
        return False, ["human-held check: FAIL", msg]
    st, notes = status(repo_root, data_dir)
    if st == "NOT MET":
        return True, [NOT_MET_LINE]
    return st != "FAIL", [f"human-held check: {st}", *notes]
