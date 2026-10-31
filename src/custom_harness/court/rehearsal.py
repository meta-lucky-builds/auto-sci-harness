"""Rehearsal and the power check (GOAL 18.6; UPGRADES N13, N9 power part). docs/court_rehearsal.md.

A rehearsal is a mock trial: the frozen arms go through the real Court route (seal, register, Kernel K0-K9,
re-execution) on Lab data, inside a throwaway store that exists only for this call. Everything is shown: every check,
every number.
No fresh truth is spent, because no fresh truth is touched: the case's store is the scratch store, and the only thing
written to the real store is a `court_rehearsal` record that says what ran. The scratch store's certificate cannot
become evidence: it is not in the real ledger, and every report says "Lab data, not evidence". The two human steps of
the route are played by the rehearsal on the scratch store alone.

`readiness` is the power part of the readiness check. It uses public inputs only (the claim and outcome counts from Lab
data), it is labelled UNCALIBRATED, and it can only deny or warn (invariant 21).
"""

from __future__ import annotations

import csv
import hashlib
import io
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from ..kerneld.scope import ScopedKernel
from ..kerneld.seal import seal_files
from ..research.preflight import PreflightConfig, PublicInputs, preflight
from ..store import EvidenceStore
from ..verify import metrics as M
from . import minted as C
from . import sealed as S

RECORD_KIND = "court_rehearsal"
NOT_EVIDENCE = "Lab data, not evidence"
UNCALIBRATED = "UNCALIBRATED"
SCRATCH_APPROVER = "rehearsal-scratch-store"
SCRATCH_BEACON = "rehearsal-scratch-beacon-not-public"
SCRATCH_PROJECT = "rehearsal"
SIZES = (500, 1000, 2000, 4500)  # stops at the first size with no objection
READINESS_SIMS = 20  # UNCALIBRATED (owner decision, GOAL 13.8): the default 200 takes minutes per size. A coarse
#                      estimate; the verdict that counts is the registration preflight (court/minted.size_held_out)


class RehearsalRefused(RuntimeError):
    pass


def readiness(part: Any, dev_pairs: tuple[int, int, int, int] | None, sizes: tuple[int, ...] = SIZES
              ) -> dict[str, Any]:
    """P(certify) per held-out size, from public inputs. It never grants anything: `can_only` says so."""
    out: dict[str, Any] = {"label": UNCALIBRATED, "can_only": "deny or warn", "sizes": []}
    if dev_pairs is None or part.metric != "accuracy":
        return {**out, "verdict": "not run", "why": "it needs Lab outcome counts and the accuracy metric"}
    for n in sizes:
        r = preflight(PublicInputs(metric=part.metric, higher_is_better=True, n_test=n, dev_pairs=dev_pairs,
                                   min_effect=part.min_effect, alpha=part.alpha, family_size=1,
                                   seeds=tuple(part.seeds), n_boot=part.n_boot), PreflightConfig(n_sims=READINESS_SIMS))
        out["sizes"].append({"n_test": n, "p_certify": r.p_certify, "verdict": r.verdict, "denied": r.denied})
        if not r.denied:
            return {**out, "verdict": "no objection", "smallest_size_with_no_objection": n}
    return {**out, "verdict": "unlikely to certify at any of these sizes because the effect on Lab data is too small "
                              "for the stated minimum effect"}


def outcome_counts(labels: list[str], method: list[str], baseline: list[str]) -> tuple[int, int, int, int]:
    """(both right, only the method, only the baseline, neither) on Lab data."""
    both = only_m = only_b = neither = 0
    for y, m, b in zip(labels, method, baseline, strict=True):
        if m == y and b == y:
            both += 1
        elif m == y:
            only_m += 1
        elif b == y:
            only_b += 1
        else:
            neither += 1
    return both, only_m, only_b, neither


def _private_copy(store: EvidenceStore, data: bytes) -> bool:
    from ..lab.walls import private_blob_index
    return hashlib.sha256(data).hexdigest() in set().union(*private_blob_index(store).values())


def _feature_rows(data: bytes) -> tuple[list[str], list[list[str]]]:
    table = [r for r in csv.reader(io.StringIO(data.decode("utf-8", "replace"))) if r]
    return (table[0], table[1:]) if table else ([], [])


def sealed_rows_problem(store: EvidenceStore, data: bytes, label_column: str) -> str | None:
    """Why `data` may not be rehearsed on: it is the file a project sealed, or it holds a row of a sealed part. Either
    would show scores computed from sealed labels. Rows are compared on the part's feature columns, value by value;
    a file without those columns is not compared."""
    sha = hashlib.sha256(data).hexdigest()
    head, rows = _feature_rows(data)
    vault = ScopedKernel(store).vault
    for seal in store.records(S.RECORD_KIND):
        if seal.get("event") != "sealed":
            continue
        if seal.get("data_sha256") == sha:
            return f"this is the file sealed for project {seal['project']!r}: use the Lab's part"
        for target in seal.get("targets") or []:
            part_head, part_rows = _feature_rows(vault.read_public(target, "test_X.csv"))
            if not set(part_head) <= set(head) - {label_column}:
                continue
            at = [head.index(c) for c in part_head]
            sealed = {tuple(r) for r in part_rows}
            if any(tuple(r[i] for i in at) in sealed for r in rows if len(r) == len(head)):
                return f"this file holds rows sealed in project {seal['project']!r}: use the Lab's part"
    return None


