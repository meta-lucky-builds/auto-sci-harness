"""Claim Audit (`chh audit`, GOAL §16.10): pre-registered confirmation of someone else's published claim.

Order (each step refuses unless the previous one happened):
  approve-fidelity  HUMAN: the typed claim is faithful to the paper's sentence (gate.py)
  prepare           the arms are registered and the claim PRE-REGISTERED with the Kernel; only then are the pinned
                    fresh OpenML datasets read from the cache and registered (train split dev, held-out EXTERNAL)
  approve-role      HUMAN (`chh approve-role`, existing): each fresh dataset really is external data
  run               both arms, every seed and target, through the same wrapper under one contract in the candidate
                    sandbox; ALL programs run before any label is read (a crash then costs no look); the labels are
                    read through the vault; the Kernel certifies (K0-K9, with re-execution)
  verdict + report  computed ONLY from the certificate's fields (never from run state or a model):
                    CERTIFIED (the certificate) | NOT CONFIRMED (naming the failing check) | INCONCLUSIVE | COULD
                    NOT RUN (no certificate; infra, crash or data unavailable)
  reply, approve-publication, publish   the publication gate (gate.py)
"""

from __future__ import annotations

import csv
import hashlib
import io
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..errors import InfraError
from ..kernel import (
    DEFAULT_ARGV,
    Certificate,
    Claim,
    EvidenceClass,
    ExecRecord,
    InputRef,
    Kernel,
    Label,
    SandboxReplayer,
    adapters,
    run_program,
)
from ..kernel.certificate import CHECK_IDS, CHECK_NAMES
from ..kerneld.scope import ScopedKernel
from ..literature.cite import Citation as LitCitation
from ..literature.cite import Resolver, check_citations, i3_verdict
from ..records import Run, StepKind, TraceStep
from ..sandbox.base import Sandbox
from ..store import EvidenceStore
from ..store.run_bundle import write_run_bundle
from . import data, gate, wrapper
from .spec import LoadedSpec

SandboxFactory = Callable[[str], Sandbox]
RECORD_KIND = "audit"


class Verdict(StrEnum):
    CERTIFIED = "CERTIFIED"
    NOT_CONFIRMED = "NOT CONFIRMED"
    INCONCLUSIVE = "INCONCLUSIVE"
    COULD_NOT_RUN = "COULD NOT RUN"


# ------------------------------------------------------------------ verdict from certificate fields only
_K6_CRITERION, _K7_SAFE = "K6_CRITERION_NOT_MET", "K7_TRIVIALLY_SAFE"


def _only(cert: Certificate, kid: str, code: str, text: str) -> bool:
    """True iff every reason of the failed check `kid` is the given kind (closed codes when the Kernel records
    them, §16.5.8; else the reason text). Anything unrecognised counts as a real failure."""
    res = cert.checks[kid]
    codes = tuple(str(c) for c in getattr(res, "codes", ()) or ())
    if codes:
        return all(c == code for c in codes)
    return bool(res.reasons) and all(text in r for r in res.reasons)


def verdict(cert: Certificate) -> tuple[Verdict, list[str]]:
    """(verdict, the checks it names). A certificate is CERTIFIED. A rejection whose only failures are a missed
    effect criterion (K6) and/or a too-weak claim (K7) is INCONCLUSIVE unless some target's upper bound lies below
    the claimed minimum effect (then the data rule the claim out: NOT CONFIRMED, K6). Any other failed check is NOT
    CONFIRMED naming it."""
    if cert.label != Label.REJECTED:
        return Verdict.CERTIFIED, []
    failed = cert.failed()
    soft = {k for k in failed if (k == "K6" and _only(cert, "K6", _K6_CRITERION, "the claim's criterion was not met"))
            or (k == "K7" and _only(cert, "K7", _K7_SAFE, "trivially safe claim"))}
    hard = [k for k in failed if k not in soft]
    if hard:
        return Verdict.NOT_CONFIRMED, hard
    delta = float(cert.claim["effect"]["min_effect"])
    targets = cert.resolution.get("targets") or {}
    ruled_out = [t for t, s in targets.items() if not s.get("met") and s.get("hi") is not None and s["hi"] < delta]
    if ruled_out:
        return Verdict.NOT_CONFIRMED, ["K6"]
    return Verdict.INCONCLUSIVE, sorted(soft)


