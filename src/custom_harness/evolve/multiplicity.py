"""Order-independent certification during an evolution campaign (finding R2-2).

K4 certifies a claim at Bonferroni alpha / m with m = max(declared n_comparisons, claims registered so far on the
target and on every target sharing its held-out examples). Every evolution run of a task registers its claims on the
SAME target in the campaign's shared store, so without a declaration m grows during the campaign: the arm evaluated
first on a unit is judged at a laxer alpha than the arm evaluated second, and evaluation ORDER, not the edit, moves
the acceptance evidence.

Fix (decided design, HANDOFF2 §2 R2-2): the campaign keeps m CONSTANT by pre-declaring n_comparisons >= every
family size the campaign can reach. Two checks, both fail closed:
  1. before the campaign (`check_campaign`): every claim node of the seed spec (module research.claim; protected,
     so no edit can change it) declares n_comparisons >= max over targets of (claims already registered on the
     target's family + tasks sharing the target x claim nodes x an upper bound of runs per task in the campaign);
  2. after every harness run (`check_run`): for every claim the run registered, the family on each of its targets
     (mirroring K4: shared example rows OR a byte-identical private file) is <= that claim's declared
     n_comparisons. Then every certification in the run used m = declared exactly (families only grow, and the
     check runs after the run's last certification). A violation raises MultiplicityDrift: the outcome is NOT
     used and the campaign stops.
Cost: a stricter alpha for every claim in the campaign (accepted in the design).

Account-aware (I1, stage-a on int18): both checks mirror `kernel/checks.target_level` exactly (`kernel_level` below
re-states it line by line on ledger bodies, so it also works on the minimal prereg bodies tests write; a test pins it
equal to the Kernel's own function on real claims):
  - a target WITHOUT an alpha-account: K4's level is alpha / max(n_comparisons, N), N = the transcript bound over the
    family's claims and releases. Drift iff N > declared n_comparisons (unchanged).
  - a target WITH an alpha-account (GOAL §16.4, opened by a human before the first claim): K4's level is the slot level
    min(alpha / n_comparisons, alpha_T / S_T), fixed at registration whatever the order of claims and releases
    (releases are paid in slots at registration). Drift iff the Kernel's level for the claim is below that fixed level
    (a claim on the family holds no slot: Bonferroni over the transcript bound takes over) or K4 has an account reason
    (the claim holds no slot; the account is overdrawn): both depend on evaluation order.
  - `family_size` (the campaign pre-check's base) is the transcript bound on a target without an account and 0 on an
    account target: there the declaration does not set the level, so the campaign bound asks nothing of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..kernel import alpha as A
from ..kernel.checks import family, related_targets, releases_on
from ..kernel.claim import AnalysisPlan
from ..kernel.releases import transcript_bound
from ..kerneld.scope import ScopedKernel
from ..runtime.spec import HarnessSpec, ModuleNode
from ..store import EvidenceStore

CLAIM_MODULE = "research.claim"


class MultiplicityDrift(ValueError):
    """The Kernel multiplicity family of a campaign target exceeded the declared n_comparisons (R2-2)."""


def claim_nodes(spec: HarnessSpec) -> dict[str, int]:
    """{node id: declared n_comparisons} for every claim node (the claim module's default is 1)."""
    return {nid: int(n.params.get("n_comparisons", 1)) for nid, n in spec.nodes.items()
            if isinstance(n, ModuleNode) and n.module == CLAIM_MODULE}


def runs_per_task_bound(*, generations: int, n_edits: int, n_seeds: int, n_fresh_seeds: int,
                        infra_retries: int) -> int:
    """Upper bound of harness runs on ONE task during a campaign: per generation at most the parent and n_edits
    candidates, each on the race seeds and the fresh re-measure seeds (gate tasks see fewer), each attempted up to
    1 + infra_retries times."""
    return generations * (1 + n_edits) * (n_seeds + n_fresh_seeds) * (1 + infra_retries)


def held_out_target_id(task_dir: Path) -> str:
    """The vault id research.setup registers for a task's held-out data (content-based; register_task). Computed in
    kerneld (truth_query "target_id", GOAL §16.5.2): the host never reads the held-out labels itself; kerneld
    unavailable raises KernelDUnavailable (no in-process fallback)."""
    from ..kerneld.client import truth_query
    return str(truth_query("target_id", task_dir=str(Path(task_dir).resolve()))["target"])


def related(store: EvidenceStore, target: str) -> set[str]:
    """`target` plus every registered dataset tested against the same held-out examples: K4's own rule
    (kernel/checks.related_targets: shared example rows OR a byte-identical private file)."""
    return related_targets(ScopedKernel(store).vault, target)


def kernel_level(store: EvidenceStore, claim_hash: str, plan: dict[str, Any], target: str
                 ) -> tuple[float, int, list[str], float | None]:
    """K4's level for the claim `claim_hash` (analysis plan `plan`, as in its ledger body) on `target` as the ledger
    stands now: (level, effective multiplicity, account reasons, fixed slot level or None without an account). A
    line-by-line mirror of kernel/checks.target_level (which needs a validated Claim in a Context)."""
    kernel = ScopedKernel(store)
    fields = AnalysisPlan.model_fields  # the claim language's own defaults (alpha 0.05, n_comparisons 1)
    a_lvl = float(plan.get("alpha", fields["alpha"].default))
    n_comp = int(plan.get("n_comparisons", fields["n_comparisons"].default))
    rel = related_targets(kernel.vault, target)
    fam, rels = family(kernel.ledger, rel), releases_on(kernel.ledger, rel)
    n_bound = transcript_bound([e.seq for e in fam], rels, kernel.config.count_alphabet)
    accts = A.accounts(kernel.ledger, rel)
    if not accts:
        m = max(n_comp, n_bound)
        return a_lvl / m, m, [], None
    fixed = min(a_lvl / n_comp, *(a.level for a in accts))
    level = fixed
    reasons = [f"{target}: the alpha-account opened at ledger seq {a.seq} on {a.dataset_id!r} covers this target but "
               f"the claim holds no slot in it" for a in accts if not A.holds_slot(kernel.ledger, a, claim_hash)]
    reasons += [f"{target}: the alpha-account opened at ledger seq {a.seq} paid {A.charged(kernel.ledger, a)} "
                f"slot(s) of {a.slots}" for a in accts if A.charged(kernel.ledger, a) > a.slots]
    if any(not A.holds_slot(kernel.ledger, a, str(e.body.get("claim_hash"))) for e in fam for a in accts):
        level = min(level, a_lvl / max(n_comp, n_bound))  # a claim outside the account: Bonferroni
    return level, max(1, round(a_lvl / level)), reasons, fixed


def family_size(store: EvidenceStore, target: str) -> int:
    """Where the campaign pre-check starts on `target`: K4's transcript bound over the family as the ledger stands
    (no account), or 0 on an alpha-account target (the account fixes the level; the declaration does not set it).
    Mirrors kernel/checks.target_level."""
    kernel = ScopedKernel(store)
    if A.accounts(kernel.ledger, related_targets(kernel.vault, target)):
        return 0
    return kernel_family_size(store, target)


def kernel_family_size(store: EvidenceStore, target: str) -> int:
    """K4's multiplicity N on `target` as the ledger stands now (kernel/checks.target_level without an
    alpha-account): the transcript bound over the family's claims, where every release logged before a claim
    multiplies its term (a verdict bit by 2). Equals the claim count only while no release touched the family."""
    kernel = ScopedKernel(store)
    rel = related_targets(kernel.vault, target)
    return transcript_bound([e.seq for e in family(kernel.ledger, rel)], releases_on(kernel.ledger, rel),
                            kernel.config.count_alphabet)


def check_campaign(spec: HarnessSpec, store: EvidenceStore, task_dirs: list[Path], *, runs_per_task: int) -> int:
    """Refuse (MultiplicityDrift) unless every claim node declares n_comparisons >= the campaign's bound. Returns
    the bound. A spec without claim nodes registers no claims: nothing to declare."""
    nodes = claim_nodes(spec)
    if not nodes:
        return 0
    targets: dict[str, int] = {}
    for d in task_dirs:
        t = held_out_target_id(d)
        targets[t] = targets.get(t, 0) + 1
    bound = max((family_size(store, t) + n * len(nodes) * runs_per_task for t, n in targets.items()), default=0)
    low = {nid: m for nid, m in nodes.items() if m < bound}
    if low:
        raise MultiplicityDrift(
            f"claim node(s) {low} declare n_comparisons below this campaign's bound {bound} (claims already "
            "registered on a target + tasks sharing it x claim nodes x runs per task). Without it, Kernel "
            "certification strictness would drift with evaluation order (R2-2). Set params.n_comparisons >= "
            f"{bound} on every claim node of the seed spec (a human edit; claim nodes are protected).")
    return bound


def check_run(store: EvidenceStore, since_seq: int) -> list[dict[str, Any]]:
    """After a run: every claim registered after ledger seq `since_seq` must have been certified at a level that did
    not depend on evaluation order, on each of its targets (kernel_level, the mirror of K4's target_level):
      - no alpha-account: K4's multiplicity max(declared, N) is the declared n_comparisons (N <= declared);
      - an alpha-account: K4's level is the slot level fixed at registration and no account reason applies.
    Returns the checked claims; raises MultiplicityDrift otherwise."""
    checked = []
    for e in ScopedKernel(store).ledger.entries("prereg"):
        if e.seq <= since_seq:
            continue
        claim = e.body.get("claim") or {}
        plan = claim.get("analysis_plan") or {}
        declared = int(plan.get("n_comparisons", 1))
        claim_hash = str(e.body.get("claim_hash"))
        for target in claim.get("generalization_targets") or []:
            level, size, reasons, fixed = kernel_level(store, claim_hash, plan, target)
            if fixed is None and size > declared:
                # K4 certifies at alpha / max(declared, N), N = the transcript bound (not the claim count): the
                # releases between runs (each evolve run's verdict bits) grow N, so counting claims misses the drift
                raise MultiplicityDrift(
                    f"claim {claim_hash[:12]} declared n_comparisons={declared}, but {size} "
                    f"is K4's family size (transcript bound over claims and releases) on target {target}: its "
                    "certification depended on evaluation "
                    "order (R2-2). Outcome discarded; campaign stopped.")
            if fixed is not None and (reasons or level < fixed):
                why = "; ".join(reasons) or (f"a claim on the family holds no slot, so K4 tested at {level:.3g}, "
                                             f"below the slot level {fixed:.3g} fixed at registration")
                raise MultiplicityDrift(
                    f"claim {claim_hash[:12]} on alpha-account target {target}: {why}. Its certification depended "
                    "on evaluation order (R2-2). Outcome discarded; campaign stopped.")
            checked.append({"claim_hash": e.body.get("claim_hash"), "target": target, "family": size,
                            "declared": declared, "level": level, "account": fixed is not None})
    return checked


def last_prereg_seq(store: EvidenceStore) -> int:
    entries = ScopedKernel(store).ledger.entries("prereg")
    return entries[-1].seq if entries else 0
