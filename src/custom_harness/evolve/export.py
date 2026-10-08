"""Read-only evidence export for the harness proposer (GOAL §5 L6.1; Meta-Harness, done safely).

A directory of JSON files that a (future, sandboxed, tool-using) proposer may read in full:

    manifest.json           format, policy, file list
    candidates/<hash>.json  harness specs (parents and candidates) with their typed edits and parent hash
    traces/<run_id>.json    full traces of EVOLVE-split runs (already scrubbed by the engine; scrubbed again here)
    scores.json             per exported run: score, verified, status, cost, failure signatures, certificate label
    certificates.json       certificate SUMMARIES of exported runs: label, failed checks, their reasons
    lineage.json            every lineage record (incl. rejects), through `lineage_view`
    ledger.json             harness-science ledger, through `proposer_view`
    generation_stats.json   per-generation yield/prune counters

What the proposer sees, and why (policy):
  - Runs of EVOLVE-split tasks only. Gate tasks are the never-shown split (invariant 6) and sealed tasks are touched
    only by `finalize`; the held-out family/domain must not appear in "any evolver-visible export" (§5 L6.8). Their
    run ids, task ids, family names, traces, scores and certificates are left out entirely.
  - Gate/sealed outcomes appear only as the AGGREGATE decision on each gated candidate: accepted or rejected, and
    the stage that rejected it (`rejected_on`: evolve re-measurement / held-out gate / cost). Gate means, CIs,
    per-family LOFO results, OOD drops, the gate-unit cost ratio, per-task gate attribution and the free-text gate
    decision are dropped: each is a per-split measurement that, fed back across generations, turns the gate into a
    training signal (adaptive overfitting of the "never-shown" split). Views are WHITELISTS, so a field added to the
    ledger later stays hidden until someone decides it is safe.
  - Never: run bundles (they carry hidden labels), blobs (checkpoints hold `_private*` state, vault private files are
    blobs), workspaces/snapshots, run inputs, `_private*` keys or values, canaries, task private directories, bundle
    paths, or the raw Kernel ledger (dataset registrations name private file blobs).

Fail closed: after writing, the tree is audited (verify/leak.py rules) against fingerprints of EVERY task's private
directory (all splits: hashes, canary tokens, verbatim contents, label windows of private CSVs, the directory paths),
the caller's extra canaries and every `_private*` value found in the exported runs' checkpoints (per-run canaries,
private refs). Any match deletes the export and raises ExportRefused (which reports locations, never values). Files
are made read-only on disk. The export is written beside its destination and renamed into place only after the audit
passes; an existing destination is refused (no overwrite).

Limits: verbatim matching only (leak.py's limits apply); read-only file modes stop accidental writes, not a process
with the owner's rights; the proposer must still be mounted read-only inside its sandbox (not built: L6.2).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..kerneld.releases import Releases, private_reads
from ..store import EvidenceStore
from ..verify.leak import CANARY_PREFIX, label_windows, scan_text
from .evaluate import RunOutcome, SuiteTask

FORMAT = "chh-evidence-export/1"
POLICY = ("evolve-split runs only; gate/sealed outcomes only as aggregate accept/reject decisions; no bundles, blobs, "
          "workspaces, private values, canaries or task private directories")

# whitelists: only these fields of a ledger/lineage record are proposer-visible. NOT `loop_label`: it is computed
# from the certificate labels of the GATE runs (all CERTIFIED-VALIDATED or not), a per-split gate outcome beyond
# the accept/reject bit.
_EDIT_VIEW = ("at", "parent", "candidate", "type", "rationale", "accepted", "predicted_delta", "actual_evolve_delta",
              "race_evolve_delta", "clusters_claimed", "claims_check", "signature")
_LINEAGE_VIEW = ("parent", "status", "candidate", "edit", "reason", "problem", "race", "survived", "evolve_fresh",
                 "signature", "claims_check", "rejected_on")
# generation_stats: counters over lineage statuses only (loop._counters)
_STATS_VIEW = ("parent", "proposed", "malformed", "declined", "rejected_novelty", "rejected_static",
               "no_op_or_duplicate", "rejected_smoke", "accepted", "rejected_gate", "raced", "pruned", "gated",
               "yield")
_DROP_KEYS = ("bundle_path", "checkpoint_ref")  # host path of a label-carrying bundle; private state blob
# verdict embargo (GOAL §16.5.3): held-out results reach the proposer as verdict labels only. These state keys hold
# certificate summaries (subject/comparator means, effect, CIs: continuous held-out values) or text rendered from
# them; in exported traces they are reduced to their label (certificates) or withheld (text).
_CERT_KEYS = ("certificate", "replica_certificate")
_EMBARGOED_KEYS = ("report", "report_ref", "publish_blockers", "checklist")
EMBARGOED = "<embargoed until the target retires>"
_HEX = re.compile(r"^[0-9a-f]{16,128}$")
_MAX_PRIVATE_SCAN = 1024 * 1024  # private files above this are matched by hash only


class ExportRefused(RuntimeError):
    def __init__(self, problems: list[str]):
        super().__init__("evidence export refused: " + "; ".join(problems[:10]))
        self.problems = problems


@dataclass
class ExportResult:
    path: Path
    n_runs: int
    excluded: dict[str, int] = field(default_factory=dict)  # runs left out, by split (never written to the export)
    files: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------- views
def _rejected_on(rec: dict[str, Any]) -> str | None:
    if rec.get("accepted"):
        return None
    if rec.get("rejected_on") in ("evolve", "held_out_gate", "cost", "exclusion_asymmetry"):
        return str(rec["rejected_on"])
    return "evolve" if str(rec.get("decision", "")).startswith("evolve") else "held_out_gate"


def proposer_view(rec: dict[str, Any], evolve_ids: set[str]) -> dict[str, Any]:
    """What the proposer may see of one `harness_edit` ledger record: whitelisted fields, the aggregate decision,
    and per-task attribution restricted to EVOLVE tasks."""
    out = {k: rec[k] for k in _EDIT_VIEW if k in rec}
    out["rejected_on"] = _rejected_on(rec)
    # a task is shown only if it is an evolve task NOW and was one when the record was written (per_task carries the
    # split at the time): a task rotated from an earlier campaign's gate split into this evolve split must not carry
    # that gate result along. Without per_task the split at the time is unknown: nothing is shown (fail closed).
    raw = rec.get("per_task")
    per_task: dict[str, Any] = raw if isinstance(raw, dict) else {}
    was_evolve = {t for t, r in per_task.items() if isinstance(r, dict) and r.get("split") == "evolve"}
    for k in ("flipped", "regressed", "regressed_unpredicted"):
        if isinstance(rec.get(k), list):
            out[k] = [t for t in rec[k] if t in evolve_ids and t in was_evolve]
    if per_task:
        out["per_task"] = {t: r for t, r in per_task.items() if t in evolve_ids and t in was_evolve}
    return out


def stats_view(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: rec[k] for k in _STATS_VIEW if k in rec}


def lineage_view(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: rec[k] for k in _LINEAGE_VIEW if k in rec}


# ---------------------------------------------------------------------------------------------- export
def _clean(obj: Any, drop: tuple[str, ...] = _DROP_KEYS) -> Any:
    """Drop `_private*` keys (the engine already replaced their values; a planted one must not survive) and the
    `drop` keys (in traces: bundle paths and checkpoint refs)."""
    if isinstance(obj, dict):
        return {k: _clean(v, drop) for k, v in obj.items()
                if not str(k).startswith("_private") and str(k) not in drop}
    if isinstance(obj, (list, tuple)):
        return [_clean(v, drop) for v in obj]
    return obj


def embargo(obj: Any) -> Any:
    """Reduce held-out results to verdict labels: certificate dicts keep only hash and label; report text is
    withheld. Applied recursively to exported trace outputs."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in _CERT_KEYS and isinstance(v, dict):
                out[k] = {"hash": v.get("hash"), "label": v.get("label"), "embargoed": True}
            elif k in _EMBARGOED_KEYS:
                out[k] = EMBARGOED
            else:
                out[k] = embargo(v)
        return out
    if isinstance(obj, list):
        return [embargo(v) for v in obj]
    return obj