def render_report(cert: Certificate) -> str:
    """The audit report, a pure function of the certificate (same certificate, same bytes)."""
    v, named = verdict(cert)
    cl = cert.claim
    cond = cl["scope"]["conditions"]
    delta = cl["effect"]["min_effect"]
    lines = [f"# Claim Audit {cond.get('audit_id', '?')}: {v.value}", "",
             f"Paper: {cond.get('paper_title', '')} (doi {cond.get('paper_doi', '')})",
             f"Audited sentence: \"{cond.get('paper_sentence', '')}\"",
             f"Paper's own datasets: {cond.get('paper_datasets', '') or '(not stated)'}; paper's reported effect: "
             f"{cond.get('paper_reported_effect', '') or '(not stated)'}",
             f"Typed claim: method beats baseline on {cl['metric']['id']} by >= {delta} on every fresh dataset "
             f"(protocol: {cond.get('protocol', '')}; budget {cond.get('budget_timeout_s', '')} s per run; seeds "
             f"{cl['analysis_plan']['seeds']}; wrapper sha256 {cond.get('wrapper_sha256', '')[:16]})", ""]
    if v == Verdict.CERTIFIED:
        lines.append(f"Verdict: CERTIFIED ({cert.label.value}) under this protocol.")
    elif v == Verdict.NOT_CONFIRMED:
        lines.append("Verdict: NOT CONFIRMED under this protocol; failing check(s): "
                     + ", ".join(f"{k} ({CHECK_NAMES[k]})" for k in named) + ".")
    else:
        lines.append("Verdict: INCONCLUSIVE under this protocol: the data neither confirm nor rule out the claimed "
                     "effect (" + ", ".join(f"{k} ({CHECK_NAMES[k]})" for k in named) + ").")
    lines += ["", "This is a statement about the claim under the protocol above, on these fresh datasets; it is not a "
              "statement about the authors. A non-confirmation on new data can mean a narrower scope than claimed.",
              "", "## Effect per fresh dataset", ""]
    for t, s in sorted((cert.resolution.get("targets") or {}).items()):
        mark = "met" if s.get("met") else "not met"
        below = s.get("hi") is not None and s["hi"] < delta
        lines.append(f"- {t}: estimate {s.get('estimate'):+.4g}, interval [{s.get('lo'):+.4g}, {s.get('hi'):+.4g}]"
                     f", criterion >= {delta} {mark}" + (" (upper bound below the claimed effect)" if below else ""))
    lines += ["", "## Kernel checks", ""]
    for k in CHECK_IDS:
        r = cert.checks[k]
        lines.append(f"- {k} {CHECK_NAMES[k]}: {r.status}" + (f": {'; '.join(r.reasons)}" if r.status == "fail"
                                                               else ""))
    lines += ["", "## Assumptions", ""]
    lines += [f"- {a.text} ({'tested by ' + a.tested_by if a.tested_by else 'untested'})" for a in cert.assumptions]
    lines += ["", f"Certificate {cert.cert_hash()} (label {cert.label.value}, kernel {cert.kernel_version}); "
              f"{cert.vg_ladder}.", ""]
    return "\n".join(lines)


def render_could_not_run(audit_id: str, claim_hash: str, reason: str, needs_owner: bool) -> str:
    return "\n".join([f"# Claim Audit {audit_id}: COULD NOT RUN", "",
                      f"No certificate was issued for claim {claim_hash}. Reason: {reason}",
                      "Needs an owner decision." if needs_owner else "", ""])


# ------------------------------------------------------------------ steps
def _outcomes(store: EvidenceStore, claim_hash: str) -> list[dict[str, Any]]:
    return store.records_where(RECORD_KIND, "claim_hash", claim_hash)


def latest_outcome(store: EvidenceStore, audit_id: str) -> dict[str, Any] | None:
    rows = store.records_where(RECORD_KIND, "audit_id", audit_id)
    return rows[-1] if rows else None


def _record(store: EvidenceStore, outcome: dict[str, Any]) -> dict[str, Any]:
    store.append_record(RECORD_KIND, outcome)
    return outcome


