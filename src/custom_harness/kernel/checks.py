"""Kernel checks K0-K9 (GOAL §4.3). Deterministic, LLM-free, fail closed.

Each check reads the Context (claim, ledger, vault, parsed evidence) and returns a CheckResult. Any
exception inside a check is a failure of that check, never a pass (see kernel.Kernel.certify).
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import ValidationError

from ..records import EvidenceKind
from ..store.evidence import EvidenceStore
from ..verify import leak
from ..verify import noninterference as ni
from . import adapters, alpha, predictions, simulated
from .certificate import CHECK_IDS, Certificate, CheckResult
from .claim import SUPPORTED_KINDS, AnalysisPlan, Claim, ClaimKind, EvidenceClass, exact_value, strict_json
from .execution import ExecRecord, ExecutionError
from .ledger import AsOfLedger, Entry, Ledger, verify_chain
from .reasons import Code, R, Refusal, coded, codes_of, describe, exception_name
from .releases import Alphabet, exposed, transcript_bound
from .vault import EXTERNAL_ROLES, HELD_OUT_ROLES, ArtifactRecord, CertificateRecord, Vault, VaultError

# K2: an INTEGRITY read (building leak fingerprints) never feeds a model; it is listed on the certificate, not counted
# as peeking. TRUST BOUNDARY (docs/kernel.md): purposes are declared by trusted host code, so a host that mislabels a
# peek as "leakscan" is not caught. "reproduce" is NOT excused: nothing on a live store reads with it (bundles are
# re-checked on a fresh store with purpose "kernel"), so excusing it would only widen the trusted surface.
INTEGRITY_PURPOSES = frozenset({"leakscan"})


class Replayer(Protocol):
    def replay(self, record: ExecRecord, vault: Vault) -> list[str] | None: ...


@dataclass(frozen=True)
class KernelConfig:
    """Human-owned policy (L9). Not part of any claim, so a claimant cannot relax it."""

    # 1.1: domain adapters (K1), evidence classes, shared-example checks (K4/K6), K7 fail-closed
    # 1.2 (§16): closed reason codes, q >= p0 (K1), alpha-accounts and releases (K4/K6), K3 reliability
    # demonstration, plan-code divergence (K4), replayed evidence refused (K0), self-reported tier cap
    # 1.3 (s3-kernel): strict JSON, alpha slots pay releases (K4), K1 statement = adapter rendering (exact/formal),
    # K3 non-interference and CPU-time replays, core/adapter split. A certificate of 1.2 that re-derives differently
    # is then reported STALE ("other rules"), never "forged" (Kernel.verify_status)
    version: str = "1.3"
    min_seeds: int = 3
    max_alpha: float = 0.2
    min_boot: int = 1000
    min_risk_bits: float = 2.0  # K7: success must have been at most 2^-2 = 25% likely under the null
    max_reproduce_tolerance: float = 0.02  # K1: a looser declared re-execution tolerance makes K3 meaningless
    # K3 reliability demonstration presets, frozen into a claim's pre-registration when it declares none
    k3_eps_max: float = 0.2  # UNCALIBRATED (owner decision, GOAL §13.8)
    k3_beta: float = 0.1  # UNCALIBRATED (owner decision, GOAL §13.8): with eps 0.2 -> 11 clean replays
    max_eps_max: float = 0.5  # UNCALIBRATED (owner decision, GOAL §13.8): loosest failure rate a claim may declare
    max_beta: float = 0.25  # UNCALIBRATED (owner decision, GOAL §13.8): loosest confidence a claim may declare
    count_alphabet: int = 16  # UNCALIBRATED (owner decision, GOAL §13.8): transcripts one `count` release can tell
    # a certificate that relies on a field no human approved is capped at this tier (int(tiers.Tier); 4 = T4)
    self_reported_tier_cap: int = 4  # UNCALIBRATED (owner decision, GOAL §13.8)
    delta_floor: float = 0.02  # UNCALIBRATED (owner decision, GOAL §13.8): min_effect floor on minted targets
    nonstat_p0: float = 0.5  # UNCALIBRATED (owner decision, GOAL §13.8): VDS p0 for classes without a null level
    # §16.10 bounded nulls (docs/bounded_null.md): the declared power range and the plasmode's confidence 1 - gamma
    min_power: float = 0.5  # UNCALIBRATED (owner decision, GOAL §13.8)
    max_power: float = 0.95  # UNCALIBRATED (owner decision, GOAL §13.8): 0.95 -> 45 plantings at gamma 0.1
    plasmode_gamma: float = 0.1  # UNCALIBRATED (owner decision, GOAL §13.8)


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
    stats: dict[str, Any] = field(default_factory=dict)  # target -> the adapter's statistics (ml.TargetStats)
    prereg: Entry | None = None
    adapter: dict[str, Any] | None = None  # the approved adapter K1 resolved for this claim
    certificates: list[ArtifactRecord] = field(default_factory=list)  # computational: certificate artifacts (K0)
    exclusion: dict[str, Any] | None = None  # §16.10: a bounded-null claim's exclusion statement, set by K7

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

    def material(self) -> str:
        """The plan-matched evidence (a claim target and a planned seed), fixed once the evidence is: it seeds K3's
        replay choice and K7's plasmode, so neither can be known or steered in advance (R1-F2)."""
        targets, seeds_planned = set(self.claim.generalization_targets), set(self.claim.analysis_plan.seeds)
        return "|".join(sorted(r.preds_ref for _, r in self.records if r.target in targets and r.seed in seeds_planned))


def procedures(ctx: Context) -> Any:
    """The domain adapter's procedures for this claim (K1, K3-K8), found by the REGISTRY (evidence class and metric:
    adapters.procedures_for), not by the approval, which K1 judges. The core holds no domain logic."""
    return importlib.import_module(adapters.procedures_for(ctx.claim.evidence_class, ctx.claim.metric.id))


def __getattr__(name: str) -> Any:
    """Names that moved to the ML adapter's procedures (kernel/ml.py) on 2026-10-09, still importable from here."""
    moved = {"TargetStats": "TargetStats", "_criterion": "criterion", "plan_code_divergence": "plan_code_divergence",
             "REGISTERED_TESTS": "REGISTERED_TESTS", "SCORE_TOLERANCE": "SCORE_TOLERANCE"}
    if name in moved:
        return getattr(importlib.import_module(".ml", __package__), moved[name])
    raise AttributeError(name)


def _result(reasons: list[str], observations: list[str] | None = None) -> CheckResult:
    """Every reason must carry a closed code (reasons.Reason); an uncoded one raises, which fails the check."""
    return CheckResult(status="fail" if reasons else "pass", reasons=tuple(reasons), codes=codes_of(reasons),
                       observations=tuple(observations or ()))


# ------------------------------------------------------------------ shared examples across datasets
def _csv_rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8", errors="replace"))))


def shared_examples(vault: Vault, target: str, other: str) -> dict[str, tuple[int, bool]]:
    """Held-out examples of `target` (rows of its public files) found in `other`'s public files, compared on the
    target file's columns (exact text). Returns {"other/file": (count, may_carry_labels)}, where may_carry_labels
    means the other file has columns beyond the target's features (a label column can be among them)."""
    found: dict[str, tuple[int, bool]] = {}
    targets = [_csv_rows(vault.store.blobs.get(f.blob)) for f in vault.dataset(target).files.values() if not f.private]
    for name, f in vault.dataset(other).files.items():
        data = [] if f.private else _csv_rows(vault.store.blobs.get(f.blob))
        for t in targets:
            if not data or not t or not set(t[0]) <= set(data[0]):
                continue
            idx = [data[0].index(col) for col in t[0]]
            rows = {tuple(r) for r in t[1:]}
            n = len(rows & {tuple(r[i] for i in idx) for r in data[1:] if len(r) == len(data[0])})
            if n:
                found[f"{other}/{name}"] = (n, len(data[0]) > len(t[0]))
    return found


