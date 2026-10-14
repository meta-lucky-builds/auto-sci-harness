"""ML adapter procedures (data class; GOAL §5 "Domain adapters", M3 exit): the domain-specific parts of K1 and K3-K8
that the Kernel core (checks.py) calls through the adapter registry (AdapterSpec.procedures):

  metric_reasons   K1  the metric registry (id, direction)
  recompute        K3  reported score vs the score recomputed from the stored predictions
  replay_failure   K3  when a replay's output reproduces the record (exact predictions, or the metric within the
                       registered tolerance); is_regression: how K3's non-interference replay compares decisions
  statistics       K4  the registered test (paired t over seeds + BCa over examples), plan-code divergence, criterion
  conformance      K5  the comparator conformance rules (registered baseline; identically run arms)
  risk             K7  the noise model (normal, resampling SE) and which claim kinds have one
  record_bounds    K8  metric range, prediction count and empty predictions

Moved verbatim from checks.py on 2026-10-09 (bit-identical: docs/kernel_core_adapter_split.md). Part of the ML
adapter's identity (adapters.ML.modules): editing it needs a new human approval. Checks pass a checks.Context, typed
Any here (checks imports this module).
"""

from __future__ import annotations

import math
import statistics as _statistics
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from ..verify import metrics as M
from ..verify.stats import Interval, mean_t_interval, paired_example_interval
from .claim import Claim, ClaimKind
from .execution import ExecRecord
from .reasons import Code, R, exception_name

_NORMAL = _statistics.NormalDist()
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
    for any bound, so no noise model can tell a vacuous bound ("does not improve by 0.9 accuracy") from a tight one;
    that needs a prior over effects, which v1 does not have. Such claims are reported, never certified."""
    c = ctx.claim
    if c.kind not in (ClaimKind.IMPROVEMENT, ClaimKind.ABLATION_EFFECT):
        return [R(Code.K7_NO_RISK_MODEL,
            f"no risk model for {c.kind.value} claims in Kernel v{ctx.config.version}: a vacuous bound "
                  f"(one that could not have failed) cannot be detected without a prior over effects")], []
    missing = sorted(set(c.generalization_targets) - set(ctx.stats))
    if missing:  # KD2 (assure): risk is judged on every target, never on the subset that has statistics
        return [R(Code.K7_NO_STATS, f"no statistics to assess risk on {missing} (see K4)")], []
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
