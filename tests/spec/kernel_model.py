"""Clean-room reference model of the Evidence Kernel's vault read rule and certify verdict (K0..K9 + label).

Written ONLY from GOAL.md §4 (goal_s4.md), docs/kernel.md, the public API docstrings (api.md) and the vocabulary in
history.py. It never read kernel/checks.py or any other implementation or test file. Pure functions, stdlib only.

Scope: the history vocabulary cannot express everything the Kernel checks. The model ASSUMES, for every claim:
- K1 inputs the vocabulary cannot vary are well-formed: registered metric "accuracy" (higher is better), the ML
  adapter is human-approved, seeds are >= 3 distinct, alpha and n_boot are valid, reproduce = "all", tolerance 0;
- artifacts and evidence blobs resolve (K0); every record's predictions have the right length, no empty values;
- the comparator ran under identical conditions to the subject (K5); code holds no canary and no label (K9);
- the replayer reproduces every stored prediction exactly (history.py), so K3 fails only by rule, not by data;
- metric values are accuracies in [0, 1] (K8 always passes).
Statistical criterion (paired seed CI and example-bootstrap CI exclude 0 by >= delta in the claim's direction) is an
ORACLE: met on a target exactly when the claim's `strong` is True, its kind is improvement or ablation_effect, and
the statistics could be computed for that target (see `_stats_computable`). For negative_result and equivalence
the criterion is treated as NOT met (the spec does not fix how degenerate zero-variance intervals are handled).
Every reading the spec left open is listed in AMBIGUITIES.md next to this file's build folder.
"""

from __future__ import annotations

from history import (
    LABELS,
    ApproveRole,
    Artifact,
    Certify,
    ClaimSpec,
    Dataset,
    History,
    Prereg,
    Read,
    Record,
    Verdict,
)

CHECK_IDS = ("K0", "K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9")
HELD_OUT_ROLES = frozenset({"test", "sealed", "external", "future"})  # api.md vault.py HELD_OUT_ROLES
EXTERNAL_ROLES = frozenset({"external", "future"})  # api.md vault.py EXTERNAL_ROLES
MIN_SEEDS = 3  # KernelConfig.min_seeds
EFFECT_KINDS = frozenset({"improvement", "ablation_effect"})
CLAIM_PURPOSES = frozenset({"evaluate", "kernel"})  # reads that must name a registered claim on one of its targets


# ---------------------------------------------------------------------------------------------- history views


def _datasets(history: History) -> dict[str, Dataset]:
    """Current registration of every dataset id (a later registration of the same id replaces the earlier one)."""
    out: dict[str, Dataset] = {}
    for ev in history:
        if isinstance(ev, Dataset):
            out[ev.id] = ev
    return out


def _artifacts(history: History) -> dict[str, Artifact]:
    out: dict[str, Artifact] = {}
    for ev in history:
        if isinstance(ev, Artifact):
            out[ev.id] = ev
    return out


def _preregs(history: History) -> dict[str, tuple[int, ClaimSpec]]:
    """Claim key -> (index of its FIRST Prereg event, spec). Pre-registration is idempotent per claim hash."""
    out: dict[str, tuple[int, ClaimSpec]] = {}
    for i, ev in enumerate(history):
        if isinstance(ev, Prereg) and ev.claim.key not in out:
            out[ev.claim.key] = (i, ev.claim)
    return out


def _content(d: Dataset) -> tuple:
    return (d.kind, d.examples, d.private)


def _related(a: Dataset, b: Dataset) -> bool:
    """kernel_doc §1 related_targets: shared example rows, or a byte-identical private file under another id."""
    if a.id == b.id:
        return True
    if a.examples & b.examples:
        return True
    return a.private is not None and a.private == b.private


