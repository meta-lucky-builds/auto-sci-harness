"""Facts: what the scorecard reads from one campaign's Evidence store and its input files, each with its refs.

One campaign = one store. `extract` is the only function that reads the store; the axes are computed from its output
alone (axes.py, pure). `scorecard check` re-extracts from the store and requires the identical facts, so a number can
only be reproduced from the artifacts it names.

What counts (definitions, so the numbers can be audited):
  certificates  every Kernel `certificate` ledger entry; the latest per claim decides the claim's label; a retraction
                entry withdraws it. Certificates are counted as filed (content-hash checked by Kernel.certificate),
                NOT re-derived: `chh certificates` / Kernel.verify_status does the re-execution.
  store claims  every row of the store's claims table (records.Claim). EvidenceStore.add_claim checks that listed
                evidence exists, but accepts an EMPTY evidence list: such a claim is a claim without evidence.
  reports       every run whose final state holds a `report` (research.report). A report that names a certificate
                (`certificate`, `replica_certificate`) needs that certificate in the ledger, a complete checklist
                (kernel.checklist.missing == []) and a ledgered bundle (a `bundle` ledger entry with that cert hash and
                bundle hash). A report with no certificate makes no claim: it is listed as such, not counted.
  citations     every citation in a run state's `citations` (by key) and every citation result in a Claim Audit
                record; a citation is verifiable only if its check result says `verified` (a citation without a
                result is unverifiable: fail closed).
  leak audits   every run state `audit` with an I5 entry (True = the leak/canary scan passed).
  cost          every run's summed trace cost (EvidenceStore.run_cost).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from ..gateway.costs import run_cost_record
from ..kernel.checklist import missing as checklist_missing
from ..kernel.kernel import Kernel, KernelError
from ..records import Claim as StoreClaim
from ..store.evidence import EvidenceStore
from .refs import all_claim_rows, canonical, record_ref, run_ref, sha
from .runner import AS_REPORTED_KIND, RUN_KIND

CERTIFIED_PREFIX = "CERTIFIED-"
AUDIT_RECORD_KIND = "audit"  # audit/claim_audit.py RECORD_KIND


def _evidence_problems(store: EvidenceStore, ids: list[str] | tuple[str, ...]) -> list[str]:
    bad = []
    for e in ids:
        try:
            ev = store.get_evidence(e)
            store.blobs.get(ev.payload_ref)
        except (KeyError, OSError):
            bad.append(str(e))
    return bad


def _certificates(store: EvidenceStore, kernel: Kernel) -> list[dict[str, Any]]:
    entries = kernel.ledger.entries()
    retracted = {e.body.get("cert_hash") for e in entries if e.kind == "retraction"}
    bundles: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        if e.kind == "bundle":
            bundles.setdefault(str(e.body.get("cert_hash")), []).append(
                {"bundle_hash": e.body.get("bundle_hash"), "ref": f"ledger:{e.seq}:{e.hash}"})
    latest: dict[str, int] = {}
    for e in entries:
        if e.kind == "certificate":
            latest[str(e.body.get("claim_hash"))] = e.seq
    out = []
    for e in entries:
        if e.kind != "certificate":
            continue
        h = str(e.body.get("cert_hash"))
        try:
            cert = kernel.certificate(h)
            ids, label, problem = list(cert.evidence_ids), cert.label.value, None
        except (KernelError, OSError, ValueError) as ex:
            ids, label, problem = [], str(e.body.get("label")), f"certificate does not load: {type(ex).__name__}"
        out.append({"ref": f"ledger:{e.seq}:{e.hash}", "cert_hash": h, "claim_hash": e.body.get("claim_hash"),
                    "label": label, "latest_for_claim": latest.get(str(e.body.get("claim_hash"))) == e.seq,
                    "retracted": h in retracted, "n_evidence": len(ids),
                    "unresolved_evidence": _evidence_problems(store, ids), "load_problem": problem,
                    "bundles": bundles.get(h, [])})
    return out


def _store_claims(store: EvidenceStore) -> list[dict[str, Any]]:
    out = []
    for cid, body in all_claim_rows(store):
        doc = json.loads(body)
        claim = StoreClaim.model_validate(doc)
        out.append({"ref": f"claim:{cid}:{sha(doc)}", "id": cid, "n_evidence": len(claim.evidence_ids),
                    "unresolved_evidence": _evidence_problems(store, claim.evidence_ids)})
    return out


def _final_state(store: EvidenceStore, run_id: str) -> dict[str, Any] | None:
    steps = store.trace(run_id)
    for s in reversed(steps):
        if s.checkpoint_ref:
            try:
                cp = store.blobs.get_json(s.checkpoint_ref)
            except (OSError, ValueError):
                return None
            st = cp.get("state") if isinstance(cp, dict) else None
            return st if isinstance(st, dict) else None
    return None


def _run(store: EvidenceStore, kernel: Kernel, run_id: str, ledger_certs: dict[str, dict[str, Any]]
         ) -> dict[str, Any]:
    cost = store.run_cost(run_id)
    fact: dict[str, Any] = {"ref": run_ref(store, run_id), "run_id": run_id,
                            "cost": {"usd": cost.usd, "gpu_hours": cost.gpu_hours, "quota_units": cost.quota_units,
                                     "tokens": cost.prompt_tokens + cost.completion_tokens,
                                     "estimated": cost.estimated},
                            # priced through the gateway cost table: usd None unless every rate is real (axis G)
                            "cost_record": run_cost_record(store, run_id)}
    state = _final_state(store, run_id)
    if state is None:
        fact["state"] = None
        return fact
    fact["state"] = "checkpoint"
    if "report" in state:
        claims = []
        for p in ("", "replica_"):
            info = state.get(f"{p}certificate")
            if not isinstance(info, dict):
                continue
            h = str(info.get("hash"))
            cl = state.get(f"{p}checklist")
            bh = info.get("bundle_hash")
            cert = ledger_certs.get(h)
            ledgered = bool(cert) and any(b["bundle_hash"] == bh for b in cert["bundles"])  # type: ignore[index]
            claims.append({"prefix": p, "cert_hash": h, "in_ledger": cert is not None,
                           "label": cert["label"] if cert else info.get("label"),
                           "checklist_missing": checklist_missing(cl) if isinstance(cl, dict) else None,
                           "bundle_hash": bh, "bundle_ledgered": ledgered})
        fact["report"] = {"claims": claims}
    raw_audit = state.get("audit")
    audit: dict[str, Any] = raw_audit if isinstance(raw_audit, dict) else {}
    if "I5" in audit:
        fact["leak_audit_passed"] = audit["I5"] is True
    cites = state.get("citations")
    if isinstance(cites, list) and cites:
        results = ((audit.get("citations") or {}).get("results") or []) if isinstance(audit, dict) else []
        status = {str(r.get("key")): str(r.get("status")) for r in results if isinstance(r, dict)}
        fact["citations"] = [{"key": str(c.get("key")) if isinstance(c, dict) else repr(c),
                              "status": status.get(str(c.get("key")) if isinstance(c, dict) else "", "no result")}
                             for c in cites]
    return fact


def _input(path: Path | None, name: str, inputs_dir: Path | None) -> tuple[str, bytes] | None:
    if path is None:
        return None
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if inputs_dir is not None:
        inputs_dir.mkdir(parents=True, exist_ok=True)
        (inputs_dir / digest).write_bytes(data)
    return f"input:{digest}:{name}", data


def parse_museum_log(text: str) -> list[dict[str, str]]:
    """Lines `VERDICT   ID: detail` as printed by scripts/museum.py (KILLED / SURVIVED / BROKEN)."""
    out = []
    for ln in text.splitlines():
        parts = ln.split(None, 1)
        if len(parts) == 2 and parts[0] in ("KILLED", "SURVIVED", "BROKEN") and ":" in parts[1]:
            out.append({"finding": parts[1].split(":", 1)[0].strip(), "verdict": parts[0]})
    return out


def extract(store: EvidenceStore, *, museum_log: Path | None = None, findings: Path | None = None,
            salts_audit: Path | None = None, registry: Path | None = None, capabilities: Path | None = None,
            timesplit: Path | None = None,
            inputs_dir: Path | None = None) -> dict[str, Any]:
    kernel = Kernel(store)
    certs = _certificates(store, kernel)
    by_hash = {c["cert_hash"]: c for c in certs}
    runs = [_run(store, kernel, r.id, by_hash) for r in reversed(store.list_runs(limit=2**62))]
    facts: dict[str, Any] = {
        "certificates": certs, "store_claims": _store_claims(store), "runs": runs,
        "scorecard_runs": [{"ref": record_ref(RUN_KIND, b), **b} for b in store.records(RUN_KIND)],
        "as_reported": [{"ref": record_ref(AS_REPORTED_KIND, b), **b} for b in store.records(AS_REPORTED_KIND)],
        "claim_audit_citations": [
            {"ref": record_ref(AUDIT_RECORD_KIND, b), "key": str(r.get("key")), "status": str(r.get("status"))}
            for b in store.records(AUDIT_RECORD_KIND) if isinstance(b.get("citations"), dict)
            for r in (b["citations"].get("results") or []) if isinstance(r, dict)],
        "ledger_intact": not kernel.ledger.verify(),
        "inputs": {},
    }
    for r in facts["scorecard_runs"]:
        r["scores"] = store.blobs.get_json(r["results_blob"])
    m = _input(museum_log, "museum_log", inputs_dir)
    if m:
        facts["inputs"]["museum"] = {"ref": m[0], "outcomes": parse_museum_log(m[1].decode("utf-8"))}
    f = _input(findings, "findings", inputs_dir)
    if f:
        doc = yaml.safe_load(f[1].decode("utf-8")) or {}
        facts["inputs"]["findings"] = {"ref": f[0], "statuses": {str(x.get("id")): str(x.get("status"))
                                                                 for x in doc.get("findings") or []}}
    s = _input(salts_audit, "salts_audit", inputs_dir)
    if s:
        facts["inputs"]["salts"] = {"ref": s[0], "audit": json.loads(s[1].decode("utf-8"))}
    g = _input(registry, "registry", inputs_dir)
    if g:
        rows = (yaml.safe_load(g[1].decode("utf-8")) or {}).get("benchmarks") or []
        facts["inputs"]["registry"] = {"ref": g[0], "benchmarks": [
            {"key": f"{b.get('name')}@{b.get('version')}", "axis": str(b.get("axis"))} for b in rows]}
    k = _input(capabilities, "capabilities", inputs_dir)
    if k:
        rows = (yaml.safe_load(k[1].decode("utf-8")) or {}).get("capabilities") or []
        facts["inputs"]["capabilities"] = {"ref": k[0], "capabilities": [
            {"id": str(c.get("id")), "ablation": c.get("ablation")} for c in rows]}
    t = _input(timesplit, "timesplit", inputs_dir)
    if t:
        facts["inputs"]["timesplit"] = {"ref": t[0], "comparison": json.loads(t[1].decode("utf-8"))}
    json.loads(canonical(facts))  # facts must be plain JSON (they are hashed and compared)
    return facts