def _dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=1, sort_keys=True, default=str)


def export_evidence(store: EvidenceStore, out_dir: Path, *, runs: Iterable[RunOutcome], tasks: list[SuiteTask],
                    canaries: Iterable[str] = (), releases: Releases | None = None) -> ExportResult:
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise ExportRefused([f"destination exists: {out_dir} (an export is never overwritten)"])
    by_id = {t.task_id: t for t in tasks}
    runs = list(runs)
    unknown = sorted({o.task_id for o in runs if o.task_id not in by_id})
    if unknown:  # fail closed: a run whose split is unknown could be a gate or sealed run
        raise ExportRefused([f"run(s) of unknown task(s): {', '.join(unknown[:5])}"])
    evolve_ids = {t.task_id for t in tasks if t.split == "evolve"}
    exported: dict[str, RunOutcome] = {}
    excluded: Counter[str] = Counter()
    for o in runs:
        split = by_id[o.task_id].split
        if split != "evolve":
            excluded[split] += 1
        elif o.run_id not in exported:
            exported[o.run_id] = o
    tmp = out_dir.parent / f".{out_dir.name}.partial-{secrets.token_hex(4)}"
    try:
        files = _write(store, tmp, exported, evolve_ids)
        rel = releases if releases is not None else Releases.live(store)
        for run_id, o in sorted(exported.items()):  # each exported run releases its verdict labels
            if run_id:
                rel.emit(o.cert_label, targets=private_reads(store, run_id=run_id), channel="export",
                         alphabet="verdict_bit", note="export.run:verdict", key=f"{o.task_id}|{o.seed}")
        problems = _audit(tmp, _fingerprints(store, tasks, exported, canaries))
        if problems:
            raise ExportRefused(problems)
        os.replace(tmp, out_dir)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    _make_read_only(out_dir)
    return ExportResult(out_dir, len([r for r in exported if r]), dict(excluded), files)


