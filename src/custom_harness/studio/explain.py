"""`chh explain CERT`: explain a certificate entirely from the store (GOAL §5 L9; invariant 8).

The explanation is a tree whose every leaf is a FACT: {"value": v, "src": {"artifact", "ref", "path"}, "label"?}.
`src` names the stored artifact the value was read from (a ledger entry, a blob, an evidence row, the findings
register, a code table) or the Kernel computation that re-derives it (verify_status, the prediction score), and the
path inside it. `resolve(kernel, src)` reads (or recomputes) that value again, so a reader or a test can check every
value, number or not, against its artifact (tests/test_studio.py does this for a scripted run). Keys of non-fact
nodes are schema words only (no data in keys).

Contents: the status (valid / stale / forged, via Kernel.verify_status, plus retractions), every check K0-K9 with its
closed reason codes, reasons and observations, the evidence chain claim -> pre-registration -> label reads ->
evaluation records -> certificate, the adapters it relied on, its assumptions (untested ones separately, including
every open loophole of the limitations register that applies), the releases on its targets, and its prediction
score. A certificate whose stored content does not match its ledger entry is shown as FORGED with the lookup error.

Not read-only: verify_status re-runs the checks, and the label reads a re-derivation makes are logged in the ledger
(a real event, kernel/ledger.py AsOfLedger). With `verify=False` nothing is re-derived and the status is UNVERIFIED.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any

from ..kernel import Kernel, KernelError
from ..kernel.certificate import CHECK_IDS, CHECK_NAMES
from ..kernel.predictions import score_predictions
from . import limitations

EXEC_FIELDS = ("arm", "seed", "target", "metric", "score", "run_id", "artifact_hash", "claim_hash", "artifact_id",
               "hash", "replayed")
READ_FIELDS = ("dataset_id", "file", "private", "purpose", "arm", "seed")
CODE_TABLES = {"CHECK_NAMES": CHECK_NAMES}


class ExplainError(LookupError):
    pass


def _src(artifact: str, ref: str | int, *path: str | int, key: bool = False) -> dict[str, Any]:
    s: dict[str, Any] = {"artifact": artifact, "ref": ref, "path": list(path)}
    if key:
        s["key"] = True  # the value is the last path element itself (a key present in the artifact)
    return s


def _fact(value: Any, src: dict[str, Any], label: str | None = None) -> dict[str, Any]:
    f: dict[str, Any] = {"value": value, "src": src}
    if label:
        f["label"] = label
    return f


def _walk(obj: Any, path: list[str | int]) -> Any:
    for p in path:
        obj = obj[p]
    return obj


class Resolver:
    """Reads (or recomputes) the value a fact's `src` names. Recomputations are cached per resolver."""

    def __init__(self, kernel: Kernel, findings_path: Any = None):
        self.kernel, self.findings_path = kernel, findings_path
        self._verify: dict[str, Any] = {}
        self._scores: dict[str, Any] | None = None

    def artifact(self, artifact: str, ref: Any) -> Any:
        k = self.kernel
        if artifact == "ledger":
            e = next((e for e in k.ledger.entries() if e.seq == int(ref)), None)
            if e is None:
                raise ExplainError(f"no ledger entry {ref}")
            return {"seq": e.seq, "kind": e.kind, "body": e.body, "prev": e.prev, "hash": e.hash, "at": e.at}
        if artifact == "blob":
            return k.store.blobs.get_json(str(ref))
        if artifact == "evidence":
            return k.store.get_evidence(str(ref)).model_dump(mode="json")
        if artifact == "verify":
            if ref not in self._verify:
                self._verify[ref] = asdict(k.verify_status(str(ref)))
            return self._verify[ref]
        if artifact == "lookup":
            try:
                k.certificate(str(ref))
                return {"status": "found", "error": None}
            except (KernelError, OSError, ValueError) as e:
                return {"status": "forged", "error": f"{type(e).__name__}: {e}"}
        if artifact == "predictions":
            if self._scores is None:
                try:
                    self._scores = {s.claim_hash: asdict(s) for s in score_predictions(k.ledger, time.time())}
                except (KeyError, TypeError, ValueError) as e:  # a malformed certificate entry breaks the scorer
                    self._scores = {"": {"error": f"prediction ledger unreadable ({type(e).__name__}: {e})"}}
            return self._scores.get(str(ref)) or self._scores[""]
        if artifact == "findings":
            fs = limitations.findings(self.findings_path) or ()
            return next(f for f in fs if str(f.get("id")) == str(ref))
        if artifact == "limitations":
            return {lim.finding_id: lim.to_json() for lim in limitations.register(k, self.findings_path)}[str(ref)]
        if artifact == "code":
            return CODE_TABLES[str(ref)]
        raise ExplainError(f"unknown artifact kind {artifact!r}")

    def resolve(self, src: dict[str, Any]) -> Any:
        path = list(src["path"])
        if src.get("key"):
            container = _walk(self.artifact(src["artifact"], src["ref"]), path[:-1])
            if path[-1] not in container:
                raise ExplainError(f"{path[-1]!r} is not a key of {src}")
            return path[-1]
        return _walk(self.artifact(src["artifact"], src["ref"]), path)