def rehearse(store: EvidenceStore, pid: str, data_file: Path, *, label_column: str, rows: int,
             sandbox_factory: Any) -> dict[str, Any]:
    """One mock trial of frozen promotion `pid` on the researcher's Lab data. `sandbox_factory(scratch_root)` gives the
    factory for that run: the same kind of sandbox the Court would use."""
    from ..lab.promote import PromoteRequest, _promotion
    data = Path(data_file).read_bytes()
    if _private_copy(store, data):
        raise RehearsalRefused("this file is a private vault file: a rehearsal runs on Lab data only")
    problem = sealed_rows_problem(store, data, label_column)
    if problem:
        raise RehearsalRefused(problem)
    frozen = _promotion(store, pid)[0]
    req = PromoteRequest.model_validate(frozen["request"])
    root = Path(tempfile.mkdtemp(prefix="chh-rehearsal-"))
    scratch = EvidenceStore(root / "store")
    try:
        ScopedKernel(scratch).approve_adapter("ml", approver=SCRATCH_APPROVER)
        S.seal_project(scratch, SCRATCH_PROJECT, Path(data_file), label_column=label_column, rows=rows, parts=1,
                       beacon_round="rehearsal", beacon_value=SCRATCH_BEACON)
        S.bind_session(scratch, SCRATCH_PROJECT, str(frozen["session_id"]))
        route = C.sealed_route(scratch, SCRATCH_PROJECT, 1)
        body = C.register_case(
            scratch, role="promotion", promotion_id=pid, session_id=str(frozen["session_id"]), title=req.title,
            sentence=req.sentence, part=req.claim, budget=req.budget,
            method=store.blobs.get(frozen["frozen"]["method"]["blob"]),
            baseline=store.blobs.get(frozen["frozen"]["baseline"]["blob"]),
            lock=store.blobs.get(frozen["frozen"]["lock"]["blob"]), family=f"sealed:{SCRATCH_PROJECT}",
            beacon_round=route["beacon_round"], sizing={"n_test": route["n_test"]}, n_train=route["n_train"],
            anchor_file=root / "anchors" / "a.jsonl", contract=req.contract, sealed=route,
            extra={"project": SCRATCH_PROJECT, "part": 1})
        ScopedKernel(scratch).approve_role(body["target"], approver=SCRATCH_APPROVER,
                                           note="rehearsal: scratch store only")
        out = C.run(scratch, body["case_id"], sandbox_factory(root), bundle_root=root / "bundles")
        report = _report(scratch, data, label_column, rows, out, req.claim)
    finally:
        scratch.close()
        shutil.rmtree(root, ignore_errors=True)
    pairs = report.pop("dev_pairs")
    report.update(readiness=readiness(req.claim, pairs), label=NOT_EVIDENCE)
    store.append_record(RECORD_KIND, {"promotion_id": pid, "label": NOT_EVIDENCE, "state": report["state"],
                                      "data_sha256": hashlib.sha256(data).hexdigest(), "dev_pairs": pairs,
                                      "scores": report.get("scores"), "readiness": report["readiness"],
                                      "would_label": report.get("would_label"), "checks": report.get("checks"),
                                      "at": time.time()})
    return report


def last_rehearsal(store: EvidenceStore, pid: str) -> dict[str, Any] | None:
    """The last rehearsal of a promotion: Lab data only."""
    rows = store.records_where(RECORD_KIND, "promotion_id", pid)
    return rows[-1] if rows else None


def _report(scratch: EvidenceStore, data: bytes, label_column: str, rows: int, out: dict[str, Any], part: Any
            ) -> dict[str, Any]:
    """Everything: the rehearsal's data are Lab data, so nothing here is held back."""
    if not out.get("cert_hash"):
        return {"state": "COULD NOT RUN", "reason": out.get("reason"), "dev_pairs": None}
    cert = ScopedKernel(scratch).certificate(out["cert_hash"])
    labels = seal_files(data, SCRATCH_PROJECT, label_column, 1, rows, f"rehearsal:{SCRATCH_BEACON}").parts[0].test_y
    y = [ln.strip() for ln in labels.decode().splitlines()[1:]]
    arms: dict[str, dict[int, list[str]]] = {"subject": {}, "comparator": {}}
    for ev in cert.evidence_ids:
        doc = scratch.blobs.get_json(scratch.get_evidence(ev).payload_ref)
        arms[doc["arm"]][doc["seed"]] = scratch.blobs.get_json(doc["preds_ref"])
    pairs = None
    if part.metric == "accuracy":
        seed = min(arms["subject"])
        pairs = outcome_counts(y, arms["subject"][seed], arms["comparator"][seed])
    scores = {arm: {s: M.score(part.metric, y, p) for s, p in by.items()} for arm, by in arms.items()}
    return {"state": "RAN", "would_label": cert.label.value, "failed": cert.failed(), "scores": scores,
            "checks": {k: v.model_dump(mode="json") for k, v in cert.checks.items()}, "dev_pairs": pairs}
