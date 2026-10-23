"""`chh` command line: run / resume a harness spec, evolve it, inspect runs and traces."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .audit import cli as audit_cli
from .config import data_dir
from .gateway import Backend, MockBackend
from .human import cli as human_cli
from .kerneld.hoststore import open_host_store
from .literature import cli as lit_cli
from .records import Cost
from .runtime import Engine, HarnessSpec
from .sandbox import LocalSandbox, Sandbox
from .scorecard import cli as scorecard_cli
from .store import EvidenceStore
from .studio import cli as studio_cli
from .timesplit import cli as timesplit_cli
from .verify import provenance_cli


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


MAP_WORKERS_HELP = ("opt-in: run the model calls of a map whose body is an LLM node on up to N threads (default 1 = "
                    "sequential); results are committed in item order, so the committed outputs equal the sequential "
                    "run's; on an error or budget stop up to N-1 started branches are also charged "
                    "(runtime/engine.py _ParallelBranches)")


def _cost_out(store: EvidenceStore, run_id: str, cost: Cost) -> dict[str, Any]:
    """A run's cost as the CLI reports it (invariant 9): the recorded units, with usd taken from the cost-table record
    (gateway/costs.py), so a usd the table cannot price is printed as null with its status, never as 0."""
    from .gateway.costs import run_cost_record
    rec = run_cost_record(store, run_id)
    return {"cost": {**cost.model_dump(), "usd": rec["usd"], "usd_status": rec["usd_status"]}, "cost_record": rec}


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
    from .compute import fresh_dir  # a NEW directory per run (GOAL L0): never one another run already used
    ws = fresh_dir(data_dir() / "sandboxes", f"{spec.id}-{a.task}-{int(time.time())}")
    if a.docker:
        from .sandbox.docker import DockerSandbox
        sandbox = DockerSandbox(ws)
    elif a.dev_sandbox:
        sandbox = LocalSandbox(ws, i_understand_no_isolation=True)
    res = Engine(spec, backends=backends, store=store, sandbox=sandbox, allow_same_family=a.allow_same_family,
                 map_workers=a.map_workers).run(a.task, inputs, seed=a.seed)
    print(json.dumps({"run_id": res.run_id, "status": res.status, "detail": res.detail,
                      "steps": res.steps, **_cost_out(store, res.run_id, res.cost)}, indent=1))
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
    res = Engine(spec, backends=backends, store=store, sandbox=sandbox, allow_same_family=a.allow_same_family,
                 map_workers=a.map_workers).resume(a.run_id, force=a.force)
    print(json.dumps({"run_id": res.run_id, "status": res.status, "detail": res.detail,
                      "steps": res.steps, **_cost_out(store, res.run_id, res.cost)}, indent=1))
    return 0 if res.status == "completed" else 1


APPROVAL_ITEM_PRINT_CHARS = 20_000


# closed problem classes of refused returned measurements (kerneld/protocol.MEASUREMENT_PROBLEMS) as text
_MEASUREMENT_TEXT = {"not_utf8": "data is not UTF-8 text", "columns": "the columns differ from the protocol's columns",
                     "row_length": "a line has the wrong number of values",
                     "non_finite": "a number column holds a value that is not a finite number",
                     "replicates": "an arm has fewer replicates than the protocol's minimum",
                     "other": "the data do not match the protocol"}


def cmd_return_data(a: argparse.Namespace, store: EvidenceStore) -> int:
    """HUMAN action (GOAL §4.7): register the measurements humans returned for an APPROVED physical protocol, with an
    attestation. Refused unless the run is paused at the data gate, the exact protocol was approved by a human
    (engine rule), the protocol passes the prohibited screen, the data match the protocol, and nothing was registered
    for it before. The data become a `future` dataset; the attestation is recorded but remains untested."""
    from .kerneld.client import KernelD, KernelDError
    from .kerneld.scope import ScopedKernel
    from .research.physical import Protocol, screen
    from .runtime.engine import approval_decision, approval_item_hash, paused_approval
    from .studio.kill import kill_entry, killed_detail
    kill = kill_entry(store, a.run_id)
    if kill is not None:  # the pause is read from the trace, so the kill must be checked explicitly
        raise SystemExit(f"refused: run {a.run_id} was {killed_detail(kill)}")
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
    if item["dataset_id"] in ScopedKernel(store).vault.datasets():
        raise SystemExit(f"data for this protocol are already registered ({item['dataset_id']})")
    # kerneld reads the file, checks it against the protocol and registers it (kerneld/ingest.py, GOAL §16.5.2):
    # the measurements never enter this process; a refusal names closed problem classes only
    try:
        with KernelD(store.root) as kd:
            run = kd.open_run(a.run_id)
            try:
                data_sha256 = run.ingest_returned_measurements(
                    item["dataset_id"], a.file, protocol.model_dump(mode="json"), claim_hash=item["claim_hash"],
                    attestation=a.attest, approved_protocol_seq=decision["ledger_seq"])
            finally:
                run.close()
    except KernelDError as e:
        if e.code == "measurements_refused":
            raise SystemExit("returned data refused: " + "; ".join(_MEASUREMENT_TEXT[p] for p in e.problems)) from None
        if e.code == "already_registered":
            raise SystemExit(f"data for this protocol are already registered ({item['dataset_id']})") from None
        raise SystemExit(f"returned data refused by kerneld ({e.code})") from None
    print(json.dumps({"registered": item["dataset_id"], "data_sha256": data_sha256,
                      "next": f"chh approve {a.run_id} data_received --decision approve --approver <you>",
                      "role": f"role 'future' earns CERTIFIED-VALIDATED only after a human runs: chh approve-role "
                              f"{item['dataset_id']} --approver <you>"}))
    return 0


def cmd_approve(a: argparse.Namespace, store: EvidenceStore) -> int:
    """Record a human decision for the item a PAUSED run is waiting on (GOAL invariant 15). The writer is
    custom_harness.human.nodes.approve_node, the ONLY code path that writes an approval-node decision. It does not
    resume the run: `chh resume RUN_ID` does."""
    from .human.guard import HumanActionRefused
    from .human.nodes import approve_node, node_item, render_item
    try:
        it = node_item(store, a.run_id, a.node_id)
        print(f"run {a.run_id} node {a.node_id}: {it.prompt}")
        print(f"item ({it.item_key}):\n{render_item(it, APPROVAL_ITEM_PRINT_CHARS)}")
        print(f"item_hash: {it.item_hash}")
        entry = approve_node(store, a.run_id, a.node_id, decision=a.decision, approver=a.approver,
                             item_hash=a.item_hash, note=a.note or "")
    except HumanActionRefused as e:
        raise SystemExit(f"refused: {e}") from e
    print(json.dumps({"recorded": entry.body["decision"], "approver": entry.body["approver"],
                      "ledger_seq": entry.seq, "next": f"chh resume {a.run_id}"}))
    return 0


def _refuse_unpriced_usd_cap(backends: list[Backend]) -> None:
    """A usd cap counts the usd each call's cost carries. A backend whose provider has no CALIBRATED cost-table row
    reports no real usd (a subscription reports 0; the cost table today holds UNCALIBRATED placeholders only), so the
    cap could never trigger: refuse it instead of pretending to enforce it (missing != 0)."""
    from .gateway.costs import UnknownRate, rate_for
    for b in backends:
        model = getattr(b, "model", None) or "*"
        try:
            rate = rate_for(b.provider, str(model))
        except UnknownRate:
            rate = None
        if rate is None or rate.placeholder or rate.usd_per_mtok_in is None or rate.usd_per_mtok_out is None:
            raise SystemExit(f"refused: --max-usd needs a calibrated cost-table usd rate for every backend; "
                             f"{b.name} (provider {b.provider!r}) has "
                             f"{'no row' if rate is None else 'an UNCALIBRATED placeholder row'}, so its calls "
                             f"carry no real usd and the cap would never trigger. Use --max-tokens / --max-quota.")


def cmd_evolve(a: argparse.Namespace, store: EvidenceStore) -> int:
    import custom_harness.research  # noqa: F401

    from .evolve.branches import BranchConfig
    from .evolve.driver import evolve, load_suite
    from .evolve.loop import EvolveConfig
    if a.islands is not None:
        return cmd_evolve_islands(a, store)
    if not a.proposer:
        raise SystemExit("--proposer is required (or pass --islands ISLANDS_YAML, which names its proposers)")
    spec = HarnessSpec.model_validate(yaml.safe_load(Path(a.spec).read_text(encoding="utf-8")))
    backends = {}
    for binding in a.role:
        role, _, kind = binding.partition("=")
        backends[role] = _backend(kind)
    proposer = _backend(a.proposer)
    if a.allow_same_family:  # evaluation engines read it; recorded as NOT MET in every run
        os.environ["CHH_ALLOW_SAME_FAMILY"] = "1"
    run_tag = f"evolve-{spec.id}-{int(time.time())}"

    factory = _sandbox_factory(a, data_dir() / "sandboxes" / run_tag)

    # --race-allocator: opt-in race allocation (GOAL §5 L1; evolve/loop.py RACE_ALLOCATORS). race_slots stays 1: the
    # per-completion allocator then evaluates in exactly the batch order (concurrency needs an owner ack, not a flag)
    cfg = EvolveConfig(seeds=tuple(a.seeds), n_edits=a.n_edits, race_rounds=a.race_rounds,
                       race_allocator=a.race_allocator)
    from .budget import persistent_budget
    if a.max_usd is not None:
        _refuse_unpriced_usd_cap([*backends.values(), proposer])
    limits = {"usd": a.max_usd, "tokens": a.max_tokens, "quota_units": a.max_quota, "wall_s": a.max_wall_s}
    campaign_id = a.campaign_id or run_tag
    campaign = persistent_budget(store, campaign_id, {k: v for k, v in limits.items() if v is not None})
    # the campaign id also names the generation ledger (`chh evolve report`), the durable unit outcomes and the
    # persisted proposals: re-running with the same --campaign-id resumes it (evolve/durable.py)
    out = data_dir() / "evolve" / campaign_id
    evolve(spec, load_suite(Path(a.suite)), generations=a.generations, backends=backends, proposer=proposer,
           store=store, sandbox_factory=factory, cfg=cfg, out_dir=out, campaign_budget=campaign,
           campaign=campaign_id,
           branching=BranchConfig(cluster_key=a.branch_by) if a.branch_by else None)
    print(f"outputs: {out}")
    print(f"campaign: {campaign_id} (report: chh evolve report {campaign_id}; resume: re-run with --campaign-id "
          f"{campaign_id})")
    return 0


def cmd_evolve_islands(a: argparse.Namespace, store: EvidenceStore) -> int:
    """`chh evolve SPEC --suite S --islands ISLANDS.yaml` (GOAL §5 L7, explicit opt-in): evolve/search.py
    evolve_islands with the deterministic orchestrator, the policy gate (a declared challenger is trialled, never set
    without a passing trial) and, with `controller:`, the downgrade-only budget gate. Rounds come from the file
    (`--generations` is refused), branching is not offered, no weights lever (no model training). Writes
    islands.json (spec hashes, policy version, stop reason, trial counts: no gate value) and each island's final
    spec into the output directory."""
    import custom_harness.research  # noqa: F401

    from .evolve.driver import load_suite
    from .evolve.loop import EvolveConfig
    from .evolve.search import evolve_islands, islands_summary, load_islands_config
    if a.generations != 1 or a.branch_by or a.proposer:
        raise SystemExit("refused: with --islands the rounds and proposers come from the islands file; --generations, "
                         "--proposer and --branch-by do not apply")
    icfg = load_islands_config(Path(a.islands))
    spec = HarnessSpec.model_validate(yaml.safe_load(Path(a.spec).read_text(encoding="utf-8")))
    backends = {role: _backend(kind) for role, _, kind in (b.partition("=") for b in a.role)}
    proposers = {name: _backend(kind) for name, kind in sorted(icfg.proposers.items())}
    controller = _backend(icfg.controller) if icfg.controller else None
    if a.allow_same_family:
        os.environ["CHH_ALLOW_SAME_FAMILY"] = "1"
    run_tag = f"islands-{spec.id}-{int(time.time())}"
    factory = _sandbox_factory(a, data_dir() / "sandboxes" / run_tag)
    cfg = EvolveConfig(seeds=tuple(a.seeds), n_edits=a.n_edits, race_rounds=a.race_rounds,
                       race_allocator=a.race_allocator)
    from .budget import persistent_budget
    limits = {"usd": a.max_usd, "tokens": a.max_tokens, "quota_units": a.max_quota, "wall_s": a.max_wall_s}
    campaign_id = a.campaign_id or run_tag
    if a.max_usd is not None:  # invariant 9: a usd cap an unpriced backend can never trigger is refused
        _refuse_unpriced_usd_cap([*backends.values(), *proposers.values(), *([controller] if controller else [])])
    budget = persistent_budget(store, campaign_id, {k: v for k, v in limits.items() if v is not None})
    res = evolve_islands(spec, load_suite(Path(a.suite)), islands=list(icfg.islands), policy=icfg.policy,
                         rounds=icfg.rounds, total_budget=icfg.total_budget, proposers=proposers,
                         backends=backends, store=store, sandbox_factory=factory, cfg=cfg, campaign=campaign_id,
                         controller=controller, challenger=icfg.challenger, trial=icfg.trial,
                         campaign_budget=budget)
    out = data_dir() / "evolve" / campaign_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "islands.json").write_text(json.dumps(islands_summary(res), indent=1, sort_keys=True), encoding="utf-8")
    for iid, parent in sorted(res.parents.items()):
        (out / f"spec_{iid}.yaml").write_text(yaml.safe_dump(parent.model_dump(mode="json"), sort_keys=False),
                                              encoding="utf-8")
    print(f"outputs: {out}")
    return 0


def cmd_evolve_report(a: argparse.Namespace, store: EvidenceStore) -> int:
    """`chh evolve report CAMPAIGN`: render every generation's report from the generation ledger and run the
    recompute audit on it (and on every stopped-generation record). Exit 1 if any cell or record does not reproduce,
    2 if the campaign is unknown or its ledger is unreadable."""
    from .evolve import generations as G
    if not store.records_where(G.CAMPAIGN_KIND, "campaign", a.campaign):
        print(f"no evolve campaign {a.campaign!r} in this store", file=sys.stderr)
        return 2
    try:
        completed = [r["generation"] for r in store.records_where(G.GENERATION_KIND, "campaign", a.campaign)]
        wanted = completed if a.generation is None else [a.generation]
        reports = [G.generation_report(store, a.campaign, g) for g in wanted]
    except G.LedgerError as e:
        print(f"FAILED: the generation ledger does not reproduce: {e}", file=sys.stderr)
        return 1
    problems = {g: G.audit_report(rep, store) for g, rep in zip(wanted, reports, strict=True)}
    stopped = G.stopped_records(store, a.campaign)
    stop_problems = {n: G.verify_stopped(store, n) for n, _ in stopped}
    bad = {**{f"generation {g}": p for g, p in problems.items() if p},
           **{f"generation_stopped #{n}": p for n, p in stop_problems.items() if p}}
    if a.json:
        print(json.dumps({"campaign": a.campaign, "reports": reports, "stopped": [r for _, r in stopped],
                          "audit": {"passed": not bad, "problems": bad}}, indent=1, sort_keys=True))
    else:
        for rep in reports:
            print(G.render_markdown(rep))
        if stopped:
            print("## Stopped generations (began, did not complete)\n")
            for n, r in stopped:
                print(f"- generation_stopped #{n}: generation {r.get('generation')}, reason {r.get('reason')}, "
                      f"budget {r.get('budget')}, parent {str(r.get('parent'))[:12]}")
        print(f"\naudit: {'OK' if not bad else 'FAILED'} ({len(reports)} generation report(s), {len(stopped)} stopped "
              f"record(s) recomputed from the ledger)")
    for where, ps in bad.items():
        for p in ps:
            print(f"AUDIT FAILED {where}: {p}", file=sys.stderr)
    return 1 if bad else 0


def _sandbox_factory(a: argparse.Namespace, root: Path):
    """key -> sandbox, every call in a NEW directory (compute.unique_workspaces): the evolve Evaluator retries an
    infra-failed unit under the same key, and the retry must not inherit the failed attempt's files (GOAL L0)."""
    from .compute import unique_workspaces
    if a.docker:
        from .sandbox.docker import DockerSandbox
        return unique_workspaces(DockerSandbox, root)
    if a.dev_sandbox:
        return unique_workspaces(lambda ws: LocalSandbox(ws, i_understand_no_isolation=True), root)
    raise SystemExit("pass --docker (or --dev-sandbox for trusted code only)")


