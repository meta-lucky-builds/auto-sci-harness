"""`chh research multi ...`: the L4 multi-agent research mode from the command line (docs/multi_agent_mode.md).

    chh research multi run CONFIG.yaml --docker      open (or resume) the mode and run every remaining round
    chh research multi status CONFIG.yaml            per-agent budgets, lanes, labels, invariant 5, archive counts
    chh research multi archive CONFIG.yaml           inspection listing of the archive (no read is logged)
    chh research multi verify CONFIG.yaml            the mode log's and the archive log's hash chains
    chh research multi revoke CONFIG.yaml ENTRY --reason R --by NAME     HUMAN: revoke an entry and its dependents

CONFIG.yaml: {spec: <harness spec path>, mode: <ModeConfig fields>}; each agent's `roles` maps a role to a backend
kind as for `chh run --role` (codex[:model] | ollama:<model> | openai:<model> | mock). Building backends makes no
model call. Agent code runs only in Docker (`--docker`); `--dev-sandbox` is refused unless the test-mode variable is set
(the same guard as `chh run`).
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ...config import data_dir
from ...kernel import Kernel, SandboxReplayer
from ...policy import PolicyError
from ...runtime import HarnessSpec
from ...store import EvidenceStore
from .mode import ModeConfig, StationError, StationInfraError, StationMode
from .runner import EngineRunner, SpecError


def add_parsers(sub: Any) -> None:
    r = sub.add_parser("research", help="research modes: `chh research multi ...` (L4 multi-agent mode)")
    rs = r.add_subparsers(dest="research_cmd", required=True)
    m = rs.add_parser("multi", help="multi-agent research mode coordinated through a shared archive")
    ms = m.add_subparsers(dest="multi_cmd", required=True)
    run = ms.add_parser("run", help="open (or resume) the mode and run every remaining round")
    run.add_argument("config", type=Path)
    run.add_argument("--docker", action="store_true", help="run agent code in Docker (required for real runs)")
    run.add_argument("--dev-sandbox", action="store_true", help="UNISOLATED local sandbox: trusted test code only")
    for name, help_ in (("status", "per-agent budgets, lanes, labels and invariant 5"),
                        ("archive", "inspection listing of the archive"),
                        ("verify", "verify the mode log and the archive log hash chains")):
        p = ms.add_parser(name, help=help_)
        p.add_argument("config", type=Path)
    rv = ms.add_parser("revoke", help="HUMAN: revoke an archive entry and every entry derived from it")
    rv.add_argument("config", type=Path)
    rv.add_argument("entry")
    rv.add_argument("--reason", required=True)
    rv.add_argument("--by", required=True, help="the human revoking")


def load(config: Path) -> tuple[ModeConfig, HarnessSpec]:
    doc = yaml.safe_load(Path(config).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or set(doc) != {"spec", "mode"}:
        raise StationError("a station config is {spec: <harness spec path>, mode: <mode config>}")
    spec_path = Path(str(doc["spec"]))
    if not spec_path.is_absolute():
        spec_path = (Path(config).parent / spec_path).resolve()
    spec = HarnessSpec.model_validate(yaml.safe_load(spec_path.read_text(encoding="utf-8")))
    return ModeConfig.model_validate(doc["mode"]), spec


def _factory(a: Any) -> Any:
    from ...sandbox import LocalSandbox, Sandbox

    def factory(ws: Path) -> Sandbox:
        if getattr(a, "docker", False):
            from ...sandbox.docker import DockerSandbox
            return DockerSandbox(ws)
        if getattr(a, "dev_sandbox", False):  # the root CLI refuses this flag outside the test mode
            return LocalSandbox(ws, i_understand_no_isolation=True)
        raise StationError("agent code runs only in a sandbox: pass --docker")
    return factory


def _runner(cfg: ModeConfig, spec: HarnessSpec, store: EvidenceStore, a: Any) -> EngineRunner:
    from ...cli import _backend  # local import: the root CLI imports this module
    backends = {ag.id: {role: _backend(kind) for role, kind in sorted(ag.roles.items())} for ag in cfg.agents}
    return EngineRunner(store, spec, backends, sandbox_factory=_factory(a), workdir=data_dir() / "station",
                        allow_same_family=cfg.allow_same_family)


def _kernel(store: EvidenceStore, a: Any) -> Kernel:
    """The archive's gate re-derives every certificate (Kernel.verify), which re-executes data claims (K3): with a
    sandbox chosen the Kernel gets a sandbox replayer, as research.certify's does. Without one (status, archive,
    verify, revoke) it has none: those commands re-derive nothing."""
    if not (getattr(a, "docker", False) or getattr(a, "dev_sandbox", False)):
        return Kernel(store)
    factory = _factory(a)
    root = data_dir() / "station" / "replay"
    return Kernel(store, replayer=SandboxReplayer(lambda key: factory(root / f"{key}-{uuid.uuid4().hex[:8]}")))


def cmd_research(a: Any, store: EvidenceStore) -> int:
    try:
        cfg, spec = load(a.config)
        runner = _runner(cfg, spec, store, a)
        mode = StationMode(store, cfg, runner, spec_hash=runner.spec_hash, kernel=_kernel(store, a))
        if a.multi_cmd == "run":
            if not (getattr(a, "docker", False) or getattr(a, "dev_sandbox", False)):
                raise StationError("agent code runs only in a sandbox: pass --docker")
            t0 = time.time()
            out = mode.run()
            print(json.dumps({**out, "wall_s": round(time.time() - t0, 3)}, indent=1, default=str))
            return 0
        if a.multi_cmd == "status":
            print(json.dumps(mode.status(), indent=1, default=str))
        elif a.multi_cmd == "archive":
            print(json.dumps(mode.archive.entries(), indent=1, default=str))
        elif a.multi_cmd == "verify":
            problems = mode.log.verify() + mode.archive.store.verify_log()
            print(json.dumps({"mode_log": mode.log.kind, "intact": not problems, "problems": problems}, indent=1))
            return 0 if not problems else 1
        elif a.multi_cmd == "revoke":
            if not (a.reason.strip() and a.by.strip()):
                raise StationError("a revocation needs --reason and --by (the human revoking)")
            print(json.dumps({"revoked": mode.archive.revoke(a.entry, a.reason, by=a.by)}, indent=1))
        return 0
    except StationInfraError as e:
        print(json.dumps({"error": "infrastructure", "detail": str(e)}))
        return 3
    except (StationError, SpecError, PolicyError, ValidationError, OSError, yaml.YAMLError) as e:
        print(json.dumps({"error": type(e).__name__, "detail": str(e)[:2000]}))
        return 2


COMMANDS = {"research": cmd_research}

__all__ = ["COMMANDS", "add_parsers", "cmd_research", "load"]
