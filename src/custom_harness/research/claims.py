"""Kernel-integrated end of the research loop (GOAL §4, §5 L4 steps 6 and 10; M2).

    claim    -> deterministic code (no LLM) turns the best dev candidate into a typed Claim with a forecast,
                and pre-registers it BEFORE any held-out label is read
    confirm  -> both arms run on the held-out target, every seed in a fresh sandbox; labels are read through
                the vault (logged), one ExecRecord per run
    certify  -> the Evidence Kernel issues the certificate (K0-K9, with re-execution); bundle + checklist
    report   -> rendered ONLY from the ledger and the evidence store (never from mutable run state)
    audit    -> I1 certificate resolves and matches the run state, I2 comparator integrity (K5),
                I4 report re-renders byte-identically + every score recomputed from stored predictions,
                I5 leak and canary scan

All of these nodes are protected from automatic harness edits (evolve/edits.py).
"""

from __future__ import annotations

import csv
import hashlib
import io
import statistics
import time
from pathlib import Path
from typing import Any

from ..config import data_dir
from ..kernel import (
    DEFAULT_ARGV,
    AnalysisPlan,
    ArtifactRef,
    Claim,
    ClaimKind,
    DatasetRole,
    Effect,
    ExecRecord,
    InputRef,
    Kernel,
    KernelError,
    MetricRef,
    Risk,
    SandboxReplayer,
    Scope,
    run_program,
)
from ..kernel.bundle import build_bundle
from ..kernel.checklist import fill, publishable
from ..kernel.checklist import render as render_checklist
from ..runtime import register
from ..runtime.modules import ModuleContext
from ..verify import metrics as M
from ..verify.leak import label_windows, leak_audit
from ..verify.stats import paired_example_interval
from .modules import _dev_labels, _require_isolation, _sb, _task
from .task import TaskSpec

_NORMAL = statistics.NormalDist()


def _labels(raw: bytes) -> list[str]:
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
    return [r[0] for r in rows[1:] if r]


def _kernel(ctx: ModuleContext) -> Kernel:
    """A Kernel whose replayer re-executes in fresh sandboxes of the same kind as the run's sandbox."""
    sb = _sb(ctx)
    root = sb.workspace.parent / f"{sb.workspace.name}-replay"
    return Kernel(ctx.store, replayer=SandboxReplayer(lambda key: sb.spawn(root / key)))


def register_task(ctx: ModuleContext, task: TaskSpec, task_dir: Path) -> dict[str, str]:
    """Register the task's data and baseline in the vault and return their ids (namespaced by a digest of the
    files, so equal names with different data never collide). The labels are read from disk here, once, to
    register them; afterwards only through the vault."""
    vault = Kernel(ctx.store).vault
    train = (task_dir / "public" / task.train_file).read_bytes()
    test_x = (task_dir / "public" / task.test_features_file).read_bytes()
    test_y = (task_dir / "private" / task.test_labels_file).read_bytes()
    baseline = (task_dir / task.baseline_file).read_bytes()
    parts = (train, test_x, test_y, baseline)  # fixed-length per-file digests: no separator ambiguity
    digest = hashlib.sha256(b"".join(hashlib.sha256(b).digest() for b in parts)).hexdigest()
    ids = task.dataset_ids(digest)
    vault.register_dataset(ids["train"], DatasetRole.DEV, public={task.train_file: train}, private={})
    canary_path = task_dir / "private" / task.canary_file
    vault.register_dataset(ids["test"], DatasetRole.TEST, public={task.test_features_file: test_x},
                           private={task.test_labels_file: test_y},
                           canary=canary_path.read_text(encoding="utf-8").strip() if canary_path.exists() else None)
    vault.register_artifact(ids["baseline"], baseline, "baseline")
    return ids