def _write(store: EvidenceStore, root: Path, exported: dict[str, RunOutcome], evolve_ids: set[str]) -> list[str]:
    root.mkdir(parents=True)
    written: list[str] = []

    def put(rel: str, obj: Any) -> None:
        _dump(root / rel, obj)
        written.append(rel)

    cert_blobs: dict[str, Any] = {}
    for _seq, _kind, body, *_ in store.ledger_rows("certificate"):
        b = json.loads(body)
        if isinstance(b.get("cert_hash"), str):
            cert_blobs[b["cert_hash"]] = b.get("blob")
    scores, certs, spec_hashes = [], [], set()
    for run_id, o in sorted(exported.items()):
        scores.append({"run_id": o.run_id, "task_id": o.task_id, "seed": o.seed, "score": o.score,
                       "verified": o.verified, "status": o.status, "cost": o.cost.model_dump(mode="json"),
                       "failure_signatures": list(o.failure_signatures), "cert_label": o.cert_label})
        if not run_id:
            continue  # no run record (e.g. the sandbox could not be created): score only
        run = store.get_run(run_id)
        status, detail = store.run_status(run_id)
        spec_hashes.add(run.harness_hash)
        steps = []
        for st in store.trace(run_id):
            doc = embargo(_clean(st.model_dump(mode="json")))
            steps.append(doc)
            out = st.output if isinstance(st.output, dict) else {}
            for key in ("certificate", "replica_certificate"):
                if isinstance(out.get(key), dict):
                    certs.append(_cert_summary(store, run_id, st.index, key, out[key], cert_blobs))
        put(f"traces/{run_id}.json", {"run": {"id": run.id, "harness_hash": run.harness_hash, "task_id": run.task_id,
                                              "seed": run.seed, "status": status.value, "detail": detail},
                                      "steps": steps})
    put("scores.json", scores)
    put("certificates.json", certs)
    lineage = store.records("lineage")
    put("lineage.json", [lineage_view(r) for r in lineage])
    put("ledger.json", [proposer_view(r, evolve_ids) for r in store.records("harness_edit")])
    put("generation_stats.json", [stats_view(r) for r in store.records("generation_stats")])
    # candidates: every spec named by the lineage or run by an exported run, with its parent and edits
    cands: dict[str, dict[str, Any]] = {h: {"hash": h, "parent": None, "edits": []} for h in spec_hashes}
    for r in lineage:
        parent = r.get("parent")
        if isinstance(parent, str):
            cands.setdefault(parent, {"hash": parent, "parent": None, "edits": []})
        h = r.get("candidate")
        if isinstance(h, str):
            c = cands.setdefault(h, {"hash": h, "parent": None, "edits": []})
            c["parent"] = parent
            if isinstance(r.get("edit"), dict) and r["edit"] not in c["edits"]:
                c["edits"].append(r["edit"])
    for h, c in sorted(cands.items()):
        if not _HEX.match(h):
            continue  # not a content hash: never used as a file name
        try:
            c["spec"] = store.harness_spec(h)
        except (OSError, KeyError, ValueError):
            c["spec"] = None
        put(f"candidates/{h}.json", _clean(c, ()))
    put("manifest.json", {"format": FORMAT, "policy": POLICY, "n_runs": len(scores),
                          "files": sorted([*written, "manifest.json"])})
    return sorted(written)


