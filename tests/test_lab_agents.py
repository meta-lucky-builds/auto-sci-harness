"""Agent adapters and the ACP client of the Lab: lab/agents.py, lab/acp_client.py.

Fail-first design: the adapters drop OpenResearch's unsafe defaults (host execution, secrets fan-out, store access,
telemetry, auto-update, client-side host tools). Each test pins one dropped default; reverting the port to OR's
behaviour (e.g. advertising fs/terminal to ACP agents, accepting a subscription key, leaving OpenCode sharing on)
makes it fail.

Written 2026-10-23 by lab-agents; built, NOT run.
"""

from __future__ import annotations

import io
import json

import pytest

from custom_harness.lab import acp_client as A
from custom_harness.lab import agents as G


def test_claude_code_is_api_key_only_and_quiet():
    plan = G.adapter("claude-code").plan(G.AgentOptions(registry_key="anthropic-api"))
    assert plan.box_env == "ANTHROPIC_API_KEY"
    for k in ("DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING", "DISABLE_AUTOUPDATER",
              "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
        assert plan.env[k] == "1"
    assert all(p.startswith(G.BOX_HOME + "/") for p in plan.config_files)
    assert plan.headless is not None and "--output-format" in plan.headless
    with pytest.raises(G.AdapterRefused):
        G.adapter("claude-code").plan(G.AgentOptions(registry_key="anthropic-subscription"))
    with pytest.raises(G.AdapterRefused):
        G.adapter("claude-code").plan(G.AgentOptions(registry_key=None))


def test_codex_refuses_the_subscription_entry_and_turns_off_update_checks():
    with pytest.raises(G.AdapterRefused):
        G.adapter("codex").plan(G.AgentOptions(registry_key="openai-codex-cli"))
    plan = G.adapter("codex").plan(G.AgentOptions(registry_key="openai-api"))
    cfg = plan.config_files[f"{G.BOX_HOME}/.codex/config.toml"]
    assert "check_for_update_on_startup = false" in cfg and 'exporter = "none"' in cfg
    assert plan.box_env == "OPENAI_API_KEY"


def test_opencode_local_model_needs_no_credential_and_never_shares():
    plan = G.adapter("opencode").plan(G.AgentOptions(model="qwen2.5-coder:7b"))
    assert plan.box_env is None and plan.host_gateway
    cfg = json.loads(plan.config_files[f"{G.BOX_HOME}/.config/opencode/lab.json"])
    assert cfg["share"] == "disabled" and cfg["autoupdate"] is False
    assert cfg["model"] == "lab-local/qwen2.5-coder:7b"
    assert plan.env["OPENCODE_DISABLE_AUTOUPDATE"] == "1" and plan.env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"


def test_opencode_deepseek_config_names_the_variable_never_a_key_and_never_shares():
    plan = G.adapter("opencode").plan(G.AgentOptions(registry_key=G.DEEPSEEK_KEY, model="some-model"))
    assert plan.box_env == "DEEPSEEK_API_KEY"
    assert not plan.host_gateway and plan.host_port is None  # a hosted API: this box gets no port on the host
    cfg = json.loads(plan.config_files[f"{G.BOX_HOME}/.config/opencode/lab.json"])
    assert cfg["share"] == "disabled" and cfg["autoupdate"] is False
    assert cfg["model"] == "lab-deepseek/some-model"
    options = cfg["provider"]["lab-deepseek"]["options"]
    assert options["apiKey"] == "{env:DEEPSEEK_API_KEY}"
    assert options["baseURL"] == G.DEEPSEEK_BASE_URL and options["baseURL"].startswith("https://")
    assert "some-model" in cfg["provider"]["lab-deepseek"]["models"]


def test_opencode_deepseek_needs_a_model():
    with pytest.raises(G.AdapterRefused, match="name the model"):
        G.adapter("opencode").plan(G.AgentOptions(registry_key=G.DEEPSEEK_KEY))


def test_deepseek_is_only_for_opencode():
    with pytest.raises(G.AdapterRefused):
        G.adapter("claude-code").plan(G.AgentOptions(registry_key=G.DEEPSEEK_KEY, model="m"))
    with pytest.raises(G.AdapterRefused):
        G.adapter("codex").plan(G.AgentOptions(registry_key=G.DEEPSEEK_KEY, model="m"))


def test_the_bundled_deepseek_entry_passes_the_registry_rules_and_is_not_yet_signed_off():
    from custom_harness.gateway.costs import rate_for, registry_problems
    from custom_harness.gateway.registry import load_registry, policy_problems, require_usable

    reg = load_registry()
    entry = require_usable(G.DEEPSEEK_KEY, reg)
    assert entry["provider"] == "deepseek" and entry["mode"] == "api"
    assert entry["training_on_outputs"] == "unknown"
    assert entry["verified_by"] is None  # only a person may fill this in
    assert not [p for p in registry_problems(reg) + policy_problems(reg) if p.startswith(G.DEEPSEEK_KEY)]
    assert rate_for("deepseek", "any-model").placeholder  # no price invented


@pytest.mark.parametrize("name", ["cursor", "antigravity"])
def test_deferred_agents_are_refused_with_a_reason(name):
    ad = G.adapter(name)
    assert not ad.supported and ad.unsupported_reason
    with pytest.raises(G.AdapterRefused):
        ad.plan(G.AgentOptions())


def test_fill_prompt_keeps_the_prompt_one_argv_element():
    argv = G.fill_prompt(["claude", "--print", "{prompt}"], "a; rm -rf / && echo $HOME")
    assert argv == ["claude", "--print", "a; rm -rf / && echo $HOME"]


class _Peer:
    """A scripted ACP agent: replies to the client's requests and makes reverse requests (fs, terminal, permission),
    like OR's acp-peer.py fixture."""

    def __init__(self):
        self.to_client = io.BytesIO()
        self.from_client: list[dict] = []

    def readline(self, limit: int = -1) -> bytes:
        return self.to_client.readline(limit)

    def write(self, b: bytes) -> int:
        msg = json.loads(b.decode("utf-8"))
        self.from_client.append(msg)
        pos = self.to_client.tell()
        self.to_client.seek(0, io.SEEK_END)
        out = self._answer(msg)
        for o in out:
            self.to_client.write((json.dumps({"jsonrpc": "2.0", **o}) + "\n").encode("utf-8"))
        self.to_client.seek(pos)
        return len(b)

    def flush(self) -> None:
        pass

    def _answer(self, msg: dict) -> list[dict]:
        m = msg.get("method")
        if m == "initialize":
            return [{"id": msg["id"], "result": {"protocolVersion": 1, "agentInfo": {"name": "peer"}}}]
        if m == "session/new":
            return [{"id": msg["id"], "result": {"sessionId": "s1"}}]
        if m == "session/prompt":
            self._prompt_id = msg["id"]
            upd = {"method": "session/update", "params": {"sessionId": "s1", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hello"}}}}
            return [upd,
                    {"id": "r1", "method": "fs/read_text_file", "params": {"sessionId": "s1", "path": "/etc/passwd"}},
                    {"id": "r2", "method": "session/request_permission", "params": {
                        "sessionId": "s1", "toolCall": {"title": "run"},
                        "options": [{"optionId": "deny", "kind": "reject_once"},
                                    {"optionId": "once", "kind": "allow_once"}]}}]
        if msg.get("id") == "r2":
            return [{"id": self._prompt_id, "result": {"stopReason": "end_turn"}}]
        return []


def test_acp_client_offers_no_host_tools_and_answers_permissions_in_the_box():
    peer = _Peer()
    c = A.AcpClient(peer, peer)  # type: ignore[arg-type]
    c.initialize()
    caps = peer.from_client[0]["params"]["clientCapabilities"]
    assert caps == {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
    c.new_session("/workspace")
    turn = c.prompt("do it")
    assert turn.transcript == "hello" and turn.stop_reason == "end_turn"
    assert turn.refused == ["fs/read_text_file"]
    fs_reply = next(m for m in peer.from_client if m.get("id") == "r1")
    assert fs_reply["error"]["code"] == -32601
    perm = next(m for m in peer.from_client if m.get("id") == "r2")
    assert perm["result"]["outcome"] == {"outcome": "selected", "optionId": "once"}


def test_acp_client_refuses_another_protocol_version():
    peer = _Peer()
    peer._answer = lambda msg: [{"id": msg["id"], "result": {"protocolVersion": 2}}]  # type: ignore[method-assign]
    with pytest.raises(A.AcpError):
        A.AcpClient(peer, peer).initialize()  # type: ignore[arg-type]
