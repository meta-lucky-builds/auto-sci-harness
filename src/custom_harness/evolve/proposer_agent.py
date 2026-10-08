"""Agentic proposer (GOAL §5 L6 step 2; Meta-Harness, done safely).

A tool-using agent that reads the read-only evidence export (export.py) and answers with TYPED EDITS ONLY.

    per generation:  export_evidence(evolve-split runs)  ->  copy the export + export_tools.py into a FRESH sandbox
                     workspace (nothing else is in it)  ->  agent loop: model turn -> one read-only tool call executed
                     INSIDE the sandbox -> host-side content filter -> result back to the model  ->  final answer
                     parsed by propose.parse_proposal  ->  workspace integrity check  ->  Proposal

Protocol (one JSON object per model turn):
    {"tool": "list" | "read" | "grep", "path": ..., "offset": ..., "limit": ..., "pattern": ...}
    {"final": {"edits": [<EDIT_SCHEMA items>], "decline": "<reason>"}}

Defences, each tested in tests/test_proposer_agent.py:
  - the sandbox workspace holds only the export copy and the tool script; the tools (export_tools.py) list, read and
    grep, never write; paths are confined to the export and to the files its manifest lists (no '..', absolute
    paths, links, unlisted/planted files);
  - an unknown tool (write, exec, shell, ...) is refused on the host before anything runs;
  - every tool result is filtered on the HOST before the model sees it: a result naming a gate/sealed task, a
    held-out family/domain, a canary, `_private` or another private-data pattern is withheld (fail closed);
  - the final answer is parsed as typed edits only (propose.parse_proposal: unknown fields, free-form patches and
    untyped ops are malformed); the loop then applies every static check (edits.py), the prediction screen
    (edits.screen_prediction: covert channel through rationale / predicted effect) and the one-mechanism rule;
  - after the session the workspace is re-hashed: if the export copy or the tool script changed, the proposal is
    refused (the agent had no write tool; a change means the sandbox was not read-only in practice).

Limits (honest): with LocalSandbox (tests) the tool process is a host process confined only by the tool script's
own path checks; real isolation needs DockerSandbox (the workspace is then the container's only mount). The model
itself runs host-side through the gateway (as every model call does); only the tools run in the sandbox.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from ..errors import InfraError
from ..gateway import Backend, ChatRequest, Message
from ..runtime.engine import parse_json_output
from ..runtime.spec import HarnessSpec
from ..sandbox.base import Sandbox
from ..store import EvidenceStore
from . import export_tools
from .edits import _CANARY, _LEAK_PATTERNS, DEFAULT_PROTECTED_NODES, Edit, _token_hit, denied_tokens
from .evaluate import RunOutcome, SandboxFactory, SuiteTask
from .export import ExportRefused, export_evidence
from .propose import EDIT_SCHEMA, build_prompt, parse_proposal

TOOL_PROTOCOL = """
You are working as a tool-using agent. The read-only EVIDENCE EXPORT of this campaign is mounted at the root of
your tools (manifest.json, candidates/, traces/, scores.json, certificates.json, lineage.json, ledger.json,
generation_stats.json). Answer each turn with exactly ONE JSON object:
  {"tool": "list", "path": "<dir, '' for the root>"}
  {"tool": "read", "path": "<file>", "offset": 0, "limit": 20000}
  {"tool": "grep", "pattern": "<literal substring>", "path": "<dir or file, '' for all>"}
  {"final": {"edits": [...], "decline": "<reason, only with no edits>"}}