def _related_ids(ds: dict[str, Dataset], target: str) -> frozenset[str]:
    """The target itself plus every registered dataset directly related to it (one step, not transitive)."""
    if target not in ds:
        return frozenset({target})
    t = ds[target]
    return frozenset({target} | {u.id for u in ds.values() if _related(t, u)})


# ---------------------------------------------------------------------------------------------- vault


def read_allowed(history: History, read: Read) -> bool:
    """Vault.read: the dataset must be registered and have a private (label) file; `evaluate` and `kernel` reads
    must name a pre-registered claim AND the dataset must be one of that claim's generalization targets (R1-F1).
    Every other purpose is accepted (and logged); its consequences are judged by K2/K6, not by the vault."""
    ds = _datasets(history)
    d = ds.get(read.dataset)
    if d is None or d.private is None:
        return False
    if read.purpose in CLAIM_PURPOSES:
        reg = _preregs(history).get(read.claim) if read.claim is not None else None
        return reg is not None and read.dataset in reg[1].targets
    return True


# ---------------------------------------------------------------------------------------------- checks


def _k1(history: History, claim: ClaimSpec) -> bool:
    """Claim parses; scope, metric and comparator resolve; delta > 0 is stated (GOAL §4.3 K1)."""
    ds, arts = _datasets(history), _artifacts(history)
    if not claim.min_effect > 0:
        return False
    if claim.subject not in arts or claim.comparator not in arts:
        return False
    if not claim.targets:
        return False
    return all(t in ds for t in claim.targets) and all(d in ds for d in claim.development)


def _k2(history: History, claim: ClaimSpec) -> bool:
    """Registered before any private read of a target (or a dataset related to one), except the documented
    excuses: leakscan reads, and evaluate/kernel reads for ANOTHER claim registered before the read whose targets
    include this target or a related one (kernel_doc §1). develop/audit/reproduce reads are peeking."""
    regs = _preregs(history)
    if claim.key not in regs:
        return False
    p = regs[claim.key][0]
    ds = _datasets(history)
    rel = {t: _related_ids(ds, t) for t in claim.targets}
    for i, ev in enumerate(history[:p]):
        if not isinstance(ev, Read) or ev.purpose == "leakscan":
            continue
        for group in rel.values():
            if ev.dataset not in group:
                continue
            other = regs.get(ev.claim) if ev.claim is not None else None
            excused = (
                ev.purpose in CLAIM_PURPOSES
                and other is not None
                and ev.claim != claim.key
                and other[0] < i
                and bool(set(other[1].targets) & group)
            )
            if not excused:
                return False
    return True


def _k3(history: History, claim: ClaimSpec, records: tuple[Record, ...]) -> bool:
    """Re-execution reproduces exactly (assumed for every record of a registered dataset), but: no records = nothing
    re-executed (fail closed); and once a recorded certificate of this claim, issued while it was registered,
    failed K3, K3 fails for good (kernel_doc §2, R1-F3)."""
    ds = _datasets(history)
    if not records or any(r.target not in ds for r in records):
        return False
    for i, ev in enumerate(history):
        if isinstance(ev, Certify) and ev.claim.key == claim.key:
            prefix = history[:i]
            if claim.key in _preregs(prefix):
                if not _k3(prefix, ev.claim, ev.records):
                    return False
    return True


def _stats_computable(history: History, claim: ClaimSpec, records: tuple[Record, ...], target: str) -> bool:
    """The Kernel can score target `target`: it reads the labels with purpose `kernel` (vault: only for a REGISTERED
    claim on one of its targets; the dataset must have a private label file), and every planned seed has a record
    in BOTH arms on that target (paired seed test)."""
    d = _datasets(history).get(target)
    if d is None or d.private is None:
        return False
    if not read_allowed(history, Read(dataset=target, purpose="kernel", claim=claim.key)):
        return False
    have = {(r.arm, r.seed) for r in records if r.target == target}
    return all((arm, s) in have for s in claim.seeds for arm in ("subject", "comparator"))


