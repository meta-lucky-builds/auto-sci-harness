"""Telemetry stays opt-in (GOAL §5 L9): nothing phones home.

Static: only the modules listed in NETWORK_MODULES import a network library, each for a user-invoked purpose; no
module mentions a telemetry/analytics client. Dynamic: with every socket connection and name lookup trapped, an engine
run with a local model, and the studio commands, make no network attempt. The model backends that DO use the network
(OpenAI-compatible API, the Codex CLI, literature retrieval, the OpenML fetch of the claim audit) run only when a user
binds them or invokes that command; none runs at import time or by default.
"""

import ast
import socket
from pathlib import Path

import pytest

from custom_harness import cli
from custom_harness.config import data_dir
from custom_harness.gateway import MockBackend
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.store import EvidenceStore

SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness"
NETWORK_LIBS = {"socket", "urllib.request", "urllib3", "http.client", "http.server", "requests", "httpx", "aiohttp",
                "smtplib", "ftplib", "telnetlib", "xmlrpc.client", "websocket", "websockets", "grpc", "ssl",
                "sentry_sdk", "posthog", "mixpanel", "analytics", "opentelemetry", "segment"}
# module -> why it may reach the network (all user-invoked, none on import or by default)
NETWORK_MODULES = {
    "gateway/openai_compat.py": "model API backend, only when a user binds an openai:/ollama: role",
    "literature/http.py": "literature retrieval, only from `chh lit ...` commands (per-host pacing)",
    "audit/data.py": "OpenML dataset fetch, only from the claim audit command a user runs",
    "runtime/engine.py": "socket.gethostname() for the run-owner record (no connection)",
    # lab-compute 2026-10-23 (GOAL §17): Ray Jobs / HF Jobs REST calls, only when the owner configured that backend
    # and a run is submitted to it; httpx is imported inside the call, never at import time
    "compute/rest.py": "Ray Jobs / HF Jobs REST backends, only on an owner-configured submit (lab-compute)",
}


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
            out |= {f"{node.module}.{a.name}" for a in node.names}
    return out


def _network_imports(path: Path) -> set[str]:
    return {m for m in _imports(path) if m in NETWORK_LIBS or m.split(".")[0] in NETWORK_LIBS - {"urllib", "http"}}


def test_only_listed_modules_import_a_network_library():
    found = {p.relative_to(SRC).as_posix(): sorted(_network_imports(p)) for p in SRC.rglob("*.py")}
    found = {k: v for k, v in found.items() if v}
    assert len(list(SRC.rglob("*.py"))) > 100  # the scan really covered the package
    assert set(found) == set(NETWORK_MODULES), found
    assert found["runtime/engine.py"] == ["socket"]
    text = "\n".join(p.read_text(encoding="utf-8").lower() for p in SRC.rglob("*.py"))
    for client in ("sentry_sdk", "posthog", "mixpanel", "opentelemetry", "segment.io", "telemetry endpoint"):
        assert client not in text


def test_the_scan_catches_a_planted_phone_home(tmp_path):
    planted = tmp_path / "phone_home.py"
    planted.write_text("import urllib.request\nurllib.request.urlopen('https://example.invalid/ping')\n")
    assert _network_imports(planted) == {"urllib.request"}
    planted.write_text("from http.client import HTTPSConnection\n")
    assert _network_imports(planted) == {"http.client"}
    planted.write_text("from urllib import request\nimport urllib.parse\n")
    assert _network_imports(planted) == {"urllib.request"}  # urllib.parse is text handling, not network


@pytest.fixture
def no_network(monkeypatch):
    attempts: list[str] = []

    def trap(name):
        def f(*a, **k):
            attempts.append(f"{name}{a[:2]}")
            raise OSError("network disabled by test_telemetry")
        return f

    monkeypatch.setattr(socket.socket, "connect", trap("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", trap("connect_ex"))
    monkeypatch.setattr(socket, "create_connection", trap("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", trap("getaddrinfo"))
    return attempts


def test_a_run_and_the_studio_commands_make_no_network_attempt(no_network, capsys):
    s = EvidenceStore(data_dir() / "store")
    try:
        spec = HarnessSpec.model_validate({"id": "quiet", "entry": "a", "nodes": {
            "a": {"kind": "llm", "prompt": "hello", "output_key": "a"}}})
        res = Engine(spec, backends={"executor": MockBackend(["hi"])}, store=s).run("t", {})
        assert res.status == "completed"
    finally:
        s.close()
    for argv in (["queue", "--json"], ["graph"], ["tier-policy", "show"], ["runs"], ["certificates"],
                 ["predictions"], ["ledger-verify"]):
        assert cli.main(argv) == 0, argv
    capsys.readouterr()
    with pytest.raises(OSError):  # the trap works: a real attempt would have been recorded
        socket.create_connection(("example.invalid", 443))
    assert no_network == ["create_connection(('example.invalid', 443),)"]


def test_no_telemetry_setting_exists_to_be_on_by_default():
    """Nothing reads a telemetry switch: there is no telemetry to opt out of (opt-in would need new code + a test)."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in SRC.rglob("*.py"))
    assert "TELEMETRY" not in text.upper()
