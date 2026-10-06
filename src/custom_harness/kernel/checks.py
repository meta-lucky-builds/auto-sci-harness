"""Kernel checks K0-K9 (GOAL §4.3). Deterministic, LLM-free, fail closed.

Each check reads the Context (claim, ledger, vault, parsed evidence) and returns a CheckResult. Any
exception inside a check is a failure of that check, never a pass (see kernel.Kernel.certify).
"""

from __future__ import annotations

import csv
import io
import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from ..records import EvidenceKind
from ..store.evidence import EvidenceStore
from ..verify import leak
from ..verify import metrics as M
from ..verify.stats import Interval, mean_t_interval, paired_example_interval
from .certificate import CheckResult
from .claim import SUPPORTED_KINDS, Claim, ClaimKind
from .execution import ExecRecord, ExecutionError
from .ledger import Entry, Ledger, verify_chain
from .vault import EXTERNAL_ROLES, HELD_OUT_ROLES, Vault, VaultError

_NORMAL = statistics.NormalDist()
PEEKING_PURPOSES = frozenset({"develop", "audit"})
SCORE_TOLERANCE = 1e-9  # reported vs recomputed score: anything larger is a different number


class Replayer(Protocol):
    def replay(self, record: ExecRecord, vault: Vault) -> list[str] | None: ...


@dataclass(frozen=True)
class KernelConfig:
    """Human-owned policy (L9). Not part of any claim, so a claimant cannot relax it."""

    version: str = "1.0"
    min_seeds: int = 3
    max_alpha: float = 0.2
    min_boot: int = 1000
    min_risk_bits: float = 2.0  # K7: success must have been at most 2^-2 = 25% likely under the null


@dataclass
class TargetStats:
    seed: Interval
    example: Interval
    alpha_eff: float
    met: bool  # the claim's criterion holds on this target
    p_null: float | None = None


@dataclass
class Context:
    claim: Claim
    claim_hash: str
    store: EvidenceStore
    vault: Vault
    ledger: Ledger
    config: KernelConfig
    replayer: Replayer | None
    evidence_ids: tuple[str, ...]
    full_chain: bool = True  # False only when re-checking a ledger EXCERPT from a bundle
    records: list[tuple[str, ExecRecord]] = field(default_factory=list)
    preds: dict[str, list[str]] = field(default_factory=dict)  # evidence id -> predictions
    labels: dict[str, list[str]] = field(default_factory=dict)  # target -> labels
    stats: dict[str, TargetStats] = field(default_factory=dict)
    prereg: Entry | None = None

    @property
    def sign(self) -> float:
        return 1.0 if self.claim.metric.higher_is_better else -1.0

    def by_target(self, target: str) -> dict[tuple[str, int], tuple[str, ExecRecord]]:
        return {(r.arm, r.seed): (eid, r) for eid, r in self.records if r.target == target}

    def label_values(self, target: str) -> list[str]:
        if target not in self.labels:
            rec = next(r for _, r in self.records if r.target == target)
            raw = self.vault.read(target, rec.label_file, purpose="kernel", claim_hash=self.claim_hash)
            rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
            self.labels[target] = [r[0] for r in rows[1:] if r]
        return self.labels[target]


def _result(reasons: list[str], observations: list[str] | None = None) -> CheckResult:
    return CheckResult(status="fail" if reasons else "pass", reasons=tuple(reasons),
                       observations=tuple(observations or ()))


