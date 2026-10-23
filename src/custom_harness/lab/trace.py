"""The Lab DISCOVERY TRACE: a versioned, agent-agnostic, append-only record of how a Lab session got to an idea
(GOAL §17; orchestrator data contract 2026-10-23). Schema `chh-lab-trace/1`, documented in docs/lab.md §9.

Every event is one `lab_trace` store record (store.append_record), labelled EXPLORATORY, with
- a stable id: `event_id` = sha256 of the canonical event without its id (32 hex), so the same event always has the
  same id and any edit changes it;
- a per-session sequence number `seq` and `prev` = the previous event's id in that session (a hash chain: a removed
  or reordered event breaks `verify_chain`);
- `actor` (who: human / agent / system / backend, with the agent and model when known) and `source` (how it reached
  the trace: the manager itself, an agent's self-report, a human command, a compute backend).

Kinds and their payloads (pydantic models below, `extra="forbid"`):
  session     start / stop / attach of the box (manager)
  idea        propose / branch / abandon (reason required) / revisit, with a stable `idea_id` and `parent_id`: the
              idea tree
  run         command, env hash, compute backend, cost ref, duration, exit code, outputs as content hashes, metrics on
              DEV / TRAIN data only
  file        workspace changes as content hashes (before, after); never content
  literature  retrieval-log entry digests and the ids they returned (literature/retrieval.py RetrievalLog)
  action      one coarse agent step: tool, target, outcome, agent, model
  human       steer / edit / stop / redirect / prompt / attach, with the human's identity; prompt text by hash
  cost        per call or per prompt: agent, model, usd (None = unknown, never 0), tokens, who reported it
  promote     frozen / handed_off / verdict_released: links the frozen snapshot to the pre-registered claim

Guards (fail closed, `TraceRefused`), applied to every event whatever its source:
- no held-out truth: run metrics may name only splits `dev` or `train`; a metric whose dataset is a vault dataset
  with a role other than DEV is refused; a run output or file hash equal to a PRIVATE vault blob is refused;
- no verdict details: keys such as `verdict`, `certificate`, `checks`, `test_score` are refused anywhere in a
  payload; the only verdict-shaped value in the trace is the label of a `promote/verdict_released` event, written by
  `promote.release_verdict` after its ledgered `verdict_bit` release;
- no secrets: values that look like keys or tokens (sk-..., ghp_..., AKIA..., long secrets under secret-like keys)
  are refused; environments are recorded as a hash only; file events hold hashes, never bytes;
- bounded sizes (strings, lists, events per ingest).

Agent-agnostic intake:
- the manager writes session / human / action / cost / file / promote events itself (session.py, promote.py);
- ANY agent may append self-reported events (ideas, runs, literature) as JSON lines to `/workspace/.lab/trace.jsonl`;
  the manager ingests them after each headless prompt and at promotion, with the box paused. They are untrusted:
  validated against the same schema and guards, tagged `source: agent-reported`, de-duplicated by `client_id`;
- compute backends (the parallel `lab-compute` work) and humans add events through `record()` / `chh lab trace-add`.
  On int18 (stage-a) every lab-compute producer (compute jobs, Lab literature search, LaTeX / Court paper / figure
  builds) writes HERE through `discovery_trace.LabTraceSink` (source `backend`): compute lifecycle -> `run` events
  with the optional lifecycle fields, literature search -> `literature`, builds -> `action` with output hashes. One
  schema, one record kind, one chain per session; lab-compute's separate JSONL chain is gone.

The story of a discovery (`story`): a Court certificate's claim names the promotion (`paper_doi = lab:<session>:
<promotion>` in its scope conditions, written by promote.handoff), and the `promote/handed_off` event names the claim
hash, so certificate -> promotion -> session -> idea path -> every event of the session, in order.

Built, NOT test-verified.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..sandbox.base import is_link_like
from ..store import EvidenceStore
from . import EXPLORATORY

SCHEMA = "chh-lab-trace/1"
RECORD_KIND = "lab_trace"
AGENT_TRACE_FILE = ".lab/trace.jsonl"  # workspace-relative; the agent-reported intake
MAX_TEXT = 2000  # UNCALIBRATED (owner decision, GOAL §13.8): characters per free-text field
MAX_LIST = 500  # UNCALIBRATED (owner decision, GOAL §13.8): items per list field
MAX_INGEST = 5000  # UNCALIBRATED (owner decision, GOAL §13.8): agent-reported events per ingest
MAX_FILE_EVENTS = 2000  # UNCALIBRATED (owner decision, GOAL §13.8): file events per workspace diff (then a summary)
MAX_INGEST_BYTES = 16 << 20  # UNCALIBRATED (owner decision, GOAL §13.8)

_HEX64 = r"^[0-9a-f]{64}$"
_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"
FORBIDDEN_KEYS = frozenset({"verdict", "certificate", "cert", "cert_hash", "checks", "failed_checks", "passed_checks",
                            "test_score", "heldout_score", "held_out_score", "test_labels", "labels", "truth",
                            "test_y", "canary", "reasons"})
_SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                           r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|"
                           r"hf_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|token|secret|passw|credential|cookie|private[_-]?key)")
DEV_SPLITS = ("dev", "train")


class TraceRefused(ValueError):
    pass


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


Text = Annotated[str, Field(max_length=MAX_TEXT)]
Sha = Annotated[str, Field(pattern=_HEX64)]
Ident = Annotated[str, Field(pattern=_ID)]


class Actor(_M):
    type: Literal["human", "agent", "system", "backend"]
    id: Annotated[str, Field(min_length=1, max_length=120)]  # human name / agent adapter / backend name
    agent: str | None = None
    model: str | None = None


class SessionEv(_M):
    op: Literal["start", "stop", "attach", "start_failed"]
    agent: str | None = None
    model: str | None = None
    image: str | None = None
    image_id: str | None = None


class IdeaEv(_M):
    op: Literal["propose", "branch", "abandon", "revisit", "merge"]
    idea_id: Ident
    parent_id: Ident | None = None
    title: Annotated[str, Field(max_length=300)] = ""
    summary: Text = ""
    reason: Text = ""  # required for abandon


class Metric(_M):
    name: Annotated[str, Field(max_length=80)]
    value: float
    split: Literal["dev", "train"]  # DEV data only: never a held-out / test split
    dataset: str | None = None


class Output(_M):
    path: Annotated[str, Field(max_length=500)]
    sha256: Sha
    size: int | None = None


class Revision(_M):
    kind: Annotated[str, Field(max_length=40)]
    ref: Annotated[str, Field(max_length=300)]


Short = Annotated[str, Field(max_length=200)]


class RunEv(_M):
    run_id: Ident
    command: Annotated[str, Field(max_length=MAX_TEXT)]
    env_hash: Sha | None = None  # hash of the environment description; the environment itself is never recorded
    backend: Annotated[str, Field(max_length=80)] = "lab-box"
    idea_id: Ident | None = None
    duration_s: float | None = None
    exit_code: int | None = None
    cost_event: Ident | None = None
    outputs: Annotated[tuple[Output, ...], Field(max_length=MAX_LIST)] = ()
    metrics: Annotated[tuple[Metric, ...], Field(max_length=MAX_LIST)] = ()
    # optional compute-backend lifecycle fields (lab-compute's producers through discovery_trace.LabTraceSink; int18
    # stage-a: one schema for every Lab event). Minor change within chh-lab-trace/1 (new OPTIONAL fields only).
    op: Literal["submitted", "started", "log", "finished", "failed", "cancelled", "collected"] | None = None
    job_key: Short | None = None
    isolation_class: Short | None = None
    origin: Short | None = None
    item_hash: Short | None = None
    revision: Revision | None = None
    command_sha256: Sha | None = None  # the command by hash (backend events)
    env_names: Annotated[tuple[Short, ...], Field(max_length=MAX_LIST)] = ()  # names only, never values
    log_cursor: int | None = None  # a POINTER into the job's log, never its content
    log_next_cursor: int | None = None
    log_eof: bool | None = None
    stage: Short | None = None
    failure: Short | None = None
    oom: bool | None = None
    message: Text = ""
    wall_s: float | None = None
    usd: float | None = None  # None = unknown (never a silent 0)


class FileEv(_M):
    op: Literal["add", "modify", "delete", "summary"]
    path: Annotated[str, Field(max_length=500)] = ""
    sha256_before: Sha | None = None
    sha256_after: Sha | None = None
    size: int | None = None
    manifest: Sha | None = None  # the workspace manifest blob this diff ends at
    count: int | None = None  # summary: number of changes not listed one by one


class LiteratureEv(_M):
    retrieval_session: Annotated[str, Field(max_length=64)]
    entry_digests: Annotated[tuple[Sha, ...], Field(max_length=MAX_LIST)] = ()
    ids: Annotated[tuple[Annotated[str, Field(max_length=200)], ...], Field(max_length=MAX_LIST)] = ()
    idea_id: Ident | None = None
    # optional per-source search fields (literature/lab.lab_search through discovery_trace.LabTraceSink; int18)
    source: Short | None = None
    query: Text = ""
    window: Annotated[tuple[Short, ...], Field(max_length=4)] = ()
    as_of: Short | None = None
    n_records: int | None = None
    complete: bool | None = None
    envelopes: Annotated[tuple[Short, ...], Field(max_length=MAX_LIST)] = ()  # stored response digests
    errors: Annotated[tuple[Short, ...], Field(max_length=MAX_LIST)] = ()


class ActionEv(_M):
    tool: Annotated[str, Field(max_length=120)]
    target: Annotated[str, Field(max_length=500)] = ""
    outcome: Literal["ok", "error", "refused", "unknown"] = "unknown"
    step: int | None = None
    # optional build outputs (Lab paper / LaTeX / figure builds through discovery_trace.LabTraceSink; int18): content
    # hashes only, guarded like run outputs; problems are short machine-written strings
    outputs: Annotated[tuple[Output, ...], Field(max_length=MAX_LIST)] = ()
    problems: Annotated[tuple[Short, ...], Field(max_length=MAX_LIST)] = ()


class HumanEv(_M):
    op: Literal["steer", "edit", "stop", "redirect", "prompt", "attach", "start", "approve", "note"]
    prompt_sha256: Sha | None = None
    prompt_blob: Sha | None = None  # the prompt text, by reference (store blob), never inline
    note: Text = ""


class CostEv(_M):
    agent: str | None = None
    model: str | None = None
    usd: float | None = None  # None = unknown (never a silent 0)
    tokens_in: int | None = None
    tokens_out: int | None = None
    reported_by: Literal["provider", "agent-cli", "agent-reported", "backend", "estimate"]


class PromoteEv(_M):
    op: Literal["frozen", "handed_off", "verdict_released"]
    promotion_id: Ident
    workspace_snapshot: Sha | None = None
    idea_id: Ident | None = None
    idea_path: Annotated[tuple[Ident, ...], Field(max_length=MAX_LIST)] = ()
    claim_hash: Sha | None = None
    spec_hash: Sha | None = None
    audit_id: str | None = None
    released_label: str | None = None  # verdict_released only: the label the ledgered release carried
    # verdict_released only (int18): the verification level (court/levels.py) its own ledgered `count` release carried
    released_level: Annotated[str, Field(max_length=40)] | None = None


PAYLOADS: dict[str, type[_M]] = {"session": SessionEv, "idea": IdeaEv, "run": RunEv, "file": FileEv,
                                 "literature": LiteratureEv, "action": ActionEv, "human": HumanEv, "cost": CostEv,
                                 "promote": PromoteEv}
Source = Literal["manager", "agent-reported", "human", "backend"]


# ---------------------------------------------------------------- guards
def _walk(obj: Any, path: str = "") -> Iterable[tuple[str, str | None, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}", str(k), v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list | tuple):
        for i, v in enumerate(obj):
            yield f"{path}[{i}]", None, v
            yield from _walk(v, f"{path}[{i}]")


def guard_problems(kind: str, payload: dict[str, Any], *, store: EvidenceStore | None = None,
                   source: str = "manager", private: set[str] | None = None) -> list[str]:
    problems = []
    for path, key, value in _walk(payload):
        if key is not None and key.lower() in FORBIDDEN_KEYS:
            problems.append(f"{path}: key {key!r} may carry held-out or verdict information")
        if isinstance(value, str):
            if _SECRET_VALUE.search(value):
                problems.append(f"{path}: value looks like a key or token")
            if key is not None and _SECRET_KEY.search(key) and len(value) >= 16:
                problems.append(f"{path}: secret-like field holds a long value")
    if kind == "promote" and payload.get("op") == "verdict_released" and source != "manager":
        problems.append("only promote.release_verdict may record a released verdict label")
    released = payload.get("released_label") is not None or payload.get("released_level") is not None
    if kind == "promote" and released and payload.get("op") != "verdict_released":
        problems.append("a verdict label or level appears outside a verdict_released event")
    if store is not None:
        problems += _truth_problems(kind, payload, store, private)
    return problems


def private_hashes(store: EvidenceStore) -> set[str]:
    """sha256 of every PRIVATE vault file (metadata only; walls.private_blob_index)."""
    from .walls import private_blob_index
    return set().union(*private_blob_index(store).values())


def _truth_problems(kind: str, payload: dict[str, Any], store: EvidenceStore,
                    private: set[str] | None = None) -> list[str]:
    from ..kernel import DatasetRole
    from ..kerneld.scope import ScopedKernel
    problems = []
    hashes: list[str] = []
    if kind == "run":
        hashes = [o.get("sha256", "") for o in payload.get("outputs") or []]
        ds = ScopedKernel(store).vault.datasets()
        for m in payload.get("metrics") or []:
            d = m.get("dataset")
            if d and d in ds and ds[d].role != DatasetRole.DEV:
                problems.append(f"metric {m.get('name')!r} is on dataset {d!r} with role {ds[d].role.value}: DEV only")
    elif kind == "file":
        hashes = [h for h in (payload.get("sha256_before"), payload.get("sha256_after")) if h]
    elif kind == "action":  # build outputs (int18): a hash equal to a private vault file is refused like a run output
        hashes = [o.get("sha256", "") for o in payload.get("outputs") or []]
    if hashes:
        priv = private if private is not None else private_hashes(store)
        problems += [f"content hash {h[:12]}... is a PRIVATE vault file" for h in hashes if h in priv]
    return problems


# ---------------------------------------------------------------- write
def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def events(store: EvidenceStore, session_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    rows = store.records_where(RECORD_KIND, "session_id", session_id)
    return [r for r in rows if kind is None or r.get("kind") == kind]


def record(store: EvidenceStore, session_id: str, kind: str, payload: dict[str, Any] | BaseModel, *,
           actor: Actor | dict[str, Any], source: Source = "manager", client_id: str | None = None,
           at: float | None = None, private: set[str] | None = None,
           prior: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Validate, guard and append one trace event; returns it (with its stable `event_id`). Raises TraceRefused.
    `prior`: the session's events so far, when the caller holds them (a batch appends to it; single writer)."""
    if kind not in PAYLOADS:
        raise TraceRefused(f"unknown trace kind {kind!r} (known: {sorted(PAYLOADS)})")
    try:
        p = PAYLOADS[kind].model_validate(payload.model_dump() if isinstance(payload, BaseModel) else payload)
        a = actor if isinstance(actor, Actor) else Actor.model_validate(actor)
    except ValidationError as e:
        raise TraceRefused(f"{kind}: payload does not match {SCHEMA}: {e.error_count()} error(s): "
                           f"{[x['loc'] for x in e.errors()][:5]}") from None
    body_payload = p.model_dump(mode="json", exclude_none=True)
    if kind == "idea" and body_payload.get("op") == "abandon" and not body_payload.get("reason"):
        raise TraceRefused("an abandoned idea needs its reason")
    problems = guard_problems(kind, body_payload, store=store, source=source, private=private)
    if problems:
        raise TraceRefused("; ".join(problems[:10]))
    if prior is None:
        prior = events(store, session_id)
    if client_id is not None and any(e.get("client_id") == client_id and e.get("source") == source for e in prior):
        return next(e for e in prior if e.get("client_id") == client_id and e.get("source") == source)
    ev: dict[str, Any] = {"schema": SCHEMA, "session_id": session_id, "seq": len(prior) + 1, "kind": kind,
                          "at": time.time() if at is None else at,
                          "actor": a.model_dump(mode="json", exclude_none=True),
                          "source": source, "label": EXPLORATORY,
                          "prev": prior[-1]["event_id"] if prior else "genesis", "payload": body_payload}
    if client_id is not None:
        ev["client_id"] = client_id
    ev["event_id"] = hashlib.sha256(_canonical(ev)).hexdigest()[:32]
    store.append_record(RECORD_KIND, ev)
    prior.append(ev)
    return ev


