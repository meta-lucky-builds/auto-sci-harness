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
import statistics
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import ValidationError

from ..records import EvidenceKind
from ..store.evidence import EvidenceStore
from ..verify import leak
from ..verify import metrics as M
from ..verify.stats import Interval, mean_t_interval, paired_example_interval
from . import adapters
from .certificate import CHECK_IDS, Certificate, CheckResult
from .claim import SUPPORTED_KINDS, AnalysisPlan, Claim, ClaimKind, EvidenceClass, exact_value
from .execution import ExecRecord, ExecutionError
from .ledger import Entry, Ledger, verify_chain
from .vault import EXTERNAL_ROLES, HELD_OUT_ROLES, ArtifactRecord, CertificateRecord, Vault, VaultError

_NORMAL = statistics.NormalDist()
# K2: an INTEGRITY read (building leak fingerprints) never feeds a model; it is listed on the certificate, not counted
# as peeking. TRUST BOUNDARY (docs/kernel.md): purposes are declared by trusted host code, so a host that mislabels a
# peek as "leakscan" is not caught. "reproduce" is NOT excused: nothing on a live store reads with it (bundles are
# re-checked on a fresh store with purpose "kernel"), so excusing it would only widen the trusted surface.
INTEGRITY_PURPOSES = frozenset({"leakscan"})
SCORE_TOLERANCE = 1e-9  # reported vs recomputed score: anything larger is a different number


class Replayer(Protocol):
    def replay(self, record: ExecRecord, vault: Vault) -> list[str] | None: ...


@dataclass(frozen=True)
class KernelConfig:
    """Human-owned policy (L9). Not part of any claim, so a claimant cannot relax it."""

    version: str = "1.1"  # 1.1: domain adapters (K1), evidence classes, shared-example checks (K4/K6), K7 fail-closed
    min_seeds: int = 3
    max_alpha: float = 0.2
    min_boot: int = 1000
    min_risk_bits: float = 2.0  # K7: success must have been at most 2^-2 = 25% likely under the null
    max_reproduce_tolerance: float = 0.02  # K1: a looser declared re-execution tolerance makes K3 meaningless


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
    adapter: dict[str, Any] | None = None  # the approved adapter K1 resolved for this claim
    certificates: list[ArtifactRecord] = field(default_factory=list)  # computational: certificate artifacts (K0)

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


def related_targets(vault: Vault, target: str) -> set[str]:
    """`target` plus every other dataset tested against the same held-out examples: sharing example rows (exact
    text, `shared_examples`) or sharing a byte-identical PRIVATE file (the same labels under another id, R1-F4)."""
    datasets = vault.datasets()
    if target not in datasets:  # unregistered (K1 reports it): related to nothing
        return {target}
    private = {f.blob for f in datasets[target].files.values() if f.private}
    return {target} | {d for d, rec in datasets.items() if d != target and (
        shared_examples(vault, target, d) or any(f.private and f.blob in private for f in rec.files.values()))}


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
    if c.value is not None:
        reasons.append("only computed_value claims carry an exact value")
    if c.metric.id not in M.METRICS:
        reasons.append(f"metric {c.metric.id!r} is not registered")
    elif M.HIGHER_IS_BETTER[c.metric.id] != c.metric.higher_is_better:
        reasons.append(f"metric {c.metric.id!r} direction does not match the registry")
    # GOAL §4.7 / invariant 13: only a human-approved adapter, unchanged since its approval, may check this class
    ctx.adapter, why = adapters.resolve(ctx.ledger, c.evidence_class, c.metric.id)
    reasons += why
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
    if not 0 <= plan.reproduce_tolerance <= cfg.max_reproduce_tolerance:
        reasons.append(f"reproduce_tolerance must be in [0, {cfg.max_reproduce_tolerance}]")
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
        for a in ctx.vault.accesses(target):
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
            reasons.append(f"private data of target {target!r} was read {len(early)} time(s) before "
                           f"pre-registration (first at ledger seq {early[0].seq}, purpose "
                           f"{early[0].body.get('purpose')!r})")
        if integrity:
            obs.append(f"{target}: {len(integrity)} integrity read(s) (leakscan) before pre-registration: not "
                       f"counted as peeking (never fed to a model; purpose declared by trusted host code)")
    arts = ctx.vault.artifacts()
    for ref in (ctx.claim.subject, ctx.claim.comparator):
        reg = arts.get(ref.artifact_id)
        if reg is not None and reg.seq > prereg.seq:
            reasons.append(f"artifact {ref.artifact_id!r} was registered after the pre-registration")
    return _result(reasons, [f"pre-registered at ledger seq {prereg.seq}", *obs])


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
    reasons += _earlier_reproduction_failures(ctx)
    if ctx.replayer is None:
        return _result([*reasons, "no replayer configured: re-execution was not performed"], obs)
    plan = ctx.claim.analysis_plan
    chosen = [(e, r) for e, r in ctx.records if plan.reproduce == "all" or r.seed == replay_seed(ctx, r)]
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
            earlier = Certificate.model_validate(ctx.store.blobs.get_json(e.body["blob"]))
            if earlier.cert_hash() != e.body.get("cert_hash"):
                raise ValueError("content does not match its ledger entry")
        except (KeyError, ValueError, OSError, ValidationError) as err:
            out.append(f"earlier certificate at ledger seq {e.seq} is unreadable ({type(err).__name__}): its K3 "
                       f"result is unknown (fail closed)")
            continue
        if earlier.prereg_seq is not None and earlier.checks["K3"].status != "pass":
            out.append(f"an earlier certificate of this claim (ledger seq {e.seq}) failed K3: a claim that did not "
                       f"reproduce once is not certified by trying again (re-register a new claim instead)")
    return out


