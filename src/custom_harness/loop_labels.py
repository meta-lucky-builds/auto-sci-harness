"""Invariant 10 check: every loop instance in reports, ledgers and the scorecard carries MECHANICAL or VALIDATED.

Loop instances and where their label lives (GOAL §6.1):
  research run        an inner research loop. Its report has the line `Loop label (GOAL §6.1): **X**`, which must
                      equal the label recomputed from the run's certificate (`research.claims.loop_label`), so a
                      wrong label fails too; `checklist.loop_label` (publication.py, since 2026-10-09), when
                      present, must agree as well (its absence blocks publication: axis F, not this check).
  evolve generation   `generation_stats` (one per generation; since 2026-10-09 it carries `loop_label`), its decision
                      records (`harness_edit`; `lineage` rows with status accepted / rejected_gate / infra_aborted)
                      and the driver's gen<N>.json summary. A `generation_ledger` record carries no label field (its
                      field set is fixed by evolve/generations.py); its label is the one of the `generation_stats`
                      record it references, and an unresolvable reference is unlabelled (fail closed).
  scorecard           the local scorecard (scorecard/local_axes.py) lists the instances found here with their label.

Not covered, stated: the proposer-visible generation report (evolve/generations.py `generation_report`) omits the
label ON PURPOSE (the verdict embargo, invariant 18, treats it as a gate outcome): a conflict between invariants 10
and 18 left to the owner (docs/scorecard_local.md).

`audit_store` is the runtime check (over every record and run of a store); `static_problems` is the static check
(over the source: every writer of a loop record names `loop_label`). Both return problems; empty = labelled.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .publication import LOOP_LABELS
from .scorecard.refs import record_ref, run_ref
from .store.evidence import EvidenceStore

DECISION_STATUSES = ("accepted", "rejected_gate", "infra_aborted")
LABELLED_KINDS = ("generation_stats", "harness_edit")
LABEL_LINE = re.compile(r"^Loop label \(GOAL §6\.1\): \*\*([A-Z]+)\*\*", re.M)


def _final_state(store: EvidenceStore, run_id: str) -> dict[str, Any] | None:
    from .scorecard.facts import _final_state as fs
    return fs(store, run_id)


def _recomputed(state: dict[str, Any]) -> str:
    from .research.claims import loop_label
    return loop_label(state)


def audit_store(store: EvidenceStore) -> dict[str, Any]:
    """Every loop instance of the store with its label and source ref; `unlabelled` lists each one whose label is
    missing, not MECHANICAL/VALIDATED, or disagrees with its recomputation."""
    inst: list[dict[str, Any]] = []

    def add(kind: str, ref: str, label: Any, why: str | None = None) -> None:
        ok = label in LOOP_LABELS and why is None
        inst.append({"kind": kind, "ref": ref, "label": label if label in LOOP_LABELS else None,
                     "problem": None if ok else (why or f"loop label {label!r} is not MECHANICAL or VALIDATED")})

    for kind in LABELLED_KINDS:
        for b in store.records(kind):
            add(kind, record_ref(kind, b), b.get("loop_label"))
    for b in store.records("lineage"):
        if b.get("status") in DECISION_STATUSES:
            add("lineage", record_ref("lineage", b), b.get("loop_label"))
    stats = store.records("generation_stats")
    for b in store.records("generation_ledger"):
        n = (b.get("refs") or {}).get("generation_stats") if isinstance(b.get("refs"), dict) else None
        ok_ref = isinstance(n, int) and not isinstance(n, bool) and 0 <= n < len(stats)
        lab = stats[n].get("loop_label") if ok_ref else None  # type: ignore[index]
        add("generation_ledger", record_ref("generation_ledger", b), lab,
            None if ok_ref else "its generation_stats reference does not resolve")
    for run in reversed(store.list_runs(limit=2**62)):
        state = _final_state(store, run.id)
        if not isinstance(state, dict) or "report" not in state:
            continue
        ref = run_ref(store, run.id)
        m = LABEL_LINE.search(str(state.get("report")))
        text = m.group(1) if m else None
        want = _recomputed(state)
        why = None
        if text != want:
            why = f"report states loop label {text!r}, the certificate gives {want}"
        cl = state.get("checklist")
        # the checklist item exists since 2026-10-09 (publication.py; its ABSENCE is a publication blocker, counted
        # on axis F); when present it must agree
        if isinstance(cl, dict) and "loop_label" in cl and cl.get("loop_label") != want:
            why = why or f"checklist loop_label {cl.get('loop_label')!r}, the certificate gives {want}"
        add("research_run", ref, text, why)
    bad = [i for i in inst if i["problem"]]
    counts: dict[str, dict[str, int]] = {}
    for i in inst:
        c = counts.setdefault(i["kind"], {"MECHANICAL": 0, "VALIDATED": 0, "unlabelled": 0})
        c[i["label"] if not i["problem"] else "unlabelled"] += 1
    return {"instances": inst, "unlabelled": bad, "counts": counts}


# ------------------------------------------------------------------ static check
def _dict_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def static_problems(src_root: Path) -> list[str]:
    """Writers of loop records and loop reports in the source that do not name the loop label:
    every `append_record("generation_stats" | "harness_edit", {...})`, every dict literal with a `status` key whose
    value can be a decision status (the lineage decision rows), and every evolve-driver summary dict with a
    `generation` key must have a `loop_label` key; `research/claims.py` must render the label line."""
    root = Path(src_root)
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "append_record" and len(node.args) == 2
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value in LABELLED_KINDS):
                body = node.args[1]
                if not isinstance(body, ast.Dict) or "loop_label" not in _dict_keys(body):
                    problems.append(f"{rel}:{node.lineno}: append_record({node.args[0].value!r}, ...) without "
                                    f"a literal 'loop_label' key")
            if isinstance(node, ast.Dict) and rel.startswith("evolve/"):
                keys = _dict_keys(node)
                status = next((v for k, v in zip(node.keys, node.values, strict=True)
                               if isinstance(k, ast.Constant) and k.value == "status"), None)
                decision = status is not None and any(
                    isinstance(c, ast.Constant) and c.value in DECISION_STATUSES for c in ast.walk(status))
                if decision and "loop_label" not in keys:
                    problems.append(f"{rel}:{node.lineno}: a loop decision record (status {DECISION_STATUSES}) "
                                    f"without 'loop_label'")
                if rel == "evolve/driver.py" and "generation" in keys and "decision" in keys \
                        and "loop_label" not in keys:
                    problems.append(f"{rel}:{node.lineno}: the generation summary without 'loop_label'")
    claims = (root / "research" / "claims.py").read_text(encoding="utf-8")
    if 'Loop label (GOAL §6.1): **{loop_label(state)}**' not in claims:
        problems.append("research/claims.py: the report no longer renders the loop label line")
    return problems