# ------------------------------------------------------------------ K0 provenance
def k0_provenance(ctx: Context) -> CheckResult:
    """Every artifact, record, prediction and input resolves to an intact stored object."""
    reasons: list[str] = []
    arts = ctx.vault.artifacts()
    for role, ref in (("subject", ctx.claim.subject), ("comparator", ctx.claim.comparator)):
        reg = arts.get(ref.artifact_id)
        if reg is None or reg.hash != ref.hash:
            reasons.append(f"{role} artifact {ref.artifact_id!r} is not registered with hash {ref.hash[:12]}")
        elif not _blob_ok(ctx.store, ref.hash):
            reasons.append(f"{role} artifact bytes missing or corrupted")
    if not ctx.evidence_ids:
        reasons.append("no evidence records")
    datasets = ctx.vault.datasets()
    for eid in ctx.evidence_ids:
        try:
            ev = ctx.store.get_evidence(eid)
            if ev.kind != EvidenceKind.METRIC:
                raise ValueError(f"kind {ev.kind} is not a metric record")
            rec = ExecRecord.model_validate(ctx.store.blobs.get_json(ev.payload_ref))
            if rec.claim_hash != ctx.claim_hash:
                raise ValueError("record belongs to a different claim")
            preds = ctx.store.blobs.get_json(rec.preds_ref)
            if not isinstance(preds, list) or not all(isinstance(p, str) for p in preds):
                raise ValueError("predictions blob is not a list of strings")
            if not _blob_ok(ctx.store, rec.artifact_hash):
                raise ValueError("code blob missing or corrupted")
            for inp in rec.inputs.values():
                if inp.dataset_id not in datasets or not _blob_ok(ctx.store, inp.blob):
                    raise ValueError(f"input {inp.dataset_id}/{inp.file} unresolvable")
            if rec.target not in datasets:
                raise ValueError(f"target {rec.target!r} unregistered")
            ctx.store.get_run(rec.run_id)
        except (KeyError, ValueError, OSError, ValidationError, json.JSONDecodeError) as e:
            reasons.append(f"evidence {eid}: {type(e).__name__}: {str(e)[:200]}")
            continue
        ctx.records.append((eid, rec))
        ctx.preds[eid] = preds
    return _result(reasons, [f"{len(ctx.records)}/{len(ctx.evidence_ids)} evidence records resolved"])


def _blob_ok(store: EvidenceStore, digest: str) -> bool:
    try:
        store.blobs.get(digest)
    except OSError:
        return False
    return True


# ------------------------------------------------------------------ K1 well-formed
def k1_well_formed(ctx: Context) -> CheckResult:
    c, cfg, reasons = ctx.claim, ctx.config, []
    plan = c.analysis_plan
    if c.kind not in SUPPORTED_KINDS:
        reasons.append(f"claim kind {c.kind.value!r} is not checkable by Kernel v{cfg.version}")
    if not c.effect.min_effect > 0:  # finiteness is enforced by the Claim model itself
        reasons.append("minimum effect must be a finite number > 0")
    if c.metric.id not in M.METRICS:
        reasons.append(f"metric {c.metric.id!r} is not registered")
    elif M.HIGHER_IS_BETTER[c.metric.id] != c.metric.higher_is_better:
        reasons.append(f"metric {c.metric.id!r} direction does not match the registry")
    if c.subject.hash == c.comparator.hash:
        reasons.append("subject and comparator are the same artifact")
    targets, dev = set(c.generalization_targets), set(c.development)
    if not targets or len(targets) != len(c.generalization_targets):
        reasons.append("generalization targets must be non-empty and unique")
    if targets & dev:
        reasons.append(f"targets also used in development: {sorted(targets & dev)}")
    known = ctx.vault.datasets()
    unknown = sorted((targets | dev | set(c.scope.datasets)) - set(known))
    if unknown:
        reasons.append(f"unregistered datasets: {unknown}")
    if len(set(plan.seeds)) != len(plan.seeds) or len(plan.seeds) < cfg.min_seeds:
        reasons.append(f"analysis plan needs >= {cfg.min_seeds} distinct seeds")
    if not 0 < plan.alpha <= cfg.max_alpha:
        reasons.append(f"alpha must be in (0, {cfg.max_alpha}]")
    if plan.n_comparisons < 1 or plan.n_boot < cfg.min_boot:
        reasons.append(f"n_comparisons must be >= 1 and n_boot >= {cfg.min_boot}")
    if not plan.reproduce_tolerance >= 0:
        reasons.append("reproduce_tolerance must be finite and >= 0")
    e = c.effect
    if not e.predicted_lo <= e.predicted_hi:
        reasons.append("predicted effect interval must be finite with lo <= hi")
    if not 0 < e.probability < 1:
        reasons.append("declared probability must be strictly between 0 and 1")
    if c.risk.prior_probability is not None and not 0 < c.risk.prior_probability < 1:
        reasons.append("risk prior must be strictly between 0 and 1")
    return _result(reasons)