def prepare(store: EvidenceStore, loaded: LoadedSpec, cache: data.OpenMLCache,
            fetcher: data.Fetcher | None = None) -> dict[str, Any]:
    """Register the arms, pre-register the claim, THEN read and register the fresh datasets. Returns what the
    human must approve next (the datasets' external role)."""
    kernel = ScopedKernel(store)
    claim = loaded.claim()
    ch = claim.claim_hash()
    if gate.fidelity_approval(store, loaded.audit_id, loaded.spec_hash, ch, loaded.spec.paper.sentence) is None:
        raise gate.AuditRefused("no human fidelity approval for this exact spec (`chh audit approve-fidelity`)")
    _, why = adapters.resolve(kernel.ledger, EvidenceClass.DATA, claim.metric.id)
    if why:
        raise gate.AuditRefused("the ML adapter is not approved for this metric: " + "; ".join(why))
    mid, bid = loaded.artifact_ids()
    kernel.vault.register_artifact(mid, loaded.method_code, "method")
    kernel.vault.register_artifact(bid, loaded.baseline_code, "baseline")
    prereg = kernel.preregister(claim)
    out: dict[str, Any] = {"audit_id": loaded.audit_id, "claim_hash": ch, "prereg_seq": prereg.seq, "datasets": []}
    for ds in loaded.spec.datasets:
        try:
            sp = _ingest(store, loaded.audit_id, ch, ds, cache, fetcher)
        except (data.DataUnavailable, ValueError) as e:
            needs = isinstance(e, data.DataUnavailable) and e.needs_owner
            out["could_not_run"] = _record(store, {
                "audit_id": loaded.audit_id, "claim_hash": ch, "verdict": Verdict.COULD_NOT_RUN.value,
                "reason": str(e), "needs_owner": needs, "cert_hash": None,
                "report_hash": hashlib.sha256(render_could_not_run(loaded.audit_id, ch, str(e), needs).encode())
                .hexdigest(), "at": time.time()})
            return out
        out["datasets"].append({"dataset_id": ds.dataset_id, "n_train": sp["n_train"], "n_test": sp["n_test"],
                                "role_approved": kernel.vault.role_approval(ds.dataset_id) is not None,
                                "approve": f"chh approve-role {ds.dataset_id} --approver <you>"})
    return out


def _ingest(store: EvidenceStore, audit_id: str, claim_hash: str, ds: Any, cache: data.OpenMLCache,
            fetcher: data.Fetcher | None) -> dict[str, int]:
    """The fresh dataset, split and registered BY KERNELD (kerneld/ingest.py, GOAL §16.5.2): this process only makes
    sure the pinned file is in the local cache (an owner-approved fetch, if any); kerneld re-reads it, checks the pin,
    splits it and registers the held-out labels. No held-out label of the split enters this process. kerneld
    unavailable -> InfraError (no fallback)."""
    from ..kerneld.client import KernelD, KernelDError
    path = cache.ensure_path(ds, fetcher)
    try:
        with KernelD(store.root) as kd:
            run = kd.open_run(f"audit-prepare-{audit_id}")
            try:
                return run.ingest_openml_split(claim_hash, path.parent, ds.model_dump(mode="json"))
            finally:
                run.close()
    except KernelDError as e:
        if e.code == "not_registered":
            raise data.DataUnavailable("the audit claim is not pre-registered: fresh data are not touched before "
                                       "it is") from None
        if e.code == "not_found":
            raise data.DataUnavailable(f"openml:{ds.openml_id} is not in the local cache or does not match its pin; "
                                       f"downloading it is an owner decision (`chh audit fetch --allow-download`)",
                                       needs_owner=True) from None
        raise data.DataUnavailable(f"openml:{ds.openml_id}: kerneld refused the pinned data ({e.code}: hash, format, "
                                   f"size or an earlier registration)") from None


def _check_citations(loaded: LoadedSpec, resolver: Resolver | None) -> dict[str, Any] | None:
    cits = loaded.spec.citations
    if not cits:
        return None
    if resolver is None:
        return {"I3": "not checked", "reason": "the spec lists citations but no literature snapshot was given"}
    lit = [LitCitation(key=c.key, id=c.id, title=c.title, year=c.year, authors=list(c.authors), quotes=list(c.quotes))
           for c in cits]
    # provenance: these citations come from the paper's reference list (in the human-approved spec), not from a
    # search, so each cited paper's resolved ids count as "retrieved" for the retrieval check
    ids: set[str] = set()
    for c in lit:
        if c.id:
            lr = resolver.lookup(c.id)
            ids |= {lr.identifier} | (lr.record.ids() if lr.record is not None else set())
    return i3_verdict(check_citations(lit, resolver, ids))


