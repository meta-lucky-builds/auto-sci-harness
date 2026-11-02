"""The Lab network rule (GOAL 18.6, UPGRADES N5 and the recording part of N6): internet allowed, LAN and host blocked,
web traffic recorded into the discovery trace. docs/lab.md section 11 states what this does and does not cover.

Each session gets its own Docker network, created `--internal`: it has no default route, no DNS for outside names and
no route to the host (checked with a real probe, stage-p0 files/s2_docker_probe.txt). The box has one interface, on that
network. The only other member is the session's gateway container, which also sits on Docker's default bridge and runs
`egress_proxy.py`. Everything in the box that wants the internet goes through the proxy; a program that ignores the
proxy settings reaches nothing.

The network is checked before the box starts: `Network.up` reads `Internal` back from Docker and refuses a network
that is not internal (a name someone created earlier), and walls.docker_argv_problems refuses a box argv on any other
network. The gateway runs read-only, with no capabilities, as uid 1000, and it has no mount: nothing in the box can
reach its files. Its stdout is the record, read by the host with `docker logs`, so the box cannot write to it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..store import EvidenceStore
from . import egress_proxy
from . import trace as T

GATEWAY_IMAGE = "custom-harness-sandbox:py312"  # python and the standard library are all the gateway needs
HOST_NAME = "host.docker.internal"
MAX_LOG_BYTES = "64m"  # UNCALIBRATED (owner decision, GOAL 13.8): one gateway log file; a lost line shows as a gap
MAX_LOG_FILES = "4"  # UNCALIBRATED (owner decision, GOAL 13.8)


def network_name(session_id: str) -> str:
    return f"chh-lab-net-{session_id}"


def gateway_name(session_id: str) -> str:
    return f"chh-lab-gw-{session_id}"


def proxy_env(session_id: str) -> dict[str, str]:
    """The proxy settings of every process in the box. Convenience, not enforcement: see the module docstring."""
    url = f"http://{gateway_name(session_id)}:{egress_proxy.PORT}"
    return {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url}


def gateway_source() -> str:
    return Path(egress_proxy.__file__).read_text(encoding="utf-8")


def gateway_sha256() -> str:
    return hashlib.sha256(gateway_source().encode("utf-8")).hexdigest()


def gateway_argv(exe: str, session_id: str, image: str, allow: tuple[str, ...]) -> list[str]:
    a = [exe, "run", "-d", "--init", "--name", gateway_name(session_id), "--hostname", "lab-gateway",
         "--label", "org.custom-harness.lab.gateway=EXPLORATORY", "--label",
         f"org.custom-harness.lab.session={session_id}", "--network", "bridge", "--read-only", "--cap-drop", "ALL",
         "--security-opt", "no-new-privileges", "--user", "1000:1000", "--memory", "256m", "--pids-limit", "512",
         "--log-opt", f"max-size={MAX_LOG_BYTES}", "--log-opt", f"max-file={MAX_LOG_FILES}"]
    if allow:
        a += ["--add-host", f"{HOST_NAME}:host-gateway", "-e", f"{egress_proxy.ALLOW_ENV}={','.join(allow)}"]
    return [*a, image, "python", "-I", "-c", gateway_source()]


class Network:
    """The session's internal network and gateway. `docker` is a lab.box.DockerCLI."""

    def __init__(self, session_id: str, docker: Any):
        self.session_id = session_id
        self.docker = docker
        self.name = network_name(session_id)
        self.gateway = gateway_name(session_id)

    def _must(self, args: list[str], what: str, timeout: float = 120) -> str:
        r = self.docker.call(args, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"{what} failed (rc={r.returncode}): {r.stderr[-500:]}")
        return str(r.stdout)

    def is_internal(self) -> bool:
        r = self.docker.call(["network", "inspect", "--format", "{{.Internal}}", self.name], timeout=60)
        return r.returncode == 0 and r.stdout.strip() == "true"

    def up(self, image: str, allow: tuple[str, ...]) -> None:
        self._must(["network", "create", "--internal", "--driver", "bridge", "--label",
                    f"org.custom-harness.lab.session={self.session_id}", self.name], "creating the Lab network")
        if not self.is_internal():
            raise RuntimeError(f"Docker network {self.name} is not internal: a box on it would reach the LAN")
        self._must(gateway_argv(self.docker.exe, self.session_id, image, allow)[1:], "starting the Lab gateway", 300)
        self._must(["network", "connect", self.name, self.gateway], "connecting the Lab gateway")

    def down(self) -> None:
        self.docker.call(["rm", "-f", self.gateway], timeout=120)
        self.docker.call(["network", "rm", self.name], timeout=120)

    def log_lines(self) -> list[str] | None:
        r = self.docker.call(["logs", self.gateway], timeout=300)
        return None if r.returncode != 0 else r.stdout.splitlines()


def collect_web(store: EvidenceStore, session_id: str, docker: Any) -> dict[str, Any]:
    """Bring the session's web traffic into the discovery trace (`web` events, by the manager, never agent-reported).
    Only lines newer than the last recorded one are taken. A sequence number that was skipped (a rotated-out log) and a
    gateway whose log cannot be read are both recorded: the record says when it is incomplete."""
    rows = T.events(store, session_id, "web")
    last = max((int(r["payload"].get("seq") or 0) for r in rows), default=0)
    lines = Network(session_id, docker).log_lines()
    if lines is None:
        T.record(store, session_id, "web", {"op": "log_unavailable"}, actor=T.system())
        return {"web_events": 0, "log": "unavailable"}
    n = 0
    for line in lines[:]:
        try:
            doc = json.loads(line)
            seq = doc["seq"]
        except (ValueError, KeyError, TypeError):
            continue  # not a record line (an interpreter warning): nothing from the box can reach this stream
        if not isinstance(seq, int) or seq <= last or n >= T.MAX_INGEST:
            continue
        if seq > last + 1:
            T.record(store, session_id, "web", {"op": "gap", "seq": seq - 1, "missing": seq - last - 1},
                     actor=T.system())
        last = seq
        _record_line(store, session_id, doc)
        n += 1
    return {"web_events": n, "log": "read"}


def _record_line(store: EvidenceStore, session_id: str, doc: dict[str, Any]) -> None:
    payload = {k: doc.get(k) for k in ("seq", "method", "host", "port", "ip", "url", "query_sha256", "status", "reason",
                                       "up", "down", "response_sha256", "at", "ended") if doc.get(k) is not None}
    try:
        T.record(store, session_id, "web", {"op": "connection", **payload}, actor=T.system())
    except T.TraceRefused:  # a name or path that looks like a key (a box can choose both): they go by hash
        shown = f"{doc.get('host')} {doc.get('url')}"
        kept = {k: v for k, v in payload.items() if k not in ("url", "host", "reason")}
        T.record(store, session_id, "web", {"op": "connection", **kept,
                                            "url_sha256": hashlib.sha256(shown.encode()).hexdigest()},
                 actor=T.system())