# ------------------------------------------------------------------ claim (pre-registration)
def forecast(task: TaskSpec, state: dict[str, Any], ctx: ModuleContext, floor: float, fraction: float,
             candidate_key: str = "best", fixed_delta: float | None = None) -> tuple[float, float, float, float]:
    """(min effect, probability, predicted lo, predicted hi) from the dev estimate: out-of-fold predictions of
    k-fold cross-validation on the public training data, paired with the baseline's out-of-fold predictions.

    The claimed minimum effect is max(task floor, fraction x the lower bound of a two-sided 90% paired BCa
    interval over dev examples): the claim asserts what the dev evidence supports, with a margin, instead of the
    weakest bound (which K7 rightly rejects as trivially safe). The probability is P(held-out effect >= that
    minimum) under a normal model of the dev estimate. Two known optimistic biases, NOT corrected here and
    measured by the prediction ledger (calibration): dev picked the best of several candidates (winner's
    curse), and the bootstrap resamples out-of-fold examples as if independent, ignoring the variance between
    the k fitted models (no unbiased estimator of k-fold CV variance exists; Bengio & Grandvalet 2004)."""
    best = state[candidate_key]
    labels = _dev_labels(state, ctx)
    ref_s, ref_b = best.get("dev_preds_ref"), state.get("baseline_dev_preds_ref")
    if not ref_s or not ref_b:
        return (fixed_delta if fixed_delta is not None else floor), 0.5, 0.0, 0.0
    iv = paired_example_interval(M.METRICS[task.metric], labels, [ctx.store.blobs.get_json(ref_s)],
                                 [ctx.store.blobs.get_json(ref_b)], higher_is_better=task.higher_is_better,
                                 alpha=0.1, n_boot=1000)
    if iv.degenerate:
        delta = fixed_delta if fixed_delta is not None else max(floor, fraction * iv.estimate)
        return delta, (0.95 if iv.estimate >= delta else 0.05), iv.estimate, iv.estimate
    delta = fixed_delta if fixed_delta is not None else max(floor, fraction * iv.lo)
    se = (iv.hi - iv.lo) / (2 * _NORMAL.inv_cdf(0.95))
    q = _NORMAL.cdf((iv.estimate - delta) / se) if se > 0 else 0.5
    return delta, min(0.95, max(0.05, q)), iv.lo, iv.hi