def verify_chain(store: EvidenceStore, session_id: str) -> list[str]:
    """Problems of a session's trace chain (empty = intact): ids recompute, seq is 1..n, prev links."""
    problems = []
    prev = "genesis"
    for i, e in enumerate(events(store, session_id), 1):
        body = {k: v for k, v in e.items() if k != "event_id"}
        if hashlib.sha256(_canonical(body)).hexdigest()[:32] != e.get("event_id"):
            problems.append(f"event seq {e.get('seq')}: id does not match its content")
        if e.get("seq") != i or e.get("prev") != prev:
            problems.append(f"event seq {e.get('seq')}: chain broken (expected seq {i}, prev {prev[:12]})")
        prev = str(e.get("event_id"))
    return problems


def human(name: str | None = None) -> Actor:
    return Actor(type="human", id=(name or getpass.getuser())[:120])


def system() -> Actor:
    return Actor(type="system", id="lab-manager")


# ---------------------------------------------------------------- workspace diffs and agent-reported intake
def workspace_manifest(ws: Path, max_files: int = 500_000) -> dict[str, Any]:
    """{rel: {sha256, size}} of every regular file (links recorded, never followed)."""
    files: dict[str, dict[str, Any]] = {}
    links: list[str] = []
    n = 0
    for root, dirs, names in os.walk(ws, followlinks=False):
        rp = Path(root)
        for d in list(dirs):
            if is_link_like(rp / d):
                dirs.remove(d)
                links.append((rp / d).relative_to(ws).as_posix())
        for f in names:
            n += 1
            if n > max_files:
                raise TraceRefused(f"workspace has more than {max_files} files")
            p = rp / f
            rel = p.relative_to(ws).as_posix()
            if is_link_like(p):
                links.append(rel)
                continue
            h = hashlib.sha256()
            size = 0
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
                    size += len(chunk)
            files[rel] = {"sha256": h.hexdigest(), "size": size}
    return {"kind": "lab_workspace_manifest", "version": 1, "files": files, "links": sorted(links)}