def replay_seed(ctx: Context, rec: ExecRecord) -> int:
    """The seed re-executed for (arm, target) under 'sampled_seed': derived from a hash of ALL evidence after it
    is fixed, so a claimant cannot know in advance which seed will be checked. (Re-running evaluations to
    steer it would leave unreported held-out reads, which K4 rejects.) Only PLAN-MATCHED records (a claim target
    and a planned seed) enter the hash: extra records cannot steer it (R1-F2), and K4 rejects them anyway."""
    targets, seeds_planned = set(ctx.claim.generalization_targets), set(ctx.claim.analysis_plan.seeds)
    material = "|".join(sorted(r.preds_ref for _, r in ctx.records
                               if r.target in targets and r.seed in seeds_planned))
    digest = hashlib.sha256(f"{ctx.claim_hash}|{rec.arm}|{rec.target}|{material}".encode()).digest()
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
        return _result(["no usable evaluation records"])
    preregs = ctx.ledger.entries("prereg")
    stray = sorted({f"{r.arm}@{r.target}/seed {r.seed}" for _, r in ctx.records
                    if r.target not in c.generalization_targets or r.seed not in plan.seeds})
    if stray:  # evidence outside the plan is not part of the claim (and must not steer K3's replay seed, R1-F2)
        reasons.append(f"{len(stray)} evaluation record(s) outside the analysis plan (non-target dataset or "
                       f"unplanned seed): {stray[:5]}")
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
        # the multiplicity family: claims on this target AND on any other target sharing held-out examples with it
        # (the same labels re-used under another id are the same test-set reuse)
        related = related_targets(ctx.vault, target)
        family ={e.body.get("claim_hash") for e in preregs if related & set(e.body.get("claim", {}).get(
            "generalization_targets", []))}
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
        if rec.role in EXTERNAL_ROLES:
            approval = ctx.vault.role_approval(target)
            obs.append(f"{target}: role {rec.role.value!r} approved by a human ({approval.body.get('approver')!r}, "
                       f"ledger seq {approval.seq})" if approval is not None else
                       f"{target}: role {rec.role.value!r} is NOT human-approved: treated as internal data (at most "
                       f"CERTIFIED-INTERNAL; `chh approve-role {target}`)")
        if rec.hash in dev_hashes:
            reasons.append(f"{target}: identical content to a development dataset")
        twins = sorted(d for d, other in datasets.items() if d != target and other.hash == rec.hash)
        if twins:  # e.g. the internal test set re-registered under a new id as "external" (label laundering)
            reasons.append(f"{target}: identical content registered under other id(s) {twins}")
        for other in sorted(d for d in datasets if d != target):  # held-out labels public elsewhere = not held out
            for where, (n, may_carry_labels) in shared_examples(ctx.vault, target, other).items():
                if may_carry_labels:
                    reasons.append(f"{n} held-out example(s) of {target!r} appear in public file {where}")
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
    """CERTIFIED-VALIDATED needs every target to be external/future data AND that role approved by a human
    (`chh approve-role`). Fail closed: without the approval the claim can earn at most CERTIFIED-INTERNAL, the label
    that asserts less (its K6 still ran on held-out data never used in development)."""
    datasets = ctx.vault.datasets()
    return all(t in datasets and datasets[t].role in EXTERNAL_ROLES and ctx.vault.role_approval(t) is not None
               for t in ctx.claim.generalization_targets)


