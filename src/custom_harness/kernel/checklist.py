"""Auto-filled reporting checklist (GOAL §1.3, VG Table 10). A missing item blocks publication.

"Missing" means no answer at all. An explicit "not performed (reason)" IS an answer: honesty about a gap
is required, silence is not allowed.
"""

from __future__ import annotations

from typing import Any

from ..records import Cost
from .certificate import Certificate, Label

ITEMS = (
    ("claim", "Typed claim and its content hash"),
    ("kernel_certificate", "Kernel certificate: label, tier, checks passed and failed"),
    ("vg_ladder", "Position on the Verification Gap ladder"),
    ("seeds", "Seeds per arm and target"),
    ("statistical_test", "Statistical procedure and intervals"),
    ("multiple_comparisons", "Multiple-comparison correction"),
    ("selection_policy", "How this claim was selected among alternatives"),
    ("held_out_data", "Held-out targets and their role"),
    ("traces", "Full traces of the runs behind the evidence"),
    ("code_and_data", "Code and data availability (hashes)"),
    ("reproducibility_bundle", "Reproducibility bundle (one-command rerun)"),
    ("models", "Models and access modes used"),
    ("cost", "Compute and model cost"),
    ("human_in_the_loop", "Human-in-the-loop points"),
    ("novelty_verification", "Novelty verification against the literature"),
    ("assumptions", "Assumptions, tested and untested"),
)


def fill(cert: Certificate, *, run_costs: dict[str, Cost], models: list[str], bundle_hash: str | None,
         selection_policy: str | None, human_in_the_loop: str | None, novelty: str | None) -> dict[str, str | None]:
    claim: dict[str, Any] = cert.claim
    plan = claim["analysis_plan"]
    failed = cert.failed()
    k4 = cert.checks["K4"]
    total = Cost()
    for c in run_costs.values():
        total = total + c
    targets = claim["generalization_targets"]
    return {
        "claim": f"{claim['kind']} on {claim['metric']['id']} (min effect {claim['effect']['min_effect']}); "
                 f"hash {cert.claim_hash}",
        "kernel_certificate": f"{cert.label.value}, tier T{cert.tier}; failed: {failed or 'none'}",
        "vg_ladder": cert.vg_ladder,
        "seeds": f"{list(plan['seeds'])} for both arms on each of {len(targets)} target(s)",
        "statistical_test": f"{plan['test']}; alpha {plan['alpha']} one-sided; " + "; ".join(k4.observations),
        "multiple_comparisons": "Bonferroni over max(declared n_comparisons, all pre-registered claims on the "
                                "same target)",
        "selection_policy": selection_policy,
        "held_out_data": ", ".join(targets) + " (never used in development: Kernel K2/K6)",
        "traces": f"{len(run_costs)} run trace(s): " + ", ".join(sorted(run_costs)) if run_costs else None,
        "code_and_data": f"subject {claim['subject']['hash'][:16]}, comparator {claim['comparator']['hash'][:16]}; "
                         f"data content-addressed in the vault",
        "reproducibility_bundle": f"bundle {bundle_hash} (`chh reproduce`)" if bundle_hash else None,
        "models": ", ".join(models) if models else "none (no model calls in the evidence runs)",
        # a subscription (BYOS) call records usd 0.0 = NOT REPORTED: a zero is never rendered as a known $0 (s3-report)
        "cost": f"{total.prompt_tokens + total.completion_tokens} tokens, "
                f"{f'${total.usd:.4f}' if total.usd > 0 else 'USD unknown / UNCALIBRATED'}"
                f"{' (estimated)' if total.estimated else ''}, {total.quota_units:g} quota units, "
                f"{total.wall_s:.1f} s wall" if run_costs else None,
        "human_in_the_loop": human_in_the_loop,
        "novelty_verification": novelty,
        "assumptions": "; ".join(f"{a.text} [{'tested: ' + a.tested_by if a.tested_by else 'untested'}]"
                                 for a in cert.assumptions),
    }


def missing(checklist: dict[str, str | None]) -> list[str]:
    return [key for key, _ in ITEMS if not checklist.get(key)]


def publishable(cert: Certificate, checklist: dict[str, str | None]) -> tuple[bool, list[str]]:
    """A result may be published as verified only if certified AND every checklist item is answered."""
    reasons = [f"checklist item missing: {k}" for k in missing(checklist)]
    if cert.label not in (Label.CERTIFIED_INTERNAL, Label.CERTIFIED_VALIDATED):
        reasons.append(f"not certified ({cert.label.value})")
    return not reasons, reasons


def render(checklist: dict[str, str | None]) -> str:
    lines = ["## Reporting checklist (VG)", ""]
    for key, title in ITEMS:
        value = checklist.get(key)
        lines.append(f"- **{title}**: {value if value else '**MISSING (blocks publication)**'}")
    return "\n".join(lines) + "\n"
