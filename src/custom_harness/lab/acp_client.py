"""A minimal Agent Client Protocol v1 client (JSON-RPC 2.0, one JSON object per line on stdio) for ACP agents that
run INSIDE a lab box. The agent process is started by the box manager as `docker exec -i <box> <acp command>`; this
client only talks to its stdin/stdout.

Ported (protocol knowledge, rewritten) from OpenResearch fe36f7424855d7218e64812c5fec6f7db955586f (MIT, Copyright (c)
2026 alphaXiv): src/local/acp.rs (initialize request, L19-31), src/local/acp/host.rs (session/new, session/prompt,
session/update notifications, session/request_permission answers `{"outcome": {"outcome": "selected", "optionId"}}`
or `{"outcome": {"outcome": "cancelled"}}`, session/cancel) and the wire fixture
src/local/harness/fixtures/acp-peer.py.

Changes against OpenResearch (PORTMAP §3.1, §10 S5):
- clientCapabilities advertise NO fs and NO terminal: OR serves `fs/read_text_file`, `fs/write_text_file` and
  `terminal/*` on the HOST with any absolute path and the synced secrets. Here every client-side tool request is
  answered with a JSON-RPC error (-32601), so the agent works with its own tools inside the box;
- permission requests are answered by a fixed policy (default: allow once, inside the box) instead of a dashboard;
- no session persistence, model switching or config options: the Lab records the transcript text only.

All agent output is untrusted data: it is collected as text, never interpreted as instructions to this process.

Built, NOT test-verified.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 8 << 20  # UNCALIBRATED (owner decision, GOAL §13.8): one JSON-RPC line from the agent
MAX_TRANSCRIPT_CHARS = 4 << 20  # UNCALIBRATED (owner decision, GOAL §13.8): text kept per prompt turn
REFUSED_CLIENT_METHODS = ("fs/read_text_file", "fs/write_text_file", "terminal/create", "terminal/output",
                          "terminal/wait_for_exit", "terminal/kill", "terminal/release")


class AcpError(RuntimeError):
    pass


@dataclass
class Turn:
    stop_reason: str | None = None
    text: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)  # titles only
    permissions: list[dict[str, Any]] = field(default_factory=list)  # what was asked and answered
    refused: list[str] = field(default_factory=list)  # client methods the agent asked for and was refused

    @property
    def transcript(self) -> str:
        return "".join(self.text)


PermissionPolicy = Callable[[dict[str, Any]], str | None]  # params -> optionId to select, or None = cancel


def allow_once(params: dict[str, Any]) -> str | None:
    """Default policy inside the box: the first `allow_once` option, else the first `allow_always`, else cancel."""
    options = params.get("options") or []
    for kind in ("allow_once", "allow_always"):
        for opt in options:
            if isinstance(opt, dict) and opt.get("kind") == kind and isinstance(opt.get("optionId"), str):
                return str(opt["optionId"])
    return None


def deny_all(params: dict[str, Any]) -> str | None:
    """A stricter policy: the first `reject_once` option, else cancel."""
    for opt in params.get("options") or []:
        if isinstance(opt, dict) and opt.get("kind") == "reject_once" and isinstance(opt.get("optionId"), str):
            return str(opt["optionId"])
    return None


class AcpClient:
    """One ACP connection over a pair of byte streams (the agent's stdout to read, its stdin to write)."""

    def __init__(self, reader: IO[bytes], writer: IO[bytes], *, permission: PermissionPolicy = allow_once):
        self._r, self._w = reader, writer
        self._next = 0
        self.permission = permission
        self.session_id: str | None = None
        self.agent_info: dict[str, Any] = {}

    # ------------------------------------------------------------ wire
    def _send(self, obj: dict[str, Any]) -> None:
        self._w.write((json.dumps({"jsonrpc": "2.0", **obj}, separators=(",", ":")) + "\n").encode("utf-8"))
        self._w.flush()

    def _recv(self) -> dict[str, Any]:
        line = self._r.readline(MAX_LINE_BYTES + 1)
        if not line:
            raise AcpError("the agent closed its output")
        if len(line) > MAX_LINE_BYTES:
            raise AcpError("the agent sent an over-long line")
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise AcpError("the agent sent a line that is not JSON") from e
        if not isinstance(msg, dict):
            raise AcpError("the agent sent a JSON value that is not an object")
        return msg

    def _request(self, method: str, params: dict[str, Any], turn: Turn | None = None) -> dict[str, Any]:
        self._next += 1
        rid = self._next
        self._send({"id": rid, "method": method, "params": params})
        while True:
            msg = self._recv()
            if "method" in msg:
                self._handle_incoming(msg, turn)
                continue
            if msg.get("id") != rid:
                continue  # a stale reply: ignored
            if "error" in msg:
                err = msg.get("error") or {}
                raise AcpError(f"{method} failed: code {err.get('code')!r}")  # the agent's message text is not echoed
            result = msg.get("result")
            return result if isinstance(result, dict) else {}

    def _handle_incoming(self, msg: dict[str, Any], turn: Turn | None) -> None:
        method = str(msg.get("method"))
        raw = msg.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if "id" not in msg:  # a notification
            if method == "session/update" and turn is not None:
                self._update(params.get("update") or {}, turn)
            return
        rid = msg["id"]
        if method == "session/request_permission":
            chosen = self.permission(params)
            outcome = {"outcome": "selected", "optionId": chosen} if chosen else {"outcome": "cancelled"}
            if turn is not None:
                tc = params.get("toolCall")
                tool: dict[str, Any] = tc if isinstance(tc, dict) else {}
                turn.permissions.append({"title": str(tool.get("title", ""))[:200], "answer": outcome})
            self._send({"id": rid, "result": {"outcome": outcome}})
            return
        if turn is not None and method in REFUSED_CLIENT_METHODS:
            turn.refused.append(method)
        self._send({"id": rid, "error": {"code": -32601, "message": "not offered by the lab client: use your own "
                                                                    "tools inside the box"}})

    @staticmethod
    def _update(update: dict[str, Any], turn: Turn) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if isinstance(content, dict) and content.get("type") == "text":
                if sum(map(len, turn.text)) < MAX_TRANSCRIPT_CHARS:
                    turn.text.append(str(content.get("text", "")))
        elif kind == "tool_call":
            turn.tool_calls.append(str(update.get("title", ""))[:200])

    # ------------------------------------------------------------ protocol
    def initialize(self) -> dict[str, Any]:
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise AcpError(f"agent speaks ACP protocol {result.get('protocolVersion')!r}, not {PROTOCOL_VERSION}")
        info = result.get("agentInfo")
        self.agent_info = info if isinstance(info, dict) else {}
        return result

    def new_session(self, cwd: str) -> str:
        result = self._request("session/new", {"cwd": cwd, "mcpServers": []})
        sid = result.get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise AcpError("session/new returned no sessionId")
        self.session_id = sid
        return sid

    def prompt(self, text: str) -> Turn:
        if self.session_id is None:
            raise AcpError("no session: call new_session first")
        turn = Turn()
        result = self._request("session/prompt", {"sessionId": self.session_id,
                                                  "prompt": [{"type": "text", "text": text}]}, turn)
        reason = result.get("stopReason")
        turn.stop_reason = str(reason) if reason is not None else None
        return turn

    def cancel(self) -> None:
        if self.session_id is not None:
            self._send({"method": "session/cancel", "params": {"sessionId": self.session_id}})