def cmd_reproduce(a: argparse.Namespace, store: EvidenceStore) -> int:
    """Re-check a Reproducibility Bundle from its bytes alone (GOAL L2 done-when)."""
    from .kernel import SandboxReplayer
    from .kernel.bundle import BundleError, reproduce_bundle
    from .store.run_bundle import RunBundleError, check_artifacts, read_bundle_manifest
    factory = _sandbox_factory(a, data_dir() / "sandboxes" / f"reproduce-{int(time.time())}")
    try:  # GOAL L2 large artifacts: the bundle's artifact manifest (if it has one) is checked first, fail closed
        arts = check_artifacts(read_bundle_manifest(Path(a.bundle)), Path(a.bundle), blobs=store.blobs,
                               roots=[Path(r) for r in getattr(a, "artifact_root", None) or []])
    except RunBundleError as e:
        print(json.dumps({"reproduced": False, "refused": str(e)}, indent=1))
        return 1
    if arts.fatal:
        print(json.dumps({"reproduced": False, "refused": "artifacts: " + "; ".join(arts.fatal[:10]),
                          "artifacts": arts.to_json()}, indent=1))
        return 1
    roots = [Path(r) for r in getattr(a, "artifact_root", None) or []]

    def local(sha: str) -> bytes | None:  # a large artifact the bundle lists but does not carry: this machine's copy
        from .kerneld.hoststore import PrivateDataRefused
        try:  # through the HostStore: a private blob is refused (= missing), never copied out (GOAL §16.5.2)
            if store.blobs.has(sha):
                return store.blobs.get(sha)
        except (PrivateDataRefused, OSError):
            return None
        for f in (r / sha for r in roots):
            if f.is_file():
                return f.read_bytes()
        return None
    try:
        report = reproduce_bundle(Path(a.bundle), lambda: SandboxReplayer(factory), artifact_source=local)
    except BundleError as e:  # refused (unsupported format, over a read cap): fail closed with the reason
        print(json.dumps({"reproduced": False, "refused": str(e)}, indent=1))
        return 1
    headline = _headline(report.certificate)
    # GOAL §2.4: every headline number must reproduce, not only the label and the check statuses
    original, problems = _original_headline(Path(a.bundle)), list(report.problems)
    if headline is not None and (original is None or original["targets"] != headline["targets"]):
        problems.append(f"headline numbers differ from the bundled certificate: originally "
                        f"{original['targets'] if original else 'unreadable'}, now {headline['targets']}")
    ok = report.reproduced and len(problems) == len(report.problems)
    print(json.dumps({"reproduced": ok, "bundle_hash": report.bundle_hash,
                      "original_label": report.original_label, "reproduced_label": report.reproduced_label,
                      "ledger_check": report.ledger_check, "ledger_head": report.ledger_head,
                      "problems": problems, "missing_artifacts": report.missing_artifacts,
                      "artifacts": arts.to_json(), "headline": headline, "original_headline": original}, indent=1))
    return 0 if ok else 1


