"""Coding-agent adapters for the Lab: how to drive each agent CLI INSIDE a lab box, with all its native tools on.

Ported (protocol knowledge only, rewritten in Python) from OpenResearch, github.com/alphaXiv/OpenResearch at commit
fe36f7424855d7218e64812c5fec6f7db955586f, MIT licence, Copyright (c) 2026 alphaXiv (THIRD_PARTY_NOTICES.md).
Source files read, per adapter:
- Claude Code: src/local/claude.rs (resident `claude --print` stream-json launch, L603-700) and
  src/local/harness/claude.rs (permission modes, L829-854);
- Codex: src/local/harness/codex.rs (`codex exec --json --skip-git-repo-check`, sandbox / approval overrides through
  `-c`, L619, L3934-4060; app-server policies L1393-1415) and src/local/codex.rs (CODEX_HOME, L675-700);
- OpenCode: src/local/opencode.rs (config JSON and permissions, L92-118; env, L440-480), src/local/opencode_runtime.rs
  (OPENCODE_DISABLE_* variables, L75-78) and src/local/local_models.rs (OpenAI-compatible local providers, `share`
  disabled, L270-293);
- ACP agents: src/local/acp.rs (presets, L85-150; client capabilities, L19-31) and src/local/acp/host.rs (methods);
- Cursor, Antigravity: src/local/harness/cursor.rs (L691-713), src/local/harness/antigravity.rs (L380-409).

What changed against OpenResearch (GOAL §17, PORTMAP §3, §10):
- the agent runs inside the lab box, never on the owner's host: OR's host execution (cwd = a host worktree, the whole
  user filesystem reachable) is dropped. Inside the box every native tool is ON, and the agent's own permission
  layer is set to its full-access mode, because the BOX is the boundary (a `--dangerously-skip-permissions`-style
  mode is never used on the host);
- no secrets fan-out: OR exports every entry of `~/.openresearch/env` into every agent (`chat::prepare_env`); here an
  agent gets exactly one scoped credential (credentials.py), as a container environment variable;
- no store access: OR grants Codex its data dir (orx.db) as a writable root; here no store, ledger or truth exists in
  the box;
- usage reporting, error reporting, agent feedback and auto-update are switched off for every CLI that has a switch
  (lists below). Which switches each CLI honours is from vendor documentation and OR's code, NOT verified by running
  them here: the egress check in the owed tests is the real test;
- OR's injected playbook, skills, MCP permission bridge and hooks are dropped (they gate host execution and point
  agents at OR's own CLI and feedback channel).

Built, NOT test-verified.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

OR_SOURCE = "github.com/alphaXiv/OpenResearch@fe36f7424855d7218e64812c5fec6f7db955586f (MIT)"
BOX_HOME = "/home/lab"
DEFAULT_LOCAL_BASE_URL = "http://host.docker.internal:11434/v1"  # Ollama's default port on the host (owner's choice)


class AdapterRefused(PermissionError):
    pass


@dataclass(frozen=True)
class AgentOptions:
    registry_key: str | None = None  # provider registry entry the session credential is for (None = no credential)
    model: str | None = None
    permission_mode: str | None = None  # adapter-specific; default = the agent's full-access mode inside the box
    local_base_url: str | None = None  # OpenCode + local models: an OpenAI-compatible endpoint reachable from the box
    local_context: int = 32768
    acp_preset: str | None = None  # ACP: a preset name (PRESETS) ...
    acp_command: tuple[str, ...] = ()  # ... or an explicit in-box command (the owner's own ACP agent)
    box_env: str | None = None  # override of the in-box credential variable name


@dataclass(frozen=True)
class Plan:
    """Everything the box manager needs to run one agent: non-secret env, config files (in-box HOME only, never the
    workspace), one-time setup commands, the interactive and headless commands, and whether the box needs the
    host-gateway alias (local model server on the host)."""

    adapter: str
    env: dict[str, str]
    config_files: dict[str, str]
    setup: list[list[str]]
    interactive: list[str] | None
    headless: list[str] | None  # with the literal "{prompt}" where the prompt goes; None = driven by acp_client
    acp: list[str] | None = None  # ACP agents: the stdio command acp_client drives
    host_gateway: bool = False
    box_env: str | None = None  # where the session credential goes inside the box (None = no credential)
    notes: list[str] = field(default_factory=list)


class AgentAdapter:
    name = ""
    supported = True
    unsupported_reason = ""
    # registry entries this adapter may use, and the in-box variable each one's credential goes to (None = none)
    credentials: ClassVar[dict[str, str | None]] = {}
    permission_modes: tuple[str, ...] = ()
    default_permission_mode = ""

    def check(self, o: AgentOptions) -> None:
        if not self.supported:
            raise AdapterRefused(f"{self.name}: unsupported in the Lab: {self.unsupported_reason}")
        key = o.registry_key
        if key is None:
            if not any(v is None for v in self.credentials.values()):
                raise AdapterRefused(f"{self.name}: needs a session credential for one of "
                                     f"{sorted(k for k, v in self.credentials.items() if v)}")
        elif key not in self.credentials:
            raise AdapterRefused(f"{self.name}: registry entry {key!r} is not one this adapter may use in the Lab "
                                 f"(allowed: {sorted(self.credentials)})")
        mode = o.permission_mode or self.default_permission_mode
        if self.permission_modes and mode not in self.permission_modes:
            raise AdapterRefused(f"{self.name}: permission mode {mode!r} unknown ({list(self.permission_modes)})")

    def box_env(self, o: AgentOptions) -> str | None:
        if o.registry_key is None:
            return None
        return o.box_env or self.credentials.get(o.registry_key)

    def plan(self, o: AgentOptions) -> Plan:
        raise NotImplementedError


# ---------------------------------------------------------------- Claude Code
CLAUDE_QUIET_ENV = {
    # Claude Code's documented switches (vendor docs; not verified here): no usage reporting, no error reports, no
    # auto-update, no /bug feedback command, and none of the above "non-essential" traffic
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_BUG_COMMAND": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


class ClaudeCode(AgentAdapter):
    """Claude Code, API key only: the registry marks `anthropic-subscription` prohibited (gateway/providers.yaml), and
    a subscription login is not a scoped, capped credential anyway. A CLAUDE_CODE_OAUTH_TOKEN is never passed."""

    name = "claude-code"
    credentials: ClassVar[dict[str, str | None]] = {"anthropic-api": "ANTHROPIC_API_KEY"}
    # OR's five modes (harness/claude.rs L829-854) minus `auto` (OR-specific bridge) and with `default` for manual
    permission_modes = ("bypassPermissions", "acceptEdits", "default", "plan")
    default_permission_mode = "bypassPermissions"  # inside the box only: the box is the boundary

    def plan(self, o: AgentOptions) -> Plan:
        self.check(o)
        mode = o.permission_mode or self.default_permission_mode
        model = ["--model", o.model] if o.model else []
        settings = {"permissions": {"defaultMode": mode}, "env": dict(CLAUDE_QUIET_ENV)}
        return Plan(
            adapter=self.name, env=dict(CLAUDE_QUIET_ENV),
            config_files={f"{BOX_HOME}/.claude/settings.json": json.dumps(settings, indent=2, sort_keys=True)},
            setup=[],
            interactive=["claude", "--permission-mode", mode, *model],
            # OR's stream-json print flags (claude.rs L612-626); no playbook, no MCP gate, no hooks
            headless=["claude", "--print", "{prompt}", "--output-format", "stream-json", "--verbose",
                      "--permission-mode", mode, *model],
            box_env=self.box_env(o),
            notes=["API key only (anthropic-api); anthropic-subscription is prohibited in the provider registry",
                   "bypassPermissions is refused by Claude Code when run as root: the lab box runs as uid 1000"])


# ---------------------------------------------------------------- Codex
CODEX_CONFIG = """# lab box: written by custom_harness/lab/agents.py (not agent-authored)
# no update check, no OTel export, no analytics (keys from Codex docs; [U] not verified here)
check_for_update_on_startup = false
sandbox_mode = "danger-full-access"
approval_policy = "never"

