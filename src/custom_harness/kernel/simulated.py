"""Kernel checks of the SIMULATED evidence class (GOAL §4.7 "Simulated", invariant 14). Domain-agnostic: the approved
adapter's simulator module (AdapterSpec.exact_module) supplies SIMULATOR, VERSION, scope_phrase, check and bounds.

The claim: "in simulator S (version, config), the computed quantity is `value` within the config's tolerance". Its
scope must name exactly S, S's pinned version and the config's hash, and nothing else (no "holds in reality" scope);
its statement must be exactly the simulator's own rendering of claim and scope (no free prose). The simulation
result is a registered artifact linked by a PROOF evidence record (Kernel.record_certificate); K0, K2 and K9 are
the computational ones (see checks.py). K3 re-runs the simulation (declared step and refinements) and K8 applies
the adapter's bounds. The certificate is CERTIFIED-SIMULATED and lists "simulation matches reality" as UNTESTED:
never a physical finding.
Checks take a checks.Context (typed Any here: checks.py imports this module).
"""

from __future__ import annotations

import importlib
from fractions import Fraction
from typing import Any

from . import adapters
from .certificate import Assumption, CheckResult
from .claim import AnalysisPlan, ClaimKind, exact_value

SIMULATED_ASSUMPTIONS = (
    Assumption(text="the simulation matches reality: the simulator's model, parameters and solver describe the real "
                    "system the claim is about (the claim holds in the named simulator only)", tested_by=None),
    Assumption(text="the approved adapter's simulator and convergence criterion are correct (human-approved; "
                    "fault-injection validated)", tested_by=None),
    Assumption(text="the reported values are reproduced by re-running the pinned simulator at h, h/2 and h/4",
               tested_by="K3"),
)


def _result(reasons: list[str], observations: list[str] | None = None) -> CheckResult:
    return CheckResult(status="fail" if reasons else "pass", reasons=tuple(reasons),
                       observations=tuple(observations or ()))


def _module(ctx: Any) -> Any:
    spec = adapters.REGISTRY[ctx.adapter["name"]][0] if ctx.adapter is not None else None
    return importlib.import_module(spec.exact_module) if spec is not None and spec.exact_module else None


def k1_simulated(ctx: Any) -> CheckResult:
    c, reasons = ctx.claim, []
    if c.kind != ClaimKind.COMPUTED_VALUE:
        reasons.append(f"a simulated claim must be of kind 'computed_value', not {c.kind.value!r}")
    value = exact_value(c.value)
    if value is None:
        reasons.append(f"value {c.value!r} must be an exact rational in lowest terms, 'p' or 'p/q' (no decimals)")
    ctx.adapter, why = adapters.resolve(ctx.ledger, c.evidence_class, c.metric.id)
    reasons += why
    module = _module(ctx)
    if ctx.adapter is not None and module is None:
        reasons.append(f"adapter {ctx.adapter['name']!r} declares no simulator module")
    if module is not None:
        want = {"simulator": module.SIMULATOR, "version": module.VERSION, "config": c.subject.hash}
        got = dict(c.scope.conditions)
        if set(got) != set(want):
            reasons.append(f"scope conditions must be exactly {sorted(want)} (a simulated claim holds only in the "
                           f"named simulator), got {sorted(got)}")
        reasons += [f"scope names {k} {got[k]!r}, not {want[k]!r}" for k in ("simulator", "version")
                    if k in got and got[k] != want[k]]
        if "config" in got and got["config"] != want["config"]:
            reasons.append(f"scope names config {got['config'][:12]}, not the subject {want['config'][:12]}")
        try:
            phrase = module.scope_phrase(ctx.store.blobs.get(c.subject.hash), value or Fraction(0))
            if value is not None and c.statement != phrase:  # no free prose: nothing can widen the scope
                reasons.append(f"the statement must be exactly the simulator's scope rendering {phrase!r}")
        except (OSError, ValueError) as err:
            reasons.append(f"config unreadable: {err}")
    if c.comparator != c.subject:
        reasons.append("a simulated value has no comparator: by convention the comparator must be the subject (the "
                       "simulation config)")
    if c.development or c.generalization_targets or c.scope.datasets:
        reasons.append("a simulated claim uses no datasets: development, generalization targets and scope datasets "
                       "must be empty")
    if c.analysis_plan != AnalysisPlan(seeds=()):
        reasons.append("the analysis plan must be empty (no seeds, defaults): the convergence study replaces it")
    e = c.effect
    if (e.min_effect, e.predicted_lo, e.predicted_hi) != (0.0, 0.0, 0.0):
        reasons.append("effect fields other than the declared probability must be 0 (the claimed value is `value`)")
    if not 0 < e.probability < 1:
        reasons.append("declared probability must be strictly between 0 and 1")
    if c.risk.prior_probability is not None:
        reasons.append("risk prior must be unset: K7 does not apply to simulated claims")
    return _result(reasons)


def _inputs(ctx: Any) -> tuple[Any, bytes, bytes, Fraction] | list[str]:
    module, value = _module(ctx), exact_value(ctx.claim.value)
    reasons = []
    if module is None:
        reasons.append("no approved simulator adapter module (see K1)")
    if value is None:
        reasons.append("the claimed value is not an exact rational (see K1)")
    if len(ctx.certificates) != 1:
        reasons.append("no single resolved simulation result (see K0)")
    if reasons or value is None:
        return reasons
    return module, ctx.store.blobs.get(ctx.claim.subject.hash), ctx.store.blobs.get(ctx.certificates[0].hash), value


def k3_simulated(ctx: Any) -> CheckResult:
    """Re-run the pinned simulator at h, h/2, h/4: reported numbers reproduced, converged, value within tolerance."""
    got = _inputs(ctx)
    if isinstance(got, list):
        return _result(got)
    module, config, result, value = got
    return _result(list(module.check(config, result, value)), [
        f"holds in simulator {module.SIMULATOR} (version {module.VERSION}, config {ctx.claim.subject.hash[:12]}) only; "
        f"re-run at h, h/2 and h/4 with a Richardson error estimate by {module.__name__}"])


def k8_simulated(ctx: Any) -> CheckResult:
    got = _inputs(ctx)
    if isinstance(got, list):
        return _result(got)
    module, config, result, value = got
    return _result(list(module.bounds(config, result, value)))