def run(store: EvidenceStore, loaded: LoadedSpec, sandbox_factory: SandboxFactory, *,
        resolver: Resolver | None = None) -> dict[str, Any]:
    """Run both arms, certify, and record the verdict. Refuses (no label read) unless prepared and role-approved,
    and refuses a second run of the same claim (one look per claim)."""
    claim = loaded.claim()
    ch = claim.claim_hash()
    kernel = ScopedKernel(store, replayer=SandboxReplayer(sandbox_factory))
    vault = kernel.vault
    if gate.fidelity_approval(store, loaded.audit_id, loaded.spec_hash, ch, loaded.spec.paper.sentence) is None:
        raise gate.AuditRefused("no human fidelity approval for this exact spec")
    if not vault.registered(ch):
        raise gate.AuditRefused("the audit claim is not pre-registered: run `chh audit prepare` first")
    if any(o.get("verdict") != Verdict.COULD_NOT_RUN.value or o.get("labels_read") for o in _outcomes(store, ch)) \
            or any(a.body.get("claim_hash") == ch and a.body.get("purpose") == "evaluate" for a in vault.accesses()):
        raise gate.AuditRefused("this claim was already evaluated: one look per claim (write a new spec)")
    known = vault.datasets()
    for ds in loaded.spec.datasets:
        if ds.dataset_id not in known:
            raise gate.AuditRefused(f"{ds.dataset_id} is not registered: run `chh audit prepare` first")
        if vault.role_approval(ds.dataset_id) is None:
            raise gate.AuditRefused(f"{ds.dataset_id}: its external role is not approved by a human "
                                    f"(`chh approve-role {ds.dataset_id} --approver <you>`)")
    seeds = claim.analysis_plan.seeds
    timeout = loaded.spec.budget.timeout_s
    limits = {"timeout_s": float(timeout)}
    env = {"sandbox": "audit candidate sandbox", "audit_wrapper": wrapper.wrapper_hash()}
    arms = {"subject": (claim.subject.hash, loaded.method_code), "comparator": (claim.comparator.hash,
                                                                                loaded.baseline_code)}

    def could_not_run(reason: str, needs_owner: bool = False, labels_read: bool = False) -> dict[str, Any]:
        report = render_could_not_run(loaded.audit_id, ch, reason, needs_owner)
        return _record(store, {"audit_id": loaded.audit_id, "claim_hash": ch, "verdict": Verdict.COULD_NOT_RUN.value,
                               "reason": reason, "needs_owner": needs_owner, "cert_hash": None,
                               "labels_read": labels_read,
                               "report_hash": hashlib.sha256(report.encode()).hexdigest(), "at": time.time()})

    # 1. every program runs before any label is read
    preds: dict[tuple[str, str, int], list[str]] = {}
    try:
        for ds in loaded.spec.datasets:
            train = vault.read_public(ds.train_id, "train.csv")
            test_x = vault.read_public(ds.dataset_id, "test_X.csv")
            n_test = len(list(csv.reader(io.StringIO(test_x.decode("utf-8"))))) - 1
            for seed in seeds:
                for arm, (_, code) in arms.items():
                    sb = sandbox_factory(f"audit-{loaded.audit_id}-{arm}-{ds.openml_id}-{seed}")
                    p, diag = run_program(sb, code=code, entrypoint="solution.py", argv=DEFAULT_ARGV,
                                          inputs={"train": ("train.csv", train), "test": ("test_X.csv", test_x)},
                                          seed=seed, timeout_s=timeout)
                    if p is None:
                        return could_not_run(f"the {arm} arm failed on {ds.dataset_id} seed {seed}: {diag[:400]}")
                    if len(p) != n_test:
                        return could_not_run(f"the {arm} arm wrote {len(p)} predictions for {n_test} rows")
                    preds[(arm, ds.dataset_id, seed)] = p
    except InfraError as e:
        return could_not_run(f"infrastructure: {e}", needs_owner=type(e).__name__ == "CandidateImageMissing")
    # 2. labels stay behind the kerneld boundary (GOAL §16.5.2): the kerneld process makes the one logged 'evaluate'
    # read per (arm, seed, target) and returns only the score; one evaluation record each
    from ..kerneld.client import KernelD
    try:
        with KernelD(store.root) as kd:
            ids = _evaluate_arms(store, kernel, kd, loaded, claim, arms, preds, env, limits)
    except InfraError as e:  # kerneld unavailable mid-way: some labels may have been read (one look per claim)
        return could_not_run(f"infrastructure: {e}", labels_read=True)
    # 3. the Kernel decides
    try:
        cert = kernel.certify(claim, ids)
    except InfraError as e:
        return could_not_run(f"infrastructure during certification: {e}", labels_read=True)
    report = render_report(cert)
    v, named = verdict(cert)
    return _record(store, {"audit_id": loaded.audit_id, "claim_hash": ch, "verdict": v.value, "checks": named,
                           "cert_hash": cert.cert_hash(), "label": cert.label.value, "labels_read": True,
                           "report_hash": hashlib.sha256(report.encode()).hexdigest(),
                           "citations": _check_citations(loaded, resolver), "at": time.time()})


