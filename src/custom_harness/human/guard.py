"""Who may take a human-only action, and from where (GOAL invariants 13, 15, 22).

Three checks every write in `custom_harness.human` runs first (`require_human`):

1. **Named human.** The actor string must name a person. Empty names and agent identities are refused: names that
   start with an agent or model token ("agent", "model", "claude", "gpt", ...), contain an agent word anywhere
   ("agent", "subagent", "bot", "llm", "assistant"), have the "family/model" shape, or match this project's
   workstream names ("s4-human", "w2-taint", "b-evolve", "g-synth", "integrator", "main", ...).
2. **Not from inside a run.** The call is refused when any frame on the Python stack is a registered module
   function or the engine's step executor: a module (or an evolved node, which can only call registered modules)
   cannot reach these functions in-process.
3. **Interactive terminal** (CLI only, `require_tty`): the new human commands refuse a non-interactive stdin, so an
   agent driving a shell without a terminal cannot run them.

Honest limits (stated, as everywhere in this package): these are accountability checks, not authentication. A name
is not proof of a person; trusted host code that imports `Ledger` can still append entries directly; a module is
in-process Python and could bypass the stack check by spawning a thread. The ledger names who approved what, and
the static test (tests/test_human_static.py) keeps every write path greppable and allowlisted.
"""

from __future__ import annotations

import re
import sys
from types import CodeType, FrameType

MAX_NAME = 120

# first token of a name that marks an agent or a model (lower-case)
_AGENT_FIRST_TOKENS = frozenset({
    "agent", "agents", "subagent", "model", "bot", "llm", "assistant", "claude", "anthropic", "gpt", "openai",
    "codex", "chatgpt", "gemini", "google", "llama", "meta", "qwen", "mistral", "mixtral", "deepseek", "grok", "xai",
    "copilot", "mock", "scripted", "system", "harness", "engine", "module", "node", "evolver", "proposer", "critic",
    "judge", "replicator", "executor", "verifier",
})
# words that mark an agent wherever they appear
_AGENT_ANY_TOKENS = frozenset({"agent", "agents", "subagent", "bot", "llm", "assistant"})
# this project's agent / workstream names
_AGENT_EXACT = frozenset({"main", "integrator", "orchestrator", "coordinator", "redteam", "red-team", "red team",
                          "reviewer-agent", "rulings", "humancheck"})
_WORKSTREAM = re.compile(r"(?:s|w|ph|m)\d+[-_ ].*|[bgxk][-_].*|\w+__\w+")


class HumanActionRefused(ValueError):
    """A human-only action was refused (who, from where, or what it binds)."""


def check_human_name(name: object, *, what: str = "the human taking this action") -> str:
    """The stripped name, or HumanActionRefused when it is empty or names an agent / model / workstream."""
    if not isinstance(name, str) or not name.strip():
        raise HumanActionRefused(f"{what} must name a human (the name is empty)")
    n = name.strip()
    if len(n) > MAX_NAME or any(ord(c) < 32 for c in n):
        raise HumanActionRefused(f"{what}: a name is at most {MAX_NAME} printable characters")
    low = n.casefold()
    tokens = [t for t in re.split(r"[^a-z0-9]+", low) if t]
    agentish = (
        "/" in n
        or low in _AGENT_EXACT
        or _WORKSTREAM.fullmatch(low) is not None
        or (tokens and tokens[0] in _AGENT_FIRST_TOKENS)
        or any(t in _AGENT_ANY_TOKENS for t in tokens)
    )
    if agentish:
        raise HumanActionRefused(f"{what}: {n!r} names an agent, a model or a workstream, not a human; only a "
                                 "human may take this action")
    return n


def _run_step_codes() -> set[CodeType]:
    """Code objects that are on the stack exactly when a run step executes module code."""
    codes: set[CodeType] = set()
    try:
        from ..runtime.engine import Engine
        from ..runtime.modules import get_module, registered
    except ImportError:  # pragma: no cover - the runtime is part of the package
        return codes
    for name in ("_execute", "_map", "_attempt"):
        fn = getattr(Engine, name, None)
        code = getattr(fn, "__code__", None)
        if isinstance(code, CodeType):
            codes.add(code)
    for mid in registered():
        code = getattr(get_module(mid), "__code__", None)
        if isinstance(code, CodeType):
            codes.add(code)
    return codes


def inside_run_step(frame: FrameType | None = None) -> bool:
    """True when the current call stack passes through the engine's step executor or a registered module."""
    codes = _run_step_codes()
    f: FrameType | None = frame if frame is not None else sys._getframe(1)
    while f is not None:
        if f.f_code in codes:
            return True
        f = f.f_back
    return False


def require_human(name: object, *, what: str = "the human taking this action") -> str:
    """Both API checks: refuse from inside a run step, refuse a non-human name. Returns the stripped name."""
    if inside_run_step(sys._getframe(1)):
        raise HumanActionRefused("a human-only action cannot be taken from inside a run (a module or an evolved "
                                 "node); a human runs it from the CLI or the studio")
    return check_human_name(name, what=what)


def _windows_console(stream: object) -> bool:
    """On Windows `isatty()` is also True for the NUL device; only a real console has a console mode."""
    try:
        import ctypes
        import msvcrt
        handle = msvcrt.get_osfhandle(stream.fileno())  # type: ignore[attr-defined,unused-ignore]
        mode = ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))  # type: ignore[attr-defined,unused-ignore]
    except (AttributeError, OSError, ValueError, ImportError):
        return False


def interactive_stdin() -> bool:
    try:
        tty = sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False
    if tty and sys.platform == "win32" and hasattr(sys.stdin, "fileno"):
        try:
            sys.stdin.fileno()
        except (OSError, ValueError, AttributeError):
            return tty  # an in-memory stand-in (tests): its own isatty() answer
        return _windows_console(sys.stdin)
    return tty


def require_tty(command: str) -> None:
    """CLI only: the new human commands need an interactive terminal (stdin). Honest limit: an agent harness that
    gives its shell a real console or pty passes this check (observed 2026-10-14: this session's Bash tool reports
    isatty() True); the typed hash confirmation and the agent-name refusal still apply."""
    if not interactive_stdin():
        raise HumanActionRefused(f"{command} is a HUMAN action and needs an interactive terminal")
