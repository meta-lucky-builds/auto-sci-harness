"""Lab sessions: start / attach / status / stop (GOAL §17), recorded in the store as EXPLORATORY.

A session record is an append-only stream of `lab_session` records (store.append_record), one per event, every one
carrying `label: EXPLORATORY` and the not-a-finding notice. Nothing in a Lab record is evidence: no Kernel ledger
entry is written, no claim is created, and Lab records are never read by any Court path except `promote.py`, which
reads only the session's workspace location and box name.

What is recorded at start: the agent and its plan (commands, non-secret env, config file hashes), the box spec and
image id, the workspace path, the credential RECORD (never its value: credentials.py), and the result of every wall
check. Transcripts of headless prompts are written under `<lab root>/<session>/transcripts/` (outside the mounted
workspace), with their sha256 in the store.

Built, NOT test-verified.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import re
import subprocess
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..sandbox.base import safe_path
from ..store import EvidenceStore
from . import EXPLORATORY, NOT_A_FINDING
from . import trace as T
from .acp_client import AcpClient, AcpError, allow_once
from .agents import AgentOptions, Plan, adapter, fill_prompt
from .box import BoxSpec, DockerCLI, LabBox, network_record
from .credentials import CredentialRefused, CredentialSpec, SessionCredential, require_live, resolve
from .egress import collect_web
from .walls import (
    CONTAINER_WORKSPACE,
    LabWallError,
    configured_secret,
    lab_root,
    require_workspace,
    scan_workspace,
)

RECORD_KIND = "lab_session"
_SID = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
HEADLESS_TIMEOUT_S = 24 * 3600.0  # UNCALIBRATED (owner decision, GOAL §13.8): one headless prompt (long runs allowed)


class LabError(RuntimeError):
    pass


def _record(store: EvidenceStore, session_id: str, event: str, **body: Any) -> dict[str, Any]:
    rec = {"session_id": session_id, "event": event, "label": EXPLORATORY, "notice": NOT_A_FINDING,
           "at": time.time(), "by": getpass.getuser(), **body}
    store.append_record(RECORD_KIND, rec)
    return rec


def events(store: EvidenceStore, session_id: str) -> list[dict[str, Any]]:
    return store.records_where(RECORD_KIND, "session_id", session_id)


def started(store: EvidenceStore, session_id: str, *, allow_stopped: bool = False) -> dict[str, Any]:
    """The session's `started` record; LabError if the session never started or (unless `allow_stopped`) has
    stopped. A stopped session's workspace can still be promoted: no agent runs in it any more."""
    evs = events(store, session_id)
    starts = [e for e in evs if e.get("event") == "started"]
    if not starts:
        raise LabError(f"no started Lab session {session_id!r}")
    if not allow_stopped and any(e.get("event") == "stopped" for e in evs[evs.index(starts[-1]):]):
        raise LabError(f"Lab session {session_id!r} is stopped")
    return starts[-1]


def workspace_of(session_id: str) -> Path:
    return lab_root() / session_id / "workspace"


def _plan_from(rec: dict[str, Any]) -> Plan:
    return Plan(**rec["plan"])


