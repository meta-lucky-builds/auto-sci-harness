"""Verifier tiers and the tier policy (PLAN §3, §5).

A claim's tier is the weakest link in its evidence chain. Autonomy is gated by tier.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import IntEnum, StrEnum


class Tier(IntEnum):
    """Lower value = stronger verification."""

    T0_FORMAL = 0          # Lean / SMT
    T1_EXECUTABLE = 1      # tests, evaluator scripts, numerical certificates, invariants
    T2_STATISTICAL = 2     # multi-seed CI vs re-run baseline, independent replication
    T3_PHYSICAL = 3        # wet lab, bench, human expert replication
    T4_RETROSPECTIVE = 4   # time-split rediscovery match
    T5_LLM_JUDGE = 5       # pairwise / K-way judge panel
    T6_NONE = 6            # unsupported


def chain_tier(evidence_tiers: Iterable[Tier]) -> Tier:
    """Weakest link. No evidence at all -> T6."""
    tiers = list(evidence_tiers)
    return max(tiers) if tiers else Tier.T6_NONE


class Action(StrEnum):
    PUBLISH_VERIFIED = "publish_verified"
    PUBLISH_SUPPORTED = "publish_supported"
    ADMIT_SKILL = "admit_skill"
    ACCEPT_HARNESS_EDIT = "accept_harness_edit"
    MODIFY_VERIFIERS = "modify_verifiers"
    WEIGHT_UPDATE = "weight_update"


# Minimum tier (inclusive) for the action to proceed WITHOUT human approval.
# None = never automatic. Changing this table is a human-only action (PLAN §5, L9).
DEFAULT_POLICY: dict[Action, Tier | None] = {
    Action.PUBLISH_VERIFIED: Tier.T2_STATISTICAL,
    Action.PUBLISH_SUPPORTED: Tier.T5_LLM_JUDGE,
    Action.ADMIT_SKILL: Tier.T1_EXECUTABLE,
    Action.ACCEPT_HARNESS_EDIT: Tier.T2_STATISTICAL,
    Action.MODIFY_VERIFIERS: None,
    Action.WEIGHT_UPDATE: None,
}


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    NEEDS_HUMAN = "needs_human"
    DENY = "deny"


def decide(action: Action, tier: Tier, policy: dict[Action, Tier | None] | None = None,
           human_approved: bool = False) -> PolicyDecision:
    policy = policy or DEFAULT_POLICY
    floor = policy[action]
    if tier == Tier.T6_NONE and action in (Action.PUBLISH_VERIFIED, Action.PUBLISH_SUPPORTED):
        return PolicyDecision.DENY  # unsupported assertions are never published
    if floor is not None and tier <= floor:
        return PolicyDecision.ALLOW
    return PolicyDecision.ALLOW if human_approved else PolicyDecision.NEEDS_HUMAN