def _evaluate_arms(store: EvidenceStore, kernel: Kernel, kd: Any, loaded: LoadedSpec, claim: Claim,
                   arms: dict[str, tuple[str, bytes]], preds: dict[tuple[str, str, int], list[str]],
                   env: dict[str, str], limits: dict[str, float]) -> list[str]:
    """One evaluation record per (target, seed, arm); each score comes from kerneld (no label enters this process)."""
    ch, vault, seeds = claim.claim_hash(), kernel.vault, claim.analysis_plan.seeds
    ids: list[str] = []
    for ds in loaded.spec.datasets:
        refs = {"train": InputRef(dataset_id=ds.train_id, file="train.csv",
                                  blob=vault.dataset(ds.train_id).files["train.csv"].blob),
                "test": InputRef(dataset_id=ds.dataset_id, file="test_X.csv",
                                 blob=vault.dataset(ds.dataset_id).files["test_X.csv"].blob)}
        for seed in seeds:
            for arm, (h, _) in arms.items():
                p = preds[(arm, ds.dataset_id, seed)]
                run_rec = Run(harness_id="chh-audit", harness_hash=wrapper.wrapper_spec_ref(store, "chh-audit"),
                              task_id=ds.dataset_id, seed=seed)
                store.add_run(run_rec)
                now = time.time()
                store.add_step(TraceStep(run_id=run_rec.id, index=0, node_id="audit_arm", kind=StepKind.TOOL,
                                         input={"arm": arm, "audit_id": loaded.audit_id},
                                         output={"note": "arm run through the audit contract wrapper"},
                                         started_at=now, ended_at=now))
                run = kd.open_run(run_rec.id)
                try:
                    score = run.held_out_score(ds.dataset_id, "test_y.csv", claim.metric.id, p, claim_hash=ch,
                                               arm=arm, seed=seed)
                finally:
                    run.close()
                rec = ExecRecord(claim_hash=ch, arm=arm, artifact_hash=h, argv=DEFAULT_ARGV, inputs=refs,  # type: ignore[arg-type]
                                 target=ds.dataset_id, label_file="test_y.csv", seed=seed, env=env, limits=limits,
                                 preds_ref=store.blobs.put_json(p), metric=claim.metric.id,
                                 score=score, run_id=run_rec.id)
                ids.append(kernel.record_evaluation(rec))
                # invariant 9: every run has a Reproducibility Bundle (s3-report finding: arm runs had none)
                write_run_bundle(store, run_rec.id, environment=env)
    return ids


def current_report(store: EvidenceStore, outcome: dict[str, Any]) -> str:
    """Re-render the report of a recorded outcome from the certificate (or, without one, the recorded reason)."""
    if outcome.get("cert_hash"):
        return render_report(ScopedKernel(store).certificate(outcome["cert_hash"]))
    return render_could_not_run(outcome["audit_id"], outcome["claim_hash"], outcome["reason"],
                                bool(outcome.get("needs_owner")))


def bundle(store: EvidenceStore, outcome: dict[str, Any], out: Path) -> Path:
    ScopedKernel(store).bundle(outcome["cert_hash"], Path(out))  # a grant for the claim's private files only
    return Path(out)