def label_columns(data: bytes, labels: Sequence[str]) -> list[str]:
    """KD3 (assure, 2026-10-07): the columns of a CSV whose values are exactly `labels`, row by row (hidden labels
    shipped as a column of a public or program input file). Exact text after stripping; a file with another number
    of rows, or a short row, never matches; single-class labels carry no information and never match. No length
    floor: a column that reproduces the held-out labels makes the target trivially solvable, so failing closed on
    a small file is the conservative direction. Not detected (stated): re-mapped, noisy or re-ordered copies."""
    rows = _csv_rows(data)
    if len(set(labels)) < 2 or len(rows) - 1 != len(labels):
        return []
    return [name for j, name in enumerate(rows[0])
            if all(len(r) > j and r[j].strip() == y.strip() for r, y in zip(rows[1:], labels, strict=True))]


def related_targets(vault: Vault, target: str) -> set[str]:
    """`target` plus every other dataset tested against the same held-out examples: sharing example rows (exact
    text, `shared_examples`) or sharing a byte-identical PRIVATE file (the same labels under another id, R1-F4)."""
    datasets = vault.datasets()
    if target not in datasets:  # unregistered (K1 reports it): related to nothing
        return {target}
    private = {f.blob for f in datasets[target].files.values() if f.private}
    return {target} | {d for d, rec in datasets.items() if d != target and (
        shared_examples(vault, target, d) or any(f.private and f.blob in private for f in rec.files.values()))}


# ------------------------------------------------------------------ fixed at registration (§16.4, invariant 17)
def registration(ledger: Ledger, claim_hash: str) -> Entry | None:
    """The claim's (first) pre-registration entry."""
    return next((e for e in ledger.entries("prereg") if e.body.get("claim_hash") == claim_hash), None)


def family(ledger: Ledger, related: set[str]) -> list[Entry]:
    """First pre-registration of every claim with a generalization target in `related`, in ledger order."""
    seen: dict[str, Entry] = {}
    for e in ledger.entries("prereg"):
        if related & set(e.body.get("claim", {}).get("generalization_targets", [])):
            seen.setdefault(str(e.body.get("claim_hash")), e)
    return list(seen.values())


def releases_on(ledger: Ledger, related: set[str]) -> list[tuple[int, Alphabet]]:
    return [(e.seq, Alphabet(e.body["alphabet"])) for e in ledger.entries("release")
            if e.body.get("dataset_id") in related]


def registration_level(ledger: Ledger, vault: Vault, claim: Claim, cfg: KernelConfig) -> float:
    """p0: an upper bound, fixed when the claim is registered, on P(certified | no effect). K4 later tests each
    target at a level that can only be lower (families only grow), so p0 stays valid whatever happens afterwards."""
    if claim.evidence_class != EvidenceClass.DATA:
        return cfg.nonstat_p0
    level = claim.analysis_plan.alpha / max(1, claim.analysis_plan.n_comparisons)
    for t in claim.generalization_targets:
        for acct in alpha.accounts(ledger, related_targets(vault, t)):
            level = min(level, acct.level)
    return level


def reliability(claim: Claim, cfg: KernelConfig) -> tuple[float, float, int]:
    """(eps_max, beta, n) of the K3 zero-failure demonstration: n = ceil(ln beta / ln(1 - eps_max)) clean replays make
    P(pass) <= beta for any program that fails to reproduce with probability >= eps_max (independent trials)."""
    eps = claim.analysis_plan.eps_max if claim.analysis_plan.eps_max is not None else cfg.k3_eps_max
    beta = claim.analysis_plan.beta if claim.analysis_plan.beta is not None else cfg.k3_beta
    if not (0 < eps < 1 and 0 < beta < 1):
        return eps, beta, 0
    return eps, beta, max(1, math.ceil(math.log(beta) / math.log(1 - eps) - 1e-12))


def fixed_terms(ledger: Ledger, vault: Vault, claim: Claim, cfg: KernelConfig) -> dict[str, Any]:
    """Everything a verdict depends on that must not move after registration: p0 (VDS baseline and the K1 q >= p0
    rule), the pinned reference forecast p_ref (Laplace base rate of the certificates resolved so far), and the K3
    reliability requirement. Stored in the prereg entry."""
    # a malformed certificate entry counts as not certified (predictions._outcome; it never crashes registration)
    outcomes = {e.body.get("claim_hash"): bool(predictions.outcome_of(e.body)) for e in ledger.entries("certificate")}
    eps, beta, n = reliability(claim, cfg)
    return {"p0": registration_level(ledger, vault, claim, cfg),
            "p_ref": (sum(outcomes.values()) + 1) / (len(outcomes) + 2), "reference": "laplace-base-rate/1",
            "eps_max": eps, "beta": beta, "replays": n}


def fixed(ctx: Context) -> dict[str, Any]:
    """The registered fixed terms (or, for a pre-registration made before they existed, the same terms computed from
    the registered claim on the ledger as it stood at registration)."""
    entry = ctx.prereg or registration(ctx.ledger, ctx.claim_hash)
    if entry is not None and isinstance(entry.body.get("fixed"), dict):
        return dict(entry.body["fixed"])
    ledger = ctx.ledger if entry is None else AsOfLedger(ctx.store, entry.seq - 1)
    return fixed_terms(ledger, Vault(ctx.store, ledger), ctx.claim, ctx.config)


def slot_charge(ledger: Ledger, vault: Vault, account: alpha.Account, cfg: KernelConfig) -> int:
    """Slots a claim registered NOW on the account's target family pays: the transcripts reachable from the releases
    logged so far (at least 1). Fixed at registration (recorded in its alpha_reserve entry)."""
    rel = releases_on(ledger, related_targets(vault, account.dataset_id))
    return max(1, transcript_bound([max((at for at, _ in rel), default=0) + 1], rel, cfg.count_alphabet))


def target_level(ctx: Context, target: str) -> tuple[float, int, list[str]]:
    """K4's fixed per-target level, the effective multiplicity it implies, and coded reasons. Without an alpha-account:
    alpha / max(n_comparisons, N), where N = the transcript bound over the claims on the target family (= their count
    under the verdict embargo; today's rule). With an account: the slot level alpha_T / S_T (and the declared level),
    fixed whatever the order of claims and releases, because releases were paid in slots at registration
    (alpha.py); an account that paid more than it holds (a ledger written around preregister) fails closed."""
    plan, reasons = ctx.claim.analysis_plan, list[str]()
    related = related_targets(ctx.vault, target)
    fam, rel = family(ctx.ledger, related), releases_on(ctx.ledger, related)
    n_bound = transcript_bound([e.seq for e in fam], rel, ctx.config.count_alphabet)
    accts = alpha.accounts(ctx.ledger, related)
    if not accts:
        m = max(plan.n_comparisons, n_bound)
        return plan.alpha / m, m, reasons
    level = min(plan.alpha / plan.n_comparisons, *(a.level for a in accts))
    reasons += [R(Code.K4_NO_SLOT, f"{target}: an alpha-account (opened at ledger seq {a.seq} on {a.dataset_id!r}) "
                                   f"covers this target but the claim holds no slot in it") for a in accts
                if not alpha.holds_slot(ctx.ledger, a, ctx.claim_hash)]
    reasons += [R(Code.K4_ACCOUNT_OVERDRAWN, f"{target}: the alpha-account opened at ledger seq {a.seq} paid "
                                             f"{alpha.charged(ctx.ledger, a)} slot(s) of {a.slots}")
                for a in accts if alpha.charged(ctx.ledger, a) > a.slots]
    if any(not alpha.holds_slot(ctx.ledger, a, str(e.body.get("claim_hash"))) for e in fam for a in accts):
        level = min(level, plan.alpha / max(plan.n_comparisons, n_bound))  # a claim outside the account: Bonferroni
    return level, max(1, round(plan.alpha / level)), reasons


def replayed_marks(store: EvidenceStore, run_id: str) -> int:
    """Trace steps of the run that carry a `replayed` marker (cassette output: invariant 21)."""
    n = 0
    for step in store.trace(run_id):
        calls = step.output.get("_module_llm_calls") if isinstance(step.output, dict) else None
        marks = [getattr(step.provenance, "replayed", False) is True, step.output.get("replayed") is True,
                 step.input.get("replayed") is True,
                 *(isinstance(c, dict) and isinstance(c.get("provenance"), dict)
                   and c["provenance"].get("replayed") is True for c in (calls if isinstance(calls, list) else []))]
        n += any(marks)
    return n