[otel]
exporter = "none"

[analytics]
enabled = false
"""


class Codex(AgentAdapter):
    """Codex with an API key. The `openai-codex-cli` subscription entry is allowed for the GATEWAY (codex exec in an
    empty dir) but refused in the Lab: a ChatGPT login is the owner's own account, not a scoped, capped key."""

    name = "codex"
    credentials: ClassVar[dict[str, str | None]] = {"openai-api": "OPENAI_API_KEY"}
    permission_modes = ("danger-full-access", "workspace-write")
    default_permission_mode = "danger-full-access"  # Codex's own sandbox inside the box would only get in the way

    def plan(self, o: AgentOptions) -> Plan:
        self.check(o)
        mode = o.permission_mode or self.default_permission_mode
        model = ["-m", o.model] if o.model else []
        cfg = CODEX_CONFIG.replace('sandbox_mode = "danger-full-access"', f'sandbox_mode = "{mode}"')
        env = self.box_env(o) or "OPENAI_API_KEY"
        return Plan(
            adapter=self.name, env={"CODEX_HOME": f"{BOX_HOME}/.codex"},
            config_files={f"{BOX_HOME}/.codex/config.toml": cfg},
            # `codex login --with-api-key` reads the key on stdin and stores it in CODEX_HOME (in-box HOME, not the
            # workspace, gone when the box is removed) [U: flag spelling per Codex docs, not verified here]
            setup=[["sh", "-c", f'printenv {env} | codex login --with-api-key']],
            interactive=["codex", *model],
            # OR's exec form (harness/codex.rs L619, L3995, L4012-4020): -c overrides work for fresh and resumed runs
            headless=["codex", "exec", "--json", "--skip-git-repo-check", "-c", f'sandbox_mode="{mode}"',
                      "-c", 'approval_policy="never"', *model, "{prompt}"],
            box_env=env,
            notes=["API key only (openai-api); the openai-codex-cli subscription entry is refused in the Lab"])