def _criterion(history: History, claim: ClaimSpec, records: tuple[Record, ...], target: str) -> bool:
    """ORACLE (module docstring): met iff strong, kind improvement/ablation_effect, statistics computable."""
    return claim.strong and claim.kind in EFFECT_KINDS and _stats_computable(history, claim, records, target)


def family_sizes(history: History, claim: ClaimSpec) -> dict[str, int]:
    """Bonferroni family size m per target: max(declared n_comparisons, number of distinct claims registered so far
    (this one included, registered or not) whose targets include this target or a related one). Test-set reuse is
    charged as multiplicity over the related set (kernel_doc §1, R1-F4); re-registration after a K3 failure creates
    a new claim, charged the same way (kernel_doc §2)."""
    ds, regs = _datasets(history), _preregs(history)
    out: dict[str, int] = {}
    for t in claim.targets:
        group = _related_ids(ds, t)
        keys = {k for k, (_, spec) in regs.items() if set(spec.targets) & group}
        keys.add(claim.key)
        out[t] = max(claim.n_comparisons, len(keys))
    return out


def _k4(history: History, claim: ClaimSpec, records: tuple[Record, ...]) -> bool:
    """Plan followed (GOAL §4.3 K4; kernel_doc §2 R1-F2; vault docstring "K4 that every held-out evaluation was
    reported"):
    - seeds >= min_seeds distinct; every record inside the plan (target in targets, seed in seeds); no duplicate
      record; exactly the planned seeds per arm and target (each once);
    - every record was logged: an `evaluate` read naming this claim for its dataset, arm and seed exists;
    - every `evaluate` read naming this claim was reported: it matches a submitted record;
    - the effect criterion holds on every target (oracle)."""
    if len(set(claim.seeds)) < MIN_SEEDS or len(set(claim.seeds)) != len(claim.seeds):
        return False
    if len(set(records)) != len(records):
        return False
    planned_seeds, targets = set(claim.seeds), set(claim.targets)
    if any(r.target not in targets or r.seed not in planned_seeds for r in records):
        return False
    for t in claim.targets:
        for arm in ("subject", "comparator"):
            if sorted(r.seed for r in records if r.target == t and r.arm == arm) != sorted(planned_seeds):
                return False
    evals = [ev for ev in history if isinstance(ev, Read) and ev.purpose == "evaluate" and ev.claim == claim.key]
    logged = {(ev.arm, ev.seed, ev.dataset) for ev in evals}
    if any((r.arm, r.seed, r.target) not in logged for r in records):
        return False
    reported = {(r.arm, r.seed, r.target) for r in records}
    if any((ev.arm, ev.seed, ev.dataset) not in reported for ev in evals):
        return False
    return all(_criterion(history, claim, records, t) for t in claim.targets)


def _k5(history: History, claim: ClaimSpec) -> bool:
    """Comparator integrity: identical conditions are assumed; the comparator must resolve to a baseline."""
    a = _artifacts(history).get(claim.comparator)
    return a is not None and a.kind == "baseline"


def _k6(history: History, claim: ClaimSpec, records: tuple[Record, ...]) -> bool:
    """Held-out confirmation on targets never seen during development (GOAL §4.3 K6; vault docstring "K6 that no
    target was ever used for development"; vault.py `develop` "never allowed on a claim's targets"):
    - every target is registered with a held-out role (test/sealed/external/future);
    - no development use: a target (or a dataset related to it) is not in the claim's development list, and no
      `develop` read of a target or a related dataset is logged, at any time;
    - no identical content elsewhere: no other dataset id has the same content as a target;
    - held-out examples are not public with labels elsewhere: no OTHER dataset of kind "labelled" shares an
      example with a target;
    - the prediction is confirmed: the effect criterion holds on every target (oracle)."""
    ds = _datasets(history)
    for t in claim.targets:
        d = ds.get(t)
        if d is None or d.role not in HELD_OUT_ROLES:
            return False
        group = _related_ids(ds, t)
        if any(dev in group for dev in claim.development):
            return False
        if any(isinstance(ev, Read) and ev.purpose == "develop" and ev.dataset in group for ev in history):
            return False
        for u in ds.values():
            if u.id == t:
                continue
            if _content(u) == _content(d):
                return False
            if u.kind == "labelled" and u.examples & d.examples:
                return False
        if not _criterion(history, claim, records, t):
            return False
    return True