def resolve(kernel: Kernel, src: dict[str, Any]) -> Any:
    return Resolver(kernel).resolve(src)


def _scalars(obj: Any, artifact: str, ref: Any, path: list[str | int], label: str) -> list[dict[str, Any]]:
    """A fact for every scalar leaf under obj (its full path is in the src)."""
    if isinstance(obj, dict):
        return [f for k in sorted(obj) for f in _scalars(obj[k], artifact, ref, [*path, k], label)]
    if isinstance(obj, list | tuple):
        return [f for i, v in enumerate(obj) for f in _scalars(v, artifact, ref, [*path, i], label)]
    return [_fact(obj, _src(artifact, ref, *path), label)]


def _find_cert_entry(kernel: Kernel, cert_hash: str) -> Any:
    hits = [e for e in kernel.ledger.entries("certificate") if e.body.get("cert_hash", "").startswith(cert_hash)]
    if not hits:
        raise ExplainError(f"no certificate {cert_hash!r} in this store's ledger")
    if len({e.body["cert_hash"] for e in hits}) > 1:
        raise ExplainError(f"{cert_hash!r} is ambiguous: give more of the hash")
    return hits[-1]


def explain(kernel: Kernel, cert_hash: str, *, verify: bool = True, findings_path: Any = None) -> dict[str, Any]:
    entry = _find_cert_entry(kernel, cert_hash)
    h, blob, seq = entry.body["cert_hash"], entry.body["blob"], entry.seq
    head = {"certificate": _fact(h, _src("ledger", seq, "body", "cert_hash"), "certificate"),
            "ledger_seq": _fact(seq, _src("ledger", seq, "seq"), "ledger seq"),
            "recorded_label": _fact(entry.body.get("label"), _src("ledger", seq, "body", "label"), "label recorded")}
    lookup = Resolver(kernel, findings_path).artifact("lookup", h)
    if lookup["status"] == "forged":  # the stored content does not match the ledger entry: nothing else is trusted
        return {**head, "status": {"verdict": _fact("forged", _src("lookup", h, "status"), "status"),
                                   "problems": [_fact(lookup["error"], _src("lookup", h, "error"), "problem")]}}
    cert = kernel.certificate(h)
    doc_cert = kernel.store.blobs.get_json(blob)
    claim = doc_cert["claim"]
    out: dict[str, Any] = dict(head)

    # ---- status
    retractions = [e for e in kernel.ledger.entries("retraction") if e.body.get("cert_hash") == h]
    status: dict[str, Any] = {"retractions": [
        {"seq": _fact(e.seq, _src("ledger", e.seq, "seq"), "retraction"),
         "reason": _fact(e.body.get("reason"), _src("ledger", e.seq, "body", "reason"), "reason")}
        for e in retractions]}
    if verify:
        v = Resolver(kernel, findings_path).artifact("verify", h)
        status["verdict"] = _fact(v["status"], _src("verify", h, "status"), "status")
        status["problems"] = ([_fact(p, _src("verify", h, "forged", i), "forged") for i, p in enumerate(v["forged"])]
                              + [_fact(p, _src("verify", h, "stale", i), "stale") for i, p in enumerate(v["stale"])])
    else:
        # no verdict: the status is UNVERIFIED (nothing was re-derived), stated as such
        status["unverified"] = _fact(h, _src("ledger", seq, "body", "cert_hash"), "UNVERIFIED not re derived")
    out["status"] = status
    out["summary"] = {
        "label": _fact(doc_cert["label"], _src("blob", blob, "label"), "label"),
        "tier": _fact(doc_cert["tier"], _src("blob", blob, "tier"), "tier"),
        "kernel_version": _fact(doc_cert["kernel_version"], _src("blob", blob, "kernel_version"), "kernel"),
        "issued_at": _fact(doc_cert["issued_at"], _src("blob", blob, "issued_at"), "issued at"),
        "vg_ladder": _fact(doc_cert["vg_ladder"], _src("blob", blob, "vg_ladder"), "verification gap"),
        "self_reported": [_fact(t, _src("blob", blob, "self_reported", i), "self reported")
                          for i, t in enumerate(doc_cert.get("self_reported", []))]}

    # ---- checks
    checks = []
    for kid in CHECK_IDS:
        c = doc_cert["checks"][kid]
        checks.append({
            "check": _fact(kid, _src("blob", blob, "checks", kid, key=True), "check"),
            "name": _fact(CHECK_NAMES[kid], _src("code", "CHECK_NAMES", kid), "name"),
            "status": _fact(c["status"], _src("blob", blob, "checks", kid, "status"), "status"),
            "codes": [_fact(x, _src("blob", blob, "checks", kid, "codes", i), "code")
                      for i, x in enumerate(c.get("codes", []))],
            "reasons": [_fact(x, _src("blob", blob, "checks", kid, "reasons", i), "reason")
                        for i, x in enumerate(c.get("reasons", []))],
            "observations": [_fact(x, _src("blob", blob, "checks", kid, "observations", i), "observation")
                             for i, x in enumerate(c.get("observations", []))]})
    out["checks"] = checks

    # ---- evidence chain: claim -> prereg -> reads -> records -> certificate
    prereg = next((e for e in kernel.ledger.entries("prereg") if e.body.get("claim_hash") == cert.claim_hash), None)
    chain: dict[str, Any] = {"claim": _scalars(claim, "blob", blob, ["claim"], "claim")}
    if prereg is None:
        chain["prereg"] = []
    else:
        chain["prereg"] = [_fact(prereg.seq, _src("ledger", prereg.seq, "seq"), "prereg"),
                           _fact(prereg.body["claim_hash"], _src("ledger", prereg.seq, "body", "claim_hash"), "claim"),
                           *_scalars(prereg.body.get("fixed", {}), "ledger", prereg.seq, ["body", "fixed"], "fixed")]
    chain["reads"] = [[_fact(e.seq, _src("ledger", e.seq, "seq"), "read"),
                       *_scalars({k: e.body.get(k) for k in READ_FIELDS}, "ledger", e.seq, ["body"], "read")]
                      for e in kernel.ledger.entries("data_access") if e.body.get("claim_hash") == cert.claim_hash]
    records = []
    for i, ev_id in enumerate(doc_cert["evidence_ids"]):
        row = [_fact(ev_id, _src("blob", blob, "evidence_ids", i), "evidence")]
        try:
            ev = kernel.store.get_evidence(ev_id).model_dump(mode="json")
        except KeyError:
            row.append(_fact(None, _src("blob", blob, "evidence_ids", i), "evidence missing from the store"))
            records.append(row)
            continue
        row += [_fact(ev[k], _src("evidence", ev_id, k), k) for k in ("kind", "tier", "verifier_id",
                                                                       "produced_by_run", "payload_ref")]
        try:
            payload = kernel.store.blobs.get_json(ev["payload_ref"])
        except (OSError, ValueError):
            payload = {}
        row += [_fact(payload[k], _src("blob", ev["payload_ref"], k), k) for k in EXEC_FIELDS if k in payload]
        records.append(row)
    chain["records"] = records
    chain["certificate"] = [_fact(seq, _src("ledger", seq, "seq"), "certificate"),
                            _fact(doc_cert["prereg_seq"], _src("blob", blob, "prereg_seq"), "prereg seq"),
                            *_scalars(doc_cert["resolution"], "blob", blob, ["resolution"], "resolution")]
    out["evidence_chain"] = chain

    # ---- adapters, assumptions, limitations, releases, prediction
    out["adapters"] = [_scalars(a, "blob", blob, ["adapters", i], "adapter")
                       for i, a in enumerate(doc_cert.get("adapters", []))]
    assumptions = doc_cert["assumptions"]
    out["assumptions"] = {
        "untested": [_fact(a["text"], _src("blob", blob, "assumptions", i, "text"), "untested")
                     for i, a in enumerate(assumptions) if a.get("tested_by") is None],
        "tested": [{"text": _fact(a["text"], _src("blob", blob, "assumptions", i, "text"), "assumption"),
                    "tested_by": _fact(a["tested_by"], _src("blob", blob, "assumptions", i, "tested_by"), "tested by")}
                   for i, a in enumerate(assumptions) if a.get("tested_by") is not None],
        "open_loopholes": [{
            "finding": _fact(lim.finding_id, _src("limitations", lim.finding_id, "finding_id"), "open loophole"),
            "assumption": _fact(lim.assumption, _src("limitations", lim.finding_id, "assumption"), "untested")}
            for lim in limitations.for_certificate(kernel, h, findings_path)]}
    targets = list(claim.get("generalization_targets", []))
    releases = []
    for t in targets:
        try:
            rel = kernel.releases(t)
        except Exception:  # noqa: BLE001  an unregistered target: no releases can be listed (shown, not hidden)
            rel = []
        for e in rel:
            releases.append([_fact(e.seq, _src("ledger", e.seq, "seq"), "release"),
                             *_scalars({k: e.body.get(k) for k in ("dataset_id", "channel", "alphabet", "note")},
                                       "ledger", e.seq, ["body"], "release")])
    out["releases"] = releases
    scores = Resolver(kernel).artifact("predictions", cert.claim_hash) if prereg is not None else None
    labels = {"q": "declared probability q", "p0": "baseline level p zero", "outcome": "outcome",
              "expired": "expired", "info_gain_bits": "information gain bits", "skill_bits": "skill bits"}
    if scores is not None and "error" in scores:
        out["prediction"] = [_fact(scores["error"], _src("predictions", cert.claim_hash, "error"), "unavailable")]
    else:
        out["prediction"] = [] if scores is None else [
            _fact(scores[k], _src("predictions", cert.claim_hash, k), label) for k, label in labels.items()]
    return out