# ------------------------------------------------------------------ K0 provenance
def k0_provenance(ctx: Context) -> CheckResult:
    """Every artifact, record, prediction and input resolves to an intact stored object."""
    reasons: list[str] = []
    arts = ctx.vault.artifacts()
    for role, ref in (("subject", ctx.claim.subject), ("comparator", ctx.claim.comparator)):
        reg = arts.get(ref.artifact_id)
        if reg is None or reg.hash != ref.hash:
            reasons.append(R(Code.K0_ARTIFACT_UNREGISTERED,
                f"{role} artifact {ref.artifact_id!r} is not registered with hash {ref.hash[:12]}"))
        elif not _blob_ok(ctx.store, ref.hash):
            reasons.append(R(Code.K0_ARTIFACT_BYTES, f"{role} artifact bytes missing or corrupted"))
    if not ctx.evidence_ids:
        reasons.append(R(Code.K0_NO_EVIDENCE, "no evidence records"))
    datasets = ctx.vault.datasets()
    for eid in ctx.evidence_ids:
        try:
            ev = ctx.store.get_evidence(eid)
            if ev.kind != EvidenceKind.METRIC:
                raise Refusal(f"kind {ev.kind} is not a metric record")
            rec = ExecRecord.model_validate(strict_json(ctx.store.blobs.get(ev.payload_ref)))
            if rec.claim_hash != ctx.claim_hash:
                raise Refusal("record belongs to a different claim")
            preds = strict_json(ctx.store.blobs.get(rec.preds_ref))
            if not isinstance(preds, list) or not all(isinstance(p, str) for p in preds):
                raise Refusal("predictions blob is not a list of strings")
            if not _blob_ok(ctx.store, rec.artifact_hash):
                raise Refusal("code blob missing or corrupted")
            for inp in rec.inputs.values():
                if inp.dataset_id not in datasets or not _blob_ok(ctx.store, inp.blob):
                    raise Refusal(f"input {inp.dataset_id}/{inp.file} unresolvable")
            if rec.target not in datasets:
                raise Refusal(f"target {rec.target!r} unregistered")
            ctx.store.get_run(rec.run_id)
            marks = replayed_marks(ctx.store, rec.run_id)
        except (KeyError, ValueError, OSError, ValidationError, json.JSONDecodeError) as e:
            reasons.append(R(Code.K0_EVIDENCE_UNRESOLVABLE, f"evidence {eid}: {describe(e)}"))
            continue
        if rec.replayed or marks:  # invariant 21: replayed model output is never evidence
            reasons.append(R(Code.K0_REPLAYED_EVIDENCE, f"evidence {eid}: derived from replayed model output "
                                                        f"(marker {rec.replayed}, {marks} replayed trace step(s))"))
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
        reasons.append(R(Code.K1_KIND, f"claim kind {c.kind.value!r} is not checkable by Kernel v{cfg.version}"))
    if not c.effect.min_effect > 0:  # finiteness is enforced by the Claim model itself
        reasons.append(R(Code.K1_MIN_EFFECT, "minimum effect must be a finite number > 0"))
    if c.value is not None:
        reasons.append(R(Code.K1_VALUE, "only computed_value claims carry an exact value"))
    reasons += procedures(ctx).metric_reasons(c)
    # GOAL §4.7 / invariant 13: only a human-approved adapter, unchanged since its approval, may check this class
    ctx.adapter, why = adapters.resolve(ctx.ledger, c.evidence_class, c.metric.id)
    reasons += coded(Code.K1_ADAPTER, why)
    if c.subject.hash == c.comparator.hash:
        reasons.append(R(Code.K1_SAME_ARTIFACT, "subject and comparator are the same artifact"))
    targets, dev = set(c.generalization_targets), set(c.development)
    if not targets or len(targets) != len(c.generalization_targets):
        reasons.append(R(Code.K1_TARGETS, "generalization targets must be non-empty and unique"))
    if targets & dev:
        reasons.append(R(Code.K1_TARGETS, f"targets also used in development: {sorted(targets & dev)}"))
    known = ctx.vault.datasets()
    unknown = sorted((targets | dev | set(c.scope.datasets)) - set(known))
    if unknown:
        reasons.append(R(Code.K1_DATASETS, f"unregistered datasets: {unknown}"))
    if len(set(plan.seeds)) != len(plan.seeds) or len(plan.seeds) < cfg.min_seeds:
        reasons.append(R(Code.K1_PLAN, f"analysis plan needs >= {cfg.min_seeds} distinct seeds"))
    if not 0 < plan.alpha <= cfg.max_alpha:
        reasons.append(R(Code.K1_PLAN, f"alpha must be in (0, {cfg.max_alpha}]"))
    if plan.n_comparisons < 1 or plan.n_boot < cfg.min_boot:
        reasons.append(R(Code.K1_PLAN, f"n_comparisons must be >= 1 and n_boot >= {cfg.min_boot}"))
    if not 0 <= plan.reproduce_tolerance <= cfg.max_reproduce_tolerance:
        reasons.append(R(Code.K1_PLAN, f"reproduce_tolerance must be in [0, {cfg.max_reproduce_tolerance}]"))
    e = c.effect
    if not e.predicted_lo <= e.predicted_hi:
        reasons.append(R(Code.K1_EFFECT, "predicted effect interval must be finite with lo <= hi"))
    if not 0 < e.probability < 1:
        reasons.append(R(Code.K1_PROBABILITY, "declared probability must be strictly between 0 and 1"))
    if c.risk.prior_probability is not None and not 0 < c.risk.prior_probability < 1:
        reasons.append(R(Code.K1_RISK_PRIOR, "risk prior must be strictly between 0 and 1"))
    minted = sorted({str(e.body.get("dataset_id")) for e in ctx.ledger.entries("mint")} & targets)
    if minted and c.effect.min_effect < cfg.delta_floor:
        reasons.append(R(Code.K1_DELTA_FLOOR, f"minimum effect {c.effect.min_effect} is below the floor "
                                              f"{cfg.delta_floor} on minted target(s) {minted}"))
    eps, beta, _ = reliability(c, cfg)
    if not (0 < eps <= cfg.max_eps_max and 0 < beta <= cfg.max_beta):
        reasons.append(R(Code.K1_RELIABILITY, f"K3 reliability requirement must have eps_max in (0, {cfg.max_eps_max}] "
                                              f"and beta in (0, {cfg.max_beta}]"))
    power = plan.power
    if power is not None and not (c.kind == ClaimKind.NEGATIVE_RESULT and cfg.min_power <= power <= cfg.max_power):
        reasons.append(R(Code.K1_POWER, f"a declared power is for negative_result claims only, in [{cfg.min_power}, "
                                        f"{cfg.max_power}]"))
    return _result(reasons + p0_reasons(ctx))


def p0_reasons(ctx: Context) -> list[str]:
    """§16.4 (F2): a forecast below the registered level p0 is refused, so a miss can never score positive."""
    if ctx.claim.evidence_class != EvidenceClass.DATA:  # no null level: scored with LR = 1 when q < p0, not refused
        return []
    p0 = float(fixed(ctx)["p0"])
    q = ctx.claim.effect.probability
    return [R(Code.K1_PROBABILITY_BELOW_P0, f"declared probability {q} is below the claim's registered level p0 = "
                                            f"{p0:.4g}: such a forecast could earn VDS by failing")] if q < p0 else []


