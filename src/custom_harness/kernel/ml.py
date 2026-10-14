"""ML adapter procedures (data class; GOAL §5 "Domain adapters", M3 exit): the domain-specific parts of K1 and K3-K8
that the Kernel core (checks.py) calls through the adapter registry (AdapterSpec.procedures):

  metric_reasons   K1  the metric registry (id, direction)
  recompute        K3  reported score vs the score recomputed from the stored predictions
  replay_failure   K3  when a replay's output reproduces the record (exact predictions, or the metric within the
                       registered tolerance); is_regression: how K3's non-interference replay compares decisions
  statistics       K4  the registered test (paired t over seeds + BCa over examples), plan-code divergence, criterion
  conformance      K5  the comparator conformance rules (registered baseline; identically run arms)
  risk             K7  the noise model (normal, resampling SE) and which claim kinds have one; bounded nulls: the
                       plasmode that proves a negative result's declared power (§16.10, docs/bounded_null.md)
  record_bounds    K8  metric range, prediction count and empty predictions

Moved verbatim from checks.py on 2026-10-09 (bit-identical: docs/kernel_core_adapter_split.md). Part of the ML
adapter's identity (adapters.ML.modules): editing it needs a new human approval. Checks pass a checks.Context, typed
Any here (checks imports this module).
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics as _statistics
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from ..verify import metrics as M
from ..verify.stats import Interval, mean_t_interval, paired_example_interval
from .claim import Claim, ClaimKind
from .execution import ExecRecord
from .reasons import Code, R, exception_name

_NORMAL = _statistics.NormalDist()
BOUNDED_KINDS = frozenset({ClaimKind.NEGATIVE_RESULT, ClaimKind.EQUIVALENCE})  # bounded-null certificates (§16.10)
SIDED_TOST = "two one-sided tests (TOST): lower and upper limit, each one-sided at level"  # equivalence
SCORE_TOLERANCE = 1e-9  # reported vs recomputed score: anything larger is a different number


@dataclass
class TargetStats:
    seed: Interval
    example: Interval
    alpha_eff: float
    met: bool  # the claim's criterion holds on this target
    p_null: float | None = None


def metric_reasons(c: Claim) -> list[str]:
    if c.metric.id not in M.METRICS:
        return [R(Code.K1_METRIC, f"metric {c.metric.id!r} is not registered")]
    if M.HIGHER_IS_BETTER[c.metric.id] != c.metric.higher_is_better:
        return [R(Code.K1_METRIC, f"metric {c.metric.id!r} direction does not match the registry")]
    return []


_RELATION = {ClaimKind.IMPROVEMENT: "beats {c} by at least {d}", ClaimKind.ABLATION_EFFECT: "beats its ablation {c} "
             "by at least {d}", ClaimKind.NEGATIVE_RESULT: "does not beat {c} by {d} or more",
             ClaimKind.EQUIVALENCE: "differs from {c} by less than {d}"}


def scope_phrase(c: Claim, roles: Mapping[str, str]) -> str:
    """The data class's scope vocabulary (K1, invariant 14): the ONLY statement a data claim may make (datasets, splits,
    population, metric phrase; min_effect by repr, never rounded). docs/kernel_s4.md, item 4."""
    relation = _RELATION.get(c.kind, "?").format(c=c.comparator.artifact_id, d=repr(c.effect.min_effect))
    held = ", ".join(f"{t} (split {roles.get(t, '?')})" for t in c.generalization_targets)
    return (f"{c.subject.artifact_id} {relation} in {c.metric.id} ({'higher' if c.metric.higher_is_better else 'lower'}"
            f" is better) on the held-out examples of {held}; population: the examples each target is an i.i.d. "
            f"sample of")


def recompute(ctx: Any) -> list[str]:
    reasons = []
    for eid, rec in ctx.records:
        recomputed = M.score(rec.metric, ctx.label_values(rec.target), ctx.preds[eid])
        if not abs(recomputed - rec.score) <= SCORE_TOLERANCE:
            reasons.append(R(Code.K3_SCORE_MISMATCH,
                f"{rec.arm} seed {rec.seed} on {rec.target}: reported {rec.metric}={rec.score!r} but "
                           f"the stored predictions give {recomputed!r}"))
    return reasons


def replay_failure(ctx: Any, eid: str, rec: ExecRecord, again: list[str], where: str) -> str | None:
    plan = ctx.claim.analysis_plan
    if plan.reproduce_tolerance == 0.0:
        return None if again == ctx.preds[eid] else R(Code.K3_REPLAY_DIFFERS, f"{where} produced different predictions")
    try:
        score = M.score(rec.metric, ctx.label_values(rec.target), again)
    except ValueError as e:
        return R(Code.K3_REPLAY_ERROR, f"{where} not scorable: {exception_name(e)}")
    if abs(score - rec.score) > plan.reproduce_tolerance:
        return R(Code.K3_REPLAY_DIFFERS, f"{where}: {score!r} vs reported {rec.score!r} (tolerance "
                                         f"{plan.reproduce_tolerance})")
    return None


def is_regression(metric: str) -> bool:
    return metric in ("rmse", "mae")


def statistics(ctx: Any, target: str, alpha_eff: float) -> tuple[TargetStats, list[str], str]:
    """The registered test on one target whose records follow the plan: (stats, reasons, observation)."""
    c, plan = ctx.claim, ctx.claim.analysis_plan
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
    reasons = plan_code_divergence(ctx, target, alpha_eff, seed_ci, ex_ci, len(labels))
    obs = (f"{target}: effect {ex_ci.estimate:+.4g}; examples {1 - 2 * alpha_eff:.4g} BCa "
           f"[{ex_ci.lo:+.4g}, {ex_ci.hi:+.4g}]; seeds t [{seed_ci.lo:+.4g}, {seed_ci.hi:+.4g}]"
           + (" (seed-invariant: seed interval degenerate, not used)" if seed_ci.degenerate else ""))
    return TargetStats(seed_ci, ex_ci, alpha_eff, criterion(c, seed_ci, ex_ci)), reasons, obs


# §16.6 (the COMPare lesson): the test the Kernel EXECUTED must be the test the claim REGISTERED. Each registered test
# id names the interval methods that implement it; K4 compares what the statistics code actually returned (method,
# level, sample size) with what the registered plan, read back from the ledger, requires.
REGISTERED_TESTS: Mapping[str, tuple[str, str]] = MappingProxyType({"paired_t_seeds+bca_examples": ("t", "bca")})


def plan_code_divergence(ctx: Any, target: str, alpha_eff: float, seed_ci: Interval, ex_ci: Interval,
                         n_examples: int) -> list[str]:
    try:
        registered = Claim.model_validate(ctx.prereg.body["claim"]).analysis_plan if ctx.prereg else None
    except (KeyError, ValidationError):
        registered = None
    if registered is None or registered != ctx.claim.analysis_plan:
        return [R(Code.K4_PLAN_CODE_DIVERGENCE, f"{target}: no registered analysis plan matches the claim's")]
    want = REGISTERED_TESTS.get(registered.test)
    executed = ((seed_ci.method, seed_ci.alpha, seed_ci.n), (ex_ci.method, ex_ci.alpha, ex_ci.n))
    expected = None if want is None else ((want[0], 2 * alpha_eff, len(registered.seeds)),
                                          (want[1], 2 * alpha_eff, n_examples))
    if executed != expected:
        return [R(Code.K4_PLAN_CODE_DIVERGENCE, f"{target}: executed test {executed} is not the registered test "
                                                f"{registered.test!r} {expected}")]
    return []


def criterion(c: Claim, seed: Interval, ex: Interval) -> bool:
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


_PAIRED_FIELDS = ("inputs", "target", "label_file", "argv", "env", "limits", "metric", "entrypoint")


def conformance(ctx: Any) -> list[str]:
    """K5 rules of this domain: an improvement is claimed against a registered baseline, and both arms ran under
    identical conditions (every paired record field equal)."""
    c, reasons = ctx.claim, []
    if c.kind == ClaimKind.IMPROVEMENT:
        reg = ctx.vault.artifacts().get(c.comparator.artifact_id)
        if reg is not None and reg.kind != "baseline":
            reasons.append(R(Code.K5_NOT_BASELINE,
                f"improvement claims must be against a registered baseline; comparator "
                           f"{c.comparator.artifact_id!r} is registered as {reg.kind!r}"))
    for target in c.generalization_targets:
        by = ctx.by_target(target)
        for seed in c.analysis_plan.seeds:
            s, k = by.get(("subject", seed)), by.get(("comparator", seed))
            if s is None or k is None:
                continue  # missing records are K4/K6 failures
            diff = [f for f in _PAIRED_FIELDS if getattr(s[1], f) != getattr(k[1], f)]
            if diff:
                reasons.append(R(Code.K5_NOT_PAIRED,
                    f"{target} seed {seed}: subject and comparator differ in {diff} (not run under "
                               f"identical conditions)"))
    return reasons


def risk(ctx: Any) -> tuple[list[str], list[str]]:
    """Success must have been improbable under a no-effect noise model (and under the declared prior).
    p_null(target) = P(effect >= min_effect | true effect 0), normal model with the target's resampling SE.

    Negative-result and equivalence claims FAIL (fail closed): at their claimed bound, success is at most alpha
    for any bound, so no noise model can tell a vacuous bound ("does not improve by 0.9 accuracy") from a tight one.
    Exception (§16.10): a negative result or an equivalence (TOST) that declares its power gets a bounded-null
    certificate when a plasmode proves that power (bounded_null). Without a declaration it is never certified."""
    c = ctx.claim
    bounded = c.kind in BOUNDED_KINDS and c.analysis_plan.power is not None
    if c.kind not in (ClaimKind.IMPROVEMENT, ClaimKind.ABLATION_EFFECT) and not bounded:
        return [R(Code.K7_NO_RISK_MODEL,
            f"no risk model for {c.kind.value} claims in Kernel v{ctx.config.version}: a vacuous bound "
                  f"(one that could not have failed) cannot be detected without a prior over effects")], []
    missing = sorted(set(c.generalization_targets) - set(ctx.stats))
    if missing:  # KD2 (assure): risk is judged on every target, never on the subset that has statistics
        return [R(Code.K7_NO_STATS, f"no statistics to assess risk on {missing} (see K4)")], []
    if bounded:
        return bounded_null(ctx)
    reasons: list[str] = []
    p_values = []
    for target, st in ctx.stats.items():
        ex = st.example
        z = _NORMAL.inv_cdf(1 - st.alpha_eff)
        se = (ex.hi - ex.lo) / (2 * z)
        if ex.degenerate or not se > 0:
            reasons.append(R(Code.K7_NO_SPREAD, f"{target}: no resampling spread, the noise model is undefined"))
            continue
        st.p_null = 1 - _NORMAL.cdf(c.effect.min_effect / se)
        p_values.append(st.p_null)
    if reasons:
        return reasons, []
    p_used = max([*p_values, c.risk.prior_probability or 0.0])
    bits = -math.log2(p_used) if p_used > 0 else math.inf
    obs = [f"P(success | no effect) <= {p_used:.3g} -> {bits:.2f} bits (minimum {ctx.config.min_risk_bits})"]
    if bits < ctx.config.min_risk_bits:
        reasons.append(R(Code.K7_TRIVIALLY_SAFE,
            f"trivially safe claim: success was {p_used:.3g} likely without any real effect "
                       f"({bits:.2f} bits < {ctx.config.min_risk_bits}); raise the minimum effect or the sample size"))
    return reasons, obs


@dataclass(frozen=True)
class Exclusion:
    """A bounded-null certificate's statement (§16.10): on every target, effects >= reported_limit are excluded at
    one-sided level `level`, by a procedure whose power to detect an effect of excluded_effect was demonstrated."""

    excluded_effect: float  # delta = min_effect (sign-corrected metric units; positive = subject better)
    sided: str  # "one-sided (upper limit)"; equivalence: SIDED_TOST (both limits, each one-sided at `level`)
    level: float  # the largest per-target K4 level alpha_eff: each limit is a one-sided (1 - level) confidence limit
    observed_limit: float  # the largest upper limit (equivalence: largest |limit|) over targets and used intervals
    reported_limit: float  # max(observed_limit, excluded_effect): never tighter than the proven sensitivity (CLs-like)
    declared_power: float
    proven_power: float  # gamma ** (1 / plantings): one-sided (1 - gamma) lower bound when every planting was detected
    power_confidence: float  # 1 - gamma
    plantings: int


def plant(metric: str, labels: list[str], preds: list[str], gain: float, sign: float,
          rng: random.Random) -> list[str] | None:
    """`preds` with an effect of >= gain planted: the fewest wrong predictions, taken in a random order, replaced by
    the true label. None when replacing every wrong prediction still gains less (no such effect can exist)."""
    wrong = [i for i, (y, p) in enumerate(zip(labels, preds, strict=True)) if y != p]
    rng.shuffle(wrong)
    base = M.score(metric, labels, preds)

    def fixed(k: int) -> list[str]:
        out = list(preds)
        for i in wrong[:k]:
            out[i] = labels[i]
        return out

    def enough(k: int) -> bool:
        return sign * (M.score(metric, labels, fixed(k)) - base) >= gain - 1e-12

    if not enough(len(wrong)):
        return None
    lo, hi = 0, len(wrong)  # enough(hi), not enough(lo) (gain > 0)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if enough(mid) else (mid, hi)
    return fixed(hi)


def plasmode(metric: str, higher: bool, labels: list[str], subj: list[list[str]], comp: list[list[str]],
             delta: float, st: TargetStats, n_boot: int, plantings: int, seed: bytes,
             side: int = 1) -> tuple[Code, str] | None:
    """Positive control: `plantings` times, plant an effect of delta into the subject's stored predictions (each seed),
    draw a bootstrap resample of the test examples, and run the registered test (BCa over examples, t over seeds, at
    the target's level). Detected iff each interval the criterion uses has its lower limit above the observed
    estimate. side = -1 (equivalence, the -delta side): the same, planted into the COMPARATOR (an effect of -delta on
    subject - comparator; nothing is degraded), detected iff each upper limit is below the observed estimate. None iff
    every planting was detected; else the first failure (code, text)."""
    sign = 1.0 if higher else -1.0
    for j in range(plantings):
        rng = random.Random(hashlib.sha256(seed + j.to_bytes(4, "big")).digest())
        planted = [plant(metric, labels, p, delta, sign, rng) for p in (subj if side > 0 else comp)]
        a0 = [p for p in planted if p is not None]
        if len(a0) < len(planted):
            return Code.K7_NOT_PLANTABLE, f"no effect of {side * delta} can be planted (vacuous bound)"
        idx = [rng.randrange(len(labels)) for _ in labels]
        a, b = ([[p[i] for i in idx] for p in arm] for arm in ((a0, comp) if side > 0 else (subj, a0)))
        y = [labels[i] for i in idx]
        ex = paired_example_interval(M.METRICS[metric], y, a, b, higher_is_better=higher, alpha=2 * st.alpha_eff,
                                     n_boot=n_boot, seed=0)
        seed_ci = mean_t_interval([sign * (M.score(metric, y, pa) - M.score(metric, y, pb))
                                   for pa, pb in zip(a, b, strict=True)], alpha=2 * st.alpha_eff)
        if ex.degenerate or not _beyond(ex, st.example, side) or not (st.seed.degenerate
                                                                       or _beyond(seed_ci, st.seed, side)):
            return Code.K7_INSENSITIVE, (f"planted effect {side * delta} not detected in planting {j + 1} of "
                                         f"{plantings}: the declared power is not proven")
    return None


def _beyond(planted: Interval, observed: Interval, side: int) -> bool:
    """The planted resample's interval excludes the observed estimate on the planted side."""
    return planted.lo > observed.estimate if side > 0 else planted.hi < observed.estimate


def bounded_null(ctx: Any) -> tuple[list[str], list[str]]:
    """K7 for a negative result or an equivalence with a declared power: the zero-failure plasmode on every target
    (seeded from the claim hash and the evidence, like K3's replay choice, so verify() re-derives it; an equivalence
    runs it on both sides, N plantings each), then the exclusion statement."""
    c, plan, gamma = ctx.claim, ctx.claim.analysis_plan, ctx.config.plasmode_gamma
    n = max(1, math.ceil(math.log(gamma) / math.log(plan.power) - 1e-12))
    tost = c.kind == ClaimKind.EQUIVALENCE
    reasons, limits = [], []
    for target, st in sorted(ctx.stats.items()):
        by, seeds = ctx.by_target(target), sorted(plan.seeds)
        subj, comp = ([ctx.preds[by[(arm, s)][0]] for s in seeds] for arm in ("subject", "comparator"))
        for side, tag in ((1, ""), (-1, "|-"))[:2 if tost else 1]:  # the +delta seed is the negative result's
            seed = hashlib.sha256(f"{ctx.claim_hash}|plasmode|{target}|{ctx.material()}{tag}".encode()).digest()
            why = plasmode(c.metric.id, c.metric.higher_is_better, ctx.label_values(target), subj, comp,
                           c.effect.min_effect, st, plan.n_boot, n, seed, side)
            reasons += [R(why[0], f"{target}: {why[1]}")] if why else []
        used = (st.example,) if st.seed.degenerate else (st.example, st.seed)
        limits.append(max(max(iv.hi, -iv.lo) if tost else iv.hi for iv in used))
    if reasons:
        return reasons, []
    ex = Exclusion(c.effect.min_effect, SIDED_TOST if tost else "one-sided (upper limit)",
                   max(s.alpha_eff for s in ctx.stats.values()), max(limits), max(*limits, c.effect.min_effect),
                   plan.power, gamma ** (1 / n), 1 - gamma, n)
    ctx.exclusion = asdict(ex)
    what = (f"|effects| >= {ex.reported_limit:.4g} excluded (TOST: each side one-sided" if tost else
            f"effects >= {ex.reported_limit:.4g} excluded (one-sided")
    return [], [f"bounded null: {what}, level {ex.level:.4g}; observed limit {ex.observed_limit:.4g}); power >= "
                f"{ex.proven_power:.3g} at {1 - gamma:.3g} confidence, {n}/{n} planted effects of "
                f"{'+-' if tost else ''}{ex.excluded_effect} detected{' on each side' if tost else ''}"]


def record_bounds(ctx: Any, eid: str, rec: ExecRecord) -> list[str]:
    reasons = []
    lo, hi = M.METRIC_RANGE[rec.metric] if rec.metric in M.METRIC_RANGE else (-math.inf, math.inf)
    if not (math.isfinite(rec.score) and lo <= rec.score <= hi):
        reasons.append(R(Code.K8_RANGE,
            f"{eid}: score {rec.score!r} outside the range of {rec.metric} [{lo}, {hi}]"))
    preds = ctx.preds[eid]
    if len(preds) != len(ctx.label_values(rec.target)):
        reasons.append(R(Code.K8_LENGTH,
            f"{eid}: {len(preds)} predictions for {len(ctx.label_values(rec.target))} labels"))
    if any(p.strip() == "" for p in preds):
        reasons.append(R(Code.K8_EMPTY, f"{eid}: empty prediction values"))
    return reasons