# ---------------------------------------------------------------- rendering
def iter_facts(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict) and "src" in node and "value" in node:
        return [node]
    if isinstance(node, dict):
        return [f for v in node.values() for f in iter_facts(v)]
    if isinstance(node, list):
        return [f for v in node for f in iter_facts(v)]
    return []


def fact_line(f: dict[str, Any], indent: str = "  ") -> str:
    s = f["src"]
    tag = f"{s['artifact']}:{s['ref']}:{json.dumps(s['path'])}{'#key' if s.get('key') else ''}"
    return f"{indent}{f.get('label', '')} = {json.dumps(f['value'], ensure_ascii=False)}   <- {tag}"


def render_text(doc: dict[str, Any]) -> str:
    """Human-readable form: section headers (schema words) and one line per fact, each ending with its source."""
    lines: list[str] = []

    def walk(node: Any, title: str, depth: int) -> None:
        if isinstance(node, dict) and "src" in node and "value" in node:
            lines.append(fact_line(node, "  " * depth))
            return
        if isinstance(node, dict):
            if title:
                lines.append("  " * max(depth - 1, 0) + title.replace("_", " ").upper())
            for k, v in node.items():
                walk(v, k, depth + 1)
        elif isinstance(node, list):
            if title:
                lines.append("  " * max(depth - 1, 0) + title.replace("_", " "))
            for v in node:
                walk(v, "", depth + 1)

    walk(doc, "", 0)
    return "\n".join(lines)