def start(store: EvidenceStore, agent: str, options: AgentOptions, credential: CredentialSpec | None, *,
          box: BoxSpec | None = None, session_id: str | None = None, docker: DockerCLI | None = None,
          environ: dict[str, str] | None = None, project: str | None = None) -> dict[str, Any]:
    """Start a lab box with `agent` configured inside it. Every wall check runs first; any problem refuses."""
    sid = session_id or f"s-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    if not _SID.match(sid):
        raise LabError(f"session id {sid!r}: lower-case letters, digits and '-', 3-41 characters")
    if any(e.get("event") == "started" for e in events(store, sid)):
        raise LabError(f"Lab session {sid!r} already exists; pick a new id")
    spec = box or BoxSpec()
    plan = adapter(agent).plan(options)
    cred: SessionCredential | None = None
    if plan.box_env:
        if credential is None:
            raise CredentialRefused(f"{agent}: this configuration needs a session credential (registry entry "
                                    f"{options.registry_key!r}, in-box variable {plan.box_env})")
        if credential.registry_key != options.registry_key:
            raise CredentialRefused("the credential is declared for a different registry entry than the agent's")
        cred = resolve(CredentialSpec(**{**asdict(credential), "box_env": plan.box_env}), environ=environ)
    elif credential is not None:
        raise CredentialRefused(f"{agent}: this configuration takes no credential; none is passed into the box")
    ws = workspace_of(sid)
    ws.mkdir(parents=True, exist_ok=True)
    require_workspace(ws)
    secret, secret_file = configured_secret()
    scan = scan_workspace(ws, store=store, secret=secret, secret_file=secret_file)
    if scan:
        raise LabWallError("workspace refused: " + "; ".join(scan))
    if project is not None:  # the session's ledger entry comes before the box and before any data reaches it
        from ..court.sealed import bind_session, lab_part
        bind_session(store, project, sid)
        safe_path(ws, "data").mkdir(exist_ok=True)
        safe_path(ws, f"data/{project}-lab.csv").write_bytes(lab_part(store, project))
    lb = LabBox(sid, docker)
    base = {"project": project, "agent": agent, "plan": asdict(plan), "box": spec.record(), "workspace": str(ws),
            "network": network_record(sid, spec, plan), "container": lb.name,
            "credential": cred.record() if cred else None,
            "options": {k: v for k, v in asdict(options).items() if v not in (None, (), "")},
            "walls": {"workspace": "clean", "content_scan": "clean",
                      "secret_scan": "clean" if secret is not None else "no secret configured"}}
    try:
        cid = lb.start(ws, spec, plan, cred.docker_env() if cred else None)
        for path, content in plan.config_files.items():
            lb.write_file(path, content)
        for step in plan.setup:
            r = lb.run_setup(step)
            if r.returncode != 0:
                raise LabError(f"agent setup step failed (rc={r.returncode}); stderr not recorded (may echo a key)")
    except (LabError, LabWallError, RuntimeError) as e:
        lb.stop()
        _record(store, sid, "start_failed", reason=str(e)[:500], **base)
        T.record(store, sid, "session", {"op": "start_failed", "agent": agent, "model": options.model,
                                         "image": spec.image}, actor=T.human())
        raise
    image_id = lb.image_id(spec.image)
    rec = _record(store, sid, "started", container_id=cid, image_id=image_id,
                  config_sha256={p: hashlib.sha256(c.encode()).hexdigest() for p, c in plan.config_files.items()},
                  **base)
    # discovery trace (trace.py): who started what, and the workspace baseline (hashes only)
    T.record(store, sid, "session", {"op": "start", "agent": agent, "model": options.model, "image": spec.image,
                                     "image_id": image_id}, actor=T.human())
    T.record_workspace_diff(store, sid, ws, T.system())
    return rec


def _agent_actor(rec: dict[str, Any]) -> T.Actor:
    return T.Actor(type="agent", id=str(rec.get("agent")), agent=str(rec.get("agent")),
                   model=(rec.get("options") or {}).get("model"))


def _paused(lb: LabBox) -> bool:
    """Pause a running box for a host-side read of its workspace; True if this call paused it. Refuses when a running
    box cannot be paused (host code never reads a workspace while agent code runs)."""
    was_running = lb.state() == "running"
    paused = was_running and lb.pause()
    if lb.state() == "running":
        raise LabError("could not pause the box for a host-side workspace read")
    return paused


def collect_trace(store: EvidenceStore, session_id: str, rec: dict[str, Any], lb: LabBox) -> dict[str, Any]:
    """After agent activity: ingest agent-reported events and record the workspace diff, with the box paused."""
    ws = workspace_of(session_id)
    paused = _paused(lb)
    try:
        require_workspace(ws)
        ingest = T.ingest_agent_events(store, session_id, ws, _agent_actor(rec))
        files = T.record_workspace_diff(store, session_id, ws, T.system())
    finally:
        if paused:
            lb.unpause()
    return {"agent_events": ingest, "file_events": len(files), **collect_web(store, session_id, lb.docker)}


def attach(store: EvidenceStore, session_id: str, *, docker: DockerCLI | None = None) -> int:
    """Open the agent's own interactive UI in the box (a TTY on this terminal). Returns the agent's exit code."""
    rec = started(store, session_id)
    if rec.get("credential"):
        require_live(rec["credential"])
    plan = _plan_from(rec)
    if not plan.interactive:
        raise LabError(f"agent {rec['agent']} has no interactive command; use `chh lab attach --prompt`")
    lb = LabBox(session_id, docker)
    if lb.state() != "running":
        raise LabError(f"the box of {session_id} is {lb.state()}")
    _record(store, session_id, "attached", mode="interactive")
    T.record(store, session_id, "human", {"op": "attach"}, actor=T.human())
    code = subprocess.call(lb.exec_argv(plan.interactive, interactive=True, tty=True))
    if lb.state() == "running":
        collect_trace(store, session_id, rec, lb)
    return code