@register("research.claim")
def make_claim(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    """params: seeds, alpha, n_comparisons, delta_fraction; prefix (state-key prefix, "" or "replica_");
    candidate (state key of the candidate, default "best"); same_delta_as (state key of an earlier claim whose
    minimum effect this claim must clear, e.g. a replication of "claim")."""
    task = _task(state)
    p = params.get("prefix", "")
    candidate_key = params.get("candidate", "best")
    best = state.get(candidate_key)
    if best is None:
        raise RuntimeError("no candidate to claim about")
    fixed = None
    if params.get("same_delta_as"):
        fixed = Claim.model_validate(state[params["same_delta_as"]]).effect.min_effect
    kernel = Kernel(ctx.store)
    ids = state["datasets"]
    subject = kernel.vault.register_artifact(f"{task.id}/solution/{best['code_ref'][:16]}",
                                             ctx.store.blobs.get(best["code_ref"]), "method")
    baseline = kernel.vault.artifacts()[ids["baseline"]]
    delta, q, lo, hi = forecast(task, state, ctx, task.effect_floor(state["baseline_dev"]),
                                fraction=params.get("delta_fraction", 0.5), candidate_key=candidate_key,
                                fixed_delta=fixed)
    who = ("an independent re-implementation (written from the claim text, without the code) of hypothesis "
           f"{best.get('hypothesis_id')}") if p else f"the solution for hypothesis {best.get('hypothesis_id')}"
    claim = Claim(
        kind=ClaimKind.IMPROVEMENT,
        statement=f"On task {task.id}, {who} beats the task baseline on {task.metric} by at least {delta:.4g} "
                  f"on held-out data",
        subject=ArtifactRef(artifact_id=subject.artifact_id, hash=subject.hash),
        comparator=ArtifactRef(artifact_id=baseline.artifact_id, hash=baseline.hash),
        metric=MetricRef(id=task.metric, higher_is_better=task.higher_is_better),
        effect=Effect(min_effect=delta, predicted_lo=lo, predicted_hi=hi, probability=q),
        scope=Scope(datasets=(ids["train"], ids["test"])), development=(ids["train"],),
        generalization_targets=(ids["test"],),
        # n_comparisons: the PRE-DECLARED size of the multiplicity family on this test set. K4 uses
        # max(declared, actual registered claims), so declaring a campaign's planned number of claims per task
        # keeps certification strictness constant during the campaign (no drift against later candidates).
        analysis_plan=AnalysisPlan(seeds=tuple(params.get("seeds", (0, 1, 2))), alpha=params.get("alpha", 0.05),
                                   n_comparisons=int(params.get("n_comparisons", 1))),
        assumptions=("the k-fold cross-validation estimate on the public training data is representative of "
                     "held-out performance",),
        risk=Risk(), resolve_by=time.time() + 86400)
    entry = kernel.preregister(claim)
    return {f"{p}claim": claim.model_dump(mode="json"), f"{p}claim_hash": claim.claim_hash(),
            f"{p}prereg_seq": entry.seq}


# ------------------------------------------------------------------ confirm
@register("research.confirm")
def confirm(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    _require_isolation(ctx)
    task = _task(state)
    p = params.get("prefix", "")
    claim = Claim.model_validate(state[f"{p}claim"])
    h = claim.claim_hash()
    kernel = Kernel(ctx.store)
    vault = kernel.vault
    ids = state["datasets"]
    train = vault.read_public(ids["train"], task.train_file)
    test = vault.read_public(ids["test"], task.test_features_file)
    n_rows = len(list(csv.reader(io.StringIO(test.decode("utf-8"))))) - 1
    refs = {"train": InputRef(dataset_id=ids["train"], file=task.train_file,
                              blob=vault.dataset(ids["train"]).files[task.train_file].blob),
            "test": InputRef(dataset_id=ids["test"], file=task.test_features_file,
                             blob=vault.dataset(ids["test"]).files[task.test_features_file].blob)}
    sb = _sb(ctx)
    env = sb.environment()
    evidence: list[str] = []
    failures: list[str] = []
    # Idempotent: on a retry or resume, reuse the records this run already made for this claim, so an
    # infrastructure retry never repeats a held-out evaluation (which K4 would rightly count as unreported).
    done: dict[tuple[str, int], str] = {}
    for ev in ctx.store.evidence_for_run(ctx.run_id):
        try:
            prior = ExecRecord.model_validate(ctx.store.blobs.get_json(ev.payload_ref))
        except ValueError:
            continue
        if prior.claim_hash == h:
            done[(prior.arm, prior.seed)] = ev.id
    for seed in claim.analysis_plan.seeds:
        for arm, ref in (("subject", claim.subject), ("comparator", claim.comparator)):
            if (arm, seed) in done:
                evidence.append(done[(arm, seed)])
                continue
            box = sb.spawn(sb.workspace.parent / f"{sb.workspace.name}-confirm" / f"{p}{arm}-{seed}")
            preds, diag = run_program(box, code=ctx.store.blobs.get(ref.hash), entrypoint="solution.py",
                                      argv=DEFAULT_ARGV, inputs={"train": (task.train_file, train),
                                                                 "test": (task.test_features_file, test)},
                                      seed=seed, timeout_s=task.solution_timeout_s)
            # validate the shape BEFORE touching the labels: an unusable output never causes a label read
            if preds is None or len(preds) != n_rows:
                failures.append(f"{arm} seed {seed}: no valid predictions ({diag[-300:]})")
                continue
            labels = _labels(vault.read(ids["test"], task.test_labels_file, purpose="evaluate", run_id=ctx.run_id,
                                        claim_hash=h, arm=arm, seed=seed))
            rec = ExecRecord(claim_hash=h, arm=arm, artifact_hash=ref.hash, inputs=refs, target=ids["test"],  # type: ignore[arg-type]
                             label_file=task.test_labels_file, seed=seed, env=env,
                             limits={"timeout_s": float(task.solution_timeout_s)},
                             preds_ref=ctx.store.blobs.put_json(preds), metric=task.metric,
                             score=M.score(task.metric, labels, preds), run_id=ctx.run_id)
            evidence.append(kernel.record_evaluation(rec))
    return {f"{p}evidence_ids": evidence, f"{p}confirm_failures": failures}


# ------------------------------------------------------------------ certify
def _dev_scheme_text(state: dict[str, Any]) -> str:
    d = state.get("dev_scheme") or {}
    return (f"{'stratified ' if d.get('stratified') else ''}{d.get('k', '?')}-fold cross-validation "
            f"(split seed {d.get('seed', '?')}, {d.get('n', '?')} examples)")


def _models_used(ctx: ModuleContext) -> list[str]:
    seen: set[str] = set()
    for step in ctx.store.trace(ctx.run_id):
        provs = [step.provenance.model_dump(mode="json")] if step.provenance else []
        out = step.output if isinstance(step.output, dict) else {}
        provs += [c.get("provenance", {}) for c in out.get("_module_llm_calls", []) or []]
        for p in provs:
            if p:
                seen.add(f"{p.get('backend')}/{p.get('model')} ({p.get('mode')})")
    return sorted(seen)


@register("research.certify")
def certify(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    _require_isolation(ctx)
    p = params.get("prefix", "")
    kernel = _kernel(ctx)
    claim = Claim.model_validate(state[f"{p}claim"])
    cert = kernel.certify(claim, state.get(f"{p}evidence_ids") or [])
    h = cert.cert_hash()
    bundle_path = data_dir() / "bundles" / f"{h[:20]}.zip"
    bundle_hash = build_bundle(kernel, h, bundle_path)
    n_hyp = len((state.get("hypotheses") or {}).get("hypotheses", []))
    checklist = fill(cert, run_costs={ctx.run_id: ctx.store.run_cost(ctx.run_id)}, models=_models_used(ctx),
                     bundle_hash=bundle_hash,
                     selection_policy=(f"{n_hyp} model-proposed hypotheses ranked by a K-way tournament; "
                                       f"candidates scored by {_dev_scheme_text(state)} on the public training "
                                       f"data (out-of-fold predictions); the best dev candidate became the single "
                                       f"pre-registered claim of this run")
                     if not p else ("independent replication: a separate model call re-implemented the "
                                    "certified hypothesis from the claim text without seeing its code; the "
                                    "replica was checked on dev, then pre-registered at the same minimum effect"),
                     human_in_the_loop="none (fully automated run)",
                     novelty="not performed: the literature module does not exist yet (GOAL M5)")
    ok, blockers = publishable(cert, checklist)
    recs = [ExecRecord.model_validate(ctx.store.blobs.get_json(ctx.store.get_evidence(e).payload_ref))
            for e in cert.evidence_ids]
    target = claim.generalization_targets[0]
    certified = cert.label.value in ("CERTIFIED-INTERNAL", "CERTIFIED-VALIDATED")
    route = "replicate" if (certified and not p and params.get("replicate", False)) else "report"
    brief = None
    if route == "replicate":  # what the replicator may see: the claim and the idea, never the code
        best = state.get("best") or {}
        root = str(best.get("root_hypothesis") or best.get("hypothesis_id", ""))
        hyps = {h.get("id"): h for h in (state.get("hypotheses") or {}).get("hypotheses", [])}
        brief = {"claim": claim.statement, "hypothesis": root,
                 "idea": hyps.get(root, {}).get("idea") or "(idea text unavailable)",
                 "refined": str(best.get("hypothesis_id", "")).startswith("improve-")}
    return {"route": route,
            f"{p}certificate": {"hash": h, "label": cert.label.value, "tier": cert.tier, "failed": cert.failed(),
                            "bundle_hash": bundle_hash, "bundle_path": str(bundle_path),
                            "effect": cert.resolution.get("targets", {}).get(target, {}).get("estimate"),
                            "subject_mean": _mean([r.score for r in recs if r.arm == "subject"]) if recs else None,
                            "comparator_mean": _mean([r.score for r in recs if r.arm == "comparator"])
                            if recs else None},
            f"{p}checklist": checklist, f"{p}publishable": ok, f"{p}publish_blockers": blockers,
            **({"replication_brief": brief} if brief else {})}


# ------------------------------------------------------------------ independent replication (GOAL L4.9)
def code_similarity(a: str, b: str) -> float:
    """Jaccard overlap of the sets of non-trivial normalised lines (whitespace-insensitive). Cheap and
    conservative: only flags near-copies; it says nothing about deeper (e.g. renamed) copying."""
    def lines(s: str) -> set[str]:
        return {" ".join(ln.split()) for ln in s.splitlines() if len(ln.strip()) > 8}
    la, lb = lines(a), lines(b)
    return len(la & lb) / len(la | lb) if la and lb else 0.0


@register("research.replica_dev")
def replica_dev(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    """Score the replicator's code (written WITHOUT the original code) by the same cross-validation as every
    candidate. It becomes a replication candidate only if it works and beats the baseline on dev; otherwise the
    replication fails honestly."""
    from .modules import _better, _dev_predict, _score, _ws
    _require_isolation(ctx)
    task = _task(state)
    code = (state.get("replica_impl") or {}).get("code", "")
    original = ctx.store.blobs.get((state.get("best") or {})["code_ref"]).decode("utf-8", errors="replace")
    similarity = code_similarity(code, original)
    if similarity >= params.get("max_similarity", 0.8):
        # a near-copy of the original code is not an independent replication, whatever produced it
        return {"replica_best": {"hypothesis_id": "replication", "dev_score": None, "dev_preds_ref": None,
                                 "code_ref": ctx.store.blobs.put(code.encode()),
                                 "error": f"not independent: {similarity:.0%} of lines shared with the original"},
                "route": "report"}
    _ws(ctx, "replica.py").write_text(code, encoding="utf-8")
    labels = _dev_labels(state, ctx)
    preds, info = _dev_predict(ctx, state, code.encode("utf-8"), "replica")
    s = _score(task, labels, preds)
    cand = {"hypothesis_id": f"replication-of-{(state.get('best') or {}).get('hypothesis_id')}", "dev_score": s,
            "similarity_to_original": round(similarity, 3),
            "dev_preds_ref": ctx.store.blobs.put_json(preds) if s is not None else None,
            "code_ref": ctx.store.blobs.put(code.encode()),
            "error": None if s is not None else (info["stderr_tail"][-1500:] or "no valid predictions")}
    ok = s is not None and _better(task, s, state["baseline_dev"])
    return {"replica_best": cand, "route": "replica_claim" if ok else "report"}


# ------------------------------------------------------------------ report (rendered from stored artifacts only)
def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


_CERTIFIED = ("CERTIFIED-INTERNAL", "CERTIFIED-VALIDATED")


def headline(state: dict[str, Any]) -> bool:
    """GOAL L4.9: a headline result = certified AND an independent re-implementation certified at the same bar."""
    return ((state.get("certificate") or {}).get("label") in _CERTIFIED
            and (state.get("replica_certificate") or {}).get("label") in _CERTIFIED)


def render_report(state: dict[str, Any], ctx: ModuleContext) -> str:
    task = _task(state)
    lines = [f"# Research report: {task.title}", "",
             f"Task `{task.id}` · metric `{task.metric}` ({'higher' if task.higher_is_better else 'lower'} is better)",
             ""]
    info = state.get("certificate")
    if info:
        kernel = Kernel(ctx.store)
        cert = kernel.certificate(info["hash"])
        claim = cert.claim
        lines += ["## Result (Evidence Kernel certificate)", "",
                  f"**{cert.label.value}** · tier T{cert.tier} · certificate `{info['hash']}`", "",
                  f"- Claim: {claim['statement']}",
                  f"- Claim hash `{cert.claim_hash}`, pre-registered at ledger entry {cert.prereg_seq}",
                  f"- Verification position: {cert.vg_ladder}"]
        recs = [ExecRecord.model_validate(ctx.store.blobs.get_json(ctx.store.get_evidence(e).payload_ref))
                for e in cert.evidence_ids]
        for arm in ("subject", "comparator"):
            scores = [r.score for r in recs if r.arm == arm]
            lines.append(f"- {arm}: mean {task.metric} {_mean(scores):.6g} over {len(scores)} held-out run(s)")
        for target, t in cert.resolution.get("targets", {}).items():
            lines.append(f"- Held-out effect on `{target}`: {t['estimate']:+.6g}, interval "
                         f"[{t['lo']:+.6g}, {t['hi']:+.6g}] (one-sided level {1 - t['alpha_eff']:.6g}); "
                         f"required minimum effect {claim['effect']['min_effect']:.6g}")
        lines += ["", "### Kernel checks", ""]
        for kid, res in cert.checks.items():
            lines.append(f"- {kid} {res.status}" + (": " + "; ".join(res.reasons) if res.reasons else ""))
        lines += ["", render_checklist(state.get("checklist") or {}),
                  f"Reproduce: `chh reproduce {info['bundle_path']} --docker`", ""]
        rep = state.get("replica_certificate")
        lines += ["## Independent replication", ""]
        if rep:
            rcert = kernel.certificate(rep["hash"])
            lines += [f"**{rcert.label.value}** · certificate `{rep['hash']}`", f"- Claim: {rcert.claim['statement']}"]
            lines += [f"- {kid} {res.status}" + (": " + "; ".join(res.reasons) if res.reasons else "")
                      for kid, res in rcert.checks.items() if res.status == "fail"]
        elif state.get("replica_best"):
            why = state["replica_best"].get("error") or "it did not beat the baseline on dev"
            lines.append(f"Replication attempted; no replication claim was made ({why}).")
        else:
            lines.append("No independent replication was performed.")
        lines += ["", f"**Headline result** (certified AND independently replicated): "
                      f"{'yes' if headline(state) else 'no'}", ""]
    else:
        lines += ["## Result", "", "No claim was made: no candidate beat the baseline on the dev estimate.", ""]
    lines += [f"## Exploration log (dev estimate: {_dev_scheme_text(state)} on public training data; "
              f"NOT evidence)", "",
              f"Baseline dev {task.metric}: {state.get('baseline_dev')}", ""]
    for a in state.get("attempts") or []:
        lines.append(f"- attempt {a['n']} hypothesis={a['hypothesis_id']} dev={a['dev_score']} "
                     f"error={'yes' if a['error'] else 'no'}")
    lines += ["", "## Hypotheses considered (model-written text, quoted; not claims)", ""]
    for hyp in (state.get("hypotheses") or {}).get("hypotheses", []):
        lines.append(f"> `{hyp.get('id')}`: {hyp.get('idea')}")
    return "\n".join(lines) + "\n"


@register("research.report")
def report(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    text = render_report(state, ctx)
    return {"report": text, "report_ref": ctx.store.blobs.put(text.encode())}


# ------------------------------------------------------------------ audit
@register("research.audit")
def audit(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    """I1 certificate resolves and the run state agrees with the ledger; I2 comparator integrity (K5);
    I3 citations (n/a until the literature module exists); I4 the report re-renders byte-identically from
    stored artifacts and every held-out score recomputes from its stored predictions; I5 leak/canary scan."""
    task = _task(state)
    res: dict[str, Any] = {"I1": True, "I2": True, "I3": "n/a", "I4": True, "I5": True, "problems": []}

    def fail(key: str, msg: str) -> None:
        res[key] = False
        res["problems"].append(msg)

    kernel = Kernel(ctx.store)
    ids = state["datasets"]
    for p in ("", "replica_"):
        _audit_certificate(state, p, ctx, kernel, ids, task, fail)
    try:
        rerendered = render_report(state, ctx)
    except (KernelError, KeyError, ValueError) as e:
        fail("I4", f"report cannot be re-rendered from stored artifacts: {e}")
    else:
        if rerendered != state.get("report"):
            fail("I4", "the report differs from its re-render from stored artifacts (edited or not generated "
                       "from the certificate)")
    test = kernel.vault.dataset(ids["test"])
    raw = kernel.vault.read(ids["test"], task.test_labels_file, purpose="leakscan", run_id=ctx.run_id)
    fp = {"hashes": {f.blob: f"{ids['test']}/{name}" for name, f in test.files.items() if f.private},
          "canaries": [], "label_windows": label_windows(_labels(raw))}
    canaries = [c for c in (kernel.vault.canary(ids["test"]), state.get("_private_canary")) if c]
    leak = leak_audit(ctx.store.trace(ctx.run_id), _sb(ctx).workspace, fp, canaries,
                      extra_texts={"report": state.get("report", "")})
    for problem in leak["problems"]:
        fail("I5", problem)
    res["passed"] = all(res[k] is True for k in ("I1", "I2", "I4", "I5"))
    return {"audit": res}


def _audit_certificate(state: dict[str, Any], p: str, ctx: ModuleContext, kernel: Kernel, ids: dict[str, str],
                       task: TaskSpec, fail: Any) -> None:
    """I1/I2/I4 for one certificate in the run state (prefix "" = the claim, "replica_" = its replication)."""
    info = state.get(f"{p}certificate")
    if info:
        try:
            cert = kernel.certificate(info["hash"])
        except KernelError as e:
            fail("I1", f"certificate does not resolve: {e}")
        else:
            recs = [ExecRecord.model_validate(ctx.store.blobs.get_json(ctx.store.get_evidence(e).payload_ref))
                    for e in cert.evidence_ids]
            means = {arm: _mean([r.score for r in recs if r.arm == arm]) if recs else None
                     for arm in ("subject", "comparator")}
            if (cert.label.value != info.get("label") or cert.claim_hash != state.get(f"{p}claim_hash")
                    or info.get("subject_mean") != means["subject"]
                    or info.get("comparator_mean") != means["comparator"]):
                fail("I1", "run state disagrees with the ledger certificate or its evidence (label, claim or means)")
            if cert.checks["K5"].status != "pass":
                fail("I2", "comparator integrity (K5) failed: " + "; ".join(cert.checks["K5"].reasons))
            labels = _labels(kernel.vault.read(ids["test"], task.test_labels_file, purpose="kernel",
                                               run_id=ctx.run_id, claim_hash=cert.claim_hash))
            for eid in cert.evidence_ids:
                rec = ExecRecord.model_validate(ctx.store.blobs.get_json(ctx.store.get_evidence(eid).payload_ref))
                again = M.score(rec.metric, labels, ctx.store.blobs.get_json(rec.preds_ref))
                if abs(again - rec.score) > 1e-9:
                    fail("I4", f"{eid}: reported {rec.score!r}, recomputed {again!r}")
    elif state.get(f"{p}claim_hash"):
        fail("I1", f"a {p or 'main '}claim was pre-registered but no certificate was issued")