def _k7(history: History, claim: ClaimSpec, records: tuple[Record, ...]) -> bool:
    """Risk (fail closed): success must have been improbable under the null (>= 2 bits). For improvement/ablation
    the null success probability is at most alpha/m <= 0.2 (< 2^-2) once the statistics can be computed; a
    negative_result or equivalence is likely under the null (trivially safe), so it fails."""
    if claim.kind not in EFFECT_KINDS:
        return False
    return all(_stats_computable(history, claim, records, t) for t in claim.targets)


def _approved(history: History, dataset_id: str) -> bool:
    """A human role approval bound to the dataset's CURRENT content and role (kernel_doc §4, Vault.role_approval):
    an ApproveRole counts if, when it was written, the dataset had the same content and role as now."""
    current: dict[str, Dataset] = {}
    snaps: list[Dataset] = []
    for ev in history:
        if isinstance(ev, Dataset):
            current[ev.id] = ev
        elif isinstance(ev, ApproveRole) and ev.dataset == dataset_id and dataset_id in current:
            snaps.append(current[dataset_id])
    now = current.get(dataset_id)
    if now is None or now.role not in EXTERNAL_ROLES:
        return False
    return any(s.role == now.role and _content(s) == _content(now) for s in snaps)


# ---------------------------------------------------------------------------------------------- label + verdict


def label_from(checks: dict[str, str], history: History, claim: ClaimSpec) -> str:
    """GOAL §4.4 + kernel_doc §4: REJECTED if any check failed; CERTIFIED-VALIDATED iff every target has role
    external/future AND a human role approval of its current content and role; otherwise CERTIFIED-INTERNAL."""
    if any(checks.get(k) != "pass" for k in CHECK_IDS):
        return LABELS[2]
    ds = _datasets(history)
    if all(t in ds and ds[t].role in EXTERNAL_ROLES and _approved(history, t) for t in claim.targets):
        return LABELS[0]
    return LABELS[1]


def _verdict(history: History, claim: ClaimSpec, records: tuple[Record, ...]) -> Verdict:
    ok = {
        "K0": True,  # provenance: every number resolves (assumed)
        "K1": _k1(history, claim),
        "K2": _k2(history, claim),
        "K3": _k3(history, claim, records),
        "K4": _k4(history, claim, records),
        "K5": _k5(history, claim),
        "K6": _k6(history, claim, records),
        "K7": _k7(history, claim, records),
        "K8": True,  # background consistency: accuracy in [0, 1] (assumed)
        "K9": True,  # leak & canary audit: no canary or label in code (assumed)
    }
    checks = {k: ("pass" if ok[k] else "fail") for k in CHECK_IDS}
    return Verdict(checks=checks, label=label_from(checks, history, claim), family=family_sizes(history, claim))


def certify(history: History, claim_key: str, records: tuple[Record, ...]) -> Verdict:
    """Verdict for certifying the pre-registered claim `claim_key` with `records` after `history`."""
    regs = _preregs(history)
    if claim_key not in regs:
        raise KeyError(f"claim {claim_key!r} was never pre-registered: use certify_unregistered")
    return _verdict(history, regs[claim_key][1], records)


def certify_unregistered(history: History, claim: ClaimSpec, records: tuple[Record, ...]) -> Verdict:
    """Verdict for certifying a claim given by its spec (normally one that was never pre-registered)."""
    return _verdict(history, claim, records)