# ------------------------------------------------------------------ K7 risk
def k7_risk(ctx: Context) -> CheckResult:
    """Success must have been improbable under a no-effect noise model (and under the declared prior).
    p_null(target) = P(effect >= min_effect | true effect 0), normal model with the target's resampling SE.

    Negative-result and equivalence claims FAIL (fail closed): at their claimed bound, success is at most alpha
    for any bound, so no noise model can tell a vacuous bound ("does not improve by 0.9 accuracy") from a tight one;
    that needs a prior over effects, which v1 does not have. Such claims are reported, never certified."""
    c = ctx.claim
    if c.kind not in (ClaimKind.IMPROVEMENT, ClaimKind.ABLATION_EFFECT):
        return _result([f"no risk model for {c.kind.value} claims in Kernel v{ctx.config.version}: a vacuous bound "
                        f"(one that could not have failed) cannot be detected without a prior over effects"])
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
            owners = sorted(f"{d}/{n}" for d, drec in datasets.items() for n, f in drec.files.items()
                            if f.private and f.blob == ref.blob)
            if owners:
                reasons.append(f"{eid}: input {role} has the bytes of private file(s) {owners}")
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
            reasons.append(f"{role} code IS a private file ({private_blobs[ref.hash]})")
        try:
            text = ctx.store.blobs.get(ref.hash).decode("utf-8", errors="ignore")
        except OSError:
            continue  # K0 reports unresolvable artifacts
        reasons += leak.scan_text(text, f"{role} code", canaries, windows, runs)
    # program inputs: hidden labels must not reach a program even as a renamed or re-headed copy (records here
    # passed K0, so every input blob resolves; a read error would fail this check via certify, never pass it)
    for blob in sorted({ref.blob for _, r in ctx.records for ref in r.inputs.values()}):
        text = ctx.store.blobs.get(blob).decode("utf-8", errors="ignore")
        reasons += leak.scan_text(text, f"program input {blob[:12]}", canaries, windows, runs)
    fp = {"hashes": private_blobs, "canaries": canaries, "label_windows": windows, "label_runs": runs}
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
        reasons.append(f"problem instance {ref.artifact_id!r} is not registered with hash {ref.hash[:12]}")
    elif not _blob_ok(ctx.store, ref.hash):
        reasons.append("problem instance bytes missing or corrupted")
    if len(ctx.evidence_ids) != 1:
        reasons.append(f"a computational claim rests on exactly one certificate record, got {len(ctx.evidence_ids)}")
    for eid in ctx.evidence_ids:
        try:
            ev = ctx.store.get_evidence(eid)
            if ev.kind != EvidenceKind.PROOF:
                raise ValueError(f"kind {ev.kind} is not a certificate record")
            rec = CertificateRecord.model_validate(ctx.store.blobs.get_json(ev.payload_ref))
            if rec.claim_hash != ctx.claim_hash:
                raise ValueError("record belongs to a different claim")
            art = arts.get(rec.artifact_id)
            if art is None or art.hash != rec.hash:
                raise ValueError(f"certificate artifact {rec.artifact_id!r} is not registered with hash "
                                 f"{rec.hash[:12]}")
            if not _blob_ok(ctx.store, rec.hash):
                raise ValueError("certificate bytes missing or corrupted")
        except (KeyError, ValueError, OSError, ValidationError, json.JSONDecodeError) as e:
            reasons.append(f"evidence {eid}: {type(e).__name__}: {str(e)[:200]}")
            continue
        ctx.certificates.append(art)
    return _result(reasons, [f"{len(ctx.certificates)}/{len(ctx.evidence_ids)} certificate records resolved"])


def _exact_spec(ctx: Context) -> adapters.AdapterSpec | None:
    return adapters.REGISTRY[ctx.adapter["name"]][0] if ctx.adapter is not None else None