# ------------------------------------------------------------------ K2 pre-registered
def k2_preregistered(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    reasons += coded(Code.K2_CHAIN, verify_chain(ctx.ledger.entries(), full=ctx.full_chain))
    regs = [e for e in ctx.ledger.entries("prereg") if e.body.get("claim_hash") == ctx.claim_hash]
    if not regs:
        return _result([*reasons, R(Code.K2_NOT_REGISTERED, "the claim was never pre-registered")])
    prereg = regs[0]
    ctx.prereg = prereg
    try:
        registered = Claim.model_validate(prereg.body["claim"])
        if registered.claim_hash() != ctx.claim_hash:
            reasons.append(R(Code.K2_PREREG_MISMATCH, "the pre-registered claim content does not match its hash"))
    except (KeyError, ValidationError) as e:
        reasons.append(R(Code.K2_PREREG_MISMATCH, f"pre-registration body unreadable: {exception_name(e)}"))
    first_prereg = {e.body.get("claim_hash"): e for e in reversed(ctx.ledger.entries("prereg"))}
    obs: list[str] = []
    for target in ctx.claim.generalization_targets:
        # Peeking = any read of the target's private data before this claim's registration, EXCEPT:
        #  - evaluate/kernel reads made for ANOTHER claim that was registered before the read and whose targets
        #    include this target or a related one: that is test-set reuse, charged by K4 as multiplicity;
        #  - integrity reads (leakscan: building leak fingerprints), which never feed a model; they are listed
        #    below for accountability (purposes are declared by trusted host code: a trust boundary).
        # A read for a claim registered on an unrelated target, or for an unregistered claim, is peeking.
        related = related_targets(ctx.vault, target)
        early, integrity = [], []
        # KD4 (assure, 2026-10-08): a read of a RELATED dataset (the same held-out rows or byte-identical labels
        # under another id) is a read of this target, with the same excuses
        for a in sorted((a for d in related for a in ctx.vault.accesses(d)), key=lambda e: e.seq):
            if not a.body.get("private") or a.seq >= prereg.seq:
                continue
            purpose, ch = a.body.get("purpose"), a.body.get("claim_hash")
            if purpose in INTEGRITY_PURPOSES:
                integrity.append(a)
                continue
            other = first_prereg.get(ch)
            excused = (purpose in ("evaluate", "kernel") and ch != ctx.claim_hash and other is not None
                       and other.seq < a.seq
                       and bool(related & set(other.body.get("claim", {}).get("generalization_targets", []))))
            if not excused:
                early.append(a)
        if early:
            reasons.append(R(Code.K2_PEEK, f"private data of target {target!r} was read {len(early)} time(s) before "
                           f"pre-registration (first at ledger seq {early[0].seq}, dataset "
                           f"{early[0].body.get('dataset_id')!r}, purpose {early[0].body.get('purpose')!r})"))
        if integrity:
            obs.append(f"{target}: {len(integrity)} integrity read(s) (leakscan) before pre-registration: not "
                       f"counted as peeking (never fed to a model; purpose declared by trusted host code)")
    arts = ctx.vault.artifacts()
    for ref in (ctx.claim.subject, ctx.claim.comparator):
        reg = arts.get(ref.artifact_id)
        if reg is not None and reg.seq > prereg.seq:
            reasons.append(R(Code.K2_ARTIFACT_AFTER,
                f"artifact {ref.artifact_id!r} was registered after the pre-registration"))
    return _result(reasons, [f"pre-registered at ledger seq {prereg.seq}", *obs])


# ------------------------------------------------------------------ K3 reproduced
def k3_reproduced(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    obs: list[str] = []
    if not ctx.records:
        return _result([R(Code.K3_NO_RECORDS, "no usable evaluation records")])
    reasons += procedures(ctx).recompute(ctx)
    reasons += _earlier_reproduction_failures(ctx)
    if ctx.replayer is None:
        return _result([*reasons, R(Code.K3_NO_REPLAYER,
            "no replayer configured: re-execution was not performed")], obs)
    plan = ctx.claim.analysis_plan
    chosen = [(e, r) for e, r in ctx.records if plan.reproduce == "all" or r.seed == replay_seed(ctx, r)]
    if not chosen:
        reasons.append(R(Code.K3_NONE_SELECTED, "no record selected for re-execution"))
    # §16.4 zero-failure reliability demonstration: replay the selected records in turn until `replays` consecutive
    # clean re-executions (each selected record at least once); the first failure rejects. The requirement was
    # frozen at registration (fixed terms), so it cannot be loosened after the evidence is seen.
    terms = fixed(ctx)
    eps, beta, need = float(terms["eps_max"]), float(terms["beta"]), int(terms["replays"])
    trials = [chosen[i % len(chosen)] for i in range(max(need, len(chosen)))] if chosen else []
    done = 0
    for eid, rec in trials:
        failure = _replay_once(ctx, eid, rec)
        done += 1
        if failure is not None:
            reasons.append(failure)
            break
    if not reasons:
        for eid, rec in chosen:
            reasons += _noninterference(ctx, eid, rec, obs)
    obs.append(f"re-executed {done} time(s) over {len(chosen)} record(s) ({plan.reproduce}); "
               f"zero-failure demonstration: {need} clean replay(s) needed for failure rate <= {eps:g} at "
               f"confidence {1 - beta:g} (assumes independent replays)")
    return _result(reasons, obs)


def truth_copies(ledger: Ledger, target: str) -> dict[tuple[str, str], list[str]]:
    """§16.5.4: the declared agent-reachable copies of `target`'s truth: (dataset id, public file) -> label columns."""
    out: dict[tuple[str, str], list[str]] = {}
    for e in ledger.entries("truth_copy"):
        if e.body.get("target") == target:
            key = (str(e.body.get("dataset_id")), str(e.body.get("file")))
            out.setdefault(key, []).append(str(e.body.get("column")))
    return out


def _noninterference(ctx: Context, eid: str, rec: ExecRecord, obs: list[str]) -> list[str]:
    """§16.5.4 (invariant 18), a required K3 sub-check when the record's inputs hold a declared copy of its target's
    truth: re-run with every declared copy flipped, then constant, and compare DECISIONS with the stored production
    output (McNemar fallback for nondeterministic programs). Any irregularity fails closed."""
    declared = truth_copies(ctx.ledger, rec.target)
    copies = [ni.TruthCopy(role, col) for role, ref in sorted(rec.inputs.items())
              for col in declared.get((ref.dataset_id, ref.file), [])]
    if not copies:
        return []
    where = f"non-interference of {rec.arm} seed {rec.seed} on {rec.target}"
    run_inputs = getattr(ctx.replayer, "run_inputs", None)
    if run_inputs is None:
        return [R(Code.K3_NONINTERFERENCE, f"{where}: the replayer cannot replace inputs (fail closed)")]
    files = {c.name: ctx.vault.read_public(rec.inputs[c.name].dataset_id, rec.inputs[c.name].file) for c in copies}
    try:
        rep = ni.replay_noninterference(lambda fs: run_inputs(rec, ctx.vault, fs), files, copies, ctx.preds[eid],
                                        regression=procedures(ctx).is_regression(rec.metric))
    except (ExecutionError, VaultError, OSError) as e:
        return [R(Code.K3_NONINTERFERENCE, f"{where} failed: {exception_name(e)}")]
    obs.append(f"{where}: {rep.summary()}")
    return [] if rep.ok else [R(Code.K3_NONINTERFERENCE, f"{where}: {rep.mode}, "
                                                         f"{','.join(r.value for r in rep.reasons)}")]


def _replay_once(ctx: Context, eid: str, rec: ExecRecord) -> str | None:
    """One re-execution of a record: None if it reproduced, else the coded reason."""
    where = f"re-execution of {rec.arm} seed {rec.seed}"
    assert ctx.replayer is not None
    try:
        again = ctx.replayer.replay(rec, ctx.vault)
    except (ExecutionError, VaultError, OSError) as e:
        return R(Code.K3_REPLAY_ERROR, f"{where} failed: {exception_name(e)}")
    if again is None:
        return R(Code.K3_REPLAY_NO_OUTPUT, f"{where} produced no predictions")
    failure: str | None = procedures(ctx).replay_failure(ctx, eid, rec, again, where)
    return failure


def _earlier_reproduction_failures(ctx: Context) -> list[str]:
    """R1-F3: certify may be called again on the same claim, and a program that reproduces only sometimes would pass
    on a lucky replay. So once a RECORDED certificate of this claim failed K3, K3 fails for good. Only certificates
    issued while the claim was registered count (an unregistered claim's K3 is 'not evaluated', not a failure to
    reproduce); a certificate rejected for another reason (e.g. K1 before an adapter approval) blocks nothing. An
    unreadable or tampered earlier certificate counts as a failure (fail closed)."""
    out = []
    for e in ctx.ledger.entries("certificate"):
        if e.body.get("claim_hash") != ctx.claim_hash:
            continue
        try:
            earlier = Certificate.model_validate(strict_json(ctx.store.blobs.get(e.body["blob"])))
            if earlier.cert_hash() != e.body.get("cert_hash"):
                raise ValueError("content does not match its ledger entry")
        except (KeyError, ValueError, OSError, ValidationError) as err:
            out.append(R(Code.K3_EARLIER_FAILURE,
                f"earlier certificate at ledger seq {e.seq} is unreadable ({exception_name(err)}): its K3 "
                       f"result is unknown (fail closed)"))
            continue
        if earlier.prereg_seq is not None and earlier.checks["K3"].status != "pass":
            out.append(R(Code.K3_EARLIER_FAILURE,
                f"an earlier certificate of this claim (ledger seq {e.seq}) failed K3: a claim that did not "
                       f"reproduce once is not certified by trying again (re-register a new claim instead)"))
    return out


def replay_seed(ctx: Context, rec: ExecRecord) -> int:
    """The seed re-executed for (arm, target) under 'sampled_seed': derived from a hash of ALL evidence after it
    is fixed, so a claimant cannot know in advance which seed will be checked. (Re-running evaluations to
    steer it would leave unreported held-out reads, which K4 rejects.) Only PLAN-MATCHED records (a claim target
    and a planned seed) enter the hash: extra records cannot steer it (R1-F2), and K4 rejects them anyway."""
    digest = hashlib.sha256(f"{ctx.claim_hash}|{rec.arm}|{rec.target}|{ctx.material()}".encode()).digest()
    seeds = sorted(ctx.claim.analysis_plan.seeds)
    return seeds[int.from_bytes(digest[:8], "big") % len(seeds)]


# ------------------------------------------------------------------ K4 statistically valid procedure
def k4_statistics(ctx: Context) -> CheckResult:
    """The plan was followed (exact seeds, no unreported held-out evaluations), multiplicity is accounted
    for, and the intervals are computed. Whether the effect criterion holds is K6's verdict."""
    reasons: list[str] = []
    obs: list[str] = []
    c = ctx.claim
    plan = c.analysis_plan
    if not ctx.records:
        return _result([R(Code.K4_NO_RECORDS, "no usable evaluation records")])
    stray = sorted({f"{r.arm}@{r.target}/seed {r.seed}" for _, r in ctx.records
                    if r.target not in c.generalization_targets or r.seed not in plan.seeds})
    if stray:  # evidence outside the plan is not part of the claim (and must not steer K3's replay seed, R1-F2)
        reasons.append(R(Code.K4_OUTSIDE_PLAN,
            f"{len(stray)} evaluation record(s) outside the analysis plan (non-target dataset or "
                       f"unplanned seed): {stray[:5]}"))
    for target in c.generalization_targets:
        recs = [r for _, r in ctx.records if r.target == target]
        keys = Counter((r.arm, r.seed) for r in recs)
        dup = sorted(k for k, n in keys.items() if n > 1)
        if dup:
            reasons.append(R(Code.K4_DUPLICATE, f"{target}: duplicate records for {dup}"))
        for arm in ("subject", "comparator"):
            seeds = sorted(s for a, s in keys if a == arm)
            if seeds != sorted(plan.seeds):
                reasons.append(R(Code.K4_SEEDS, f"{target}: {arm} seeds {seeds} != planned seeds {sorted(plan.seeds)}"))
        logged: Counter[tuple[str, int]] = Counter(
            (str(a.body.get("arm")), int(a.body.get("seed", -1))) for a in ctx.vault.accesses(target)
            if a.body.get("purpose") == "evaluate" and a.body.get("claim_hash") == ctx.claim_hash)
        reported: Counter[tuple[str, int]] = Counter({(str(a), s): n for (a, s), n in keys.items()})
        extra = logged - reported
        unlogged = reported - logged
        if extra:
            reasons.append(R(Code.K4_SELECTIVE_REPORTING,
                f"{target}: {sum(extra.values())} held-out evaluation(s) were run but not reported "
                           f"(selective reporting): {sorted(extra)}"))
        if unlogged:
            reasons.append(R(Code.K4_UNLOGGED,
                f"{target}: {sum(unlogged.values())} reported evaluation(s) have no logged label read "
                           f"(scores not produced through the vault)"))
        # the multiplicity family: claims on this target AND on any other target sharing held-out examples with it
        # (the same labels re-used under another id are the same test-set reuse); alpha-accounts and releases: §16.4/5
        alpha_eff, m, why = target_level(ctx, target)
        reasons += why
        obs.append(f"{target}: {m} claim(s) tested on this target -> Bonferroni alpha {alpha_eff:.4g}")
        if dup or any(s != sorted(plan.seeds) for s in (sorted(s for a, s in keys if a == arm)
                                                         for arm in ("subject", "comparator"))):
            continue
        # the registered test, its plan-code divergence check and the criterion: the adapter's statistics design
        ctx.stats[target], why, line = procedures(ctx).statistics(ctx, target, alpha_eff)
        reasons += why
        obs.append(line)
    return _result(reasons, obs)


# ------------------------------------------------------------------ K5 comparator integrity
def k5_comparator(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    c = ctx.claim
    for eid, rec in ctx.records:
        want = c.subject.hash if rec.arm == "subject" else c.comparator.hash
        if rec.artifact_hash != want:
            reasons.append(R(Code.K5_WRONG_CODE,
                f"{rec.arm} seed {rec.seed}: ran code {rec.artifact_hash[:12]}, not the registered "
                           f"{rec.arm} {want[:12]}"))
        if rec.metric != c.metric.id:
            reasons.append(R(Code.K5_WRONG_METRIC,
                f"{eid}: scored with {rec.metric!r}, the claim's metric is {c.metric.id!r}"))
    return _result(reasons + procedures(ctx).conformance(ctx))  # the domain's conformance rules


# ------------------------------------------------------------------ K6 held-out confirmation
def k6_held_out(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    obs: list[str] = []
    datasets = ctx.vault.datasets()
    dev_hashes = {datasets[d].hash for d in ctx.claim.development if d in datasets}
    for target in ctx.claim.generalization_targets:
        rec = datasets.get(target)
        if rec is None:
            reasons.append(R(Code.K6_UNREGISTERED, f"{target}: unregistered"))
            continue
        if rec.role not in HELD_OUT_ROLES:
            reasons.append(R(Code.K6_ROLE, f"{target}: role {rec.role.value!r} is not a held-out role"))
        if rec.role in EXTERNAL_ROLES:
            approval = ctx.vault.role_approval(target)
            obs.append(f"{target}: role {rec.role.value!r} approved by a human ({approval.body.get('approver')!r}, "
                       f"ledger seq {approval.seq})" if approval is not None else
                       f"{target}: role {rec.role.value!r} is NOT human-approved: treated as internal data (at most "
                       f"CERTIFIED-INTERNAL; `chh approve-role {target}`)")
        if rec.hash in dev_hashes:
            reasons.append(R(Code.K6_DEV_CONTENT, f"{target}: identical content to a development dataset"))
        twins = sorted(d for d, other in datasets.items() if d != target and other.hash == rec.hash)
        if twins:  # e.g. the internal test set re-registered under a new id as "external" (label laundering)
            reasons.append(R(Code.K6_TWIN, f"{target}: identical content registered under other id(s) {twins}"))
        for other in sorted(d for d in datasets if d != target):  # held-out labels public elsewhere = not held out
            for where, (n, may_carry_labels) in shared_examples(ctx.vault, target, other).items():
                if may_carry_labels:
                    reasons.append(R(Code.K6_PUBLIC_LABELS,
                        f"{n} held-out example(s) of {target!r} appear in public file {where}"))
        prereg = ctx.prereg or registration(ctx.ledger, ctx.claim_hash)
        if prereg is not None and exposed(prereg.seq, releases_on(ctx.ledger, related_targets(ctx.vault, target))):
            reasons.append(R(Code.K6_ADAPTIVELY_EXPOSED, f"{target}: a continuous release of target-derived values "
                                                         f"preceded this claim's registration (adaptively exposed: "
                                                         f"use a fresh target)"))
        dev_use = [a for a in ctx.vault.accesses(target) if a.body.get("purpose") == "develop"]
        if dev_use:
            reasons.append(R(Code.K6_DEVELOP_READ,
                f"{target}: used for development ({len(dev_use)} logged 'develop' read(s))"))
        # KD1 (assure, 2026-10-07): the same held-out examples under another dataset id that was used for development
        # (declared in the claim, or read with purpose 'develop') are development use of this target
        dev_twins = sorted(d for d in related_targets(ctx.vault, target) - {target} if d in ctx.claim.development
                           or any(a.body.get("purpose") == "develop" for a in ctx.vault.accesses(d)))
        if dev_twins:
            reasons.append(R(Code.K6_DEVELOP_READ, f"{target}: its held-out examples are also in development "
                                                   f"dataset(s) {dev_twins}"))
        st = ctx.stats.get(target)
        if st is None:
            reasons.append(R(Code.K6_NO_STATS, f"{target}: no valid statistics (see K4)"))
        elif not st.met:
            reasons.append(R(Code.K6_CRITERION_NOT_MET,
                f"{target}: the claim's criterion was not met (effect {st.example.estimate:+.4g}, "
                           f"interval [{st.example.lo:+.4g}, {st.example.hi:+.4g}], min effect "
                           f"{ctx.claim.effect.min_effect})"))
        else:
            obs.append(f"{target}: criterion met")
    return _result(reasons, obs)


def is_external(ctx: Context) -> bool:
    """CERTIFIED-VALIDATED needs every target to be external/future data AND that role approved by a human
    (`chh approve-role`). Fail closed: without the approval the claim can earn at most CERTIFIED-INTERNAL, the label
    that asserts less (its K6 still ran on held-out data never used in development)."""
    datasets = ctx.vault.datasets()
    return all(t in datasets and datasets[t].role in EXTERNAL_ROLES and ctx.vault.role_approval(t) is not None
               for t in ctx.claim.generalization_targets)


# ------------------------------------------------------------------ K7 risk
def k7_risk(ctx: Context) -> CheckResult:
    """Success must have been improbable under the domain's noise model (the adapter's risk procedure)."""
    return _result(*procedures(ctx).risk(ctx))


# ------------------------------------------------------------------ K8 background consistency
def k8_background(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    datasets = ctx.vault.datasets()
    for eid, rec in ctx.records:
        reasons += procedures(ctx).record_bounds(ctx, eid, rec)  # the domain's bounds (metric range, outputs)
        target = datasets[rec.target]
        if rec.label_file not in target.files or not target.files[rec.label_file].private:
            reasons.append(R(Code.K8_LABEL_FILE,
                f"{eid}: label file {rec.label_file!r} is not a private file of {rec.target!r}"))
        for role, ref in rec.inputs.items():
            reg = datasets[ref.dataset_id].files.get(ref.file)
            owners = sorted(f"{d}/{n}" for d, drec in datasets.items() for n, f in drec.files.items()
                            if f.private and f.blob == ref.blob)
            if owners:
                reasons.append(R(Code.K8_PRIVATE_INPUT,
                    f"{eid}: input {role} has the bytes of private file(s) {owners}"))
            if reg is None or reg.blob != ref.blob:
                reasons.append(R(Code.K8_INPUT_BYTES,
                    f"{eid}: input {role} is not the registered bytes of {ref.dataset_id}/{ref.file}"))
            elif reg.private:
                reasons.append(R(Code.K8_PRIVATE_INPUT,
                    f"{eid}: input {role} is a PRIVATE file ({ref.dataset_id}/{ref.file}) "
                               f"given to the program"))
    return _result(reasons)


# ------------------------------------------------------------------ K9 leak & canary audit
def k9_leaks(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    datasets = ctx.vault.datasets()
    canaries = [x for x in (ctx.vault.canary(d) for d in datasets) if x]
    private_blobs: dict[str, str] = {}
    windows: list[str] = []
    label_lists: list[list[str]] = []
    for target in ctx.claim.generalization_targets:
        drec = datasets.get(target)
        if drec is None:
            continue
        private_blobs |= {f.blob: f"{target}/{name}" for name, f in drec.files.items() if f.private}
        if any(r.target == target for _, r in ctx.records):
            windows += leak.label_windows(ctx.label_values(target))
            label_lists.append(ctx.label_values(target))
    runs = [leak.label_runs(*label_lists)]  # any long, informative run of labels, at any offset (not only the first)
    for role, ref in (("subject", ctx.claim.subject), ("comparator", ctx.claim.comparator)):
        if ref.hash in private_blobs:
            reasons.append(R(Code.K9_PRIVATE_CODE, f"{role} code IS a private file ({private_blobs[ref.hash]})"))
        try:
            text = ctx.store.blobs.get(ref.hash).decode("utf-8", errors="ignore")
        except OSError:
            continue  # K0 reports unresolvable artifacts
        reasons += coded(Code.K9_LEAK, leak.scan_text(text, f"{role} code", canaries, windows, runs))
    # program inputs: hidden labels must not reach a program even as a renamed or re-headed copy (records here
    # passed K0, so every input blob resolves; a read error would fail this check via certify, never pass it)
    for blob in sorted({ref.blob for _, r in ctx.records for ref in r.inputs.values()}):
        data = ctx.store.blobs.get(blob)
        text = data.decode("utf-8", errors="ignore")
        reasons += coded(Code.K9_LEAK, leak.scan_text(text, f"program input {blob[:12]}", canaries, windows, runs))
        # KD3: the labels as a COLUMN (interleaved with features, so no consecutive run exists in the text)
        reasons += [R(Code.K9_LEAK, f"hidden test labels found as column {col!r} of program input {blob[:12]}")
                    for labels in label_lists for col in label_columns(data, labels)]
    fp = {"hashes": private_blobs, "canaries": canaries, "label_windows": windows, "label_runs": runs}
    for run_id in sorted({r.run_id for _, r in ctx.records}):
        result = leak.leak_audit(ctx.store.trace(run_id), None, fp, canaries)
        reasons += coded(Code.K9_TRACE, [f"run {run_id}: {p}" for p in result["problems"]])
    for eid, rec in ctx.records:
        if any(part.startswith(("/", "\\")) or ".." in part or ":" in part for part in rec.argv):
            reasons.append(R(Code.K9_PATH, f"{eid}: command references paths outside the workspace"))
    return _result(reasons, [f"scanned code, {len({r.run_id for _, r in ctx.records})} run trace(s), "
                             f"{len(canaries)} canary token(s)"])


ALL_CHECKS: dict[str, Any] = {
    "K0": k0_provenance, "K1": k1_well_formed, "K2": k2_preregistered, "K3": k3_reproduced,
    "K4": k4_statistics, "K5": k5_comparator, "K6": k6_held_out, "K7": k7_risk, "K8": k8_background,
    "K9": k9_leaks,
}


# ================================================================== computational class (exact certificates, T1)
# The claim: "the exact value of the computation on the registered instance (subject) is `value`". The certificate
# is a registered artifact, linked to the claim by a PROOF evidence record (Kernel.record_certificate); the approved
# adapter's exact checker re-checks it from the stored bytes. Nothing here is domain-specific: the adapter is.
def k0_computational(ctx: Context) -> CheckResult:
    reasons: list[str] = []
    arts = ctx.vault.artifacts()
    ref = ctx.claim.subject
    reg = arts.get(ref.artifact_id)
    if reg is None or reg.hash != ref.hash:
        reasons.append(R(Code.K0_ARTIFACT_UNREGISTERED,
            f"problem instance {ref.artifact_id!r} is not registered with hash {ref.hash[:12]}"))
    elif not _blob_ok(ctx.store, ref.hash):
        reasons.append(R(Code.K0_ARTIFACT_BYTES, "problem instance bytes missing or corrupted"))
    if len(ctx.evidence_ids) != 1:
        reasons.append(R(Code.K0_CERTIFICATE_COUNT,
            f"a computational claim rests on exactly one certificate record, got {len(ctx.evidence_ids)}"))
    for eid in ctx.evidence_ids:
        try:
            ev = ctx.store.get_evidence(eid)
            if ev.kind != EvidenceKind.PROOF:
                raise Refusal(f"kind {ev.kind} is not a certificate record")
            rec = CertificateRecord.model_validate(strict_json(ctx.store.blobs.get(ev.payload_ref)))
            if rec.claim_hash != ctx.claim_hash:
                raise Refusal("record belongs to a different claim")
            art = arts.get(rec.artifact_id)
            if art is None or art.hash != rec.hash:
                raise Refusal(f"certificate artifact {rec.artifact_id!r} is not registered with hash "
                                 f"{rec.hash[:12]}")
            if not _blob_ok(ctx.store, rec.hash):
                raise Refusal("certificate bytes missing or corrupted")
        except (KeyError, ValueError, OSError, ValidationError, json.JSONDecodeError) as e:
            reasons.append(R(Code.K0_EVIDENCE_UNRESOLVABLE, f"evidence {eid}: {describe(e)}"))
            continue
        ctx.certificates.append(art)
    return _result(reasons, [f"{len(ctx.certificates)}/{len(ctx.evidence_ids)} certificate records resolved"])


def _exact_spec(ctx: Context) -> adapters.AdapterSpec | None:
    return adapters.REGISTRY[ctx.adapter["name"]][0] if ctx.adapter is not None else None


def statement_reasons(ctx: Context, phrase: Callable[[], str] | None) -> list[str]:
    """Computational and formal claims (invariant 14): no scope conditions (a certificate or a proof holds for the
    registered instance or the approved statement only) and the statement is exactly the adapter's rendering, so no
    prose (e.g. "this circuit works when built") rides on an exact result. An adapter without a rendering fails."""
    c, reasons = ctx.claim, []
    if c.scope.conditions:
        reasons.append(R(Code.K1_SCOPE, f"a {c.evidence_class.value} claim has no scope conditions (it holds for the "
                                        f"registered subject only), got {sorted(c.scope.conditions)}"))
    if phrase is None:
        return [*reasons, R(Code.K1_STATEMENT, "the adapter renders no scope phrase: the statement cannot be checked")]
    try:
        want = phrase()
    except (OSError, ValueError) as err:
        return [*reasons, R(Code.K1_UNREADABLE, f"subject unreadable: {exception_name(err)}")]
    if c.statement != want:
        reasons.append(R(Code.K1_STATEMENT, f"the statement must be exactly the adapter's scope rendering {want!r}"))
    return reasons


def _phrase(ctx: Context, *args: Any) -> Callable[[], str] | None:
    spec = _exact_spec(ctx)
    module = importlib.import_module(spec.exact_module) if spec is not None and spec.exact_module else None
    fn = getattr(module, "scope_phrase", None)
    return None if fn is None else lambda: str(fn(ctx.store.blobs.get(ctx.claim.subject.hash), *args))


def k1_computational(ctx: Context) -> CheckResult:
    c, reasons = ctx.claim, []
    if c.kind != ClaimKind.COMPUTED_VALUE:
        reasons.append(R(Code.K1_KIND, f"a computational claim must be of kind 'computed_value', not {c.kind.value!r}"))
    if exact_value(c.value) is None:
        reasons.append(R(Code.K1_VALUE,
            f"value {c.value!r} must be an exact rational in lowest terms, 'p' or 'p/q' (no decimals, "
                       f"no floats)"))
    ctx.adapter, why = adapters.resolve(ctx.ledger, c.evidence_class, c.metric.id)
    reasons += coded(Code.K1_ADAPTER, why)
    spec = _exact_spec(ctx)
    if spec is not None and spec.exact_module is None:
        reasons.append(R(Code.K1_ADAPTER, f"adapter {spec.name!r} declares no exact certificate checker"))
    elif spec is not None and importlib.import_module(str(spec.exact_module)).HIGHER_IS_BETTER != \
            c.metric.higher_is_better:
        reasons.append(R(Code.K1_METRIC, f"metric {c.metric.id!r} direction does not match the adapter"))
    if c.comparator != c.subject:
        reasons.append(R(Code.K1_COMPARATOR,
            "a computed value has no comparator: by convention the comparator must be the subject (the "
                       "problem instance)"))
    if c.development or c.generalization_targets or c.scope.datasets:
        reasons.append(R(Code.K1_DATASETS,
            "a computational claim uses no datasets: development, generalization targets and scope "
                       "datasets must be empty"))
    if c.analysis_plan != AnalysisPlan(seeds=()):
        reasons.append(R(Code.K1_PLAN,
            "the analysis plan must be empty (no seeds, defaults): an exact certificate replaces statistics"))
    e = c.effect
    if (e.min_effect, e.predicted_lo, e.predicted_hi) != (0.0, 0.0, 0.0):
        reasons.append(R(Code.K1_EFFECT,
            "effect fields other than the declared probability must be 0 (the claimed value is `value`)"))
    if not 0 < e.probability < 1:
        reasons.append(R(Code.K1_PROBABILITY, "declared probability must be strictly between 0 and 1"))
    if c.risk.prior_probability is not None:
        reasons.append(R(Code.K1_RISK_PRIOR, "risk prior must be unset: K7 does not apply to computational claims"))
    value = exact_value(c.value)
    if ctx.adapter is not None and value is not None:  # (without them K1 already fails)
        reasons += statement_reasons(ctx, _phrase(ctx, value))
    return _result(reasons + p0_reasons(ctx))


def k2_computational(ctx: Context) -> CheckResult:
    """K2 as for data claims (chain, registration, instance registered first), and the certificate artifact must be
    registered AFTER the claim: the value is predicted, and the prediction ledger scores it, before it is computed."""
    base = k2_preregistered(ctx)
    reasons = list(base.reasons)
    if ctx.prereg is not None:
        seq = ctx.prereg.seq
        reasons += coded(Code.K2_CERTIFICATE_BEFORE,
            [f"certificate artifact {a.artifact_id!r} was registered before the pre-registration (ledger seq "
                    f"{a.seq} < {seq})" for a in ctx.certificates if a.seq < seq])
    return _result(reasons, list(base.observations))


def _exact_inputs(ctx: Context) -> tuple[Any, bytes, bytes, Fraction] | list[str]:
    spec, value = _exact_spec(ctx), exact_value(ctx.claim.value)
    module = spec.exact_module if spec is not None else None
    reasons = []
    if module is None:
        reasons.append(R(Code.K3_INPUTS, "no approved adapter with an exact certificate checker (see K1)"))
    if value is None:
        reasons.append(R(Code.K3_INPUTS, "the claimed value is not an exact rational (see K1)"))
    if len(ctx.certificates) != 1:
        reasons.append(R(Code.K3_INPUTS, "no single resolved certificate (see K0)"))
    if module is None or value is None or reasons:
        return reasons
    return (importlib.import_module(module), ctx.store.blobs.get(ctx.claim.subject.hash),
            ctx.store.blobs.get(ctx.certificates[0].hash), value)


def k3_computational(ctx: Context) -> CheckResult:
    """The adapter's exact checker re-checks the certificate from the stored bytes (deterministic: no re-run)."""
    got = _exact_inputs(ctx)
    if isinstance(got, list):
        return _result(got)
    module, problem, cert, value = got
    return _result(coded(Code.K3_ADAPTER_CHECK,
        list(module.check(problem, cert, value))), [f"certificate re-checked exactly by {module.__name__}"])


def k8_computational(ctx: Context) -> CheckResult:
    """The adapter's registered sanity bounds (dimensions, number sizes, the claimed value included)."""
    got = _exact_inputs(ctx)
    if isinstance(got, list):
        return _result(got)
    module, problem, cert, value = got
    return _result(coded(Code.K8_ADAPTER_BOUNDS, list(module.bounds(problem, cert, value))))


def k9_computational(ctx: Context) -> CheckResult:
    """No canary token and no private file's bytes in the problem instance or the certificate."""
    datasets = ctx.vault.datasets()
    canaries = [x for x in (ctx.vault.canary(d) for d in datasets) if x]
    private = {f.blob: f"{d}/{n}" for d, rec in datasets.items() for n, f in rec.files.items() if f.private}
    reasons: list[str] = []
    scanned = [("problem instance", ctx.claim.subject.hash), *(("certificate", a.hash) for a in ctx.certificates)]
    for role, digest in scanned:
        if digest in private:
            reasons.append(R(Code.K9_PRIVATE_CODE, f"{role} IS a private file ({private[digest]})"))
        reasons += coded(Code.K9_LEAK,
            leak.scan_text(ctx.store.blobs.get(digest).decode("utf-8", errors="ignore"), role, canaries, []))
    return _result(reasons, [f"scanned the instance and {len(ctx.certificates)} certificate(s) for "
                             f"{len(canaries)} canary token(s) and {len(private)} private file(s)"])


COMPUTATIONAL_CHECKS: dict[str, Any] = {
    "K0": k0_computational, "K1": k1_computational, "K2": k2_computational, "K3": k3_computational,
    "K8": k8_computational, "K9": k9_computational,
}


# ================================================================== formal class (Lean 4 check records, T0)
# The claim: "the human-approved Lean statement (subject) holds". The evidence is ONE check record produced by the
# adapter's sandbox runner (a registered artifact linked by a PROOF record, as for computational claims, so K0, K2
# and K9 are shared); the approved adapter's record checker re-checks it from the stored bytes. The Kernel runs no Lean.
def k1_formal(ctx: Context) -> CheckResult:
    c, reasons = ctx.claim, []
    if c.kind != ClaimKind.THEOREM:
        reasons.append(R(Code.K1_KIND, f"a formal claim must be of kind 'theorem', not {c.kind.value!r}"))
    if c.value is not None:
        reasons.append(R(Code.K1_VALUE, "a theorem claim carries no value"))
    ctx.adapter, why = adapters.resolve(ctx.ledger, c.evidence_class, c.metric.id)
    reasons += coded(Code.K1_ADAPTER, why)
    spec = _exact_spec(ctx)
    if spec is not None and spec.exact_module is None:
        reasons.append(R(Code.K1_ADAPTER, f"adapter {spec.name!r} declares no Lean record checker"))
    approved = [e for e in ctx.ledger.entries("statement_approval")
                if e.body.get("hash") == c.subject.hash and str(e.body.get("approver", "")).strip()]
    if not approved:
        reasons.append(R(Code.K1_STATEMENT,
            f"statement {c.subject.artifact_id!r} (hash {c.subject.hash[:12]}) has no human approval: run "
                       f"`chh approve-statement` (models can never approve a statement)"))
    if c.comparator != c.subject:
        reasons.append(R(Code.K1_COMPARATOR,
            "a theorem has no comparator: by convention the comparator must be the subject (the statement)"))
    if c.development or c.generalization_targets or c.scope.datasets:
        reasons.append(R(Code.K1_DATASETS,
            "a formal claim uses no datasets: development, generalization targets and scope datasets "
                       "must be empty"))
    if c.analysis_plan != AnalysisPlan(seeds=()):
        reasons.append(R(Code.K1_PLAN,
            "the analysis plan must be empty (no seeds, defaults): a proof replaces statistics"))
    e = c.effect
    if (e.min_effect, e.predicted_lo, e.predicted_hi) != (0.0, 0.0, 0.0):
        reasons.append(R(Code.K1_EFFECT, "effect fields other than the declared probability must be 0"))
    if not 0 < e.probability < 1:
        reasons.append(R(Code.K1_PROBABILITY, "declared probability must be strictly between 0 and 1"))
    if c.risk.prior_probability is not None:
        reasons.append(R(Code.K1_RISK_PRIOR,
            "risk prior must be unset: no K7 rule exists for formal claims (owner decision pending)"))
    if ctx.adapter is not None:
        reasons += statement_reasons(ctx, _phrase(ctx))
    return _result(reasons + p0_reasons(ctx))


def _formal_inputs(ctx: Context) -> tuple[Any, bytes, bytes] | list[str]:
    spec = _exact_spec(ctx)
    module = spec.exact_module if spec is not None else None
    reasons = []
    if module is None:
        reasons.append(R(Code.K3_INPUTS, "no approved adapter with a Lean record checker (see K1)"))
    if len(ctx.certificates) != 1:
        reasons.append(R(Code.K3_INPUTS, "no single resolved check record (see K0)"))
    if module is None or reasons:
        return reasons
    return (importlib.import_module(module), ctx.store.blobs.get(ctx.claim.subject.hash),
            ctx.store.blobs.get(ctx.certificates[0].hash))


def k3_formal(ctx: Context) -> CheckResult:
    """The adapter's record checker re-checks the Lean check record against the approved statement's bytes."""
    got = _formal_inputs(ctx)
    if isinstance(got, list):
        return _result(got)
    module, statement, record = got
    reasons = coded(Code.K3_ADAPTER_CHECK, list(module.check(statement, record)))
    axioms = [] if reasons else [f"relies on axiom {a}" for a in module.axioms_used(record)]
    return _result(reasons, [f"Lean check record re-checked by {module.__name__}", *axioms])


def k8_formal(ctx: Context) -> CheckResult:
    """The adapter's registered sanity bounds (source and output sizes)."""
    got = _formal_inputs(ctx)
    if isinstance(got, list):
        return _result(got)
    module, statement, record = got
    return _result(coded(Code.K8_ADAPTER_BOUNDS, list(module.bounds(statement, record))))


FORMAL_CHECKS: dict[str, Any] = {
    "K0": k0_computational, "K1": k1_formal, "K2": k2_computational, "K3": k3_formal, "K8": k8_formal,
    "K9": k9_computational,
}

# HUMAN-OWNED (L9), like KernelConfig: which checks apply to each evidence class. None = the check applies and must
# pass; a string = the check does not apply to the class, and is the reason recorded on the certificate. No claim can
# change it. A class absent from the table gets every check (fail closed: without an approved adapter, K1 rejects it).
CHECK_APPLICABILITY: Mapping[EvidenceClass, Mapping[str, str | None]] = MappingProxyType({
    EvidenceClass.DATA: MappingProxyType(dict.fromkeys(CHECK_IDS)),
    EvidenceClass.COMPUTATIONAL: MappingProxyType({
        "K0": None, "K1": None, "K2": None, "K3": None,
        "K4": "n/a (computational): an exact certificate replaces statistics; there is no sampling, no seeds and no "
              "multiplicity to correct",
        "K5": "n/a (computational): no comparator; the claim states the exact value of one computation on one "
              "registered instance",
        "K6": "n/a (computational): no held-out data; an exact certificate holds for the stated instance, not for a "
              "sample of a population",
        "K7": "n/a (computational): correctness is proven exactly; triviality/importance are not judged by the "
              "Kernel (GOAL §4.6)",
        "K8": None, "K9": None}),
    EvidenceClass.SIMULATED: MappingProxyType({
        "K0": None, "K1": None, "K2": None, "K3": None,
        "K4": "n/a (simulated): a deterministic simulation has no sampling and no seeds; the convergence study (K3) "
              "replaces statistics",
        "K5": "n/a (simulated): no comparator; the claim states one quantity of one pinned simulation",
        "K6": "n/a (simulated): no held-out data; the claim holds in the named simulator only, never in reality",
        "K7": "n/a (simulated): the value is re-computed, not predicted from data; importance is not judged (GOAL "
              "§4.6)",
        "K8": None, "K9": None}),
    EvidenceClass.FORMAL: MappingProxyType({
        "K0": None, "K1": None, "K2": None, "K3": None,
        "K4": "n/a (formal): a kernel-checked proof replaces statistics; there is no sampling, no seeds and no "
              "multiplicity to correct",
        "K5": "n/a (formal): no comparator; the claim is that one human-approved statement holds",
        "K6": "n/a (formal): no held-out data; a proof holds for the stated theorem, not for a sample of a population",
        "K7": "n/a (formal) - OWNER DECISION PENDING (GOAL §13.3): a theorem has no resampling noise, so K7's noise "
              "model does not apply, and no risk prior over statements is defined yet; riskiness is NOT assessed",
        "K8": None, "K9": None}),
})
_CHECKS_BY_CLASS: Mapping[EvidenceClass, dict[str, Any]] = MappingProxyType({
    EvidenceClass.COMPUTATIONAL: COMPUTATIONAL_CHECKS, EvidenceClass.FORMAL: FORMAL_CHECKS,
    EvidenceClass.SIMULATED: {"K0": k0_computational, "K1": simulated.k1_simulated, "K2": k2_computational,
                              "K3": simulated.k3_simulated, "K8": simulated.k8_simulated, "K9": k9_computational}})


def applicability(evidence_class: EvidenceClass) -> Mapping[str, str | None]:
    return CHECK_APPLICABILITY.get(evidence_class, CHECK_APPLICABILITY[EvidenceClass.DATA])


def checks_for(evidence_class: EvidenceClass) -> dict[str, Any]:
    return _CHECKS_BY_CLASS.get(evidence_class, ALL_CHECKS)
