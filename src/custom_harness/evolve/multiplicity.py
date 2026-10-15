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
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from ..kernel.checks import family, releases_on, shared_examples
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
    """The vault id research.setup registers for a task's held-out data (content-based; register_task)."""
    from ..research.task import load_task
    from ..truth_layout import truth_file
    task_dir = Path(task_dir)
    task = load_task(task_dir)
    parts = [(task_dir / "public" / task.train_file).read_bytes(),
             (task_dir / "public" / task.test_features_file).read_bytes(),
             truth_file(task_dir, task.test_labels_file).read_bytes()]
    digest = hashlib.sha256(b"".join(hashlib.sha256(b).digest() for b in parts)).hexdigest()
    return task.dataset_ids(digest, digest)["test"]


def related(store: EvidenceStore, target: str) -> set[str]:
    """`target` plus every registered dataset sharing held-out example rows with it (K4's rule) or a byte-identical
    private file (R1-F4's rule): a superset of the Kernel's related targets on main and on the R1 fix."""
    vault = ScopedKernel(store).vault
    datasets = vault.datasets()
    if target not in datasets:
        return {target}
    private = {f.blob for f in datasets[target].files.values() if f.private}
    return {target} | {d for d, rec in datasets.items() if d != target and (
        shared_examples(vault, target, d) or any(f.private and f.blob in private for f in rec.files.values()))}


def family_size(store: EvidenceStore, target: str) -> int:
    rel = related(store, target)
    return len({e.body.get("claim_hash") for e in ScopedKernel(store).ledger.entries("prereg")
                if rel & set((e.body.get("claim") or {}).get("generalization_targets") or [])})


def kernel_family_size(store: EvidenceStore, target: str) -> int:
    """K4's multiplicity N on `target` as the ledger stands now (kernel/checks.target_level without an
    alpha-account): the transcript bound over the family's claims, where every release logged before a claim
    multiplies its term (a verdict bit by 2). Equals family_size only while no release touched the family."""
    kernel = ScopedKernel(store)
    rel = related(store, target)
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
    """After a run: every claim registered after ledger seq `since_seq` must have a family <= its declared
    n_comparisons on each target. Returns the checked claims; raises MultiplicityDrift otherwise."""
    checked = []
    for e in ScopedKernel(store).ledger.entries("prereg"):
        if e.seq <= since_seq:
            continue
        claim = e.body.get("claim") or {}
        declared = int((claim.get("analysis_plan") or {}).get("n_comparisons", 1))
        for target in claim.get("generalization_targets") or []:
            # K4 certifies at alpha / max(declared, N), N = the transcript bound (not the claim count): the
            # releases between runs (each evolve run's verdict bits) grow N, so counting claims misses the drift
            size = kernel_family_size(store, target)
            if size > declared:
                raise MultiplicityDrift(
                    f"claim {str(e.body.get('claim_hash'))[:12]} declared n_comparisons={declared}, but {size} "
                    f"is K4's family size (transcript bound over claims and releases) on target {target}: its "
                    "certification depended on evaluation "
                    "order (R2-2). Outcome discarded; campaign stopped.")
            checked.append({"claim_hash": e.body.get("claim_hash"), "target": target, "family": size,
                            "declared": declared})
    return checked


def last_prereg_seq(store: EvidenceStore) -> int:
    entries = ScopedKernel(store).ledger.entries("prereg")
    return entries[-1].seq if entries else 0