def _last_manifest(store: EvidenceStore, session_id: str) -> dict[str, Any]:
    for e in reversed(events(store, session_id, "file")):
        ref = e["payload"].get("manifest")
        if ref:
            return dict(store.blobs.get_json(ref))
    return {"files": {}}


def record_workspace_diff(store: EvidenceStore, session_id: str, ws: Path, actor: Actor) -> list[dict[str, Any]]:
    """File events for every change since the last recorded manifest (hashes only), the last one carrying the new
    manifest's blob ref. A change to a PRIVATE vault file's hash is refused by the guards (TraceRefused)."""
    new = workspace_manifest(ws)
    ref = store.blobs.put(_canonical(new))
    old = _last_manifest(store, session_id)["files"]
    changes: list[dict[str, Any]] = []
    for rel in sorted(set(old) | set(new["files"])):
        a, b = old.get(rel), new["files"].get(rel)
        if a == b:
            continue
        op = "add" if a is None else "delete" if b is None else "modify"
        changes.append({"op": op, "path": rel, "sha256_before": a["sha256"] if a else None,
                        "sha256_after": b["sha256"] if b else None, "size": b["size"] if b else None})
    priv = private_hashes(store)  # once per diff, not once per file
    prior = events(store, session_id)  # once per diff: record() appends to it
    out = [record(store, session_id, "file", c, actor=actor, private=priv, prior=prior)
           for c in changes[:MAX_FILE_EVENTS]]
    out.append(record(store, session_id, "file", {"op": "summary", "manifest": ref,
                                                   "count": max(0, len(changes) - MAX_FILE_EVENTS)},
                      actor=actor, prior=prior))
    return out


