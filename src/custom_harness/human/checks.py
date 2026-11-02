"""HUMAN registration of a human-held check (GOAL invariant 22, §16.7; assure/humancheck.py runs and gates them).

Invariant 22 needs at least one check that no AI agent designed or can see. A human writes it and drops it into
`<data_dir>/human/checks/`. `register_check` is the human's registration step, bound to the sha256 of the file's raw
bytes (the content is hashed, never decoded, printed or recorded):

1. the file must exist in the checks directory under a plain file name and hash to the sha256 the human names;
2. `registry.json` in that directory gets (or keeps) the entry {id, file, sha256, registered_by, registered};
3. `redteam/human_checks.lock` in the repository gets {id: sha256} (the human commits it, so the commitment is
   visible in git history);
4. a Kernel ledger entry records who registered which hash: kind `approval`, node id `human_check_registration`,
   run id `human_check:<id>`.

A different file under an id that is already registered is refused unless `replace=True` (a new check is a new
human decision, and the old hash stays in the ledger). Nothing here runs a check: `python scripts/human_check.py
--run` does (a human action, once per milestone).

Honest limits (assure/humancheck.py states them in full): agents can read the data directory; nothing here proves
the registrant is a person; the check's adequacy is the human's responsibility.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from ..assure import humancheck as H
from ..kernel.ledger import Entry, Ledger
from ..store import EvidenceStore
from .catalog import HUMAN_CHECK_NODE
from .guard import HumanActionRefused, require_human

CHECK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RUN_PREFIX = "human_check:"


def default_repo_root() -> Path:
    """The repository this package was imported from (src/custom_harness/human/checks.py -> repository root)."""
    return Path(__file__).resolve().parents[3]


def _plain(fname: str) -> bool:
    return bool(fname) and Path(fname).name == fname and fname not in (".", "..") and "/" not in fname \
        and "\\" not in fname


def _read_registry(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"checks": []}
    try:
        doc = json.loads(path.read_bytes().decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise HumanActionRefused(f"{path} is malformed ({type(e).__name__}); fix it by hand first") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("checks"), list):
        raise HumanActionRefused(f"{path} is not a {{\"checks\": [...]}} document")
    return doc


def _read_lock(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_bytes().decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise HumanActionRefused(f"{path} is malformed ({type(e).__name__}); fix it by hand first") from None
    if not isinstance(doc, dict):
        raise HumanActionRefused(f"{path} is not a JSON object")
    return {str(k): str(v).lower() for k, v in doc.items()}


def registrations(store: EvidenceStore) -> list[dict[str, Any]]:
    """Every intact ledgered human-check registration, oldest first: {id, sha256, approver, ledger_seq}."""
    out = []
    for e in Ledger(store).entries("approval"):
        b = e.body
        if e.intact() and b.get("node_id") == HUMAN_CHECK_NODE and b.get("decision") == "approved" \
                and isinstance(b.get("check"), dict) and isinstance(b.get("approver"), str) and b["approver"].strip():
            out.append({**b["check"], "approver": b["approver"], "ledger_seq": e.seq})
    return out


def register_check(store: EvidenceStore, check_id: str, *, by: str, sha256: str, file_name: str | None = None,
                   repo_root: Path | None = None, data_dir: Path | None = None, replace: bool = False,
                   today: dt.date | None = None) -> Entry:
    """HUMAN action: register the check file `file_name` (default `<id>.py`) under `check_id`, bound to `sha256`."""
    name = require_human(by, what="the human registering the check")
    if not isinstance(check_id, str) or CHECK_ID.fullmatch(check_id) is None:
        raise HumanActionRefused("a check id is 1-64 characters of letters, digits, '.', '_' or '-'")
    want = str(sha256).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", want) is None:
        raise HumanActionRefused("name the full sha256 (64 hex digits) of the check file you wrote")
    fname = file_name or f"{check_id}.py"
    if not _plain(fname):
        raise HumanActionRefused("the check file must be a plain file name inside the checks directory")
    root = Path(repo_root) if repo_root is not None else default_repo_root()
    lock_path = root / H.LOCK
    if not lock_path.parent.is_dir():
        raise HumanActionRefused(f"{lock_path.parent} does not exist: pass the repository root")
    cdir = H.checks_dir(data_dir)
    f = cdir / fname
    if not f.is_file():
        raise HumanActionRefused(f"no check file {fname!r} in {cdir} (a human puts it there first)")
    got = H.sha256_bytes_of(f)
    if got != want:
        raise HumanActionRefused(f"{fname} hashes to {got}, not the {want} you named")
    reg_path = cdir / "registry.json"
    reg = _read_registry(reg_path)
    lock = _read_lock(lock_path)
    old = [r for r in reg["checks"] if isinstance(r, dict) and str(r.get("id")) == check_id]
    old_sha = {str(r.get("sha256", "")).lower() for r in old} | ({lock[check_id]} if check_id in lock else set())
    if old_sha - {want} and not replace:
        raise HumanActionRefused(f"check {check_id!r} is already registered with another hash; pass replace to "
                                 f"register this file in its place (a new human decision)")
    day = (today or dt.date.today()).isoformat()
    reg["checks"] = [r for r in reg["checks"] if not (isinstance(r, dict) and str(r.get("id")) == check_id)]
    reg["checks"].append({"id": check_id, "file": fname, "sha256": want, "registered_by": name, "registered": day})
    lock[check_id] = want
    reg_path.write_text(json.dumps(reg, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    lock_path.write_text(json.dumps(dict(sorted(lock.items())), indent=1) + "\n", encoding="utf-8")
    item = {"id": check_id, "sha256": want, "registered": day}
    from ..runtime.engine import approval_item_hash
    return Ledger(store).append("approval", {
        "run_id": f"{RUN_PREFIX}{check_id}", "node_id": HUMAN_CHECK_NODE, "item_hash": approval_item_hash(item),
        "decision": "approved", "approver": name, "note": "", "check": item})


def pending_checks(store: EvidenceStore, *, repo_root: Path | None = None,
                   data_dir: Path | None = None) -> list[dict[str, Any]]:
    """What invariant 22 waits on (read-only): no check at all; a check on disk whose hash no human registered in
    the ledger; a registered check with no passing ledgered run of its exact file."""
    root = Path(repo_root) if repo_root is not None else default_repo_root()
    if not (root / H.LOCK).parent.is_dir():
        return [{"kind": "human_check", "action": "unknown", "binds": {},
                 "detail": f"repository root {root} has no redteam/ directory; status unknown",
                 "decide": "run from the repository checkout: python scripts/human_check.py --status"}]
    status, notes = H.status(root, data_dir)
    if status == "NOT MET":
        return [{"kind": "human_check", "action": "write_and_register", "binds": {"sha256": "<of the file you wrote>"},
                 "detail": H.NOT_MET_LINE,
                 "decide": "a human writes a check into <data_dir>/human/checks/ID.py, then: chh register-human-check "
                           "ID --by NAME --sha256 H"}]
    out: list[dict[str, Any]] = []
    ledgered = {(r.get("id"), r.get("sha256")) for r in registrations(store)}
    report = H.verify_hashes(root, data_dir)
    for cid, sha in sorted(report.checks.items()):
        if (cid, sha) not in ledgered:
            out.append({"kind": "human_check", "action": "register", "id": cid, "binds": {"sha256": sha},
                        "decide": f"chh register-human-check {cid} --by NAME --sha256 {sha}"})
    if status in ("REGISTERED-NOT-RUN", "FAIL"):
        out.append({"kind": "human_check_run", "action": "run", "status": status, "notes": list(notes),
                    "binds": dict(sorted(report.checks.items())),
                    "decide": "python scripts/human_check.py --run (a human, once per milestone)"})
    return out