def run_prompt(store: EvidenceStore, session_id: str, prompt: str, *, docker: DockerCLI | None = None,
               timeout_s: float = HEADLESS_TIMEOUT_S) -> dict[str, Any]:
    """Run one headless prompt in the box. The transcript is Lab output (EXPLORATORY), kept outside the workspace."""
    rec = started(store, session_id)
    if rec.get("credential"):
        require_live(rec["credential"])
    plan = _plan_from(rec)
    lb = LabBox(session_id, docker)
    if lb.state() != "running":
        raise LabError(f"the box of {session_id} is {lb.state()}")
    tdir = lab_root() / session_id / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    out = tdir / f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}.txt"
    pbytes = prompt.encode("utf-8")
    T.record(store, session_id, "human", {"op": "prompt", "prompt_sha256": hashlib.sha256(pbytes).hexdigest(),
                                          "prompt_blob": store.blobs.put(pbytes)}, actor=T.human())
    if plan.acp:
        text, rc, extra = _acp_prompt(lb, plan, prompt, timeout_s)
    elif plan.headless:
        r = lb.docker.call(lb.exec_argv(fill_prompt(plan.headless, prompt), interactive=False, tty=False)[1:],
                           timeout=timeout_s)
        text, rc, extra = r.stdout, r.returncode, {}
    else:
        raise LabError(f"agent {rec['agent']} has no headless mode")
    out.write_text(text, encoding="utf-8")
    agent = _agent_actor(rec)
    if plan.acp:
        actions = [{"tool": "acp", "target": t, "outcome": "unknown"} for t in extra.get("tool_calls", [])]
        actions += [{"tool": m, "outcome": "refused"} for m in extra.get("refused_client_methods", [])]
    else:
        actions = T.actions_from_stream_json(text)
    n_actions = T.record_many(store, session_id, "action", actions, agent)
    n_costs = T.record_many(store, session_id, "cost", T.costs_from_stream_json(text, str(rec.get("agent")),
                                                                                agent.model), agent)
    if not n_costs:  # no cost line parsed: recorded as UNKNOWN, never as zero
        T.record(store, session_id, "cost", {"agent": rec.get("agent"), "model": agent.model, "usd": None,
                                             "reported_by": "estimate"}, actor=agent)
    collected = collect_trace(store, session_id, rec, lb)
    return _record(store, session_id, "prompt", returncode=rc, transcript=str(out),
                   transcript_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                   prompt_sha256=hashlib.sha256(pbytes).hexdigest(), trace={"actions": n_actions, **collected},
                   **extra)


def _acp_prompt(lb: LabBox, plan: Plan, prompt: str, timeout_s: float) -> tuple[str, int, dict[str, Any]]:
    assert plan.acp is not None
    argv = lb.exec_argv(plan.acp, interactive=True, tty=False)
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert p.stdin is not None and p.stdout is not None
    timer = threading.Timer(timeout_s, p.kill)
    timer.start()
    try:
        client = AcpClient(p.stdout, p.stdin, permission=allow_once)
        client.initialize()
        client.new_session(CONTAINER_WORKSPACE)
        turn = client.prompt(prompt)
        client.cancel()
        extra = {"stop_reason": turn.stop_reason, "tool_calls": turn.tool_calls[:200],
                 "permissions": turn.permissions[:200], "refused_client_methods": sorted(set(turn.refused)),
                 "agent_info": json.loads(json.dumps(client.agent_info, default=str))}
        return turn.transcript, 0, extra
    except AcpError as e:
        return "", 1, {"acp_error": str(e)[:300]}
    finally:
        timer.cancel()
        p.kill()
        p.wait(timeout=30)


def status(store: EvidenceStore, session_id: str | None = None, *, docker: DockerCLI | None = None
           ) -> list[dict[str, Any]]:
    """One row per session: its last event and the box's live state (all sessions when no id is given)."""
    if session_id:
        ids = [session_id]
    else:
        ids = sorted({str(r.get("session_id")) for r in store.records(RECORD_KIND)})
    rows = []
    for sid in ids:
        evs = events(store, sid)
        if not evs:
            continue
        last = evs[-1]
        rows.append({"session_id": sid, "label": EXPLORATORY, "last_event": last.get("event"), "at": last.get("at"),
                     "agent": next((e.get("agent") for e in reversed(evs) if e.get("agent")), None),
                     "workspace": str(workspace_of(sid)), "box": LabBox(sid, docker).state(),
                     "promotions": [e.get("promotion_id") for e in evs if e.get("event") == "promoted"]})
    return rows


def stop(store: EvidenceStore, session_id: str, *, docker: DockerCLI | None = None) -> dict[str, Any]:
    rec = started(store, session_id)
    T.record(store, session_id, "human", {"op": "stop"}, actor=T.human())
    lb = LabBox(session_id, docker)
    collect_web(store, session_id, lb.docker)  # the gateway's log goes with the gateway
    ok = lb.stop()
    ws = workspace_of(session_id)
    require_workspace(ws)  # the box is gone: no agent runs, the workspace can be read without a pause
    T.ingest_agent_events(store, session_id, ws, _agent_actor(rec))
    T.record_workspace_diff(store, session_id, ws, T.system())
    T.record(store, session_id, "session", {"op": "stop", "agent": rec.get("agent")}, actor=T.human())
    return _record(store, session_id, "stopped", removed=ok, workspace_kept=str(ws))
