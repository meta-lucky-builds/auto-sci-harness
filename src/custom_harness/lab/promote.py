"""Promote a Lab idea to the Court (GOAL §17): freeze, hand off to the EXISTING Court path, release only a verdict bit.

    chh lab promote SESSION --request promote.yaml      freeze: code + environment, recorded before any target exists
    chh lab handoff PROMOTION --datasets targets.yaml   write the Court spec (chh-audit/1) naming the held-out targets
    ... the existing Court commands, run by the owner (nothing here pre-registers or certifies):
    chh audit show / approve-fidelity (HUMAN) / prepare / approve-role (HUMAN) / run --docker / report
    chh lab release-verdict PROMOTION --by NAME         HUMAN: give the Lab the verdict label, as a ledgered release

1. Freeze (`freeze`). The box is paused (no agent process runs while host code reads the workspace), the walls and the
   content scan run again, then:
   - a manifest of the whole workspace (relative path -> sha256, size; links recorded, never followed) is hashed: the
     workspace snapshot hash. Only the manifest and the three frozen files are stored in the Court's store
     (a workspace can hold gigabytes of caches; the bytes stay in the Lab workspace, the hash pins them);
   - the method file, the baseline file and the package lock are copied from that frozen read into the intake dir
     `<data>/lab_court/<promotion>/` (outside the lab root: never mounted into any box) and into the store's blobs;
   - the lock must pin every requirement exactly (`name==version`; `--hash` lines recorded when present). The box's
     own `pip freeze` is recorded next to it (box-reported, not trusted);
   - the image id is recorded.
2. Hand-off (`handoff`). The held-out targets are named only AFTER the freeze is recorded (the record order shows it),
   and a deny-only screen refuses a target whose OpenML id the frozen code mentions. The Court spec is the existing
   Claim Audit format (`audit/spec.py`, chh-audit/1): the method arm is the Lab's frozen code, the baseline arm the
   frozen baseline, the claim the researcher's typed claim from the request. `load_spec` validates it and gives the
   spec hash that the human fidelity approval binds. From there, the Court is the existing, unchanged path:
   pre-registration with the Kernel BEFORE the data are read (`chh audit prepare`), both arms under one contract in
   the network-free candidate sandbox (`chh audit run --docker`), K0-K9 with re-execution, certificate or rejection.
3. Feedback (`release_verdict`). The Lab sees nothing of the Court by default. A human may release the verdict LABEL
   (a finite alphabet) to the session workspace; it goes through the release ledger (`kerneld.releases.Releases`,
   channel `prompt`, alphabet `verdict_bit`, one release per held-out target), which K4 counts. Since int18 the
   verification BADGES (court/levels.py, GOAL §17.1: REPRODUCIBLE / REPLICATED / VALIDATED / RE-VALIDATED, and
   "RE-VALIDATION-FAILED") go with it, as a second release (alphabet `count`, at most 16 values: no families,
   reasons or numbers), and into the discovery trace (`promote/verdict_released.released_level`). Failed checks,
   reasons, effects and intervals are never released here.

Two Court routes per freeze (exactly one may be taken):
  - the MINTED route (int18, stage-a; court/minted.py, `chh court register PROMOTION --family F ...`): the claim is
    pre-registered first, then kerneld mints fresh held-out truth after the freeze (GOAL §16.2): eligible for
    VALIDATED and RE-VALIDATED (court/levels.py);
  - the RETROSPECTIVE route (`handoff`, below): fixed public OpenML targets, which a networked Lab can download:
    hence the freeze-then-name order, the deny-only screen and the assumption on every such certificate; it can
    never hold VALIDATED or RE-VALIDATED and is labelled RETROSPECTIVE (GOAL §17.1).

Built, NOT test-verified.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..audit import program
from ..audit.spec import Budget, ClaimPart, OpenMLDataset, load_spec
from ..config import data_dir
from ..sandbox.base import is_link_like
from ..store import EvidenceStore
from . import EXPLORATORY
from . import trace as T
from .box import DockerCLI, LabBox
from .egress import collect_web
from .session import _record, started, workspace_of
from .walls import (
    LabWallError,
    configured_secret,
    lab_root,
    overlaps,
    require_workspace,
    scan_workspace,
)

FORMAT = "chh-lab-promote/1"
RECORD_KIND = "lab_promotion"
LOCK_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9.+!_-]+(\s*;.*)?(\s*\\)?$")
HASH_LINE = re.compile(r"^--hash=sha256:[0-9a-f]{64}(\s*\\)?$")
MANIFEST_MAX_FILES = 500_000  # UNCALIBRATED (owner decision, GOAL §13.8)
FROZEN_FILE_MAX_BYTES = 16 << 20  # UNCALIBRATED (owner decision, GOAL §13.8): method / baseline / lock


class PromoteError(RuntimeError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class PromoteRequest(_Model):
    """chh-lab-promote/1: written by the researcher (a human), not by the Lab agent."""

    format: str = Field(pattern=r"^chh-lab-promote/1$")
    title: str = Field(min_length=3, max_length=300)
    sentence: str = Field(min_length=10, max_length=2000)  # the claim in words; the human fidelity approval reads it
    method: str  # workspace-relative path: build_model(seed) -> fit/predict (the audit wrapper's contract)
    baseline: str
    lock: str  # workspace-relative path of the pinned package lock
    claim: ClaimPart
    budget: Budget
    idea: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")  # trace idea_id it came from
    contract: Literal["build_model", "program"] = "build_model"  # program: audit/program.py (minted and sealed routes)
    method_files: tuple[str, ...] = ()  # program contract: more workspace-relative text files of the method arm
    baseline_files: tuple[str, ...] = ()


class TargetsFile(_Model):
    format: str = Field(pattern=r"^chh-lab-targets/1$")
    datasets: tuple[OpenMLDataset, ...]


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def lock_problems(text: str) -> list[str]:
    """Problems of a package lock (empty = every requirement pinned exactly). Refused: unpinned or ranged
    requirements, editable or URL/VCS requirements, other indexes, includes of other files."""
    problems = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if HASH_LINE.match(line):
            continue
        if line.startswith(("-e", "--editable", "-r", "--requirement", "-c", "--constraint", "--index-url",
                            "--extra-index-url", "-i", "--find-links", "-f", "--trusted-host")):
            problems.append(f"line {n}: {line.split()[0]} is refused in a frozen lock")
        elif "://" in line or " @ " in line:
            problems.append(f"line {n}: URL / VCS requirements are refused (pin a released version)")
        elif not LOCK_LINE.match(line):
            problems.append(f"line {n}: not an exact pin `name==version`: {line[:80]!r}")
    return problems


def _manifest(ws: Path) -> dict[str, Any]:
    try:
        return T.workspace_manifest(ws, MANIFEST_MAX_FILES)
    except T.TraceRefused as e:
        raise PromoteError(f"{e}: move caches out first") from None


def _frozen_file(ws: Path, rel: str, manifest: dict[str, Any], what: str) -> bytes:
    """Read a file the manifest recorded, and check its bytes are the ones recorded (no change since)."""
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise PromoteError(f"{what}: {rel!r} is not a plain workspace-relative path")
    key = Path(rel).as_posix()
    entry = manifest["files"].get(key)
    if entry is None:
        raise PromoteError(f"{what}: {rel} is not a regular file in the frozen workspace (missing or a link)")
    if entry["size"] > FROZEN_FILE_MAX_BYTES:
        raise PromoteError(f"{what}: {rel} is larger than {FROZEN_FILE_MAX_BYTES} bytes")
    from ..sandbox.base import safe_path
    data = safe_path(ws, key).read_bytes()
    if _sha(data) != entry["sha256"]:
        raise PromoteError(f"{what}: {rel} changed during the freeze (box not paused?)")
    return data


def _frozen_arm(ws: Path, req: PromoteRequest, manifest: dict[str, Any], entry: str, more: tuple[str, ...], what: str
                ) -> bytes:
    """What is frozen for one arm: its file, or for the program contract its files as one bundle (audit/program.py)."""
    if req.contract != "program":
        if more:
            raise PromoteError(f"{what}: extra files need the program contract")
        return _frozen_file(ws, entry, manifest, what)
    files = {Path(r).as_posix(): _frozen_file(ws, r, manifest, what) for r in (entry, *more)}
    try:
        return program.bundle(Path(entry).as_posix(), files)
    except program.ProgramError as e:
        raise PromoteError(f"{what}: {e}") from None


def intake_root() -> Path:
    """Where frozen promotions wait for the Court: `<data>/lab_court`, never inside the lab root."""
    return data_dir() / "lab_court"


def freeze(store: EvidenceStore, session_id: str, request_path: Path, *, docker: DockerCLI | None = None
           ) -> dict[str, Any]:
    rec = started(store, session_id, allow_stopped=True)
    raw = Path(request_path).read_bytes()
    try:
        req = PromoteRequest.model_validate(yaml.safe_load(raw.decode("utf-8")))
    except (yaml.YAMLError, UnicodeDecodeError, ValueError) as e:
        raise PromoteError(f"{request_path}: not a valid {FORMAT} request: {e}") from e
    if overlaps(request_path, lab_root()):
        raise PromoteError("the promotion request must be written by the researcher outside the lab root (a file in "
                           "a workspace is agent-writable)")
    if overlaps(intake_root(), lab_root()):
        raise LabWallError(f"intake dir {intake_root()} overlaps the lab root {lab_root()}")
    ws = workspace_of(session_id)
    lb = LabBox(session_id, docker)
    paused = lb.state() == "running" and lb.pause()
    if lb.state() == "running":
        raise PromoteError("could not pause the box: host code never reads a workspace while agent code runs")
    try:
        require_workspace(ws)
        secret, secret_file = configured_secret()
        scan = scan_workspace(ws, store=store, secret=secret, secret_file=secret_file)
        if scan:
            raise LabWallError("workspace refused at promotion: " + "; ".join(scan))
        # bring the discovery trace up to date first (agent-reported ideas, file changes), then check the idea
        agent = T.Actor(type="agent", id=str(rec.get("agent")), agent=str(rec.get("agent")),
                        model=(rec.get("options") or {}).get("model"))
        T.ingest_agent_events(store, session_id, ws, agent)
        T.record_workspace_diff(store, session_id, ws, T.system())
        if lb.state() != "absent":  # a stopped session's gateway went with it, and stop() collected its log
            collect_web(store, session_id, lb.docker)
        if req.idea is not None and req.idea not in T.idea_tree(store, session_id):
            raise PromoteError(f"idea {req.idea!r} is not in the session's discovery trace")
        manifest = _manifest(ws)
        method = _frozen_arm(ws, req, manifest, req.method, req.method_files, "method")
        baseline = _frozen_arm(ws, req, manifest, req.baseline, req.baseline_files, "baseline")
        lock = _frozen_file(ws, req.lock, manifest, "lock")
        if method == baseline:
            raise PromoteError("method and baseline are the same code")
        lp = lock_problems(lock.decode("utf-8", "replace"))
        if lp:
            raise PromoteError("lock refused: " + "; ".join(lp[:20]))
        box_freeze = lb.docker.call(["exec", lb.name, "python", "-m", "pip", "freeze", "--all"], timeout=300) \
            if paused and lb.unpause() else None
    finally:
        if paused and lb.state() == "paused":
            lb.unpause()
    manifest_ref = store.blobs.put(_canonical(manifest))
    refs = {"method": store.blobs.put(method), "baseline": store.blobs.put(baseline), "lock": store.blobs.put(lock),
            "request": store.blobs.put(raw)}
    pid = _sha(_canonical({"session": session_id, "manifest": manifest_ref, **refs}))[:16]
    out = intake_root() / pid
    out.mkdir(parents=True, exist_ok=True)
    (out / "method.py").write_bytes(method)
    (out / "baseline.py").write_bytes(baseline)
    (out / "requirements.lock").write_bytes(lock)
    (out / "request.yaml").write_bytes(raw)
    body = {"promotion_id": pid, "session_id": session_id, "event": "frozen", "label": EXPLORATORY,
            "notice": "frozen for the Court; still not a finding until a certificate exists",
            "workspace_snapshot": manifest_ref, "n_files": len(manifest["files"]), "links": len(manifest["links"]),
            "frozen": {k: {"blob": v} for k, v in refs.items()},
            "lock_has_hashes": "--hash=sha256:" in lock.decode("utf-8", "replace"),
            "box_pip_freeze_sha256": _sha(box_freeze.stdout.encode()) if box_freeze and box_freeze.returncode == 0
            else None,
            "image": rec.get("box", {}).get("image"), "image_id": rec.get("image_id"),
            "agent": rec.get("agent"), "intake_dir": str(out), "request": req.model_dump(mode="json"),
            "at": time.time()}
    path = T.idea_path(store, session_id, req.idea) if req.idea else []
    body["idea_id"], body["idea_path"] = req.idea, path
    store.append_record(RECORD_KIND, body)
    T.record(store, session_id, "promote", {"op": "frozen", "promotion_id": pid, "workspace_snapshot": manifest_ref,
                                            "idea_id": req.idea, "idea_path": path}, actor=T.human())
    (out / "freeze.json").write_text(json.dumps(body, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _record(store, session_id, "promoted", promotion_id=pid)
    return body


def _promotion(store: EvidenceStore, pid: str) -> list[dict[str, Any]]:
    rows = store.records_where(RECORD_KIND, "promotion_id", pid)
    if not rows or rows[0].get("event") != "frozen":
        raise PromoteError(f"no frozen promotion {pid!r}")
    return rows


def _screen(code: bytes, datasets: tuple[OpenMLDataset, ...]) -> list[str]:
    """Deny-only: a target the frozen code names (its OpenML id near the word openml, or its file hash) is refused.
    Passing this screen proves nothing (the code may fetch data by other means)."""
    text = code.decode("utf-8", "replace")
    low = text.lower()
    hits = []
    for d in datasets:
        if d.sha256 in low or ("openml" in low and re.search(rf"(?<![0-9]){d.openml_id}(?![0-9])", text)):
            hits.append(f"openml:{d.openml_id}")
    return hits


def handoff(store: EvidenceStore, pid: str, targets_path: Path) -> dict[str, Any]:
    rows = _promotion(store, pid)
    if any(r.get("event") == "handed_off" for r in rows):
        raise PromoteError(f"promotion {pid} was already handed off (one Court spec per freeze)")
    frozen = rows[0]
    if PromoteRequest.model_validate(frozen["request"]).contract != "build_model":
        raise PromoteError("the retrospective Claim Audit route takes the build_model contract only")
    if overlaps(targets_path, lab_root()):
        raise PromoteError("the targets file must be written outside the lab root")
    try:
        tf = TargetsFile.model_validate(yaml.safe_load(Path(targets_path).read_text(encoding="utf-8")))
    except (yaml.YAMLError, UnicodeDecodeError, ValueError, OSError) as e:
        raise PromoteError(f"{targets_path}: not a valid chh-lab-targets/1 file: {e}") from e
    method = store.blobs.get(frozen["frozen"]["method"]["blob"])
    baseline = store.blobs.get(frozen["frozen"]["baseline"]["blob"])
    hits = sorted(set(_screen(method, tf.datasets)) | set(_screen(baseline, tf.datasets)))
    if hits:
        raise PromoteError(f"the frozen code names Court target(s) {hits}: choose targets it does not reference")
    req = PromoteRequest.model_validate(frozen["request"])
    out = Path(frozen["intake_dir"])
    if (out / "method.py").read_bytes() != method or (out / "baseline.py").read_bytes() != baseline:
        raise PromoteError(f"intake files in {out} differ from the frozen blobs: re-freeze")
    spec = {"format": "chh-audit/1",
            "paper": {"doi": f"lab:{frozen['session_id']}:{pid}", "title": req.title, "sentence": req.sentence},
            "claim": req.claim.model_dump(mode="json"), "method": {"path": "method.py"},
            "baseline": {"path": "baseline.py"}, "budget": req.budget.model_dump(mode="json"),
            "datasets": [d.model_dump(mode="json") for d in tf.datasets]}
    spec_path = out / "audit.yaml"
    spec_path.write_text("# Court spec of a Lab promotion (written by custom_harness/lab/promote.py)\n"
                         + yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    loaded = load_spec(spec_path)  # the existing validator; fails closed
    claim = loaded.claim()
    body = {"promotion_id": pid, "event": "handed_off", "label": EXPLORATORY, "spec_path": str(spec_path),
            "spec_hash": loaded.spec_hash, "audit_id": loaded.audit_id, "claim_hash": claim.claim_hash(),
            "targets": list(claim.generalization_targets), "frozen_at": frozen.get("at"), "at": time.time(),
            "court_route": "claim-audit (retrospective OpenML targets; minted route not wired)",
            "next": [f"chh audit show {spec_path}",
                     f"chh audit approve-fidelity {spec_path} --approver NAME --spec-hash {loaded.spec_hash}",
                     f"chh audit prepare {spec_path}",
                     "chh approve-role DATASET --approver NAME   (each target, HUMAN)",
                     f"chh audit run {spec_path} --docker",
                     f"chh audit report {spec_path}"]}
    store.append_record(RECORD_KIND, body)
    T.record(store, frozen["session_id"], "promote", {"op": "handed_off", "promotion_id": pid,
                                                      "claim_hash": body["claim_hash"], "spec_hash": loaded.spec_hash,
                                                      "audit_id": loaded.audit_id,
                                                      "idea_id": frozen.get("idea_id")}, actor=T.human())
    return body


def release_verdict(store: EvidenceStore, pid: str, by: str, *, docker: DockerCLI | None = None) -> dict[str, Any]:
    """HUMAN: write the Court's verdict LABEL for promotion `pid` into its session workspace, as a ledgered release
    (one per held-out target, alphabet verdict_bit). Nothing else of the Court reaches the Lab."""
    if not by.strip():
        raise PromoteError("name the human releasing the verdict (--by)")
    rows = _promotion(store, pid)
    hand = [r for r in rows if r.get("event") == "handed_off"]
    if not hand:
        raise PromoteError(f"promotion {pid} has no Court spec yet (chh lab handoff)")
    if hand[-1].get("route") == "minted":  # int18: the minted Court route (court/minted.py)
        from ..court.minted import latest
        outcome = latest(store, str(hand[-1]["case_id"]), "outcome")
    else:
        from ..audit.claim_audit import latest_outcome
        outcome = latest_outcome(store, hand[-1]["audit_id"])
    if outcome is None:
        raise PromoteError(f"the Court has no outcome for promotion {pid} yet")
    session_id = rows[0]["session_id"]
    level: str | None = None
    if outcome.get("cert_hash"):
        from ..court.display import emit_outcome
        from ..court.levels import level as level_of
        from ..kerneld.scope import ScopedKernel
        cert = ScopedKernel(store).certificate(outcome["cert_hash"])
        label = cert.label.value
        targets = list(cert.claim.get("generalization_targets") or hand[-1]["targets"])
        # the verification badges (GOAL §17.1; court/levels.py, code only): LevelResult.release_value, a closed
        # alphabet of at most 16 values, released as a COUNT, which K4 counts as such
        level = level_of(store, str(outcome["cert_hash"])).release_value()
        emit_outcome(store, label, level, targets, channel="prompt", label_note="lab:promotion_verdict",
                     extra_note="lab:promotion_level", key=f"lab:{pid}")
        payload: dict[str, Any] = {"promotion_id": pid, "label": label, "level": level}
    elif not outcome.get("labels_read"):
        targets, label = [], "COULD NOT RUN"
        payload = {"promotion_id": pid, "status": "COULD NOT RUN (no held-out label was read)"}
    else:
        raise PromoteError("the Court read labels but issued no certificate: releasing anything needs a review")
    payload["notice"] = ("verdict label and verification badges only; reasons, effects and intervals stay with "
                         "the Court (GOAL §17)")
    _write_into_workspace(session_id, f".court/verdict-{pid}.json", json.dumps(payload, sort_keys=True), docker)
    body = {"promotion_id": pid, "event": "verdict_released", "label": EXPLORATORY, "released_label": label,
            "released_level": level, "targets": targets, "by": by, "at": time.time(),
            "released_on": date.today().isoformat()}
    store.append_record(RECORD_KIND, body)
    ev: dict[str, Any] = {"op": "verdict_released", "promotion_id": pid, "released_label": label}
    if level is not None:
        ev["released_level"] = level
    T.record(store, session_id, "promote", ev, actor=T.human(by))
    return body


def _write_into_workspace(session_id: str, rel: str, text: str, docker: DockerCLI | None) -> None:
    """Host write into an agent-controlled tree: box paused, every path component checked (no link, no junction)."""
    from ..sandbox.base import safe_path
    ws = workspace_of(session_id)
    lb = LabBox(session_id, docker)
    paused = lb.state() == "running" and lb.pause()
    if lb.state() == "running":
        raise PromoteError("could not pause the box for the host write")
    try:
        require_workspace(ws)
        parent = safe_path(ws, Path(rel).parent.as_posix())
        parent.mkdir(exist_ok=True)
        target = safe_path(ws, rel)
        if target.exists() and is_link_like(target):
            raise LabWallError(f"{rel} is a link")
        target.write_text(text, encoding="utf-8")
    finally:
        if paused:
            lb.unpause()