There are no other tools: nothing can write. `final` must follow the edit format described above (typed JSON-pointer
ops, each edit with its predicted effect) and each edit must change ONE mechanism (one node, or one node plus the
wiring of a step it inserts or removes); bundles that change several mechanisms are refused."""

AGENT_SCHEMA = {"type": "object", "properties": {
    "tool": {"type": "string", "enum": list(export_tools.TOOLS)}, "path": {"type": "string"},
    "offset": {"type": "integer"}, "limit": {"type": "integer"}, "pattern": {"type": "string"},
    "final": EDIT_SCHEMA}}

WITHHELD = "result withheld: it contains held-out, canary or private content (fail closed)"
_TOOL_TIMEOUT_S = 60.0


class AgentProposal(NamedTuple):
    edits: list[Edit]
    problems: list[str]
    raw: Any
    decline: str | None
    transcript: list[dict[str, Any]]


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob("*")):
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode() + b"\0")
        if p.is_file():
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


class SandboxTools:
    """The export, mounted in a fresh sandbox workspace that contains nothing else, plus the stdlib tool script.
    Every tool call runs as `python -I _tools/export_tools.py export <call>` inside that sandbox."""

    def __init__(self, sandbox: Sandbox, export_dir: Path):
        self.sandbox = sandbox
        ws = Path(sandbox.workspace)
        if any(ws.iterdir()):
            raise ExportRefused([f"proposer sandbox workspace {ws} is not empty (only the export may be mounted)"])
        shutil.copytree(export_dir, ws / "export")
        (ws / "_tools").mkdir()
        shutil.copyfile(export_tools.__file__, ws / "_tools" / "export_tools.py")
        self.digest = tree_digest(ws)

    def call(self, call: dict[str, Any]) -> dict[str, Any]:
        r = self.sandbox.exec([self.sandbox.python, "-I", "_tools/export_tools.py", "export",
                               json.dumps(call, default=str)], timeout_s=_TOOL_TIMEOUT_S)
        if r.returncode != 0:
            return {"ok": False, "refused": f"tool process failed (exit {r.returncode})"}
        try:
            out = json.loads(r.stdout)
        except ValueError:
            return {"ok": False, "refused": "tool process returned no JSON"}
        return out if isinstance(out, dict) else {"ok": False, "refused": "tool process returned no object"}

    def integrity_problems(self) -> list[str]:
        if tree_digest(self.sandbox.workspace) != self.digest:
            return ["the proposer's sandbox workspace changed during the session (export or tool script modified): "
                    "proposal refused"]
        return []


@dataclass
class ProposerAgent:
    """`export_root`: a directory that holds one fresh export per session (never overwritten). `sandbox_factory`:
    the candidate sandbox factory (each session gets a fresh, empty workspace from it)."""
    backend: Backend
    export_root: Path
    sandbox_factory: SandboxFactory
    canaries: tuple[str, ...] = ()
    # session limits: cost bounds, not claim policy (no owner decision needed; a short session only proposes less)
    max_turns: int = 12
    max_result_chars: int = 12000
    sessions: list[dict[str, Any]] = field(default_factory=list)

    def propose_from_export(self, spec: HarnessSpec, digest: dict, ledger: list[dict], *, store: EvidenceStore,
                            tasks: list[SuiteTask], runs: list[RunOutcome], n_edits: int = 3,
                            protected: tuple[str, ...] = DEFAULT_PROTECTED_NODES) -> AgentProposal:
        token = secrets.token_hex(4)
        transcript: list[dict[str, Any]] = []
        deny = denied_tokens(tasks, self.canaries)
        evolve_runs = [o for o in runs if any(t.task_id == o.task_id and t.split == "evolve" for t in tasks)]
        try:
            exp = export_evidence(store, Path(self.export_root) / f"export-{spec.spec_hash()[:12]}-{token}",
                                  runs=evolve_runs, tasks=tasks, canaries=self.canaries)
            sandbox = self.sandbox_factory(f"proposer-{spec.spec_hash()[:10]}-{token}")
            tools = SandboxTools(sandbox, exp.path)
        except (ExportRefused, InfraError, OSError) as e:  # fail closed: no export/sandbox, no agentic proposal
            return AgentProposal([], [f"agentic proposer not run: {type(e).__name__}: {str(e)[:300]}"], None, None,
                                 transcript)
        messages = [Message(role="user", content=build_prompt(spec, digest, ledger, n_edits, protected)
                            + "\n" + TOOL_PROTOCOL)]
        result: AgentProposal | None = None
        for turn in range(self.max_turns):
            resp = self.backend.complete(ChatRequest(messages=messages, json_schema=AGENT_SCHEMA))
            messages.append(Message(role="assistant", content=resp.text))
            try:
                obj = parse_json_output(resp.text)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict) and "final" in obj:
                p = parse_proposal(obj["final"], n_edits, resp)
                transcript.append({"turn": turn, "final": True, "n_edits": len(p.edits), "problems": len(p.problems)})
                result = AgentProposal(p.edits, p.problems, resp, p.decline, transcript)
                break
            reply, entry = self._tool_turn(obj, tools, deny)
            transcript.append({"turn": turn, **entry})
            messages.append(Message(role="user", content=reply))
        if result is None:
            result = AgentProposal([], [f"agentic proposer gave no final answer in {self.max_turns} turns"], None,
                                   None, transcript)
        broken = tools.integrity_problems()
        if broken:
            result = AgentProposal([], [*broken, *result.problems], result.raw, None, transcript)
        self.sessions.append({"export": str(exp.path), "workspace": str(sandbox.workspace), "transcript": transcript})
        return result

    def _tool_turn(self, obj: Any, tools: SandboxTools, deny: list[str]) -> tuple[str, dict[str, Any]]:
        if not isinstance(obj, dict) or not isinstance(obj.get("tool"), str):
            return ("not a tool call: reply with one JSON object, a tool call or a final answer",
                    {"tool": None, "ok": False, "refused": "not a tool call"})
        tool = obj["tool"]
        path = str(obj.get("path", ""))[:200]
        entry: dict[str, Any] = {"tool": str(tool)[:40], "path": "<withheld>" if _token_hit(path, deny) else path}
        if tool not in export_tools.TOOLS:  # refused on the host: nothing is executed
            reason = f"unknown tool {tool!r}: the only tools are {list(export_tools.TOOLS)} (all read-only)"
            return json.dumps({"ok": False, "refused": reason}), {**entry, "ok": False, "refused": reason,
                                                                  "executed": False}
        call = {k: obj[k] for k in ("tool", "path", "offset", "limit", "pattern") if k in obj}
        out = tools.call(call)
        text = json.dumps(out, default=str)
        if _token_hit(text, deny) or _CANARY.search(text) or any(p.search(text) for p in _LEAK_PATTERNS):
            out = {"ok": False, "refused": WITHHELD}
            text = json.dumps(out)
        elif len(text) > self.max_result_chars:
            text = text[:self.max_result_chars] + " ...[truncated: read with offset/limit]"
        return text, {**entry, "ok": bool(out.get("ok")), "refused": out.get("refused"), "executed": True}
