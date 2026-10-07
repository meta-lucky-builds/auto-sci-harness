"""`chh` command line: run / resume a harness spec, evolve it, inspect runs and traces."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml

from .config import data_dir
from .gateway import Backend, MockBackend
from .runtime import Engine, HarnessSpec
from .sandbox import LocalSandbox, Sandbox
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
    sandbox: Sandbox | None = None
    ws = data_dir() / "sandboxes" / f"{spec.id}-{a.task}-{int(time.time())}"
    if a.docker:
        from .sandbox.docker import DockerSandbox
        sandbox = DockerSandbox(ws)
    elif a.dev_sandbox:
        sandbox = LocalSandbox(ws, i_understand_no_isolation=True)
    res = Engine(spec, backends=backends, store=store, sandbox=sandbox,
                 allow_same_family=a.allow_same_family).run(a.task, inputs, seed=a.seed)
    print(json.dumps({"run_id": res.run_id, "status": res.status, "detail": res.detail,
                      "steps": res.steps, "cost": res.cost.model_dump()}, indent=1))
    return 0 if res.status == "completed" else 1


def cmd_resume(a: argparse.Namespace, store: EvidenceStore) -> int:
    """Resume an interrupted run with the exact spec it started with (loaded from the store)."""
    import custom_harness.research  # noqa: F401
    run = store.get_run(a.run_id)
    spec = HarnessSpec.model_validate(store.harness_spec(run.harness_hash))
    backends = {role: _backend(kind) for role, _, kind in (b.partition("=") for b in a.role)}
    sandbox: Sandbox | None = None
    if run.workspace is not None:
        if a.docker:
            from .sandbox.docker import DockerSandbox
            sandbox = DockerSandbox(Path(run.workspace))
        elif a.dev_sandbox:
            sandbox = LocalSandbox(Path(run.workspace), i_understand_no_isolation=True)
        else:
            raise SystemExit("this run used a sandbox: pass --docker (or --dev-sandbox for trusted test code)")
    res = Engine(spec, backends=backends, store=store, sandbox=sandbox,
                 allow_same_family=a.allow_same_family).resume(a.run_id, force=a.force)
    print(json.dumps({"run_id": res.run_id, "status": res.status, "detail": res.detail,
                      "steps": res.steps, "cost": res.cost.model_dump()}, indent=1))
    return 0 if res.status == "completed" else 1


APPROVAL_ITEM_PRINT_CHARS = 20_000


def cmd_approve(a: argparse.Namespace, store: EvidenceStore) -> int:
    """Record a human decision for the item a PAUSED run is waiting on (GOAL invariant 15). This is the ONLY code
    path that writes an "approval" ledger entry. It does not resume the run: `chh resume RUN_ID` does."""
    from .kernel import Ledger
    from .records import RunStatus
    from .runtime import ApprovalNode
    from .runtime.engine import approval_item_hash, paused_approval
    from .runtime.template import lookup
    status, _ = store.run_status(a.run_id)
    if status != RunStatus.PAUSED:
        raise SystemExit(f"refused: run {a.run_id} is {status.value}, not paused")
    pause = paused_approval(store, a.run_id)
    if pause is None or pause["node_id"] != a.node_id:
        at = pause["node_id"] if pause else "?"
        raise SystemExit(f"refused: run {a.run_id} is not paused at node '{a.node_id}' (paused at '{at}')")
    run = store.get_run(a.run_id)
    node = HarnessSpec.model_validate(store.harness_spec(run.harness_hash)).nodes.get(a.node_id)
    if not isinstance(node, ApprovalNode):
        raise SystemExit(f"refused: node '{a.node_id}' is not an approval node")
    approver = a.approver.strip()
    if not approver:
        raise SystemExit("refused: --approver must name a human")
    item = lookup(pause["state"], node.item_key)  # recomputed from the paused checkpoint, never trusted from the trace
    item_hash = approval_item_hash(item)
    if item_hash != pause.get("item_hash"):
        raise SystemExit(f"refused: the checkpointed item hashes to {item_hash}, the pause recorded "
                         f"{pause.get('item_hash')}; store inconsistent")
    text = json.dumps(item, indent=1, ensure_ascii=False, sort_keys=True)
    if len(text) > APPROVAL_ITEM_PRINT_CHARS:
        text = text[:APPROVAL_ITEM_PRINT_CHARS] + f"\n...[{len(text) - APPROVAL_ITEM_PRINT_CHARS} chars not shown]"
    print(f"run {a.run_id} node {a.node_id}: {node.prompt}")
    print(f"item ({node.item_key}):\n{text}")
    print(f"item_hash: {item_hash}")
    if a.item_hash is not None and a.item_hash != item_hash:
        raise SystemExit(f"refused: --item-hash {a.item_hash} does not match the item hash {item_hash}")
    decision = {"approve": "approved", "reject": "rejected"}[a.decision]
    entry = Ledger(store).append("approval", {"run_id": a.run_id, "node_id": a.node_id, "item_hash": item_hash,
                                              "decision": decision, "approver": approver, "note": a.note or ""})
    print(json.dumps({"recorded": decision, "approver": approver, "ledger_seq": entry.seq,
                      "next": f"chh resume {a.run_id}"}))
    return 0


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
    if a.allow_same_family:  # evaluation engines read it; recorded as NOT MET in every run
        os.environ["CHH_ALLOW_SAME_FAMILY"] = "1"
    run_tag = f"evolve-{spec.id}-{int(time.time())}"

    def factory(key: str):
        ws = data_dir() / "sandboxes" / run_tag / key
        if a.docker:
            from .sandbox.docker import DockerSandbox
            return DockerSandbox(ws)
        if a.dev_sandbox:
            return LocalSandbox(ws, i_understand_no_isolation=True)
        raise SystemExit("pass --docker (or --dev-sandbox for trusted test code only)")

    cfg = EvolveConfig(seeds=tuple(a.seeds), n_edits=a.n_edits, race_rounds=a.race_rounds)
    from .budget import persistent_budget
    limits = {"usd": a.max_usd, "tokens": a.max_tokens, "quota_units": a.max_quota, "wall_s": a.max_wall_s}
    campaign = persistent_budget(store, a.campaign_id or run_tag, {k: v for k, v in limits.items() if v is not None})
    evolve(spec, load_suite(Path(a.suite)), generations=a.generations, backends=backends, proposer=proposer,
           store=store, sandbox_factory=factory, cfg=cfg, out_dir=data_dir() / "evolve" / run_tag,
           campaign_budget=campaign)
    print(f"outputs: {data_dir() / 'evolve' / run_tag}")
    return 0


def _sandbox_factory(a: argparse.Namespace, root: Path):
    if a.docker:
        from .sandbox.docker import DockerSandbox
        return lambda key: DockerSandbox(root / key)
    if a.dev_sandbox:
        return lambda key: LocalSandbox(root / key, i_understand_no_isolation=True)
    raise SystemExit("pass --docker (or --dev-sandbox for trusted code only)")


def cmd_reproduce(a: argparse.Namespace, store: EvidenceStore) -> int:
    """Re-check a Reproducibility Bundle from its bytes alone (GOAL L2 done-when)."""
    from .kernel import SandboxReplayer
    from .kernel.bundle import reproduce_bundle
    factory = _sandbox_factory(a, data_dir() / "sandboxes" / f"reproduce-{int(time.time())}")
    report = reproduce_bundle(Path(a.bundle), lambda: SandboxReplayer(factory))
    print(json.dumps({"reproduced": report.reproduced, "bundle_hash": report.bundle_hash,
                      "original_label": report.original_label, "reproduced_label": report.reproduced_label,
                      "problems": report.problems}, indent=1))
    return 0 if report.reproduced else 1


def cmd_bundle(a: argparse.Namespace, store: EvidenceStore) -> int:
    from .kernel import Kernel
    from .kernel.bundle import build_bundle
    out = Path(a.out or (data_dir() / "bundles" / f"{a.cert_hash[:16]}.zip"))
    print(json.dumps({"bundle": str(out), "bundle_hash": build_bundle(Kernel(store), a.cert_hash, out)}))
    return 0


def cmd_certificates(a: argparse.Namespace, store: EvidenceStore) -> int:
    from .kernel import Kernel
    k = Kernel(store)
    for e in k.ledger.entries("certificate"):
        cert = k.certificate(e.body["cert_hash"])
        print(f"{e.body['cert_hash'][:16]}  {cert.label.value:<20} {k.status(e.body['cert_hash']):<9} "
              f"failed={cert.failed() or '-'}  {cert.claim['statement'][:60]}")
    return 0


def cmd_ledger_verify(a: argparse.Namespace, store: EvidenceStore) -> int:
    from .kernel import Ledger
    problems = Ledger(store).verify()
    print(json.dumps({"entries": len(store.ledger_rows()), "intact": not problems, "problems": problems}, indent=1))
    return 0 if not problems else 1


def cmd_predictions(a: argparse.Namespace, store: EvidenceStore) -> int:
    from .kernel import Ledger
    from .kernel.predictions import calibration, score_predictions, vds
    scores = score_predictions(Ledger(store), time.time())
    print(json.dumps({"predictions": len(scores), "vds_bits": vds(scores), "calibration": calibration(scores)},
                     indent=1))
    return 0


def cmd_build_suite(a: argparse.Namespace, store: EvidenceStore) -> int:
    """Write the M2 task suite (synthetic families on the host; real + repair tasks inside the sandbox)."""
    from .research.suite import build_suite, export_real
    real = None
    if a.docker:
        from .sandbox.docker import DockerSandbox
        real = export_real(lambda ws: DockerSandbox(ws), Path(a.out) / "_export")
    path = build_suite(Path(a.out), real_data=real)
    print(json.dumps({"suite": str(path), "real_data": real is not None}))
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


def cmd_rerun(a: argparse.Namespace, store: EvidenceStore) -> int:
    """Re-run a per-run Reproducibility Bundle (invariant 9): same spec, inputs and seed; refused if the task files on
    disk no longer match the hashes recorded in the bundle."""
    import custom_harness.research  # noqa: F401

    from .store.run_bundle import check_task_files, read_run_bundle
    m = read_run_bundle(Path(a.bundle))
    spec = HarnessSpec.model_validate(m["spec"])
    inputs = dict((m.get("inputs") or {}).get("input") or {})
    if a.task_dir:
        inputs["task_dir"] = a.task_dir
    if inputs.get("task_dir"):
        diff = check_task_files(m, inputs["task_dir"])
        if diff:
            raise SystemExit("task files differ from the bundle: " + "; ".join(diff[:10]))
    backends = {role: _backend(kind) for role, _, kind in (b.partition("=") for b in a.role)}
    ws = data_dir() / "sandboxes" / f"rerun-{m['run']['id']}-{int(time.time())}"
    sandbox: Sandbox | None = None
    if a.docker:
        from .sandbox.docker import DockerSandbox
        sandbox = DockerSandbox(ws)
    elif a.dev_sandbox:
        sandbox = LocalSandbox(ws, i_understand_no_isolation=True)
    res = Engine(spec, backends=backends, store=store, sandbox=sandbox,
                 allow_same_family=a.allow_same_family).run(m["run"]["task_id"], inputs, seed=int(m["seed"]))
    print(json.dumps({"rerun_of": m["run"]["id"], "run_id": res.run_id, "status": res.status,
                      "detail": res.detail}, indent=1))
    return 0 if res.status == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chh")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a harness spec (YAML)")
    r.add_argument("spec")
    r.add_argument("--role", action="append", default=[], help="role=backend, e.g. executor=codex")
    r.add_argument("--allow-same-family", action="store_true",
                   help="override invariant 5 (judge/replicator family); recorded as NOT MET")
    r.add_argument("--input", action="append", default=[], help="key=value")
    r.add_argument("--task", default="adhoc")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--dev-sandbox", action="store_true", help="LocalSandbox (NO isolation; dev only)")
    r.add_argument("--docker", action="store_true", help="Docker sandbox (required for agent-written code)")
    ev = sub.add_parser("evolve", help="evolve a harness spec on a task suite (never touches the sealed split)")
    ev.add_argument("spec")
    ev.add_argument("--suite", required=True)
    ev.add_argument("--role", action="append", default=[], help="role=backend for harness runs")
    ev.add_argument("--allow-same-family", action="store_true",
                   help="override invariant 5 (judge/replicator family); recorded as NOT MET")
    ev.add_argument("--proposer", required=True, help="backend for the edit proposer")
    ev.add_argument("--generations", type=int, default=1)
    ev.add_argument("--seeds", type=int, nargs="+", default=[0])
    ev.add_argument("--n-edits", type=int, default=3)
    ev.add_argument("--race-rounds", type=int, default=2)
    ev.add_argument("--docker", action="store_true")
    ev.add_argument("--dev-sandbox", action="store_true", help="LocalSandbox (NO isolation; dev only)")
    ev.add_argument("--campaign-id", help="persisted campaign budget id (reuse it to continue a campaign)")
    ev.add_argument("--max-usd", type=float)
    ev.add_argument("--max-tokens", type=float)
    ev.add_argument("--max-quota", type=float, help="subscription quota units (one per BYOS call)")
    ev.add_argument("--max-wall-s", type=float)
    rr = sub.add_parser("resume", help="resume an interrupted run (process died, engine crash, infra failure) "
                                         "or a run paused at an approval node")
    rr.add_argument("run_id")
    rr.add_argument("--role", action="append", default=[], help="role=backend, e.g. executor=codex")
    rr.add_argument("--allow-same-family", action="store_true",
                   help="override invariant 5 (judge/replicator family); recorded as NOT MET")
    rr.add_argument("--docker", action="store_true")
    rr.add_argument("--dev-sandbox", action="store_true")
    rr.add_argument("--force", action="store_true", help="resume even if the owning process looks alive")
    ap = sub.add_parser("approve", help="record a human approve/reject for a run paused at an approval node")
    ap.add_argument("run_id")
    ap.add_argument("node_id")
    ap.add_argument("--decision", required=True, choices=["approve", "reject"])
    ap.add_argument("--approver", required=True, help="the human taking this decision")
    ap.add_argument("--note", default="")
    ap.add_argument("--item-hash", default=None, help="full sha256 of the item you reviewed; refused if it differs")
    rb = sub.add_parser("rerun", help="re-run a per-run Reproducibility Bundle (same spec, inputs, seed)")
    rb.add_argument("bundle")
    rb.add_argument("--role", action="append", default=[], help="role=backend, e.g. executor=codex")
    rb.add_argument("--allow-same-family", action="store_true",
                    help="override invariant 5 (judge/replicator family); recorded as NOT MET")
    rb.add_argument("--task-dir", default=None, help="where the task lives on this machine (hashes are checked)")
    rb.add_argument("--docker", action="store_true")
    rb.add_argument("--dev-sandbox", action="store_true")
    rp = sub.add_parser("reproduce", help="re-check a Reproducibility Bundle on this machine")
    rp.add_argument("bundle")
    rp.add_argument("--docker", action="store_true")
    rp.add_argument("--dev-sandbox", action="store_true", help="LocalSandbox (NO isolation; trusted code only)")
    bd = sub.add_parser("bundle", help="write the Reproducibility Bundle of a certificate")
    bd.add_argument("cert_hash")
    bd.add_argument("-o", "--out")
    bs = sub.add_parser("build-suite", help="write the ML task suite (>= 20 tasks with --docker)")
    bs.add_argument("out")
    bs.add_argument("--docker", action="store_true", help="also build real-data and repair tasks in the sandbox")
    sub.add_parser("certificates", help="list Kernel certificates")
    sub.add_parser("ledger-verify", help="verify the Kernel ledger hash chain")
    sub.add_parser("predictions", help="prediction ledger: VDS and calibration")
    rs = sub.add_parser("runs")
    rs.add_argument("--limit", type=int, default=20)
    t = sub.add_parser("trace")
    t.add_argument("run_id")
    t.add_argument("--full", action="store_true")
    a = p.parse_args(argv)
    store = EvidenceStore(data_dir() / "store")
    try:
        return {"run": cmd_run, "resume": cmd_resume, "approve": cmd_approve, "rerun": cmd_rerun,
                "evolve": cmd_evolve, "runs": cmd_runs,
                "trace": cmd_trace, "reproduce": cmd_reproduce, "bundle": cmd_bundle,
                "certificates": cmd_certificates, "ledger-verify": cmd_ledger_verify,
                "predictions": cmd_predictions, "build-suite": cmd_build_suite}[a.cmd](a, store)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
