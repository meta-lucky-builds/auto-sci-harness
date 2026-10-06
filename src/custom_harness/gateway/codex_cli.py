"""BYOS backend via the official OpenAI Codex CLI (registry entry `openai-codex-cli`).

We run `codex exec` as a subprocess in an empty, read-only working directory. The user signs in
to Codex themselves; we never read or reuse Codex credentials.

Honest limits:
- Codex is an agent, not a raw completion API: each call carries Codex's own system prompt
  (~16.7k input tokens observed on 2026-10-06, mostly cached). Usage counts against plan limits.
- Plan usage is not reported in $; we record tokens as reported plus 1 quota unit per call (estimated).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..records import AccessMode, Cost
from .base import (
    Backend,
    BackendError,
    ChatRequest,
    ChatResponse,
    Message,
    redact,
    schema_instruction,
    to_strict_schema,
)
from .registry import require_usable


def _flatten(req: ChatRequest) -> str:
    parts = []
    for m in req.messages:
        parts.append(f"<{m.role}>\n{m.content}\n</{m.role}>")
    parts.append("Respond to the final user message only. Do not run commands or edit files; "
                 "reply with the answer text.")
    return "\n\n".join(parts)


class CodexCLIBackend(Backend):
    name = "codex-cli"
    provider = "openai"
    mode = AccessMode.SUBSCRIPTION

    def __init__(self, model: str | None = None, timeout_s: float = 600, executable: str = "codex",
                 registry: dict | None = None):
        entry = require_usable("openai-codex-cli", registry)
        self.training_on_outputs = entry.get("training_on_outputs", "unknown")
        self.model = model
        self.timeout_s = timeout_s
        exe = shutil.which(executable)
        if exe is None:
            raise BackendError("Codex CLI not found on PATH; install it and run `codex login` yourself.")
        self.exe = exe

    def complete(self, req: ChatRequest) -> ChatResponse:
        if req.json_schema is None:
            return self._complete(req, None)
        try:
            return self._complete(req, to_strict_schema(req.json_schema))
        except BackendError as e:
            if "invalid_json_schema" not in str(e):
                raise
            # Provider rejected the schema even in strict form: fall back to prompt-level instruction.
            # The engine's own validation against the original schema still applies.
            msgs = req.messages[:-1] + [Message(role=req.messages[-1].role, content=req.messages[-1].content
                                                + "\n\n" + schema_instruction(req.json_schema))]
            return self._complete(req.model_copy(update={"messages": msgs}), None)

    def _complete(self, req: ChatRequest, schema: dict | None) -> ChatResponse:
        model = req.model or self.model
        with tempfile.TemporaryDirectory(prefix="chh-codex-") as tmp:
            tmpdir = Path(tmp)
            out_file = tmpdir / "last_message.txt"
            cmd = [self.exe, "exec", "--json", "--ephemeral", "-s", "read-only",
                   "--skip-git-repo-check", "-C", str(tmpdir), "-o", str(out_file)]
            if model:
                cmd += ["-m", model]
            if schema is not None:
                schema_file = tmpdir / "schema.json"
                schema_file.write_text(json.dumps(schema), encoding="utf-8")
                cmd += ["--output-schema", str(schema_file)]
            cmd.append("-")
            t0 = time.time()
            try:
                proc = subprocess.run(cmd, input=_flatten(req), capture_output=True, text=True, check=False,
                                      encoding="utf-8", timeout=self.timeout_s)
            except subprocess.TimeoutExpired as e:
                raise BackendError(f"codex exec timed out after {self.timeout_s}s", transient=True) from e
            wall = time.time() - t0

            usage: dict = {}
            text_from_events = None
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "turn.completed":
                    usage = ev.get("usage", {}) or {}
                elif ev.get("type") == "item.completed" and ev.get("item", {}).get("type") == "agent_message":
                    text_from_events = ev["item"].get("text")
                elif ev.get("type") in ("turn.failed", "error"):
                    raise BackendError(redact(f"codex error event: {line[:2000]}"), transient=_transient(line))

            text = out_file.read_text(encoding="utf-8") if out_file.exists() else text_from_events
            if proc.returncode != 0 or text is None:
                raise BackendError(redact(f"codex exec failed (rc={proc.returncode}): {proc.stderr[-2000:]}"),
                                   transient=_transient(proc.stderr))

        cost = Cost(prompt_tokens=int(usage.get("input_tokens", 0)),
                    completion_tokens=int(usage.get("output_tokens", 0))
                    + int(usage.get("reasoning_output_tokens", 0)),
                    quota_units=1.0, wall_s=wall, estimated=True)
        return ChatResponse(text=text.strip(), cost=cost, provenance=self.provenance(model or "codex-default"))


_TRANSIENT = re.compile(r"rate.?limit|\b(429|50[0234])\b|timed out|timeout|connection (reset|refused)|"
                        r"temporarily unavailable|overloaded", re.IGNORECASE)


def _transient(text: str) -> bool:
    """Heuristic: Codex reports provider failures only as text. Unknown -> not transient (no retry storm)."""
    return _TRANSIENT.search(text) is not None