# ---------------------------------------------------------------- OpenCode (+ local models)
OPENCODE_QUIET_ENV = {
    # OR sets these for every opencode child (opencode_runtime.rs L75-78, opencode.rs L837-855)
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",  # a workspace opencode.json cannot re-enable sharing
}


class OpenCode(AgentAdapter):
    name = "opencode"
    credentials: ClassVar[dict[str, str | None]] = {"local-openai-compatible": None,
                                                    "anthropic-api": "ANTHROPIC_API_KEY",
                                                    "openai-api": "OPENAI_API_KEY"}
    permission_modes = ("allow",)
    default_permission_mode = "allow"

    def check(self, o: AgentOptions) -> None:
        if o.registry_key is None:
            o = replace(o, registry_key="local-openai-compatible")  # no __dict__: the kerneld ratchet flags it (int18)
        super().check(o)
        if o.registry_key == "local-openai-compatible" and not o.model:
            raise AdapterRefused("opencode with a local model: name the model (--model, as the server lists it)")

    def plan(self, o: AgentOptions) -> Plan:
        self.check(o)
        local = o.registry_key in (None, "local-openai-compatible")
        # every permission "allow" as in OR (opencode.rs L96-108): acceptable here because it is inside the box
        perms = {k: "allow" for k in ("edit", "bash", "webfetch", "websearch", "read", "glob", "grep", "task",
                                      "skill", "lsp", "doom_loop", "external_directory")}
        cfg: dict[str, Any] = {"$schema": "https://opencode.ai/config.json", "permission": perms,
                               "autoupdate": False, "share": "disabled"}
        base = o.local_base_url or DEFAULT_LOCAL_BASE_URL
        model = o.model
        if local:
            # OR's local provider shape (local_models.rs L281-287)
            cfg["provider"] = {"lab-local": {"npm": "@ai-sdk/openai-compatible", "name": "lab local model",
                                             "options": {"baseURL": base, "apiKey": "none"},
                                             "models": {str(o.model): {"name": f"{o.model} (local)",
                                                                       "limit": {"context": o.local_context,
                                                                                 "output": 4096}}}}}
            model = f"lab-local/{o.model}"
        if model:
            cfg["model"] = model
        path = f"{BOX_HOME}/.config/opencode/lab.json"
        env = {**OPENCODE_QUIET_ENV, "OPENCODE_CONFIG": path}
        m = ["--model", model] if model else []
        return Plan(adapter=self.name, env=env, config_files={path: json.dumps(cfg, indent=2, sort_keys=True)},
                    setup=[], interactive=["opencode", *m], headless=["opencode", "run", *m, "{prompt}"],
                    host_gateway=local and "host.docker.internal" in base, box_env=self.box_env(o),
                    notes=["local models: no credential; the box reaches the host's model server through "
                           "host.docker.internal (see docs/lab.md residual risks)"] if local else [])


# ---------------------------------------------------------------- ACP agents
@dataclass(frozen=True)
class AcpPreset:
    name: str
    acp: tuple[str, ...]  # the stdio ACP command (acp.rs presets)
    tui: tuple[str, ...] | None  # the agent's own interactive command, when it has one [U per agent]
    setup_url: str