def ingest_agent_events(store: EvidenceStore, session_id: str, ws: Path, actor: Actor) -> dict[str, Any]:
    """Ingest agent-reported events from the workspace intake file (box paused by the caller). Each line:
    {"kind": "idea" | "run" | "literature" | "action" | "cost", "client_id": "...", "payload": {...}}.
    Invalid or refused lines are counted and skipped (never partially recorded); nothing is ever trusted more for
    being in the trace."""
    from ..sandbox.base import safe_path
    try:
        p = safe_path(ws, AGENT_TRACE_FILE)
    except PermissionError as e:
        return {"ingested": 0, "refused": 0, "error": f"intake path refused: {e}"}
    if not p.is_file():
        return {"ingested": 0, "refused": 0}
    raw = p.read_bytes()[:MAX_INGEST_BYTES]
    ok = refused = 0
    reasons: list[str] = []
    for n, line in enumerate(raw.decode("utf-8", "replace").splitlines()[:MAX_INGEST], 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            kind = obj["kind"]
            if kind not in ("idea", "run", "literature", "action", "cost"):
                raise TraceRefused(f"agents may not report {kind!r} events")
            cid = str(obj["client_id"])[:80]
            record(store, session_id, kind, obj["payload"], actor=actor, source="agent-reported", client_id=cid)
            ok += 1
        except (ValueError, KeyError, TypeError) as e:
            refused += 1
            if len(reasons) < 20:
                reasons.append(f"line {n}: {type(e).__name__}: {str(e)[:160]}")
    return {"ingested": ok, "refused": refused, "reasons": reasons}


# ---------------------------------------------------------------- vendor output parsing (best effort, [U])
def costs_from_stream_json(text: str, agent: str, model: str | None) -> list[dict[str, Any]]:
    """Claude Code `--output-format stream-json`: the final `result` line carries `total_cost_usd` and `usage`
    [U: field names from vendor docs, not verified]. Codex `exec --json`: `turn.completed` lines carry `usage` [U]."""
    out = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else None
        if obj.get("type") == "result" or obj.get("type") == "turn.completed":
            usd = obj.get("total_cost_usd")
            out.append({"agent": agent, "model": model, "usd": float(usd) if isinstance(usd, int | float) else None,
                        "tokens_in": int(usage.get("input_tokens", 0)) if usage else None,
                        "tokens_out": int(usage.get("output_tokens", 0)) if usage else None,
                        "reported_by": "agent-cli"})
    return out


def actions_from_stream_json(text: str) -> list[dict[str, Any]]:
    """Coarse tool steps from Claude Code stream-json (`tool_use` content blocks) [U]: tool name and a short target."""
    out = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg = obj.get("message") if isinstance(obj, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        blocks: list[Any] = content if isinstance(content, list) else []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                raw_inp = block.get("input")
                inp: dict[str, Any] = raw_inp if isinstance(raw_inp, dict) else {}
                target = next((str(inp[k]) for k in ("file_path", "path", "command", "url", "pattern") if k in inp), "")
                out.append({"tool": str(block.get("name", ""))[:120], "target": target[:200], "outcome": "unknown"})
    return out


def record_many(store: EvidenceStore, session_id: str, kind: str, payloads: Iterable[dict[str, Any]],
                actor: Actor) -> int:
    """Record what passes the guards; a refused item is skipped (counted), never partially recorded."""
    n = 0
    for p in payloads:
        try:
            record(store, session_id, kind, p, actor=actor)
            n += 1
        except TraceRefused:
            continue
    return n


# ---------------------------------------------------------------- read side
def idea_tree(store: EvidenceStore, session_id: str) -> dict[str, dict[str, Any]]:
    """{idea_id: {parent_id, title, status, events: [event_id...]}} rebuilt from idea events in order."""
    tree: dict[str, dict[str, Any]] = {}
    for e in events(store, session_id, "idea"):
        p = e["payload"]
        node = tree.setdefault(p["idea_id"], {"parent_id": p.get("parent_id"), "title": p.get("title", ""),
                                              "status": "open", "events": []})
        if p["op"] in ("propose", "branch") and p.get("parent_id"):
            node["parent_id"] = p["parent_id"]
        node["status"] = {"abandon": "abandoned", "revisit": "open", "merge": "merged"}.get(p["op"], node["status"])
        node["events"].append(e["event_id"])
    return tree


def idea_path(store: EvidenceStore, session_id: str, idea_id: str) -> list[str]:
    """Root-to-leaf ids of the idea (cycle-safe). Empty if the idea is unknown."""
    tree = idea_tree(store, session_id)
    path: list[str] = []
    cur: str | None = idea_id
    while cur and cur in tree and cur not in path:
        path.append(cur)
        cur = tree[cur].get("parent_id")
    return list(reversed(path))


def costs(store: EvidenceStore, session_id: str) -> dict[str, Any]:
    """Cost totals per session and per agent. Unknown amounts stay unknown: a total is None if any part is."""
    per: dict[str, dict[str, Any]] = {}
    for e in events(store, session_id, "cost"):
        p = e["payload"]
        key = str(p.get("agent") or e["actor"].get("agent") or e["actor"]["id"])
        row = per.setdefault(key, {"usd": 0.0, "unknown_usd_events": 0, "tokens_in": 0, "tokens_out": 0})
        if p.get("usd") is None:
            row["unknown_usd_events"] += 1
        else:
            row["usd"] += float(p["usd"])
        row["tokens_in"] += int(p.get("tokens_in") or 0)
        row["tokens_out"] += int(p.get("tokens_out") or 0)
    total_known = sum(r["usd"] for r in per.values())
    unknown = sum(r["unknown_usd_events"] for r in per.values())
    return {"session_id": session_id, "per_agent": per, "usd_total": None if unknown else total_known,
            "usd_known_part": total_known, "unknown_usd_events": unknown}


def story(store: EvidenceStore, claim_hash: str) -> dict[str, Any]:
    """The story of a discovery: from a pre-registered claim (and so its certificate or rejection) back to the Lab
    session, its promotion and its idea path, with every trace event of the session in order. Owner-side view: it
    contains no verdict detail beyond the released label, and the trace never contained held-out truth."""
    for row in store.records(RECORD_KIND):
        p = row.get("payload") or {}
        if row.get("kind") == "promote" and p.get("op") == "handed_off" and p.get("claim_hash") == claim_hash:
            sid = row["session_id"]
            frozen = next((e for e in events(store, sid, "promote") if e["payload"].get("op") == "frozen"
                           and e["payload"].get("promotion_id") == p["promotion_id"]), None)
            idea = (frozen or {}).get("payload", {}).get("idea_id")
            return {"claim_hash": claim_hash, "session_id": sid, "promotion_id": p["promotion_id"],
                    "idea_id": idea, "idea_path": idea_path(store, sid, idea) if idea else [],
                    "chain_problems": verify_chain(store, sid), "events": events(store, sid),
                    "label": EXPLORATORY}
    raise TraceRefused(f"no Lab promotion was handed off with claim {claim_hash[:16]}...")