# ------------------------------------------------------------------ K2 pre-registered
def k2_preregistered(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    reasons += verify_chain(ctx.ledger.entries(), full=ctx.full_chain)
    regs = [e for e in ctx.ledger.entries("prereg") if e.body.get("claim_hash") == ctx.claim_hash]
    if not regs:
        return _result([*reasons, "the claim was never pre-registered"])
    prereg = regs[0]
    ctx.prereg = prereg
    try:
        registered = Claim.model_validate(prereg.body["claim"])
        if registered.claim_hash() != ctx.claim_hash:
            reasons.append("the pre-registered claim content does not match its hash")
    except (KeyError, ValidationError) as e:
        reasons.append(f"pre-registration body unreadable: {type(e).__name__}")
    for target in ctx.claim.generalization_targets:
        # Peeking = a development or inspection read, or evaluating THIS claim, before it was registered.
        # Earlier reads made to evaluate or certify OTHER registered claims are test-set reuse, which K4
        # charges as multiplicity (Bonferroni over every claim registered on the target), not a K2 failure.
        early = [a for a in ctx.vault.accesses(target)
                 if a.body.get("private") and a.seq < prereg.seq
                 and (a.body.get("purpose") in PEEKING_PURPOSES or a.body.get("claim_hash") == ctx.claim_hash)]
        if early:
            reasons.append(f"private data of target {target!r} was read {len(early)} time(s) before "
                           f"pre-registration (first at ledger seq {early[0].seq}, purpose "
                           f"{early[0].body.get('purpose')!r})")
    arts = ctx.vault.artifacts()
    for ref in (ctx.claim.subject, ctx.claim.comparator):
        reg = arts.get(ref.artifact_id)
        if reg is not None and reg.seq > prereg.seq:
            reasons.append(f"artifact {ref.artifact_id!r} was registered after the pre-registration")
    return _result(reasons, [f"pre-registered at ledger seq {prereg.seq}"])


# ------------------------------------------------------------------ K3 reproduced
def k3_reproduced(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    obs: list[str] = []
    if not ctx.records:
        return _result(["no usable evaluation records"])
    for eid, rec in ctx.records:
        recomputed = M.score(rec.metric, ctx.label_values(rec.target), ctx.preds[eid])
        if not abs(recomputed - rec.score) <= SCORE_TOLERANCE:
            reasons.append(f"{rec.arm} seed {rec.seed} on {rec.target}: reported {rec.metric}={rec.score!r} but "
                           f"the stored predictions give {recomputed!r}")
    if ctx.replayer is None:
        return _result([*reasons, "no replayer configured: re-execution was not performed"], obs)
    plan = ctx.claim.analysis_plan
    chosen = [(e, r) for e, r in ctx.records if plan.reproduce == "all" or r.seed == plan.seeds[0]]
    if not chosen:
        reasons.append("no record selected for re-execution")
    for eid, rec in chosen:
        try:
            again = ctx.replayer.replay(rec, ctx.vault)
        except (ExecutionError, VaultError, OSError) as e:
            reasons.append(f"re-execution of {rec.arm} seed {rec.seed} failed: {e}")
            continue
        if again is None:
            reasons.append(f"re-execution of {rec.arm} seed {rec.seed} produced no predictions")
            continue
        if plan.reproduce_tolerance == 0.0:
            if again != ctx.preds[eid]:
                reasons.append(f"re-execution of {rec.arm} seed {rec.seed} produced different predictions")
        else:
            try:
                score = M.score(rec.metric, ctx.label_values(rec.target), again)
            except ValueError as e:
                reasons.append(f"re-execution of {rec.arm} seed {rec.seed} not scorable: {e}")
                continue
            if abs(score - rec.score) > plan.reproduce_tolerance:
                reasons.append(f"re-execution of {rec.arm} seed {rec.seed}: {score!r} vs reported {rec.score!r} "
                               f"(tolerance {plan.reproduce_tolerance})")
    obs.append(f"re-executed {len(chosen)} record(s) ({plan.reproduce})")
    return _result(reasons, obs)


# ------------------------------------------------------------------ K4 statistically valid procedure
def k4_statistics(ctx: Context) -> CheckResult:
    """The plan was followed (exact seeds, no unreported held-out evaluations), multiplicity is accounted
    for, and the intervals are computed. Whether the effect criterion holds is K6's verdict."""
    reasons: list[str] = []
    obs: list[str] = []
    c = ctx.claim
    plan = c.analysis_plan
    if not ctx.records:
        return _result(["no usable evaluation records"])
    preregs = ctx.ledger.entries("prereg")
    for target in c.generalization_targets:
        recs = [r for _, r in ctx.records if r.target == target]
        keys = Counter((r.arm, r.seed) for r in recs)
        dup = sorted(k for k, n in keys.items() if n > 1)
        if dup:
            reasons.append(f"{target}: duplicate records for {dup}")
        for arm in ("subject", "comparator"):
            seeds = sorted(s for a, s in keys if a == arm)
            if seeds != sorted(plan.seeds):
                reasons.append(f"{target}: {arm} seeds {seeds} != planned seeds {sorted(plan.seeds)}")
        logged: Counter[tuple[str, int]] = Counter(
            (str(a.body.get("arm")), int(a.body.get("seed", -1))) for a in ctx.vault.accesses(target)
            if a.body.get("purpose") == "evaluate" and a.body.get("claim_hash") == ctx.claim_hash)
        reported: Counter[tuple[str, int]] = Counter({(str(a), s): n for (a, s), n in keys.items()})
        extra = logged - reported
        unlogged = reported - logged
        if extra:
            reasons.append(f"{target}: {sum(extra.values())} held-out evaluation(s) were run but not reported "
                           f"(selective reporting): {sorted(extra)}")
        if unlogged:
            reasons.append(f"{target}: {sum(unlogged.values())} reported evaluation(s) have no logged label read "
                           f"(scores not produced through the vault)")
        family = {e.body.get("claim_hash") for e in preregs if target in e.body.get("claim", {}).get(
            "generalization_targets", [])}
        m = max(plan.n_comparisons, len(family))
        alpha_eff = plan.alpha / m
        obs.append(f"{target}: {m} claim(s) tested on this target -> Bonferroni alpha {alpha_eff:.4g}")
        if dup or any(s != sorted(plan.seeds) for s in (sorted(s for a, s in keys if a == arm)
                                                         for arm in ("subject", "comparator"))):
            continue
        by = ctx.by_target(target)
        seeds = sorted(plan.seeds)
        subj = [by[("subject", s)][1].score for s in seeds]
        comp = [by[("comparator", s)][1].score for s in seeds]
        seed_ci = mean_t_interval([ctx.sign * (a - b) for a, b in zip(subj, comp, strict=True)],
                                  alpha=2 * alpha_eff)
        labels = ctx.label_values(target)
        ex_ci = paired_example_interval(M.METRICS[c.metric.id], labels,
                                        [ctx.preds[by[("subject", s)][0]] for s in seeds],
                                        [ctx.preds[by[("comparator", s)][0]] for s in seeds],
                                        higher_is_better=c.metric.higher_is_better, alpha=2 * alpha_eff,
                                        n_boot=plan.n_boot, seed=0)
        ctx.stats[target] = TargetStats(seed_ci, ex_ci, alpha_eff, _criterion(c, seed_ci, ex_ci))
        obs.append(f"{target}: effect {ex_ci.estimate:+.4g}; examples {1 - 2 * alpha_eff:.4g} BCa "
                   f"[{ex_ci.lo:+.4g}, {ex_ci.hi:+.4g}]; seeds t [{seed_ci.lo:+.4g}, {seed_ci.hi:+.4g}]"
                   + (" (seed-invariant: seed interval degenerate, not used)" if seed_ci.degenerate else ""))
    return _result(reasons, obs)


def _criterion(c: Claim, seed: Interval, ex: Interval) -> bool:
    d = c.effect.min_effect
    if ex.degenerate:
        return False  # no example-level variation: the effect cannot be bounded
    use_seed = not seed.degenerate
    if c.kind in (ClaimKind.IMPROVEMENT, ClaimKind.ABLATION_EFFECT):
        return ex.lo >= d and (not use_seed or seed.lo >= d)
    if c.kind == ClaimKind.NEGATIVE_RESULT:
        return ex.hi < d and (not use_seed or seed.hi < d)
    if c.kind == ClaimKind.EQUIVALENCE:
        return -d < ex.lo and ex.hi < d and (not use_seed or (-d < seed.lo and seed.hi < d))
    return False


# ------------------------------------------------------------------ K5 comparator integrity
_PAIRED_FIELDS = ("inputs", "target", "label_file", "argv", "env", "limits", "metric", "entrypoint")


def k5_comparator(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    c = ctx.claim
    for eid, rec in ctx.records:
        want = c.subject.hash if rec.arm == "subject" else c.comparator.hash
        if rec.artifact_hash != want:
            reasons.append(f"{rec.arm} seed {rec.seed}: ran code {rec.artifact_hash[:12]}, not the registered "
                           f"{rec.arm} {want[:12]}")
        if rec.metric != c.metric.id:
            reasons.append(f"{eid}: scored with {rec.metric!r}, the claim's metric is {c.metric.id!r}")
    if c.kind == ClaimKind.IMPROVEMENT:
        reg = ctx.vault.artifacts().get(c.comparator.artifact_id)
        if reg is not None and reg.kind != "baseline":
            reasons.append(f"improvement claims must be against a registered baseline; comparator "
                           f"{c.comparator.artifact_id!r} is registered as {reg.kind!r}")
    for target in c.generalization_targets:
        by = ctx.by_target(target)
        for seed in c.analysis_plan.seeds:
            s, k = by.get(("subject", seed)), by.get(("comparator", seed))
            if s is None or k is None:
                continue  # missing records are K4/K6 failures
            diff = [f for f in _PAIRED_FIELDS if getattr(s[1], f) != getattr(k[1], f)]
            if diff:
                reasons.append(f"{target} seed {seed}: subject and comparator differ in {diff} (not run under "
                               f"identical conditions)")
    return _result(reasons)


# ------------------------------------------------------------------ K6 held-out confirmation
def k6_held_out(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    obs: list[str] = []
    datasets = ctx.vault.datasets()
    dev_hashes = {datasets[d].hash for d in ctx.claim.development if d in datasets}
    for target in ctx.claim.generalization_targets:
        rec = datasets.get(target)
        if rec is None:
            reasons.append(f"{target}: unregistered")
            continue
        if rec.role not in HELD_OUT_ROLES:
            reasons.append(f"{target}: role {rec.role.value!r} is not a held-out role")
        if rec.hash in dev_hashes:
            reasons.append(f"{target}: identical content to a development dataset")
        twins = sorted(d for d, other in datasets.items() if d != target and other.hash == rec.hash)
        if twins:  # e.g. the internal test set re-registered under a new id as "external" (label laundering)
            reasons.append(f"{target}: identical content registered under other id(s) {twins}")
        dev_use = [a for a in ctx.vault.accesses(target) if a.body.get("purpose") == "develop"]
        if dev_use:
            reasons.append(f"{target}: used for development ({len(dev_use)} logged 'develop' read(s))")
        st = ctx.stats.get(target)
        if st is None:
            reasons.append(f"{target}: no valid statistics (see K4)")
        elif not st.met:
            reasons.append(f"{target}: the claim's criterion was not met (effect {st.example.estimate:+.4g}, "
                           f"interval [{st.example.lo:+.4g}, {st.example.hi:+.4g}], min effect "
                           f"{ctx.claim.effect.min_effect})")
        else:
            obs.append(f"{target}: criterion met")
    return _result(reasons, obs)


def is_external(ctx: Context) -> bool:
    datasets = ctx.vault.datasets()
    return all(t in datasets and datasets[t].role in EXTERNAL_ROLES for t in ctx.claim.generalization_targets)


# ------------------------------------------------------------------ K7 risk
def k7_risk(ctx: Context) -> CheckResult:
    """Success must have been improbable under a no-effect noise model (and under the declared prior).
    p_null(target) = P(effect >= min_effect | true effect 0), normal model with the target's resampling SE."""
    c = ctx.claim
    if c.kind not in (ClaimKind.IMPROVEMENT, ClaimKind.ABLATION_EFFECT):
        return CheckResult(status="n/a", reasons=(f"risk is defined for improvement-type claims, not {c.kind.value}",))
    if not ctx.stats:
        return _result(["no statistics to assess risk (see K4)"])
    reasons: list[str] = []
    p_values = []
    for target, st in ctx.stats.items():
        ex = st.example
        z = _NORMAL.inv_cdf(1 - st.alpha_eff)
        se = (ex.hi - ex.lo) / (2 * z)
        if ex.degenerate or not se > 0:
            reasons.append(f"{target}: no resampling spread, the noise model is undefined")
            continue
        st.p_null = 1 - _NORMAL.cdf(c.effect.min_effect / se)
        p_values.append(st.p_null)
    if reasons:
        return _result(reasons)
    p_used = max([*p_values, c.risk.prior_probability or 0.0])
    bits = -math.log2(p_used) if p_used > 0 else math.inf
    obs = [f"P(success | no effect) <= {p_used:.3g} -> {bits:.2f} bits (minimum {ctx.config.min_risk_bits})"]
    if bits < ctx.config.min_risk_bits:
        reasons.append(f"trivially safe claim: success was {p_used:.3g} likely without any real effect "
                       f"({bits:.2f} bits < {ctx.config.min_risk_bits}); raise the minimum effect or the sample size")
    return _result(reasons, obs)


# ------------------------------------------------------------------ K8 background consistency
def k8_background(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    datasets = ctx.vault.datasets()
    for eid, rec in ctx.records:
        lo, hi = M.METRIC_RANGE[rec.metric] if rec.metric in M.METRIC_RANGE else (-math.inf, math.inf)
        if not (math.isfinite(rec.score) and lo <= rec.score <= hi):
            reasons.append(f"{eid}: score {rec.score!r} outside the range of {rec.metric} [{lo}, {hi}]")
        preds = ctx.preds[eid]
        if len(preds) != len(ctx.label_values(rec.target)):
            reasons.append(f"{eid}: {len(preds)} predictions for {len(ctx.label_values(rec.target))} labels")
        if any(p.strip() == "" for p in preds):
            reasons.append(f"{eid}: empty prediction values")
        target = datasets[rec.target]
        if rec.label_file not in target.files or not target.files[rec.label_file].private:
            reasons.append(f"{eid}: label file {rec.label_file!r} is not a private file of {rec.target!r}")
        for role, ref in rec.inputs.items():
            reg = datasets[ref.dataset_id].files.get(ref.file)
            if reg is None or reg.blob != ref.blob:
                reasons.append(f"{eid}: input {role} is not the registered bytes of {ref.dataset_id}/{ref.file}")
            elif reg.private:
                reasons.append(f"{eid}: input {role} is a PRIVATE file ({ref.dataset_id}/{ref.file}) "
                               f"given to the program")
    return _result(reasons)


# ------------------------------------------------------------------ K9 leak & canary audit
def k9_leaks(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    datasets = ctx.vault.datasets()
    canaries = [x for x in (ctx.vault.canary(d) for d in datasets) if x]
    private_blobs: dict[str, str] = {}
    windows: list[str] = []
    for target in ctx.claim.generalization_targets:
        drec = datasets.get(target)
        if drec is None:
            continue
        private_blobs |= {f.blob: f"{target}/{name}" for name, f in drec.files.items() if f.private}
        if any(r.target == target for _, r in ctx.records):
            windows += leak.label_windows(ctx.label_values(target))
    for role, ref in (("subject", ctx.claim.subject), ("comparator", ctx.claim.comparator)):
        if ref.hash in private_blobs:
            reasons.append(f"{role} code IS a private file ({private_blobs[ref.hash]})")
        try:
            text = ctx.store.blobs.get(ref.hash).decode("utf-8", errors="ignore")
        except OSError:
            continue  # K0 reports unresolvable artifacts
        reasons += leak.scan_text(text, f"{role} code", canaries, windows)
    fp = {"hashes": private_blobs, "canaries": canaries, "label_windows": windows}
    for run_id in sorted({r.run_id for _, r in ctx.records}):
        result = leak.leak_audit(ctx.store.trace(run_id), None, fp, canaries)
        reasons += [f"run {run_id}: {p}" for p in result["problems"]]
    for eid, rec in ctx.records:
        if any(part.startswith(("/", "\\")) or ".." in part or ":" in part for part in rec.argv):
            reasons.append(f"{eid}: command references paths outside the workspace")
    return _result(reasons, [f"scanned code, {len({r.run_id for _, r in ctx.records})} run trace(s), "
                             f"{len(canaries)} canary token(s)"])


ALL_CHECKS: dict[str, Any] = {
    "K0": k0_provenance, "K1": k1_well_formed, "K2": k2_preregistered, "K3": k3_reproduced,
    "K4": k4_statistics, "K5": k5_comparator, "K6": k6_held_out, "K7": k7_risk, "K8": k8_background,
    "K9": k9_leaks,
}