# OR's presets (acp.rs L85-136). None is usable until the owner adds a provider registry entry for it (mode api or
# ow) and passes that key: their terms for automated use are unknown (PORTMAP §8 item 4).
PRESETS: dict[str, AcpPreset] = {
    "deepseek-harness": AcpPreset("DeepSeek Harness", ("dsh", "--profile", "acp"), ("dsh",),
                                  "https://github.com/deepseek-ai/deepseek-harness"),
    "kimi-code": AcpPreset("Kimi Code", ("kimi", "acp"), ("kimi",), "https://www.kimi.com/code/docs/en/kimi-code-cli/"),
    "minimax-code": AcpPreset("MiniMax Code", ("mcode", "acp"), ("mcode",), "https://github.com/MiniMax-AI/minimax-code"),
    "zcode": AcpPreset("ZCode", ("zcode-acp-server",), None, "https://github.com/william0wang/zcode-acp#install"),
    "pi": AcpPreset("Pi", ("pi-acp",), ("pi",), "https://github.com/svkozak/pi-acp#install"),
    "devin": AcpPreset("Devin CLI", ("devin", "acp"), ("devin",), "https://docs.devin.ai/cli"),
    "grok-build": AcpPreset("Grok Build", ("grok", "agent", "stdio"), ("grok",), "https://github.com/xai-org/grok-build"),
}


class Acp(AgentAdapter):
    """Any Agent Client Protocol v1 agent, driven by lab/acp_client.py over `docker exec -i` stdio. The client
    advertises NO fs and NO terminal capability: OR serves those on the HOST with any absolute path
    (acp/delegated.rs, PORTMAP §10 S5); here the agent uses its own native tools inside the box. An agent that can
    only work through client-side tools is therefore unsupported (stated)."""

    name = "acp"
    credentials: ClassVar[dict[str, str | None]] = {}  # any entry the registry gate admits (credentials.py)

    def check(self, o: AgentOptions) -> None:
        if bool(o.acp_preset) == bool(o.acp_command):
            raise AdapterRefused("acp: give exactly one of --acp-preset NAME or --acp-command '...'")
        if o.acp_preset and o.acp_preset not in PRESETS:
            raise AdapterRefused(f"acp: unknown preset {o.acp_preset!r} (known: {sorted(PRESETS)})")
        if o.registry_key is not None and not (o.box_env or "").strip():
            raise AdapterRefused("acp: name the in-box variable the agent reads its key from (--box-env)")

    def box_env(self, o: AgentOptions) -> str | None:
        return o.box_env if o.registry_key is not None else None

    def plan(self, o: AgentOptions) -> Plan:
        self.check(o)
        if o.acp_preset:
            p = PRESETS[o.acp_preset]
            acp, tui = list(p.acp), (list(p.tui) if p.tui else None)
        else:
            acp, tui = list(o.acp_command), None
        return Plan(adapter=self.name, env={}, config_files={}, setup=[], interactive=tui, headless=None, acp=acp,
                    box_env=self.box_env(o),
                    notes=["usage-reporting/auto-update switches of ACP agents are agent-specific and unknown: "
                           "the egress test is the only check (docs/lab.md)"])


# ---------------------------------------------------------------- deferred
class Unsupported(AgentAdapter):
    supported = False

    def __init__(self, name: str, reason: str):
        self.name = name
        self.unsupported_reason = reason

    def plan(self, o: AgentOptions) -> Plan:
        self.check(o)
        raise AssertionError("unreachable")


ADAPTERS: dict[str, AgentAdapter] = {
    "claude-code": ClaudeCode(),
    "codex": Codex(),
    "opencode": OpenCode(),
    "acp": Acp(),
    "cursor": Unsupported("cursor", "deferred (owner brief 2026-10-23): closed CLI with no provider registry entry; "
                                    "OR drives it with `--force` / `--sandbox disabled` (cursor.rs L691-713), and its "
                                    "credential is a Cursor account login, not a scoped, spend-capped key"),
    "antigravity": Unsupported("antigravity", "deferred (owner brief 2026-10-23): OR always passes "
                                              "`--dangerously-skip-permissions` (antigravity.rs L380-409); auth is a "
                                              "Google sign-in whose registry status is `unknown` "
                                              "(google-subscription)"),
}


def adapter(name: str) -> AgentAdapter:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise AdapterRefused(f"unknown agent {name!r} (known: {sorted(ADAPTERS)})") from None


def fill_prompt(argv: list[str], prompt: str) -> list[str]:
    """The headless argv with the prompt in place of "{prompt}" (one argv element: never through a shell)."""
    if argv.count("{prompt}") != 1:
        raise AdapterRefused("headless command has no single {prompt} slot")
    return [prompt if a == "{prompt}" else a for a in argv]