def k1_computational(ctx: Context) -> CheckResult:
    c, reasons = ctx.claim, []
    if c.kind != ClaimKind.COMPUTED_VALUE:
        reasons.append(f"a computational claim must be of kind 'computed_value', not {c.kind.value!r}")
    if exact_value(c.value) is None:
        reasons.append(f"value {c.value!r} must be an exact rational in lowest terms, 'p' or 'p/q' (no decimals, "
                       f"no floats)")
    ctx.adapter, why = adapters.resolve(ctx.ledger, c.evidence_class, c.metric.id)
    reasons += why
    spec = _exact_spec(ctx)
    if spec is not None and spec.exact_module is None:
        reasons.append(f"adapter {spec.name!r} declares no exact certificate checker")
    elif spec is not None and importlib.import_module(str(spec.exact_module)).HIGHER_IS_BETTER != \
            c.metric.higher_is_better:
        reasons.append(f"metric {c.metric.id!r} direction does not match the adapter")
    if c.comparator != c.subject:
        reasons.append("a computed value has no comparator: by convention the comparator must be the subject (the "
                       "problem instance)")
    if c.development or c.generalization_targets or c.scope.datasets:
        reasons.append("a computational claim uses no datasets: development, generalization targets and scope "
                       "datasets must be empty")
    if c.analysis_plan != AnalysisPlan(seeds=()):
        reasons.append("the analysis plan must be empty (no seeds, defaults): an exact certificate replaces statistics")
    e = c.effect
    if (e.min_effect, e.predicted_lo, e.predicted_hi) != (0.0, 0.0, 0.0):
        reasons.append("effect fields other than the declared probability must be 0 (the claimed value is `value`)")
    if not 0 < e.probability < 1:
        reasons.append("declared probability must be strictly between 0 and 1")
    if c.risk.prior_probability is not None:
        reasons.append("risk prior must be unset: K7 does not apply to computational claims")
    return _result(reasons)


def k2_computational(ctx: Context) -> CheckResult:
    """K2 as for data claims (chain, registration, instance registered first), and the certificate artifact must be
    registered AFTER the claim: the value is predicted, and the prediction ledger scores it, before it is computed."""
    base = k2_preregistered(ctx)
    reasons = list(base.reasons)
    if ctx.prereg is not None:
        seq = ctx.prereg.seq
        reasons += [f"certificate artifact {a.artifact_id!r} was registered before the pre-registration (ledger seq "
                    f"{a.seq} < {seq})" for a in ctx.certificates if a.seq < seq]
    return _result(reasons, list(base.observations))


def _exact_inputs(ctx: Context) -> tuple[Any, bytes, bytes, Fraction] | list[str]:
    spec, value = _exact_spec(ctx), exact_value(ctx.claim.value)
    module = spec.exact_module if spec is not None else None
    reasons = []
    if module is None:
        reasons.append("no approved adapter with an exact certificate checker (see K1)")
    if value is None:
        reasons.append("the claimed value is not an exact rational (see K1)")
    if len(ctx.certificates) != 1:
        reasons.append("no single resolved certificate (see K0)")
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
    return _result(list(module.check(problem, cert, value)), [f"certificate re-checked exactly by {module.__name__}"])


def k8_computational(ctx: Context) -> CheckResult:
    """The adapter's registered sanity bounds (dimensions, number sizes, the claimed value included)."""
    got = _exact_inputs(ctx)
    if isinstance(got, list):
        return _result(got)
    module, problem, cert, value = got
    return _result(list(module.bounds(problem, cert, value)))


def k9_computational(ctx: Context) -> CheckResult:
    """No canary token and no private file's bytes in the problem instance or the certificate."""
    datasets = ctx.vault.datasets()
    canaries = [x for x in (ctx.vault.canary(d) for d in datasets) if x]
    private = {f.blob: f"{d}/{n}" for d, rec in datasets.items() for n, f in rec.files.items() if f.private}
    reasons: list[str] = []
    scanned = [("problem instance", ctx.claim.subject.hash), *(("certificate", a.hash) for a in ctx.certificates)]
    for role, digest in scanned:
        if digest in private:
            reasons.append(f"{role} IS a private file ({private[digest]})")
        reasons += leak.scan_text(ctx.store.blobs.get(digest).decode("utf-8", errors="ignore"), role, canaries, [])
    return _result(reasons, [f"scanned the instance and {len(ctx.certificates)} certificate(s) for "
                             f"{len(canaries)} canary token(s) and {len(private)} private file(s)"])


COMPUTATIONAL_CHECKS: dict[str, Any] = {
    "K0": k0_computational, "K1": k1_computational, "K2": k2_computational, "K3": k3_computational,
    "K8": k8_computational, "K9": k9_computational,
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
})
_CHECKS_BY_CLASS: Mapping[EvidenceClass, dict[str, Any]] = MappingProxyType({
    EvidenceClass.COMPUTATIONAL: COMPUTATIONAL_CHECKS})


def applicability(evidence_class: EvidenceClass) -> Mapping[str, str | None]:
    return CHECK_APPLICABILITY.get(evidence_class, CHECK_APPLICABILITY[EvidenceClass.DATA])


def checks_for(evidence_class: EvidenceClass) -> dict[str, Any]:
    return _CHECKS_BY_CLASS.get(evidence_class, ALL_CHECKS)
