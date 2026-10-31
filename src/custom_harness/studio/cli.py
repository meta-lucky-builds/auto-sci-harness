"""`chh` commands of the L9 studio (CLI only): explain, graph, limitations, queue, kill (human), supervise (an
automated controller, downgrade-only), tier-policy."""

from __future__ import annotations

import argparse
import json
from typing import Any

from ..store import EvidenceStore


def cmd_kill(a: argparse.Namespace, store: EvidenceStore) -> int:
    """HUMAN action: stop a run safely (ledgered; the run ends FAILED; nothing is certified after it)."""
    from .kill import KillRefused, kill_run, killed_detail
    try:
        entry = kill_run(store, a.run_id, by=a.by, reason_code=a.reason)
    except KillRefused as e:
        raise SystemExit(f"refused: {e}") from e
    print(json.dumps({"killed": a.run_id, "ledger_seq": entry.seq, "detail": killed_detail(entry),
                      "note": "a step already in flight finishes; no further step runs and the run is not resumable"}))
    return 0


def cmd_supervise(a: argparse.Namespace, store: EvidenceStore) -> int:
    """One turn of an AUTOMATED controller over the killable runs. Its only path is the downgrade-only gate: kills
    with code-checked evidence are applied and ledgered as `controller_action` (never `run_kill`); everything else
    (grant, admit, enlarge, revive, ...) is rejected and ledgered."""
    from ..cli import _backend
    from .controller import result_json, supervise
    res = supervise(store, _backend(a.controller), controller_id=a.controller_id, limit=a.limit,
                    **({"stall_s": a.stall_s} if a.stall_s is not None else {}))
    print(json.dumps(result_json(res), indent=1))
    return 0


def cmd_tier_policy(a: argparse.Namespace, store: EvidenceStore) -> int:
    """init: write the code default for a human to edit. show: the policy in force and where it comes from.
    pin: HUMAN action, validate the file and record its hash in the ledger."""
    from .tierpolicy import TierPolicyError, load_policy, pin, policy_path, to_json, write_default
    try:
        if a.action == "init":
            print(json.dumps({"written": str(write_default()), "next": "edit it, then: chh tier-policy pin --by NAME"}))
        elif a.action == "show":
            source = "file pinned by a human" if policy_path().exists() else "code default (tiers.DEFAULT_POLICY)"
            print(json.dumps({"policy": to_json(load_policy(store)), "source": source, "file": str(policy_path())},
                             indent=1))
        else:
            if not a.by:
                raise TierPolicyError("pin needs --by (the human pinning the policy)")
            print(policy_path().read_text(encoding="utf-8") if policy_path().exists() else "")
            entry = pin(store, a.by, expect_sha256=a.sha256)
            print(json.dumps({"pinned": entry.body["sha256"], "by": entry.body["by"], "ledger_seq": entry.seq}))
    except TierPolicyError as e:
        raise SystemExit(f"refused: {e}") from e
    return 0


def cmd_explain(a: argparse.Namespace, store: EvidenceStore) -> int:
    """Explain a certificate from the store; every value carries the artifact it resolves to (invariant 8)."""
    import getpass

    from ..court.display import DisplayRefused, require_autopsy
    from ..kernel import SandboxReplayer
    from ..kerneld.scope import ScopedKernel
    from .explain import ExplainError, explain, render_text
    try:  # every value of a certificate: one display path (GOAL 18.5)
        require_autopsy(store, a.cert, getpass.getuser())
    except DisplayRefused as e:
        raise SystemExit(f"refused: {e}") from None
    replayer = None
    if a.docker or a.dev_sandbox:
        import time

        from ..cli import _sandbox_factory
        from ..config import data_dir
        replayer = SandboxReplayer(_sandbox_factory(a, data_dir() / "sandboxes" / f"explain-{int(time.time())}"))
    elif not a.no_verify:
        raise SystemExit("explain re-derives the certificate (K3 re-executes): pass --docker (or --dev-sandbox for "
                         "trusted code), or --no-verify to skip it (the status is then UNVERIFIED)")
    try:
        doc = explain(ScopedKernel(store, replayer=replayer), a.cert, verify=not a.no_verify)
    except ExplainError as e:
        raise SystemExit(f"refused: {e}") from e
    print(json.dumps(doc, indent=1, ensure_ascii=False) if a.json else render_text(doc))
    verdict = doc.get("status", {}).get("verdict", {}).get("value")
    return 0 if verdict in ("valid", None) else 1