def _cert_summary(store: EvidenceStore, run_id: str, index: int, key: str, info: dict[str, Any],
                  cert_blobs: dict[str, Any]) -> dict[str, Any]:
    """Label, failed checks and their reasons. The full certificate (claim, evidence ids, config) stays out."""
    out: dict[str, Any] = {"run_id": run_id, "step": index, "key": key, "hash": info.get("hash"),
                           "label": info.get("label"), "failed": [], "reasons": {}}
    blob = cert_blobs.get(str(info.get("hash")))
    if blob:
        try:
            cert = store.blobs.get_json(blob)
        except (OSError, ValueError):
            cert = None
        if isinstance(cert, dict) and isinstance(cert.get("checks"), dict):
            out["label"] = cert.get("label", out["label"])
            failed = {k: v for k, v in cert["checks"].items() if isinstance(v, dict) and v.get("status") == "fail"}
            out["failed"] = sorted(failed)
            # reasons are free text that quotes held-out statistics (CI bounds, means): embargoed (§16.5.3)
            out["reasons"] = {k: EMBARGOED for k in sorted(failed)}
    return out


# ---------------------------------------------------------------------------------------------- audit
def _private_values(obj: Any) -> list[str]:
    if not isinstance(obj, dict):
        return []
    return [str(v) for k, v in obj.items() if str(k).startswith("_private") and v is not None and len(str(v)) >= 8]


def _fingerprints(store: EvidenceStore, tasks: list[SuiteTask], exported: dict[str, RunOutcome],
                  canaries: Iterable[str]) -> dict[str, Any]:
    hashes: dict[str, str] = {}
    contents: list[str] = []
    windows: list[str] = []
    paths: list[str] = []
    found: list[str] = [c for c in canaries if c]
    for t in tasks:
        pdir = Path(t.task_dir) / "private"
        if not pdir.is_dir():
            continue
        for variant in {str(pdir), pdir.as_posix(), str(pdir.resolve()), pdir.resolve().as_posix()}:
            paths.append(variant.replace("\\", "/").lower())
        for p in sorted(pdir.rglob("*")):
            if not p.is_file():
                continue
            data = p.read_bytes()
            hashes[hashlib.sha256(data).hexdigest()] = f"{t.task_id}/private/{p.relative_to(pdir).as_posix()}"
            if len(data) > _MAX_PRIVATE_SCAN:
                continue
            text = data.decode("utf-8", errors="ignore")
            found += [tok for tok in text.split() if tok.startswith(CANARY_PREFIX)]
            if len(text.strip()) >= 16:
                contents.append(text.strip())
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            windows += label_windows(lines[1:])  # a private CSV: header, then one label per line
    secrets_: list[str] = []
    for run_id in exported:
        if not run_id:
            continue
        refs = {s.checkpoint_ref for s in store.trace(run_id) if s.checkpoint_ref}
        inputs_ref = store.get_run(run_id).inputs_ref
        for ref in sorted(refs) + ([inputs_ref] if inputs_ref else []):
            try:
                doc = store.blobs.get_json(ref)
            except (OSError, ValueError):
                continue
            state = doc.get("state", doc) if isinstance(doc, dict) else {}
            secrets_ += _private_values(state)
    return {"hashes": hashes, "contents": contents, "windows": windows, "paths": sorted(set(paths)),
            "canaries": sorted(set(found)), "secrets": sorted(set(secrets_) - set(found), key=len, reverse=True)}


def _strings(obj: Any, keys: list[str]) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            keys.append(str(k))
            yield str(k)
            yield from _strings(v, keys)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, keys)


def _audit(root: Path, fp: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() in fp["hashes"]:
            problems.append(f"{rel} is a copy of private file {fp['hashes'][hashlib.sha256(data).hexdigest()]}")
        raw = data.decode("utf-8", errors="replace")
        keys: list[str] = []
        try:
            texts = [raw, *_strings(json.loads(raw), keys)]
        except json.JSONDecodeError:
            texts = [raw]
            problems.append(f"{rel} is not valid JSON (fail closed)")
        if any(k.startswith("_private") for k in keys):
            problems.append(f"{rel} contains a `_private*` key")
        for text in texts:
            problems += scan_text(text, rel, fp["canaries"], fp["windows"])
            if any(s in text for s in fp["secrets"]):
                problems.append(f"a private run-state value found in {rel}")
            if any(c in text for c in fp["contents"]):
                problems.append(f"verbatim content of a task's private file found in {rel}")
            norm = text.replace("\\", "/").lower()
            if any(pp in norm for pp in fp["paths"]):
                problems.append(f"a task's private directory path found in {rel}")
    return list(dict.fromkeys(problems))


def _make_read_only(root: Path) -> None:
    for p in sorted(root.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        if p.is_file():
            os.chmod(p, stat.S_IREAD)
    for p in sorted([*root.rglob("*"), root], key=lambda q: len(q.parts), reverse=True):
        if p.is_dir() and os.name != "nt":  # Windows ignores the read-only attribute on directories
            os.chmod(p, stat.S_IREAD | stat.S_IEXEC)
