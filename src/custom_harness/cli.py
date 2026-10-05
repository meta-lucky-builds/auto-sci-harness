"""`chh` command line: run a harness spec, inspect runs and traces."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

from .config import data_dir
from .gateway import Backend, MockBackend
from .runtime import Engine, HarnessSpec
from .sandbox import LocalSandbox
from .store import EvidenceStore


def _backend(kind: str) -> Backend:
    """kind: 'codex[:model]' | 'mock' | 'openai:<model>' | 'ollama:<model>'"""
    name, _, arg = kind.partition(":")
    if name == "codex":
        from .gateway.codex_cli import CodexCLIBackend
        return CodexCLIBackend(model=arg or None)
    if name == "mock":
        return MockBackend(["(mock response)"])
    if name in ("openai", "ollama"):
        from .gateway.openai_compat import OpenAICompatBackend
        from .records import AccessMode
        if name == "openai":
            return OpenAICompatBackend(name="openai-api", base_url="https://api.openai.com/v1", model=arg,
                                       mode=AccessMode.API_KEY, api_key_env="OPENAI_API_KEY",
                                       training_on_outputs="forbidden")
        return OpenAICompatBackend(name="ollama", base_url="http://localhost:11434/v1", model=arg,
                                   mode=AccessMode.OPEN_WEIGHTS, provider="local")
    raise SystemExit(f"unknown backend '{kind}'")


def cmd_run(a: argparse.Namespace, store: EvidenceStore) -> int:
    spec = HarnessSpec.model_validate(yaml.safe_load(Path(a.spec).read_text(encoding="utf-8")))
    backends = {}
    for binding in a.role:
        role, _, kind = binding.partition("=")
        backends[role] = _backend(kind)
    inputs = {}
    for kv in a.input:
        k, _, v = kv.partition("=")
        inputs[k] = v
    import custom_harness.research  # noqa: F401  registers research.* modules
    sandbox = None
    ws = data_dir() / "sandboxes" / f"{spec.id}-{a.task}-{int(time.time())}"
    if a.docker:
        from .sandbox.docker import DockerSandbox
        sandbox = DockerSandbox(ws)
    elif a.dev_sandbox:
        sandbox = LocalSandbox(ws, i_understand_no_isolation=True)
    res = Engine(spec, backends=backends, store=store, sandbox=sandbox).run(a.task, inputs, seed=a.seed)
    print(json.dumps({"run_id": res.run_id, "status": res.status, "detail": res.detail,
                      "steps": res.steps, "cost": res.cost.model_dump()}, indent=1))
    return 0 if res.status == "completed" else 1


def cmd_evolve(a: argparse.Namespace, store: EvidenceStore) -> int:
    import custom_harness.research  # noqa: F401
    from .evolve.driver import evolve, load_suite
    from .evolve.loop import EvolveConfig
    spec = HarnessSpec.model_validate(yaml.safe_load(Path(a.spec).read_text(encoding="utf-8")))
    backends = {}
    for binding in a.role:
        role, _, kind = binding.partition("=")
        backends[role] = _backend(kind)
    proposer = _backend(a.proposer)
    run_tag = f"evolve-{spec.id}-{int(time.time())}"

    def factory(key: str):
        ws = data_dir() / "sandboxes" / run_tag / key
        if a.docker:
            from .sandbox.docker import DockerSandbox
            return DockerSandbox(ws)
        return LocalSandbox(ws, i_understand_no_isolation=True)

    cfg = EvolveConfig(seeds=tuple(a.seeds), n_edits=a.n_edits, race_rounds=a.race_rounds)
    evolve(spec, load_suite(Path(a.suite)), generations=a.generations, backends=backends, proposer=proposer,
           store=store, sandbox_factory=factory, cfg=cfg, out_dir=data_dir() / "evolve" / run_tag)
    print(f"outputs: {data_dir() / 'evolve' / run_tag}")
    return 0


def cmd_runs(a: argparse.Namespace, store: EvidenceStore) -> int:
    for r in store.list_runs(a.limit):
        status, detail = store.run_status(r.id)
        print(f"{r.id}  {status:<16} {r.harness_id:<20} task={r.task_id}  {detail}")
    return 0


def cmd_trace(a: argparse.Namespace, store: EvidenceStore) -> int:
    for s in store.trace(a.run_id):
        out = json.dumps(s.output, default=str)
        print(f"[{s.index}] {s.node_id} ({s.kind}) {s.ended_at - s.started_at:.2f}s "
              f"tok={s.cost.prompt_tokens}+{s.cost.completion_tokens}"
              + (f" ERROR {s.error}" if s.error else ""))
        print("    " + (out if a.full else out[:300]))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chh")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a harness spec (YAML)")
    r.add_argument("spec")
    r.add_argument("--role", action="append", default=[], help="role=backend, e.g. executor=codex")
    r.add_argument("--input", action="append", default=[], help="key=value")
    r.add_argument("--task", default="adhoc")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--dev-sandbox", action="store_true", help="LocalSandbox (NO isolation; dev only)")
    r.add_argument("--docker", action="store_true", help="Docker sandbox (required for agent-written code)")
    ev = sub.add_parser("evolve", help="evolve a harness spec on a task suite (never touches the sealed split)")
    ev.add_argument("spec")
    ev.add_argument("--suite", required=True)
    ev.add_argument("--role", action="append", default=[], help="role=backend for harness runs")
    ev.add_argument("--proposer", required=True, help="backend for the edit proposer")
    ev.add_argument("--generations", type=int, default=1)
    ev.add_argument("--seeds", type=int, nargs="+", default=[0])
    ev.add_argument("--n-edits", type=int, default=3)
    ev.add_argument("--race-rounds", type=int, default=2)
    ev.add_argument("--docker", action="store_true")
    rs = sub.add_parser("runs")
    rs.add_argument("--limit", type=int, default=20)
    t = sub.add_parser("trace")
    t.add_argument("run_id")
    t.add_argument("--full", action="store_true")
    a = p.parse_args(argv)
    store = EvidenceStore(data_dir() / "store")
    try:
        return {"run": cmd_run, "evolve": cmd_evolve, "runs": cmd_runs, "trace": cmd_trace}[a.cmd](a, store)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
