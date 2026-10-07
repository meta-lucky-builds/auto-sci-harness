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
from .literature import cli as lit_cli
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


def cmd_return_data(a: argparse.Namespace, store: EvidenceStore) -> int:
    """HUMAN action (GOAL §4.7): register the measurements humans returned for an APPROVED physical protocol, with an
    attestation. Refused unless the run is paused at the data gate, the exact protocol was approved by a human
    (engine rule), the protocol passes the prohibited screen, the data match the protocol, and nothing was registered
    for it before. The data become a `future` dataset; the attestation is recorded but remains untested."""
    import hashlib

    from .kernel import DatasetRole, Kernel
    from .research.physical import Protocol, check_measurements, screen
    from .runtime.engine import approval_decision, approval_item_hash, paused_approval
    pause = paused_approval(store, a.run_id)
    if pause is None or pause.get("node_id") != "data_received":
        raise SystemExit(f"run {a.run_id} is not paused at the data gate (data_received)")
    state = pause["state"]
    item = state.get("physical_item")
    if not isinstance(item, dict):
        raise SystemExit("the paused run carries no physical protocol")
    decision = approval_decision(store, a.run_id, "protocol_review", approval_item_hash(item))
    if decision is None or decision["decision"] != "approved":
        raise SystemExit("the protocol was not approved by a human; no data can be registered for it")
    protocol = Protocol.model_validate(item["protocol"])
    if screen(protocol):
        raise SystemExit("the protocol matches a prohibited category")
    if not a.attest.strip():
        raise SystemExit("an attestation (who, where, when) is required")
    data = Path(a.file).read_bytes()
    problems = check_measurements(protocol, data)
    if problems:
        raise SystemExit("returned data refused: " + "; ".join(problems[:10]))
    vault = Kernel(store).vault
    if item["dataset_id"] in vault.datasets():
        raise SystemExit(f"data for this protocol are already registered ({item['dataset_id']})")
    provenance = {"protocol_hash": item["protocol_hash"], "claim_hash": item["claim_hash"], "run_id": a.run_id,
                  "attestation": a.attest, "attestation_sha256": hashlib.sha256(a.attest.encode()).hexdigest(),
                  "data_sha256": hashlib.sha256(data).hexdigest(), "approved_protocol_seq": decision["ledger_seq"]}
    vault.register_dataset(item["dataset_id"], DatasetRole.FUTURE,
                           public={"provenance.json": json.dumps(provenance, sort_keys=True).encode()},
                           private={protocol.measurements.filename: data})
    print(json.dumps({"registered": item["dataset_id"], "data_sha256": provenance["data_sha256"],
                      "next": f"chh approve {a.run_id} data_received --decision approve --approver <you>",
                      "role": f"role 'future' earns CERTIFIED-VALIDATED only after a human runs: chh approve-role "
                              f"{item['dataset_id']} --approver <you>"}))
    return 0


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
    from .kernel.bundle import BundleError, reproduce_bundle
    factory = _sandbox_factory(a, data_dir() / "sandboxes" / f"reproduce-{int(time.time())}")
    try:
        report = reproduce_bundle(Path(a.bundle), lambda: SandboxReplayer(factory))
    except BundleError as e:  # refused (unsupported format, over a read cap): fail closed with the reason
        print(json.dumps({"reproduced": False, "refused": str(e)}, indent=1))
        return 1
    print(json.dumps({"reproduced": report.reproduced, "bundle_hash": report.bundle_hash,
                      "original_label": report.original_label, "reproduced_label": report.reproduced_label,
                      "ledger_check": report.ledger_check, "ledger_head": report.ledger_head,
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
    from .kernel.ledger import check_anchors
    ledger = Ledger(store)
    problems = ledger.verify()
    for path in a.anchor:
        try:
            problems += check_anchors(ledger.entries(), Path(path))
        except OSError as e:
            problems.append(f"anchor file {path} unreadable: {e}")
    print(json.dumps({"entries": len(store.ledger_rows()), "intact": not problems, "anchors_checked": a.anchor,
                      "problems": problems}, indent=1))
    return 0 if not problems else 1


def cmd_ledger_anchor(a: argparse.Namespace, store: EvidenceStore) -> int:
    """Append the ledger's (length, head hash) to an anchor file OUTSIDE the store. Keep copies where whoever controls
    this machine cannot rewrite them (another disk, a git remote, a message to a third party): `chh ledger-verify
    --anchor FILE` then detects a rewrite of anything before the anchor. It does not stop someone who also controls
    every copy of the anchor."""
    from .kernel import Ledger
    from .kernel.ledger import AnchorError, write_anchor
    try:
        anchor = write_anchor(Ledger(store), Path(a.file), data_dir() / "store")
    except AnchorError as e:
        raise SystemExit(f"NOT anchored: {e}") from e
    print(json.dumps({"anchored": str(a.file), **anchor}))
    return 0


def cmd_approve_role(a: argparse.Namespace, store: EvidenceStore) -> int:
    """HUMAN action: approve that a dataset registered as external/future really is external/future data. Without it
    the dataset never supports CERTIFIED-VALIDATED (at most CERTIFIED-INTERNAL). Models and harness code never run
    this; the ledger entry names the approver."""
    from .kernel import Kernel, VaultError
    kernel = Kernel(store)
    try:
        rec = kernel.vault.dataset(a.dataset_id)
        print(f"dataset {rec.dataset_id}: role {rec.role.value}, content hash {rec.hash}, files {sorted(rec.files)}")
        entry = kernel.approve_role(a.dataset_id, approver=a.approver, note=a.note)
    except VaultError as e:
        raise SystemExit(f"NOT approved: {e}") from e
    print(json.dumps({"approved_role": rec.role.value, "dataset": a.dataset_id, "approver": a.approver,
                      "ledger_seq": entry.seq}))
    return 0


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


def cmd_approve_adapter(a: argparse.Namespace, store: EvidenceStore) -> int:
    """HUMAN action (GOAL §4.7, invariant 13): approve a domain adapter's current content after its validation passes.
    The approval names the approver and is recorded in the hash-chained ledger."""
    from .kernel import AdapterError, Kernel
    from .kernel.adapters import REGISTRY, adapter_hash
    if a.name not in REGISTRY:
        raise SystemExit(f"unknown adapter {a.name!r}; known: {sorted(REGISTRY)}")
    spec, checks = REGISTRY[a.name]
    print(f"adapter {spec.name} v{spec.version}: classes {sorted(spec.classes)}, modules {list(spec.modules)}")
    print(f"content hash {adapter_hash(spec)}; running {len(checks)} validation checks")
    try:
        entry = Kernel(store).approve_adapter(a.name, approver=a.approver)
    except AdapterError as e:
        raise SystemExit(f"NOT approved: {e}") from e
    print(json.dumps({"approved": a.name, "approver": a.approver, "ledger_seq": entry.seq}))
    return 0


def cmd_finalize(a: argparse.Namespace, store: EvidenceStore) -> int:
    """HUMAN action (GOAL §5 L6.12, invariant 6): evaluate the frozen final harness on the sealed split, ONCE. The
    decision (approver, spec hash, sealed tasks) is recorded in the hash-chained ledger before any sealed run; a
    second finalize of any of these sealed tasks is refused. Claude and models never run this command."""
    import custom_harness.research  # noqa: F401

    from .evolve.driver import load_suite
    from .evolve.finalize import FinalizeRefused, finalize
    if not a.approver.strip():
        raise SystemExit("refused: --approver must name the human taking this one-shot decision")
    spec = HarnessSpec.model_validate(yaml.safe_load(Path(a.spec).read_text(encoding="utf-8")))
    baseline = (HarnessSpec.model_validate(yaml.safe_load(Path(a.baseline).read_text(encoding="utf-8")))
                if a.baseline else None)
    backends = {}
    for binding in a.role:
        role, _, kind = binding.partition("=")
        backends[role] = _backend(kind)
    if a.allow_same_family:
        os.environ["CHH_ALLOW_SAME_FAMILY"] = "1"
    tag = f"finalize-{spec.id}-{int(time.time())}"
    try:
        summary = finalize(spec, load_suite(Path(a.suite)), approver=a.approver, backends=backends, store=store,
                           sandbox_factory=_sandbox_factory(a, data_dir() / "sandboxes" / tag),
                           seeds=tuple(a.seeds), baseline=baseline)
    except FinalizeRefused as e:
        raise SystemExit(f"refused: {e}") from e
    print(json.dumps(summary, indent=1, default=str))
    return 0


def cmd_memory(a: argparse.Namespace, store: EvidenceStore) -> int:
    """L5 memory (GOAL §5 L5). Inspection, retraction sync, Kernel-derived negative memory and HUMAN revocation.
    No command writes free text into memory: every write comes from Kernel certificates through the gate."""
    from .kernel import Kernel
    from .memory import MemoryStore, NotReadable, harvest_negatives
    mem = MemoryStore(Kernel(store), namespace=a.namespace)
    try:
        if a.action == "verify-log":
            problems = mem.verify_log()
            print(json.dumps({"namespace": a.namespace, "records": len(mem.log()), "intact": not problems,
                              "problems": problems}, indent=1))
            return 0 if not problems else 1
        if a.action == "sync":
            print(json.dumps({"cascades": mem.sync()}, indent=1))
        elif a.action == "harvest-negatives":
            res = harvest_negatives(mem)
            print(json.dumps([{"entry_id": r.entry_id, "passed": r.passed, "status": r.status.value,
                               "reasons": list(r.reasons)} for r in res], indent=1))
        elif a.action == "list":
            mem.sync()
            for e in mem.log():
                if e["type"] == "propose":
                    eid = e["entry_id"]
                    tier = mem.tier(eid)
                    print(f"{eid[:20]}  {mem.status(eid).value:<11} {(tier.name.split('_')[0] if tier else '-'):<3} "
                          f"{e['entry']['proposal']['kind']:<9} {e['entry']['proposal']['title'][:60]}")
        elif a.action == "unused":
            print(json.dumps(mem.unused(), indent=1))
        elif a.action == "revoke":
            if not (a.entry and a.reason and a.by):
                raise SystemExit("revoke needs ENTRY, --reason and --by (the human revoking)")
            print(json.dumps({"revoked": mem.revoke(a.entry, a.reason, by=a.by)}, indent=1))
    except NotReadable as e:
        raise SystemExit(f"memory not readable: {e}") from e
    return 0


def cmd_approve_statement(a: argparse.Namespace, store: EvidenceStore) -> int:
    """HUMAN action (GOAL §4.7, invariant 15): approve a Lean theorem statement (a chh-lean-statement/1 JSON file).
    Prints exactly what is approved, registers it as an artifact and records the approval, bound to its hash. This
    is the ONLY code path that writes a `statement_approval` ledger entry; a formal claim's K1 requires one."""
    import hashlib

    from .kernel import Kernel
    from .kernel.formal import FormalError, hygiene, parse_statement
    from .kernel.vault import VaultError
    data = Path(a.file).read_bytes()
    try:
        st = parse_statement(data)
    except FormalError as e:
        raise SystemExit(f"refused: {e}") from e
    problems = hygiene(st.source, statement=True)
    if problems:
        raise SystemExit("refused: " + "; ".join(problems))
    approver = a.approver.strip()
    if not approver:
        raise SystemExit("refused: --approver must name a human")
    digest = hashlib.sha256(data).hexdigest()
    print(f"theorem {st.theorem}\n--- statement source ---\n{st.source}\n--- end ---\nstatement_hash: {digest}")
    if a.statement_hash is not None and a.statement_hash != digest:
        raise SystemExit(f"refused: --statement-hash {a.statement_hash} does not match the file's hash {digest}")
    kernel = Kernel(store)
    try:
        kernel.vault.register_artifact(a.artifact_id, data, "lean-statement")
    except VaultError as e:
        raise SystemExit(f"refused: {e}") from e
    entry = kernel.ledger.append("statement_approval", {"artifact_id": a.artifact_id, "hash": digest,
                                                        "theorem": st.theorem, "approver": approver,
                                                        "note": a.note or ""})
    print(json.dumps({"approved_statement": a.artifact_id, "approver": approver, "ledger_seq": entry.seq}))
    return 0


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
    rd = sub.add_parser("return-data", help="HUMAN: register measurements returned for an approved physical protocol")
    rd.add_argument("run_id")
    rd.add_argument("--file", required=True)
    rd.add_argument("--attest", required=True, help="who measured it, where, when (recorded; untested)")
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
    aa = sub.add_parser("approve-adapter", help="HUMAN: approve a domain adapter after its validation passes")
    aa.add_argument("name")
    aa.add_argument("--approver", required=True, help="the human approving (recorded in the ledger)")
    ar = sub.add_parser("approve-role", help="HUMAN: approve a dataset's external/future role (needed for VALIDATED)")
    ar.add_argument("dataset_id")
    ar.add_argument("--approver", required=True, help="the human approving (recorded in the ledger)")
    ar.add_argument("--note", default="", help="why this data is external/future (provenance)")
    fz = sub.add_parser("finalize", help="HUMAN: one-shot evaluation of the frozen final harness on the sealed split")
    fz.add_argument("spec")
    fz.add_argument("--suite", required=True)
    fz.add_argument("--approver", required=True, help="the human taking this one-shot decision (ledgered)")
    fz.add_argument("--baseline", help="spec to compare against on the same sealed units (paired)")
    fz.add_argument("--seeds", type=int, nargs="+", default=[0])
    fz.add_argument("--role", action="append", default=[], help="role=backend for harness runs")
    fz.add_argument("--allow-same-family", action="store_true",
                    help="override invariant 5 (judge/replicator family); recorded as NOT MET")
    fz.add_argument("--docker", action="store_true")
    fz.add_argument("--dev-sandbox", action="store_true", help="LocalSandbox (NO isolation; trusted code only)")
    asx = sub.add_parser("approve-statement", help="HUMAN: approve a Lean theorem statement (formal claims, T0)")
    asx.add_argument("file", help="a chh-lean-statement/1 JSON file")
    asx.add_argument("--artifact-id", required=True, help="the artifact id formal claims will name as their subject")
    asx.add_argument("--approver", required=True, help="the human approving (recorded in the ledger)")
    asx.add_argument("--statement-hash", default=None,
                     help="full sha256 of the file you reviewed; refused if it differs")
    asx.add_argument("--note", default="")
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
    lv = sub.add_parser("ledger-verify", help="verify the Kernel ledger hash chain (and external anchors)")
    lv.add_argument("--anchor", action="append", default=[], help="anchor file written by ledger-anchor (repeatable)")
    la = sub.add_parser("ledger-anchor", help="append the ledger head + length to an anchor file outside the store")
    la.add_argument("file")
    sub.add_parser("predictions", help="prediction ledger: VDS and calibration")
    mm = sub.add_parser("memory", help="L5 memory: list, unused, verify-log, sync, harvest-negatives, HUMAN revoke")
    mm.add_argument("action", choices=["list", "unused", "verify-log", "sync", "harvest-negatives", "revoke"])
    mm.add_argument("entry", nargs="?")
    mm.add_argument("--namespace", default="global")
    mm.add_argument("--reason")
    mm.add_argument("--by", help="the human revoking (recorded in the memory log)")
    rs = sub.add_parser("runs")
    rs.add_argument("--limit", type=int, default=20)
    t = sub.add_parser("trace")
    t.add_argument("run_id")
    t.add_argument("--full", action="store_true")
    lit_cli.add_parsers(sub)
    a = p.parse_args(argv)
    if getattr(a, "dev_sandbox", False):  # no production path may select the unisolated sandbox
        from .sandbox.local import TEST_MODE_ENV
        if os.environ.get(TEST_MODE_ENV) != "1":
            raise SystemExit(f"--dev-sandbox runs programs on the host WITHOUT isolation; it is refused unless "
                             f"{TEST_MODE_ENV}=1 (trusted test code only). Use --docker.")
    store = EvidenceStore(data_dir() / "store")
    try:
        return {"run": cmd_run, "resume": cmd_resume, "approve": cmd_approve, "return-data": cmd_return_data,
                "rerun": cmd_rerun,
                "approve-adapter": cmd_approve_adapter, "approve-role": cmd_approve_role,
                "approve-statement": cmd_approve_statement, "ledger-anchor": cmd_ledger_anchor,
                "finalize": cmd_finalize, "evolve": cmd_evolve, "runs": cmd_runs,
                "trace": cmd_trace, "reproduce": cmd_reproduce, "bundle": cmd_bundle,
                "certificates": cmd_certificates, "ledger-verify": cmd_ledger_verify,
                "predictions": cmd_predictions, "build-suite": cmd_build_suite, "memory": cmd_memory,
                **lit_cli.COMMANDS}[a.cmd](a, store)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