def _original_headline(path: Path) -> dict[str, Any] | None:
    """The headline numbers of the ORIGINAL certificate as bundled (read under the bundle caps), or None."""
    import zipfile

    from .kernel import Certificate
    from .store.run_bundle import BoundedZip, RunBundleError
    try:
        with zipfile.ZipFile(path) as z:
            r = BoundedZip(z)
            man = json.loads(r.read("manifest.json"))
            row = next(x for x in man["ledger"] if x[1] == "certificate"
                       and json.loads(x[2]).get("cert_hash") == man["cert_hash"])
            cert = Certificate.model_validate(json.loads(r.read(f"blobs/{json.loads(row[2])['blob']}")))
    except (OSError, ValueError, KeyError, IndexError, StopIteration, TypeError, zipfile.BadZipFile,
            RunBundleError):
        return None
    return _headline(cert)


def _headline(cert: Any) -> dict[str, Any] | None:
    """The headline numbers of the RE-DERIVED certificate (GOAL §2.4): label, tier and, per held-out target, the
    effect estimate and its interval, as the reproducing machine computed them (None without a certificate)."""
    if cert is None:
        return None
    targets = (cert.resolution or {}).get("targets") or {}
    return {"label": cert.label.value, "tier": cert.tier,
            "targets": {t: {k: v.get(k) for k in ("estimate", "lo", "hi")} for t, v in sorted(targets.items())
                        if isinstance(v, dict)}}


