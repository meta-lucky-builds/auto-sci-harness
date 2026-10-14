"""Research graph (GOAL §5 L9 dashboard data, CLI only): one read-only view that joins the experiment trees
(research/tree.py), the claim graph (pre-registered claims, their subjects, comparators and datasets, and the
certificates issued on them), the prediction ledger and the evidence (records, the runs that produced them), rebuilt
from the store. It never re-derives a certificate (no Kernel check runs) and writes nothing.

Node ids are typed: `claim:<hash>`, `cert:<hash>`, `evidence:<id>`, `run:<id>`, `dataset:<id>`, `artifact:<id>`,
`tree:<id>`, `treenode:<tree>/<node>`. Every node carries the ledger seq or store row it came from. Problems found
while reading (a certificate whose content does not match its ledger entry, a tampered tree log, a missing evidence
row) are listed under `problems` and the affected node is marked, never dropped.
"""

from __future__ import annotations

import time
from typing import Any

from ..kernel import KernelError
from ..kernel.predictions import score_predictions
from ..kerneld.scope import ScopedKernel
from ..store import EvidenceStore
from . import limitations


def research_graph(store: EvidenceStore, findings_path: Any = None) -> dict[str, Any]:
    kernel = ScopedKernel(store)
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []
    problems: list[str] = []

    def node(nid: str, **attrs: Any) -> None:
        nodes.setdefault(nid, {"id": nid}).update(attrs)

    def edge(src: str, rel: str, dst: str) -> None:
        edges.append({"from": src, "rel": rel, "to": dst})

    vault = kernel.vault
    for ds_id, rec in vault.datasets().items():
        node(f"dataset:{ds_id}", kind="dataset", role=rec.role.value, hash=rec.hash, ledger_seq=rec.seq,
             role_approved=vault.role_approval(ds_id) is not None)
    for art_id, art in vault.artifacts().items():
        node(f"artifact:{art_id}", kind="artifact", artifact_kind=art.kind, hash=art.hash, ledger_seq=art.seq)

    # claim graph + prediction ledger
    scores = {s.claim_hash: s for s in score_predictions(kernel.ledger, time.time())}
    for e in kernel.ledger.entries("prereg"):
        h, c = e.body["claim_hash"], e.body["claim"]
        cid = f"claim:{h}"
        s = scores.get(h)
        node(cid, kind="claim", statement=c.get("statement"), evidence_class=c.get("evidence_class", "data"),
             claim_kind=c.get("kind"), prereg_seq=e.seq,
             prediction=None if s is None else {"q": s.q, "p0": s.p0, "outcome": s.outcome, "expired": s.expired,
                                                "info_gain_bits": s.info_gain_bits, "skill_bits": s.skill_bits})
        for role in ("subject", "comparator"):
            edge(cid, role, f"artifact:{c[role]['artifact_id']}")
        for t in c.get("generalization_targets", []):
            edge(cid, "target", f"dataset:{t}")
        for d in c.get("development", []):
            edge(cid, "development", f"dataset:{d}")

    retracted = {e.body.get("cert_hash"): e.seq for e in kernel.ledger.entries("retraction")}
    for e in kernel.ledger.entries("certificate"):
        h = e.body["cert_hash"]
        nid = f"cert:{h}"
        node(nid, kind="certificate", label_recorded=e.body.get("label"), ledger_seq=e.seq,
             retracted_seq=retracted.get(h))
        edge(nid, "certifies", f"claim:{e.body.get('claim_hash')}")
        try:
            cert = kernel.certificate(h)
        except (KernelError, OSError, ValueError) as err:
            nodes[nid]["tampered"] = True
            problems.append(f"certificate {h[:16]}: {type(err).__name__}: {err}")
            continue
        nodes[nid].update(label=cert.label.value, tier=cert.tier,
                          adapters=[a.get("name") for a in cert.adapters])
        for ev_id in cert.evidence_ids:
            edge(nid, "uses", f"evidence:{ev_id}")
            try:
                ev = store.get_evidence(ev_id)
            except KeyError:
                node(f"evidence:{ev_id}", kind="evidence", missing=True)
                problems.append(f"certificate {h[:16]}: evidence {ev_id} is not in the store")
                continue
            node(f"evidence:{ev_id}", kind="evidence", evidence_kind=ev.kind.value, tier=int(ev.tier),
                 verifier_id=ev.verifier_id, summary=ev.summary)
            if ev.produced_by_run:
                edge(f"evidence:{ev_id}", "produced_by", f"run:{ev.produced_by_run}")
                try:
                    status, _ = store.run_status(ev.produced_by_run)
                    run = store.get_run(ev.produced_by_run)
                    node(f"run:{run.id}", kind="run", status=status.value, harness_id=run.harness_id,
                         task_id=run.task_id)
                except KeyError:
                    node(f"run:{ev.produced_by_run}", kind="run", missing=True)

    # experiment trees (records-table logs, research/tree.py; replayed through the tree's own checks)
    from ..research.tree import RECORD_KIND, ExperimentTree, TreeTampered
    tree_ids = list(dict.fromkeys(str(r["tree"]) for r in store.records(RECORD_KIND) if r.get("tree")))
    for tid in tree_ids:
        try:
            t = ExperimentTree.load(store, tid)
        except (TreeTampered, KeyError, ValueError) as err:
            node(f"tree:{tid}", kind="tree", tampered=True)
            problems.append(f"tree {tid}: {type(err).__name__}: {err}")
            continue
        node(f"tree:{tid}", kind="tree", head=t.head, events=t.seq, metric=t.metric, shape=t.shape(),
             selection=t.selection_record())
        for nid in t.nodes:
            v = t.node(nid)
            node(f"treenode:{tid}/{nid}", kind="tree_node", status=v.status, metric=v.metric,
                 content_hash=v.content_hash, attempts=v.n_attempts, repairs=v.repairs, round=v.round)
            edge(f"treenode:{tid}/{nid}", "child_of" if v.parent else "root_of",
                 f"treenode:{tid}/{v.parent}" if v.parent else f"tree:{tid}")

    lims = [lim.to_json() for lim in limitations.register(kernel, findings_path)]
    for lim in lims:
        for h in lim["certificates"]:
            nodes[f"cert:{h}"].setdefault("untested_open_loopholes", []).append(lim["finding_id"])
    return {"nodes": sorted(nodes.values(), key=lambda n: n["id"]), "edges": edges, "limitations": lims,
            "problems": problems}