def cmd_graph(a: argparse.Namespace, store: EvidenceStore) -> int:
    from .graph import research_graph
    g = research_graph(store)
    print(json.dumps(g["limitations"] if a.limitations else g, indent=1, ensure_ascii=False, default=str))
    return 0


def cmd_queue(a: argparse.Namespace, store: EvidenceStore) -> int:
    """Read-only: every pending human action (custom_harness.human.queue), with the hash each one binds."""
    from ..human.queue import counts, pending
    items = pending(store)
    if a.kind:
        items = [it for it in items if it["kind"] in a.kind]
    if a.json:
        print(json.dumps(items, indent=1, default=str))
    else:
        for it in items:
            what = {k: v for k, v in it.items() if k not in ("kind", "decide", "prompt", "binds")}
            print(f"[{it['kind']}] {json.dumps(what, sort_keys=True, default=str)}")
            print(f"    binds:  {json.dumps(it.get('binds', {}), sort_keys=True)}")
            print(f"    decide: {it['decide']}")
        print(f"{len(items)} pending {json.dumps(counts(items), sort_keys=True)}")
    return 0


def add_parsers(sub: Any) -> None:
    from .kill import KILL_REASONS
    k = sub.add_parser("kill", help="HUMAN: stop a run safely (ends FAILED, ledgered, never resumed)")
    k.add_argument("run_id")
    k.add_argument("--by", required=True, help="the human stopping the run (recorded in the ledger)")
    k.add_argument("--reason", default="human_stop", choices=KILL_REASONS)
    sv = sub.add_parser("supervise", help="one turn of an AUTOMATED controller over killable runs (downgrade-only "
                                          "gate: it may only deny, shrink or kill with code-checked evidence)")
    sv.add_argument("--controller", required=True, help="backend of the controller, e.g. codex or mock")
    sv.add_argument("--controller-id", default="studio-supervisor", help="recorded in every controller_action")
    sv.add_argument("--limit", type=int, default=50, help="most recent runs observed")
    sv.add_argument("--stall-s", type=float, help="seconds without a step before `stalled` evidence holds")
    ex = sub.add_parser("explain", help="explain a certificate from the store (every value with its source)")
    ex.add_argument("cert", help="certificate hash (a unique prefix is enough)")
    ex.add_argument("--json", action="store_true", help="machine-readable facts with their sources")
    ex.add_argument("--docker", action="store_true", help="re-derive (K3 re-execution) in Docker")
    ex.add_argument("--dev-sandbox", action="store_true", help="LocalSandbox (NO isolation; trusted code only)")
    ex.add_argument("--no-verify", action="store_true", help="do not re-derive; the status is UNVERIFIED")
    gr = sub.add_parser("graph", help="research graph: trees, claims, predictions, evidence (read-only JSON)")
    gr.add_argument("--limitations", action="store_true", help="only the limitations register")
    qu = sub.add_parser("queue", help="every pending HUMAN action (approval nodes, physical protocols, adapters, "
                                      "statements, roles, alpha-accounts, verifier/suite reviews, feature rulings, "
                                      "provider sign-offs, human-held checks), read-only")
    qu.add_argument("--json", action="store_true")
    qu.add_argument("--kind", action="append", default=[], help="only this kind (repeatable)")
    tp = sub.add_parser("tier-policy", help="L9 tier policy: init | show | pin (HUMAN)")
    tp.add_argument("action", choices=["init", "show", "pin"])
    tp.add_argument("--by", help="pin: the human pinning the policy (recorded in the ledger)")
    tp.add_argument("--sha256", help="pin: the sha256 of the file you reviewed; refused if it differs")


COMMANDS = {"kill": cmd_kill, "supervise": cmd_supervise, "tier-policy": cmd_tier_policy, "explain": cmd_explain,
            "graph": cmd_graph, "queue": cmd_queue}