def cmd_bundle(a: argparse.Namespace, store: EvidenceStore) -> int:
    from .kerneld.scope import ScopedKernel
    out = Path(a.out or (data_dir() / "bundles" / f"{a.cert_hash[:16]}.zip"))
    print(json.dumps({"bundle": str(out), "bundle_hash": ScopedKernel(store).bundle(a.cert_hash, out)}))
    return 0


def cmd_certificates(a: argparse.Namespace, store: EvidenceStore) -> int:
    from .kerneld.scope import ScopedKernel
    k = ScopedKernel(store)
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
    from .kernel import VaultError
    from .kerneld.scope import ScopedKernel
    kernel = ScopedKernel(store)
    try:
        rec = kernel.vault.dataset(a.dataset_id)
        print(f"dataset {rec.dataset_id}: role {rec.role.value}, content hash {rec.hash}, files {sorted(rec.files)}")
        entry = kernel.approve_role(a.dataset_id, approver=a.approver, note=a.note)
    except VaultError as e:
        raise SystemExit(f"NOT approved: {e}") from e
    print(json.dumps({"approved_role": rec.role.value, "dataset": a.dataset_id, "approver": a.approver,
                      "ledger_seq": entry.seq}))
    return 0


def cmd_alpha(a: argparse.Namespace, store: EvidenceStore) -> int:
    """`chh alpha open` is a HUMAN action (GOAL §16.4): it gives a held-out target a fixed budget of slots, each claim
    tested at alpha / slots and releases paid in slots (kernel/alpha.py). The ledger entry names the approver; no
    production code path opens an account. `chh alpha status` only reads."""
    from .kernel import KernelError, VaultError
    from .kernel import alpha as A
    from .kernel.checks import related_targets, slot_charge
    from .kerneld.scope import ScopedKernel
    kernel = ScopedKernel(store)
    try:
        if a.alpha_cmd == "open":
            e = kernel.open_alpha_account(a.dataset_id, a.alpha, a.slots, approver=a.approver)
            print(json.dumps({"opened": a.dataset_id, "alpha": a.alpha, "slots": a.slots, "approver": a.approver,
                              "ledger_seq": e.seq}))
            return 0
        accts = A.accounts(kernel.ledger, related_targets(kernel.vault, a.dataset_id))
    except (KernelError, VaultError) as e:
        raise SystemExit(f"NOT done: {e}") from e
    print(json.dumps([{"dataset_id": x.dataset_id, "opened_seq": x.seq, "alpha": x.alpha, "slots": x.slots,
                       "slot_level": x.level, "charged": A.charged(kernel.ledger, x),
                       "next_claim_charge": slot_charge(kernel.ledger, kernel.vault, x, kernel.config),
                       "retired": A.retired(kernel.ledger, x)} for x in accts], indent=1))
    return 0 if accts else 1


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
    if a.frozen or a.verify:  # GOAL §16.2: freeze the manifest, then mint (research/rebuild.py)
        from .kernel import Ledger
        from .research import rebuild as RB
        if not (a.record_dir and a.beacon_file):
            raise SystemExit("--frozen/--verify need --record-dir and --beacon-file (outside agent-readable roots)")
        try:
            RB.require_private_locations(Path(a.record_dir), Path(a.beacon_file))  # before any labelled export lands
        except RB.RebuildRefused as e:
            raise SystemExit(f"refused: {e}") from None
        if a.verify:
            res = RB.verify(Path(a.out), record_dir=Path(a.record_dir), beacon_file=Path(a.beacon_file),
                            ledger_entries=Ledger(store).entries())
            print(json.dumps(res, indent=1))
            return 1 if res["problems"] else 0
        if a.real_export:
            real = RB.load_real_export(Path(a.real_export))
    if a.docker and real is None:
        from .sandbox.docker import FULL_IMAGE, DockerSandbox
        # the ONLY full-image sandbox: trusted exporter code reads scikit-learn's bundled datasets (agent code runs
        # in the candidate image, which has none; tests/test_bundled_data.py pins this)
        real = export_real(lambda ws: DockerSandbox(ws, image=FULL_IMAGE, trusted_task_building=True),
                           Path(a.record_dir) / "_export" if a.frozen else Path(a.out) / "_export")
    if a.frozen:
        from .kerneld.scope import ScopedKernel
        try:
            res = RB.rebuild(Path(a.out), record_dir=Path(a.record_dir), beacon_file=Path(a.beacon_file),
                             vault=ScopedKernel(store).vault, store_root=store.root, real_data=real, name=a.name,
                             heldout_rows=a.heldout_rows, null_families=tuple(a.null_family or ()))
        except (RB.RebuildRefused, RB.MintRefused) as e:
            raise SystemExit(f"refused: {e}") from None
        print(json.dumps(res, indent=1))
        return 0
    path = build_suite(Path(a.out), real_data=real, heldout_rows=a.heldout_rows,
                       null_families=tuple(a.null_family or ()))
    print(json.dumps({"suite": str(path), "real_data": real is not None}))
    return 0


