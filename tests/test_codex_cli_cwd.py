"""Codex runs from an empty working directory (GOAL §16.8): the subprocess cwd AND `-C` are one fresh, empty
temp directory (no repo, no AGENTS.md, no harness files), and the schema / last-message files live elsewhere.
No live Codex: shutil.which and subprocess.run are faked."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from custom_harness.gateway import codex_cli
from custom_harness.gateway.base import ChatRequest, Message
from custom_harness.gateway.registry import require_usable

TURN = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}})


def _inside(p: Path, d: Path) -> bool:
    try:
        p.resolve().relative_to(d.resolve())
        return True
    except ValueError:
        return False


@pytest.fixture
def fake_codex(monkeypatch):
    seen: list[dict] = []

    def fake_run(cmd, **kw):
        c_dir = Path(cmd[cmd.index("-C") + 1])
        out = Path(cmd[cmd.index("-o") + 1])
        schema = Path(cmd[cmd.index("--output-schema") + 1]) if "--output-schema" in cmd else None
        cwd = kw.get("cwd")
        seen.append({
            "cmd": list(cmd), "kw": dict(kw), "c_dir": c_dir, "out": out, "schema": schema, "cwd": cwd,
            "cwd_exists": cwd is not None and Path(cwd).is_dir(),
            "cwd_entries": sorted(os.listdir(cwd)) if cwd is not None and Path(cwd).is_dir() else None,
            "c_entries": sorted(os.listdir(c_dir)) if c_dir.is_dir() else None,
            "schema_text": schema.read_text(encoding="utf-8") if schema is not None and schema.exists() else None,
        })
        out.write_text("the answer\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout=TURN + "\n", stderr="")

    monkeypatch.setattr(codex_cli.shutil, "which", lambda exe: "C:/fake/bin/codex.exe")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)
    return seen


def test_registry_gate_still_admits_codex():
    entry = require_usable("openai-codex-cli")  # raises ProviderBlocked if the default registry blocks it
    assert entry["mode"] == "byos" and entry["byos_status"] == "allowed"


@pytest.mark.parametrize("schema", [None, {"type": "object", "properties": {"a": {"type": "string"}}}])
def test_codex_runs_in_a_fresh_empty_directory_with_io_files_outside_it(fake_codex, schema):
    b = codex_cli.CodexCLIBackend(model="gpt-test")
    resp = b.complete(ChatRequest(messages=[Message(role="user", content="hi")], json_schema=schema))
    assert resp.text == "the answer" and resp.cost.prompt_tokens == 7 and resp.cost.completion_tokens == 3
    assert len(fake_codex) == 1
    call = fake_codex[0]
    # the process cwd is set explicitly (not inherited from the harness, i.e. the repo) and IS the -C directory
    assert call["cwd"] is not None, "codex inherited the harness process cwd"
    assert call["cwd_exists"] and Path(call["cwd"]).resolve() == call["c_dir"].resolve()
    assert not _inside(Path.cwd(), call["c_dir"]) and not _inside(call["c_dir"], Path.cwd())
    # ... and it is empty when codex starts: no files, no .git, no AGENTS.md, no schema / output file
    assert call["cwd_entries"] == [] and call["c_entries"] == []
    assert not _inside(call["out"], call["c_dir"])
    if schema is None:
        assert call["schema"] is None
    else:
        assert call["schema"] is not None and not _inside(call["schema"], call["c_dir"])
        assert json.loads(call["schema_text"])["additionalProperties"] is False  # strict form was written
    # both temp directories are removed after the call
    assert not call["c_dir"].exists() and not call["out"].parent.exists()


def test_codex_run_directory_is_fresh_per_call(fake_codex):
    b = codex_cli.CodexCLIBackend()
    for _ in range(2):
        b.complete(ChatRequest(messages=[Message(role="user", content="hi")]))
    assert fake_codex[0]["c_dir"] != fake_codex[1]["c_dir"]
    assert all(c["cwd_entries"] == [] for c in fake_codex)