def cmd_preflight_suite(a: argparse.Namespace, store: EvidenceStore) -> int:
    """GOAL §16.8: zero-call certifiability audit of a task suite (no model call, no program run, no private file)."""
    from .research.preflight import PreflightConfig, suite_audit
    root = Path(a.suite)
    single = (root / "task.yaml").exists()
    dirs = [root] if single else sorted(d for d in root.iterdir() if (d / "task.yaml").exists())
    rows = suite_audit(dirs, baseline_acc=a.baseline_acc, family_size=a.family,
                       config=PreflightConfig(n_sims=a.sims))
    for r in rows:
        p = "n/a" if r["p_certify"] is None else f"{r['p_certify']:.3f}"
        print(f"{r['task']:<32} n_dev={r['n_dev']:<5} n_test={r['n_test']:<6} min_effect={r['min_effect']:.4g} "
              f"P(certify)={p:<6} {r['verdict']}")
    print(f"assumption: a solution perfect on dev vs a baseline of dev accuracy {a.baseline_acc}; family {a.family}; "
          f"theta {rows[0]['theta'] if rows else '-'} (UNCALIBRATED). 0 model calls.")
    if a.json:
        Path(a.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")
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
    disk no longer match the hashes recorded in the bundle, if manifest.json differs from the hash this store
    recorded for the run, or (GOAL L2 large artifacts) if a required artifact is missing or any artifact's hash
    differs. Missing optional artifacts are reported, never skipped silently."""
    import custom_harness.research  # noqa: F401

    from .store.run_bundle import RunBundleError, check_artifacts, check_task_files, read_run_bundle_bytes
    try:
        m, manifest_sha = read_run_bundle_bytes(Path(a.bundle))
    except RunBundleError as e:
        raise SystemExit(f"run bundle refused: {e}") from e
    # a manifest is self-attested (it says which artifacts are required) unless its hash is anchored: by this store's
    # record of the run, or by a hash the user got from where the bundle was published
    recorded = {r.get("manifest_sha256") for r in store.records("run_bundle")
                if r.get("run_id") == m["run"]["id"] and r.get("manifest_sha256")}
    given = getattr(a, "manifest_sha256", None)
    if recorded and manifest_sha not in recorded:
        raise SystemExit("run bundle refused: its manifest.json differs from the one recorded in this store for run "
                         f"{m['run']['id']} (edited after it was written)")
    if given and manifest_sha != given:
        raise SystemExit(f"run bundle refused: its manifest.json sha256 {manifest_sha} differs from the hash given")
    if recorded:
        anchor = "matches the store's record"
    elif given:
        anchor = "matches the given --manifest-sha256"
    elif getattr(a, "allow_unanchored", False):
        anchor = f"NOT anchored (--allow-unanchored): manifest sha256 {manifest_sha} is self-attested"
    else:
        raise SystemExit(f"run bundle refused: its manifest is not anchored (no record of run {m['run']['id']} in "
                         f"this store). Pass --manifest-sha256 with the published hash, or --allow-unanchored. "
                         f"This manifest's sha256: {manifest_sha}")
    spec = HarnessSpec.model_validate(m["spec"])
    inputs = dict((m.get("inputs") or {}).get("input") or {})
    if a.task_dir:
        inputs["task_dir"] = a.task_dir
    if inputs.get("task_dir"):
        diff = check_task_files(m, inputs["task_dir"])
        if diff:
            raise SystemExit("task files differ from the bundle: " + "; ".join(diff[:10]))
    try:
        arts = check_artifacts(m, Path(a.bundle), blobs=store.blobs,
                               roots=[Path(r) for r in getattr(a, "artifact_root", None) or []],
                               task_dir=inputs.get("task_dir"))
    except RunBundleError as e:
        raise SystemExit(f"run bundle refused: {e}") from e
    if arts.fatal:
        print(json.dumps({"rerun_of": m["run"]["id"], "refused": True, "artifacts": arts.to_json()}, indent=1))
        raise SystemExit("run bundle refused (fail closed): " + "; ".join(arts.fatal[:10]))
    backends = {role: _backend(kind) for role, _, kind in (b.partition("=") for b in a.role)}
    from .compute import fresh_dir  # a NEW directory per re-run (GOAL L0)
    ws = fresh_dir(data_dir() / "sandboxes", f"rerun-{m['run']['id']}-{int(time.time())}")
    sandbox: Sandbox | None = None
    if a.docker:
        from .sandbox.docker import DockerSandbox
        sandbox = DockerSandbox(ws)
    elif a.dev_sandbox:
        sandbox = LocalSandbox(ws, i_understand_no_isolation=True)
    res = Engine(spec, backends=backends, store=store, sandbox=sandbox,
                 allow_same_family=a.allow_same_family).run(m["run"]["task_id"], inputs, seed=int(m["seed"]))
    print(json.dumps({"rerun_of": m["run"]["id"], "run_id": res.run_id, "status": res.status,
                      "detail": res.detail, "manifest_anchor": anchor, "artifacts": arts.to_json(),
                      **_cost_out(store, res.run_id, res.cost)}, indent=1))
    return 0 if res.status == "completed" else 1


def cmd_approve_adapter(a: argparse.Namespace, store: EvidenceStore) -> int:
    """HUMAN action (GOAL §4.7, invariant 13): approve a domain adapter's current content after its validation passes.
    The approval names the approver and is recorded in the hash-chained ledger."""
    from .kernel import AdapterError
    from .kernel.adapters import REGISTRY, adapter_hash
    from .kerneld.scope import ScopedKernel
    if a.name not in REGISTRY:
        raise SystemExit(f"unknown adapter {a.name!r}; known: {sorted(REGISTRY)}")
    spec, checks = REGISTRY[a.name]
    print(f"adapter {spec.name} v{spec.version}: classes {sorted(spec.classes)}, modules {list(spec.modules)}")
    print(f"content hash {adapter_hash(spec)}; running {len(checks)} validation checks")
    try:
        entry = ScopedKernel(store).approve_adapter(a.name, approver=a.approver)
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
    from .kerneld.scope import ScopedKernel
    from .memory import MemoryStore, NotReadable, harvest_negatives
    replayer = None
    if a.action == "harvest-negatives":
        # the gate re-derives every certificate (Kernel.verify, K3 re-executes data claims): without a replayer it
        # cannot, so the command fails closed instead of gating on an incomplete re-derivation (s4-station finding)
        if not (a.docker or a.dev_sandbox):
            raise SystemExit("harvest-negatives re-derives each certificate (K3 re-executes): pass --docker (or "
                             "--dev-sandbox for trusted code only)")
        import time

        from .config import data_dir
        from .kernel import SandboxReplayer
        replayer = SandboxReplayer(_sandbox_factory(a, data_dir() / "sandboxes" / f"harvest-{int(time.time())}"))
    mem = MemoryStore(ScopedKernel(store, replayer=replayer), namespace=a.namespace)
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
    Prints exactly what is approved; custom_harness.human.nodes.approve_statement (the ONLY code path that writes a
    `statement_approval` ledger entry) registers it as an artifact and records the approval, bound to its hash. A
    formal claim's K1 requires one."""
    from .human.guard import HumanActionRefused
    from .human.nodes import approve_statement, statement_view
    data = Path(a.file).read_bytes()
    try:
        view = statement_view(data)
        print(f"theorem {view.theorem}\n--- statement source ---\n{view.source}\n--- end ---\n"
              f"statement_hash: {view.statement_hash}")
        entry = approve_statement(store, data, artifact_id=a.artifact_id, approver=a.approver,
                                  statement_hash=a.statement_hash, note=a.note or "")
    except HumanActionRefused as e:
        raise SystemExit(f"refused: {e}") from e
    print(json.dumps({"approved_statement": a.artifact_id, "approver": entry.body["approver"],
                      "ledger_seq": entry.seq}))
    return 0


def cmd_salts_reveal(a: argparse.Namespace, store: EvidenceStore) -> int:
    """HUMAN: reveal a sealed salt campaign on/after its reveal date (GOAL §16.9; also `python -m
    custom_harness.salts reveal`)."""
    import datetime as dt

    from .kerneld.scope import ScopedKernel
    from .salts.generate import SaltError
    from .salts.plan import reveal
    try:
        revealed = reveal(a.sealed, ScopedKernel(store).vault, human=a.by, today=dt.date.today())
    except SaltError as e:
        raise SystemExit(f"refused: {e}") from None
    print(json.dumps({"campaign_hash": revealed.campaign_hash, "plant_list": list(revealed.plant_list)}, indent=1))
    return 0


def cmd_verify_registry(a: argparse.Namespace, store: EvidenceStore) -> int:
    """Verify the EFFECTIVE provider registry (bundled providers.yaml plus the local override) offline and statically:
    unknown providers, missing licence flags, auth kinds that are not official and subscription entries off the BYOS
    allowlist fail; with --release, so does any usable entry without a recent human verification or a cost-table row.
    Exit 0 only when nothing is found (a release check that fails closed)."""
    import datetime as dt

    from .gateway.registry import load_registry, verify_registry
    reg = load_registry(Path(a.override) if a.override else None)
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    if a.release:  # only LEDGERED human sign-offs of each entry's current hash count (`chh sign-off-provider`)
        from .human.providers import with_ledgered_signoffs
        reg = with_ledgered_signoffs(reg, store)
    problems = verify_registry(reg, release=a.release, today=today)
    print(json.dumps({"entries": sorted(reg), "release": a.release, "today": today.isoformat(),
                      "problems": problems, "ok": not problems}, indent=1))
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["delegate"]:  # the delegation driver (GOAL §15) has its own parser, root and ledger (no store)
        from .orchestration.cli import main as delegate_main
        return delegate_main(args[1:])
    if args[:2] == ["evolve", "report"]:  # `chh evolve report CAMPAIGN` (a spec file named "report": ./report)
        args = ["evolve-report", *args[2:]]
    from .evolve import cli as evolve_cli
    # read-only L6.9 views (`chh evolve branches|route ...`, own parser); they use the ONE store open below (int17
    # merge: the kerneld boundary ratchet lists a single EvidenceStore construction in cli.py)
    views = args[:1] == ["evolve"] and len(args) > 1 and args[1] in evolve_cli.SUBCOMMANDS
    p = argparse.ArgumentParser(prog="chh")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("delegate", add_help=False, help="delegation driver: `chh delegate <command> ...` (see "
                   "docs/orchestration.md; same as `python -m custom_harness.orchestration`)")
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
    r.add_argument("--map-workers", type=int, default=1, help=MAP_WORKERS_HELP)
    ev = sub.add_parser("evolve", help="evolve a harness spec on a task suite (never touches the sealed split)")
    ev.add_argument("spec")
    ev.add_argument("--suite", required=True)
    ev.add_argument("--role", action="append", default=[], help="role=backend for harness runs")
    ev.add_argument("--allow-same-family", action="store_true",
                   help="override invariant 5 (judge/replicator family); recorded as NOT MET")
    ev.add_argument("--proposer", help="backend for the edit proposer (required unless --islands)")
    ev.add_argument("--generations", type=int, default=1)
    ev.add_argument("--seeds", type=int, nargs="+", default=[0])
    ev.add_argument("--n-edits", type=int, default=3)
    ev.add_argument("--race-rounds", type=int, default=2)
    ev.add_argument("--race-allocator", choices=["batch", "per_completion", "voi"], default="batch",
                    help="opt-in race allocation: batch (default) | per_completion (completion-driven, one slot) | "
                         "voi (knowledge-gradient value of information); the acceptance rule is the same")
    ev.add_argument("--docker", action="store_true")
    ev.add_argument("--dev-sandbox", action="store_true", help="LocalSandbox (NO isolation; dev only)")
    ev.add_argument("--campaign-id", help="campaign id: persisted budget, generation ledger, durable units and "
                                         "proposals (reuse it to resume a campaign)")
    ev.add_argument("--max-usd", type=float)
    ev.add_argument("--max-tokens", type=float)
    ev.add_argument("--max-quota", type=float, help="subscription quota units (one per BYOS call)")
    ev.add_argument("--max-wall-s", type=float)
    ev.add_argument("--branch-by", choices=["family", "domain"],
                    help="L6.9: make branches of gate-rejected cluster winners and record the route table "
                         "(see `chh evolve branches|route SPEC`)")
    ev.add_argument("--islands", metavar="ISLANDS_YAML",
                    help="opt-in L7 campaign mode: islands under the deterministic orchestrator, its policy gate and "
                         "the downgrade-only controller (evolve/search.py load_islands_config: rounds, budget, "
                         "policy, proposers per island). No weights lever: no model training")
    er = sub.add_parser("evolve-report", help="`chh evolve report CAMPAIGN`: generation reports from the evolve "
                                              "ledger plus the recompute audit (exit 1 if anything does not reproduce)")
    er.add_argument("campaign")
    er.add_argument("--generation", type=int, help="only this campaign-global generation")
    er.add_argument("--json", action="store_true")
    rr = sub.add_parser("resume", help="resume an interrupted run (process died, engine crash, infra failure) "
                                         "or a run paused at an approval node")
    rr.add_argument("run_id")
    rr.add_argument("--role", action="append", default=[], help="role=backend, e.g. executor=codex")
    rr.add_argument("--allow-same-family", action="store_true",
                   help="override invariant 5 (judge/replicator family); recorded as NOT MET")
    rr.add_argument("--docker", action="store_true")
    rr.add_argument("--dev-sandbox", action="store_true")
    rr.add_argument("--force", action="store_true", help="resume even if the owning process looks alive")
    rr.add_argument("--map-workers", type=int, default=1, help=MAP_WORKERS_HELP)
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
    rb.add_argument("--manifest-sha256", default=None,
                    help="the published sha256 of the bundle's manifest.json (needed when this store has no record)")
    rb.add_argument("--allow-unanchored", action="store_true",
                    help="run a bundle whose manifest is not anchored (its artifact list is then self-attested)")
    rb.add_argument("--artifact-root", action="append", default=[],
                    help="a directory to look for artifacts the bundle does not carry (repeatable; hashes are checked)")
    rb.add_argument("--docker", action="store_true")
    rb.add_argument("--dev-sandbox", action="store_true")
    aa = sub.add_parser("approve-adapter", help="HUMAN: approve a domain adapter after its validation passes")
    aa.add_argument("name")
    aa.add_argument("--approver", required=True, help="the human approving (recorded in the ledger)")
    ar = sub.add_parser("approve-role", help="HUMAN: approve a dataset's external/future role (needed for VALIDATED)")
    ar.add_argument("dataset_id")
    ar.add_argument("--approver", required=True, help="the human approving (recorded in the ledger)")
    ar.add_argument("--note", default="", help="why this data is external/future (provenance)")
    al = sub.add_parser("alpha", help="per-target alpha-accounts: HUMAN `open`, read-only `status` (GOAL 16.4)")
    alsub = al.add_subparsers(dest="alpha_cmd", required=True)
    alo = alsub.add_parser("open", help="HUMAN: open an alpha-account on a held-out target before its first claim")
    alo.add_argument("dataset_id")
    alo.add_argument("--alpha", type=float, required=True, help="familywise level alpha_T of the target")
    alo.add_argument("--slots", type=int, required=True, help="slots S_T (each claim tested at alpha_T / S_T)")
    alo.add_argument("--approver", required=True, help="the human opening it (recorded in the ledger)")
    als = alsub.add_parser("status", help="accounts covering a target: slots charged, next charge, retired")
    als.add_argument("dataset_id")
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
    rp.add_argument("--artifact-root", action="append", default=[],
                    help="a directory to look for artifacts the bundle does not carry (repeatable; hashes are checked)")
    bd = sub.add_parser("bundle", help="write the Reproducibility Bundle of a certificate")
    bd.add_argument("cert_hash")
    bd.add_argument("-o", "--out")
    bs = sub.add_parser("build-suite", help="write the ML task suite (>= 20 tasks with --docker)")
    bs.add_argument("out")
    bs.add_argument("--docker", action="store_true", help="also build real-data and repair tasks in the sandbox")
    bs.add_argument("--frozen", action="store_true", help="GOAL 16.2: anchor the suite manifest, then mint the data "
                    "from draw(manifest hash, sealed beacon); idempotent (docs/suite_rebuild.md)")
    bs.add_argument("--verify", action="store_true", help="re-derive a --frozen suite and compare every file")
    bs.add_argument("--record-dir", help="private record directory (outside agent-readable roots)")
    bs.add_argument("--beacon-file", help="the sealed beacon (outside agent-readable roots)")
    bs.add_argument("--real-export", help="real-data export written by the trusted exporter (instead of --docker)")
    bs.add_argument("--name", default="m2", help="suite name recorded in the manifest")
    bs.add_argument("--heldout-rows", type=int, default=None,
                    help="held-out rows per synthetic task (GOAL 16.2 small train / large test; frozen in manifest). "
                         "Proposed 4500 (research.suite.PROPOSED_HELDOUT_ROWS, UNCALIBRATED, from a partial preflight)")
    bs.add_argument("--null-family", action="append", choices=["noise"],
                    help="add a genuinely null family to the real pool (null salts may only come from such a family)")
    pf = sub.add_parser("preflight-suite", help="zero-call certifiability audit of a task suite (GOAL 16.8)")
    pf.add_argument("suite", help="a suite directory (task subdirectories) or one task directory")
    pf.add_argument("--baseline-acc", type=float, default=0.8, help="ASSUMED baseline dev accuracy")
    pf.add_argument("--family", type=int, default=1, help="multiplicity family size per target")
    pf.add_argument("--sims", type=int, default=100)
    pf.add_argument("--json", help="also write the rows as JSON")
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
    mm.add_argument("--docker", action="store_true", help="harvest-negatives: re-derive (K3 re-execution) in Docker")
    mm.add_argument("--dev-sandbox", action="store_true", help="harvest-negatives: LocalSandbox (NO isolation; "
                    "trusted code only)")
    rs = sub.add_parser("runs")
    rs.add_argument("--limit", type=int, default=20)
    t = sub.add_parser("trace")
    t.add_argument("run_id")
    t.add_argument("--full", action="store_true")
    vr = sub.add_parser("verify-registry", help="offline check of the provider registry against the BYOS allowlist "
                        "and the licence/auth policy (invariant 11); --release adds the per-release human checks")
    vr.add_argument("--release", action="store_true", help="also require recent human verification and cost rows")
    vr.add_argument("--today", help="YYYY-MM-DD (default: today) for the --release age check")
    vr.add_argument("--override", help="registry override file (default: $CHH_DATA_DIR/providers.override.yaml)")
    sr = sub.add_parser("salts-reveal", help="HUMAN: open a sealed salt campaign on/after its reveal date")
    sr.add_argument("sealed")
    sr.add_argument("--by", required=True, help="the human revealing (recorded in the ledger)")
    lit_cli.add_parsers(sub)
    provenance_cli.add_parsers(sub)
    audit_cli.add_parsers(sub)
    scorecard_cli.add_parsers(sub)
    timesplit_cli.add_parsers(sub)
    studio_cli.add_parsers(sub)
    from .research.station import cli as station_cli  # `chh research multi ...` (L4 multi-agent mode)
    station_cli.add_parsers(sub)
    human_cli.add_parsers(sub)  # after the others: it adds hash flags to older human commands
    a = None if views else p.parse_args(args)
    if getattr(a, "dev_sandbox", False):  # no production path may select the unisolated sandbox
        from .sandbox.local import TEST_MODE_ENV
        if os.environ.get(TEST_MODE_ENV) != "1":
            raise SystemExit(f"--dev-sandbox runs programs on the host WITHOUT isolation; it is refused unless "
                             f"{TEST_MODE_ENV}=1 (trusted test code only). Use --docker.")
    store = open_host_store(data_dir() / "store")  # GOAL §16.5.2: private data only through kerneld
    try:
        if a is None:
            return evolve_cli.main(args[1:], store)
        human_cli.check_legacy_actor(a, store)  # agent-named actors refused; optional hash bindings checked
        return {"run": cmd_run, "resume": cmd_resume, "approve": cmd_approve, "return-data": cmd_return_data,
                "rerun": cmd_rerun,
                "approve-adapter": cmd_approve_adapter, "approve-role": cmd_approve_role, "alpha": cmd_alpha,
                "approve-statement": cmd_approve_statement, "ledger-anchor": cmd_ledger_anchor,
                "finalize": cmd_finalize, "evolve": cmd_evolve, "evolve-report": cmd_evolve_report, "runs": cmd_runs,
                "trace": cmd_trace, "reproduce": cmd_reproduce, "bundle": cmd_bundle,
                "certificates": cmd_certificates, "ledger-verify": cmd_ledger_verify,
                "predictions": cmd_predictions, "build-suite": cmd_build_suite, "memory": cmd_memory,
                "preflight-suite": cmd_preflight_suite, "salts-reveal": cmd_salts_reveal,
                "verify-registry": cmd_verify_registry,
                **lit_cli.COMMANDS, **provenance_cli.COMMANDS, **audit_cli.COMMANDS,
                **scorecard_cli.COMMANDS, **timesplit_cli.COMMANDS, **studio_cli.COMMANDS,
                **station_cli.COMMANDS, **human_cli.COMMANDS}[a.cmd](a, store)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
