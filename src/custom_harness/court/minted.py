"""The minted-data route of a Lab promotion (GOAL §17 Court, §16.2 "freeze, then mint", §17.1 VALIDATED).

    chh court register PROMOTION --family F --beacon-round R [--dev-pairs a,b,c,d] [--n-test N]
        the externally frozen Lab candidate (`chh lab promote`: workspace manifest, pinned lock, method, baseline) is
        registered as a Court CASE: both arms (composed with the audit contract wrapper) become Kernel artifacts, the
        researcher's typed claim is PRE-REGISTERED through the Kernel (targets: a not-yet-existing minted target and
        its training set), and the ledger is ANCHORED outside the store. The held-out size is fixed here, by the
        certifiability preflight (deny-only; default PROPOSED_HELDOUT_ROWS = 4,500, UNCALIBRATED), and frozen into the
        claim with the generator family and the beacon ROUND (committed before its value exists).
    chh court mint CASE --beacon VALUE
        kerneld mints the target AFTER the freeze (kerneld/ingest.minted_heldout: draw("heldout_mint", claim hash,
        round:value), keyed streams, truth only in the vault; Kernel.record_mint, one mint per claim).
    chh approve-role TARGET --approver NAME      HUMAN (existing): the minted target really is FUTURE data
    chh court run CASE --docker
        both frozen arms run offline in the network-free candidate sandbox on the minted data, every program before
        any label is read; scores come from kerneld; the Kernel certifies (K0-K9, re-execution). One look per claim.
    chh court revalidate CASE --beacon-round R2  the SECOND fresh-data test (RE-VALIDATED), declared before the first
                                                 verdict (or `register --revalidation-round R2`): declare_revalidation
    chh court level CERT | CASE                  the verification badges (court/levels.py), computed by code

Which claim families can be minted: the synthetic generator families (kerneld/mint.py, research/suite.FAMILIES:
rings, xor, spirals, imbalanced, friedman, interactions, and the null family noise), for the family's own metric.
A promotion on a fixed public dataset cannot be minted: it takes the existing retrospective Claim Audit route
(`chh lab handoff`), and can never be VALIDATED or RE-VALIDATED (RETROSPECTIVE, court/levels.py).

Stated limits (not solved here):
  - the claim is generator-scoped: the population is the minted family (§16.2: the family is the main Goodhart surface;
    families rotate under LOFO). The Lab may know the generator's code; it cannot know the beacon value;
  - the beacon VALUE is typed by a human; that it is the public randomness of the committed round is not checked
    offline (an owner step: a drand verifier); a value shorter than 16 characters is refused;
  - the frozen package lock is recorded and bound (its sha256 is in every evaluation record's environment), NOT
    installed: the Court sandbox is the network-free candidate image, which must already satisfy it (building a
    per-lock image from an offline wheelhouse is an owner step);
  - an arm uses the audit contract wrapper (build_model(seed) -> fit/predict on float features) or the wider program
    contract (audit/program.py); an arm that does not meet its contract is a crashed arm (COULD NOT RUN), never
    certified. A sealed route exists too (`register_sealed`, court/sealed.py).
Built, NOT test-verified.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from datetime import UTC, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any

from ..audit import program, wrapper
from ..audit.claim_audit import SandboxFactory, Verdict, render_could_not_run, render_report, verdict
from ..audit.spec import Budget, ClaimPart
from ..config import data_dir
from ..errors import InfraError
from ..kernel import (
    DEFAULT_ARGV,
    AnalysisPlan,
    ArtifactRef,
    Claim,
    ClaimKind,
    EvidenceClass,
    ExecRecord,
    InputRef,
    MetricRef,
    Risk,
    SandboxReplayer,
    Scope,
    adapters,
    canonical_json,
    ml,
    run_program,
)
from ..kernel.claim import Effect
from ..kerneld.mint import MINTED_FORMAT, MintSpecError, beacon_value_problems, mintable_families, minted_protocol
from ..kerneld.scope import ScopedKernel
from ..kerneld.seal import SEALED_FORMAT
from ..records import Run, StepKind, TraceStep
from ..store import EvidenceStore
from ..store.run_bundle import write_run_bundle
from ..verify import metrics as M

RECORD_KIND = "court_case"
ROLES = ("promotion", "replica", "revalidation")


class CourtRefused(RuntimeError):
    pass


# ---------------------------------------------------------------- held-out size (certifiability preflight)
def size_ladder() -> tuple[int, ...]:
    from ..research.suite import MINT_TEST_ROWS, PROPOSED_HELDOUT_ROWS
    return tuple(sorted({PROPOSED_HELDOUT_ROWS, 2 * PROPOSED_HELDOUT_ROWS, MINT_TEST_ROWS}))


def size_held_out(part: ClaimPart, *, dev_pairs: tuple[int, int, int, int] | None, n_test: int | None = None
                  ) -> dict[str, Any]:
    """The minted held-out size (GOAL §17: "sized by the certifiability preflight so a real effect can pass").
    research/preflight.py simulates the REAL K4/K6/K7 on outcomes drawn from the PUBLIC dev pairs (deny-only,
    invariant 21). Without dev pairs, or for a metric it does not simulate, the default PROPOSED_HELDOUT_ROWS
    (4,500, UNCALIBRATED) is used and nothing is denied. A human-given `n_test` is checked, never raised.
    Otherwise the smallest size of the ladder with no objection wins; if every size is denied, the case is refused
    (honestly uncertifiable at any size: shrink the claim)."""
    from ..research.preflight import PublicInputs, preflight
    from ..research.suite import PROPOSED_HELDOUT_ROWS
    sizes = (n_test,) if n_test is not None else size_ladder()
    if dev_pairs is None or part.metric not in ("accuracy",):
        n = n_test if n_test is not None else PROPOSED_HELDOUT_ROWS
        return {"n_test": n, "preflight": "not run (no public dev pairs, or a metric it does not simulate): the "
                "default size, UNCALIBRATED", "tried": []}
    tried = []
    for n in sizes:
        r = preflight(PublicInputs(metric=part.metric, higher_is_better=M.HIGHER_IS_BETTER[part.metric], n_test=n,
                                   dev_pairs=dev_pairs, min_effect=part.min_effect, alpha=part.alpha, family_size=1,
                                   seeds=tuple(part.seeds), n_boot=part.n_boot))
        tried.append({"n_test": n, "verdict": r.verdict, "p_certify": r.p_certify})
        if not r.denied:
            return {"n_test": n, "preflight": r.verdict, "tried": tried}
    raise CourtRefused(f"the certifiability preflight denies every held-out size {list(sizes)} ({tried}): the claim "
                       f"is unlikely to certify even on fresh data; shrink it (lower min_effect) or report it "
                       f"uncertifiable")


# ---------------------------------------------------------------- records
def events(store: EvidenceStore, case_id: str) -> list[dict[str, Any]]:
    return store.records_where(RECORD_KIND, "case_id", case_id)


def case(store: EvidenceStore, case_id: str) -> dict[str, Any]:
    rows = events(store, case_id)
    if not rows or rows[0].get("event") != "registered":
        raise CourtRefused(f"no Court case {case_id!r}")
    return rows[0]


def latest(store: EvidenceStore, case_id: str, event: str) -> dict[str, Any] | None:
    rows = [r for r in events(store, case_id) if r.get("event") == event]
    return rows[-1] if rows else None


def cases_of_promotion(store: EvidenceStore, pid: str) -> list[dict[str, Any]]:
    return [r for r in store.records_where(RECORD_KIND, "promotion_id", pid) if r.get("event") == "registered"]


def related_cases(store: EvidenceStore, case_id: str, role: str) -> list[dict[str, Any]]:
    """Registered cases of `role` (replica | revalidation) that point at case `case_id` (`of_case`), oldest first."""
    return [r for r in store.records(RECORD_KIND) if r.get("event") == "registered" and r.get("role") == role
            and r.get("of_case") == case_id]


def case_of_claim(store: EvidenceStore, claim_hash: str) -> dict[str, Any] | None:
    rows = [r for r in store.records_where(RECORD_KIND, "claim_hash", claim_hash) if r.get("event") == "registered"]
    return rows[0] if rows else None


# ---------------------------------------------------------------- the claim
def contract_of(name: str) -> tuple[Any, Any, Any]:
    """(compose, hash, spec_ref) of a run contract: `build_model` (audit/wrapper.py) or `program` (audit/program.py)."""
    if name == "program":
        return program.compose, program.shim_hash, program.shim_spec_ref
    return wrapper.compose, wrapper.wrapper_hash, wrapper.wrapper_spec_ref


def case_claim(*, case_id: str, link: str, title: str, sentence: str, part: ClaimPart, budget: Budget,
               method_code: bytes, baseline_code: bytes, family: str, n_train: int, n_test: int,
               beacon_round: str, contract: str = "build_model", sealed: dict[str, Any] | None = None,
               health_hash: str | None = None) -> Claim:
    """The typed Kernel claim of a minted case (deterministic). `method_code` / `baseline_code` are the COMPOSED
    programs (arm + audit wrapper). Scope conditions are only the ML adapter's provenance notes (K1, s4-kernel):
    `paper_doi` carries the Lab link (lab:<session>:<promotion>[:replica]) the discovery story follows, `protocol`
    the minted protocol kerneld checks before minting."""
    from ..research.suite import FAMILIES
    target = sealed["target"] if sealed else f"lab-mint-{case_id}"
    train_id = sealed["train_id"] if sealed else f"{target}-train"
    if not sealed and part.metric != FAMILIES[family].metric:
        raise CourtRefused(f"family {family!r} is scored by {FAMILIES[family].metric!r}, not {part.metric!r}: a minted "
                           f"claim uses its generator family's metric")
    conditions = {"audit_id": case_id, "paper_doi": link, "paper_title": title, "paper_sentence": sentence,
                  "budget_timeout_s": repr(budget.timeout_s), "wrapper_sha256": contract_of(contract)[1](),
                  "protocol": sealed["protocol"] if sealed else minted_protocol(family, n_train, n_test, beacon_round),
                  "licences": "the researcher's own data, sealed at project start" if sealed
                  else "minted synthetic data (no third-party licence)"}
    population = (f"the population is the researcher's sealed dataset sha256={sealed['data_sha256']}; the raw file "
                  f"reached no Lab by any other route (rests on the researcher, untested)" if sealed else
                  f"the population is the minted generator family {family!r} (a generator-scoped claim, GOAL "
                  f"16.2): it says nothing about other data")
    claim = Claim(
        kind=ClaimKind.IMPROVEMENT, statement="",
        subject=ArtifactRef(artifact_id=f"court-{case_id}-method", hash=hashlib.sha256(method_code).hexdigest()),
        comparator=ArtifactRef(artifact_id=f"court-{case_id}-baseline",
                               hash=hashlib.sha256(baseline_code).hexdigest()),
        metric=MetricRef(id=part.metric, higher_is_better=M.HIGHER_IS_BETTER[part.metric]),
        effect=Effect(min_effect=part.min_effect, predicted_lo=part.min_effect, predicted_hi=part.min_effect,
                      probability=part.probability),
        scope=Scope(datasets=(train_id, target), conditions=conditions),
        development=(train_id,), generalization_targets=(target,),
        analysis_plan=AnalysisPlan(seeds=part.seeds, alpha=part.alpha, n_boot=part.n_boot),
        assumptions=("the typed claim is a faithful reading of the researcher's sentence (written and promoted by a "
                     "human; untested)",
                     population,
                     "the beacon value is the public randomness of the committed round (typed by a human; not "
                     "verified offline)",
                     "the frozen package lock is satisfied by the Court's candidate image (recorded, not installed)",
                     *([f"this Court version passed health run {health_hash} (GOAL 18.6)"] if health_hash else [])),
        risk=Risk(),
        resolve_by=datetime.combine(part.resolve_by, dtime(0, 0), tzinfo=UTC).timestamp())
    return claim.model_copy(update={"statement": ml.scope_phrase(claim)})


# ---------------------------------------------------------------- register (freeze -> pre-register -> anchor)
def default_anchor_file() -> Path:
    """`<data>/anchors/court.jsonl`: outside the store (kernel/ledger.write_anchor refuses an anchor inside it)."""
    return data_dir() / "anchors" / "court.jsonl"


def _case_id(doc: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(doc)).hexdigest()[:16]


def register_case(store: EvidenceStore, *, role: str, promotion_id: str, session_id: str, title: str, sentence: str,
                  part: ClaimPart, budget: Budget, method: bytes, baseline: bytes, lock: bytes | None, family: str,
                  beacon_round: str, sizing: dict[str, Any], n_train: int | None = None,
                  anchor_file: Path | None = None, of_case: str | None = None,
                  extra: dict[str, Any] | None = None, contract: str = "build_model",
                  sealed: dict[str, Any] | None = None, health_hash: str | None = None) -> dict[str, Any]:
    """Register one minted Court case (module docstring). `method` / `baseline` are the ARM files (raw); they are
    composed with the audit wrapper here. Order: artifacts -> pre-registration -> anchor -> record. Fails closed."""
    from ..kernel.ledger import AnchorError, write_anchor
    from ..research.suite import DELTA_FLOOR, MINT_TRAIN_ROWS
    if role not in ROLES:
        raise CourtRefused(f"unknown case role {role!r}")
    if not sealed and family not in mintable_families():
        raise CourtRefused(f"family {family!r} has no generator ({list(mintable_families())} can be minted): a public "
                           f"dataset is a retrospective target (`chh lab handoff`, capped below VALIDATED)")
    if not sealed and part.min_effect < DELTA_FLOOR:
        raise CourtRefused(f"min_effect {part.min_effect} is below the minted-target floor {DELTA_FLOOR} (K1 refuses "
                           f"it: a large fresh test set must not make trivial effects certifiable)")
    if method == baseline:
        raise CourtRefused("method and baseline are the same code")
    n_train = MINT_TRAIN_ROWS if n_train is None else int(n_train)
    n_test = int(sizing["n_test"])
    compose = contract_of(contract)[0]
    method_code, baseline_code = compose(method), compose(baseline)
    link = f"lab:{session_id}:{promotion_id}" + (f":{role}:{of_case}" if role != "promotion" else "")
    case_id = _case_id({"format": SEALED_FORMAT if sealed else MINTED_FORMAT, "role": role, "link": link,
                        "family": family, "n_train": n_train, "n_test": n_test, "beacon_round": beacon_round,
                        "method": hashlib.sha256(method_code).hexdigest(),
                        "baseline": hashlib.sha256(baseline_code).hexdigest()})
    if events(store, case_id):
        raise CourtRefused(f"Court case {case_id} already exists (one pre-registration per case)")
    try:
        claim = case_claim(case_id=case_id, link=link, title=title, sentence=sentence, part=part, budget=budget,
                           method_code=method_code, baseline_code=baseline_code, family=family, n_train=n_train,
                           n_test=n_test, beacon_round=beacon_round, contract=contract, sealed=sealed,
                           health_hash=health_hash)
    except MintSpecError as e:
        raise CourtRefused(str(e)) from None
    kernel = ScopedKernel(store)
    _, why = adapters.resolve(kernel.ledger, EvidenceClass.DATA, claim.metric.id)
    if why:
        raise CourtRefused("the ML adapter is not approved for this metric: " + "; ".join(why))
    kernel.vault.register_artifact(claim.subject.artifact_id, method_code, "method")
    kernel.vault.register_artifact(claim.comparator.artifact_id, baseline_code, "baseline")
    prereg = kernel.preregister(claim)
    anchor_path = Path(anchor_file) if anchor_file is not None else default_anchor_file()
    try:  # the pre-registration is anchored BEFORE anything is minted (kerneld refuses an unanchored claim)
        anchor = write_anchor(kernel.ledger, anchor_path, Path(store.root))
    except AnchorError as e:
        raise CourtRefused(f"could not anchor the pre-registration: {e}") from None
    target = claim.generalization_targets[0]
    body = {"case_id": case_id, "event": "registered", "format": SEALED_FORMAT if sealed else MINTED_FORMAT,
            "route": "sealed" if sealed else "minted", "contract": contract, "health_hash": health_hash, "role": role,
            "promotion_id": promotion_id, "session_id": session_id, "of_case": of_case, "link": link,
            "claim_hash": claim.claim_hash(), "prereg_seq": prereg.seq, "target": target,
            "train_id": claim.development[0], "family": family, "n_train": n_train, "n_test": n_test,
            "beacon_round": beacon_round, "anchor_file": str(anchor_path), "anchor": anchor, "sizing": sizing,
            "lock_sha256": hashlib.sha256(lock).hexdigest() if lock is not None else None,
            "timeout_s": budget.timeout_s, "at": time.time(), **(extra or {})}
    store.append_record(RECORD_KIND, body)
    return body


def register_promotion(store: EvidenceStore, pid: str, family: str, *, beacon_round: str,
                       dev_pairs: tuple[int, int, int, int] | None = None, n_test: int | None = None,
                       n_train: int | None = None, anchor_file: Path | None = None,
                       revalidation_round: str | None = None) -> dict[str, Any]:
    """A frozen Lab promotion -> a minted Court case (HUMAN: the researcher who promoted it). Refused if the promotion
    already went to the Court (one Court route per freeze: either this or `chh lab handoff`)."""
    from ..lab import trace as T
    from ..lab.promote import RECORD_KIND as PROMOTION_KIND
    from ..lab.promote import PromoteRequest, _promotion
    from .health import require_passed
    rows = _promotion(store, pid)
    if any(r.get("event") == "handed_off" for r in rows) or cases_of_promotion(store, pid):
        raise CourtRefused(f"promotion {pid} already went to the Court (one Court route per freeze)")
    frozen = rows[0]
    req = PromoteRequest.model_validate(frozen["request"])
    method = store.blobs.get(frozen["frozen"]["method"]["blob"])
    baseline = store.blobs.get(frozen["frozen"]["baseline"]["blob"])
    lock = store.blobs.get(frozen["frozen"]["lock"]["blob"])
    sizing = size_held_out(req.claim, dev_pairs=dev_pairs, n_test=n_test)
    body = register_case(store, role="promotion", promotion_id=pid, session_id=str(frozen["session_id"]),
                         title=req.title, sentence=req.sentence, part=req.claim, budget=req.budget, method=method,
                         baseline=baseline, lock=lock, family=family, beacon_round=beacon_round, sizing=sizing,
                         n_train=n_train, anchor_file=anchor_file, contract=req.contract,
                         health_hash=require_passed(store))
    store.append_record(PROMOTION_KIND, {
        "promotion_id": pid, "event": "handed_off", "label": "EXPLORATORY", "route": "minted",
        "audit_id": body["case_id"], "case_id": body["case_id"], "claim_hash": body["claim_hash"],
        "targets": [body["target"]], "frozen_at": frozen.get("at"), "at": time.time(),
        "court_route": f"minted ({family}, {body['n_test']} held-out rows minted after the freeze)",
        "next": [f"chh court mint {body['case_id']} --beacon <public value of round {beacon_round}>",
                 f"chh approve-role {body['target']} --approver NAME   (HUMAN, after the mint)",
                 f"chh court run {body['case_id']} --docker", f"chh court level {body['case_id']}"]})
    T.record(store, str(frozen["session_id"]), "promote",
             {"op": "handed_off", "promotion_id": pid, "claim_hash": body["claim_hash"], "audit_id": body["case_id"],
              "idea_id": frozen.get("idea_id")}, actor=T.human())
    if revalidation_round is not None:  # both rounds fixed at registration (the soundest form; declare_revalidation)
        body = {**body, "revalidation": declare_revalidation(store, body["case_id"], revalidation_round,
                                                             anchor_file=anchor_file)}
    return body


def sealed_route(store: EvidenceStore, project: str, part_no: int) -> dict[str, Any]:
    """The `sealed` argument of register_case for part `part_no` of a sealed project."""
    from .sealed import sealed_record
    seal = sealed_record(store, project)
    if seal is None:
        raise CourtRefused(f"project {project!r} is not sealed")
    if not 1 <= part_no <= len(seal["targets"]):
        raise CourtRefused(f"project {project!r} has {len(seal['targets'])} sealed part(s), not part {part_no}")
    target = seal["targets"][part_no - 1]
    from ..kernel import alpha
    from .display import retired
    if retired(store, target):
        raise CourtRefused(f"sealed part {target} is retired")
    used = [r for r in store.records_where(RECORD_KIND, "target", target) if r.get("event") == "registered"]
    if used and not alpha.accounts(ScopedKernel(store).ledger, {target}):
        raise CourtRefused(f"sealed part {target} already has a case: one look per target, unless a human opened an "
                           f"alpha-account for it before its first case (`chh alpha`)")
    return {"project": project, "part": part_no, "target": target, "train_id": seal["train_id"],
            "data_sha256": seal["data_sha256"], "n_train": seal["n_lab"], "n_test": seal["n_parts"][part_no - 1],
            "protocol": f"{SEALED_FORMAT} project={project} part={part_no} beacon_round={seal['beacon_round']}: "
                        f"held-out truth sealed by kerneld before the project's first Lab session (GOAL 18.6); both "
                        f"arms through one contract", "beacon_round": seal["beacon_round"]}


def register_sealed(store: EvidenceStore, pid: str, project: str, *, anchor_file: Path | None = None,
                    revalidate: bool = False) -> dict[str, Any]:
    """A frozen Lab promotion -> a Court case on part 1 of a sealed project (HUMAN: the researcher who promoted it).
    `revalidate` fixes part 2 as the RE-VALIDATED test now, before any label of part 1 is read."""
    from ..lab import trace as T
    from ..lab.promote import RECORD_KIND as PROMOTION_KIND
    from ..lab.promote import PromoteRequest, _promotion
    from .health import require_passed
    from .sealed import project_rows
    rows = _promotion(store, pid)
    if any(r.get("event") == "handed_off" for r in rows) or cases_of_promotion(store, pid):
        raise CourtRefused(f"promotion {pid} already went to the Court (one Court route per freeze)")
    frozen = rows[0]
    sid = str(frozen["session_id"])
    if not any(r.get("event") == "session" and r.get("session_id") == sid for r in project_rows(store, project)):
        raise CourtRefused(f"session {sid} is not a session of project {project!r}")
    req = PromoteRequest.model_validate(frozen["request"])
    route = sealed_route(store, project, 1)
    body = register_case(store, role="promotion", promotion_id=pid, session_id=sid, title=req.title,
                         sentence=req.sentence, part=req.claim, budget=req.budget,
                         method=store.blobs.get(frozen["frozen"]["method"]["blob"]),
                         baseline=store.blobs.get(frozen["frozen"]["baseline"]["blob"]),
                         lock=store.blobs.get(frozen["frozen"]["lock"]["blob"]), family=f"sealed:{project}",
                         beacon_round=route["beacon_round"], sizing={"n_test": route["n_test"], "preflight":
                                                                     "the sealed part's size, fixed at the seal"},
                         n_train=route["n_train"], anchor_file=anchor_file, contract=req.contract, sealed=route,
                         extra={"project": project, "part": 1}, health_hash=require_passed(store))
    store.append_record(PROMOTION_KIND, {
        "promotion_id": pid, "event": "handed_off", "label": "EXPLORATORY", "route": "minted",
        "audit_id": body["case_id"], "case_id": body["case_id"], "claim_hash": body["claim_hash"],
        "targets": [body["target"]], "frozen_at": frozen.get("at"), "at": time.time(),
        "court_route": f"sealed ({project}, part 1, {body['n_test']} rows sealed before the first session)",
        "next": [f"chh approve-role {body['target']} --approver NAME   (HUMAN)",
                 f"chh court run {body['case_id']} --docker", f"chh court level {body['case_id']}"]})
    T.record(store, sid, "promote", {"op": "handed_off", "promotion_id": pid, "claim_hash": body["claim_hash"],
                                     "audit_id": body["case_id"], "idea_id": frozen.get("idea_id")}, actor=T.human())
    if revalidate:
        body = {**body, "revalidation": declare_revalidation(store, body["case_id"], None, anchor_file=anchor_file)}
    return body


# ---------------------------------------------------------------- re-validation (RE-VALIDATED, court/levels.py)
def first_test_started(store: EvidenceStore, case_id: str) -> bool:
    """True once any held-out label of the case's test was read: an outcome that read labels, or any `evaluate`
    vault read of its claim (the Kernel's data_access log)."""
    c = case(store, case_id)
    if any(o.get("labels_read") or o.get("cert_hash") for o in events(store, case_id) if o.get("event") == "outcome"):
        return True
    return any(a.body.get("claim_hash") == c["claim_hash"] and a.body.get("purpose") == "evaluate"
               for a in ScopedKernel(store).vault.accesses())


def declare_revalidation(store: EvidenceStore, case_id: str, beacon_round: str | None, *,
                         anchor_file: Path | None = None) -> dict[str, Any]:
    """Declare the SECOND fresh-data test of a promotion case (RE-VALIDATED): the same claim and the same frozen code
    (both arms, byte for byte), the same family and sizes, on a second minted target under a second beacon round.
    It is PRE-REGISTERED and anchored now, as its own case (role `revalidation`, `of_case`).

    Soundness (owner question: fix both rounds at registration, or require the second before the first verdict):
    both are allowed, and the second is the rule code enforces. A declaration is refused once any held-out label of
    the first test was read (`first_test_started`), and one re-validation per case, so the decision to re-validate
    and the round cannot depend on the first outcome (no selection on results, no repeated attempts until one
    passes). Independence: the second target is minted from draw("heldout_mint", <second claim hash>, <round 2>:
    <value 2>), a different frozen hash AND a different round from the first, so the two keyed streams share no key;
    the beacon value of round 2 must not be public when the round is declared (as for round 1: the round is a plain
    identifier and its futurity is not checked offline). `chh court register --revalidation-round R2` fixes both
    rounds at registration, which also removes any doubt about timing."""
    from ..lab.promote import PromoteRequest, _promotion
    c = case(store, case_id)
    if c.get("role") != "promotion":
        raise CourtRefused("only a promotion case is re-validated (one level deep)")
    if related_cases(store, case_id, "revalidation"):
        raise CourtRefused(f"case {case_id} already has its re-validation (one per case)")
    is_sealed = c.get("route") == "sealed"
    if not is_sealed and beacon_round == c.get("beacon_round"):
        raise CourtRefused("the re-validation needs a different beacon round from the first test")
    if first_test_started(store, case_id):
        raise CourtRefused("too late: held-out labels of the first test were already read; a re-validation is "
                           "declared before the first verdict")
    rows = _promotion(store, str(c["promotion_id"]))
    req = PromoteRequest.model_validate(rows[0]["request"])
    kernel = ScopedKernel(store)
    claim = next(e.body["claim"] for e in kernel.ledger.entries("prereg")
                 if e.body.get("claim_hash") == c["claim_hash"])
    arts = kernel.vault.artifacts()
    contract = str(c.get("contract", "build_model"))
    method: bytes | None
    baseline: bytes | None
    if contract == "program":  # the frozen form is the bundle; the composed program is rebuilt from it
        method = store.blobs.get(rows[0]["frozen"]["method"]["blob"])
        baseline = store.blobs.get(rows[0]["frozen"]["baseline"]["blob"])
    else:
        method = wrapper.arm_part(store.blobs.get(arts[claim["subject"]["artifact_id"]].hash))
        baseline = wrapper.arm_part(store.blobs.get(arts[claim["comparator"]["artifact_id"]].hash))
    if method is None or baseline is None:
        raise CourtRefused("the case's arms are not composed with the audit wrapper")
    lock = store.blobs.get(rows[0]["frozen"]["lock"]["blob"])
    route = sealed_route(store, str(c["project"]), 2) if is_sealed else None
    sizing = {"n_test": int(route["n_test"]) if route else int(c["n_test"]),
              "preflight": f"the size of case {case_id} (the claim is unchanged)"}
    extra: dict[str, Any] = {"first_claim_hash": c["claim_hash"], "first_beacon_round": c["beacon_round"]}
    if route:
        extra.update(project=c["project"], part=2)
    return register_case(store, role="revalidation", promotion_id=str(c["promotion_id"]),
                         session_id=str(c["session_id"]), title=req.title, sentence=req.sentence, part=req.claim,
                         budget=req.budget, method=method, baseline=baseline, lock=lock, family=str(c["family"]),
                         beacon_round=str(route["beacon_round"]) if route else str(beacon_round), sizing=sizing,
                         n_train=int(c["n_train"]), anchor_file=anchor_file, of_case=case_id, extra=extra,
                         contract=contract, sealed=route, health_hash=c.get("health_hash"))


# ---------------------------------------------------------------- mint (kerneld)
def mint(store: EvidenceStore, case_id: str, beacon_value: str) -> dict[str, Any]:
    """kerneld mints the case's target (kerneld/ingest.minted_heldout); the labels never enter this process."""
    from ..kerneld.client import KernelD, KernelDError
    c = case(store, case_id)
    if c.get("route") == "sealed":
        raise CourtRefused(f"case {case_id} tests sealed data: nothing is minted")
    if latest(store, case_id, "minted") is not None:
        raise CourtRefused(f"case {case_id} is already minted (one mint per claim)")
    problems = beacon_value_problems(c["beacon_round"], beacon_value)
    if problems:
        raise CourtRefused("; ".join(problems))
    try:
        with KernelD(store.root) as kd:
            run = kd.open_run(f"court-mint-{case_id}")
            try:
                res = run.ingest_minted_heldout(c["claim_hash"], c["target"], family=c["family"],
                                                n_train=int(c["n_train"]), n_test=int(c["n_test"]),
                                                beacon_round=c["beacon_round"], beacon_value=beacon_value,
                                                anchor_file=c["anchor_file"])
            finally:
                run.close()
    except KernelDError as e:
        raise CourtRefused(f"kerneld refused the mint ({e.code})") from None
    except InfraError as e:  # no truth was read; the same beacon finishes the mint (kerneld/ingest.minted_heldout)
        raise CourtRefused(f"could not reach kerneld ({e}): nothing was spent, repeat this command") from None
    body = {"case_id": case_id, "event": "minted", "claim_hash": c["claim_hash"], **res,
            "approve": f"chh approve-role {c['target']} --approver <you>", "at": time.time()}
    store.append_record(RECORD_KIND, body)
    return body


# ---------------------------------------------------------------- run (re-execute offline, Kernel decides)
def run(store: EvidenceStore, case_id: str, sandbox_factory: SandboxFactory, *, bundle_root: Path | None = None
        ) -> dict[str, Any]:
    """Both frozen arms on the minted data in the Court sandbox, then the Kernel. Refuses (no label read) unless
    minted and the target's FUTURE role is human-approved; one look per claim. Records the outcome."""
    from .comparator import comparator_problem
    c = case(store, case_id)
    route = str(c.get("route", "minted"))
    if route == "minted" and latest(store, case_id, "minted") is None:
        raise CourtRefused(f"case {case_id} is not minted yet (`chh court mint`)")
    kernel = ScopedKernel(store, replayer=SandboxReplayer(sandbox_factory))
    vault = kernel.vault
    ch, target, train_id = c["claim_hash"], c["target"], c["train_id"]
    if not vault.registered(ch):
        raise CourtRefused("the case claim is not pre-registered")
    if any(o.get("labels_read") or o.get("verdict") != Verdict.COULD_NOT_RUN.value
           for o in events(store, case_id) if o.get("event") == "outcome") \
            or any(a.body.get("claim_hash") == ch and a.body.get("purpose") == "evaluate" for a in vault.accesses()):
        raise CourtRefused("this claim was already evaluated: one look per claim")
    if vault.role_approval(target) is None:
        raise CourtRefused(f"{target}: its FUTURE role is not approved by a human (`chh approve-role {target} "
                           f"--approver <you>`)")
    claim = Claim.model_validate(next(e.body["claim"] for e in kernel.ledger.entries("prereg")
                                      if e.body.get("claim_hash") == ch))
    arts = vault.artifacts()
    arms = {"subject": (claim.subject.hash, store.blobs.get(arts[claim.subject.artifact_id].hash)),
            "comparator": (claim.comparator.hash, store.blobs.get(arts[claim.comparator.artifact_id].hash))}
    timeout = float(c["timeout_s"])
    _, contract_hash, spec_ref = contract_of(str(c.get("contract", "build_model")))
    env = {"sandbox": "court candidate sandbox (network-free)", "audit_wrapper": contract_hash(),
           "court_route": route, "lock_sha256": str(c.get("lock_sha256"))}
    limits = {"timeout_s": timeout}

    def labels_read() -> bool:
        """A fact from the ledger (kerneld logs each read before using it), not a guess at the failure site."""
        return any(a.body.get("claim_hash") == ch and a.body.get("purpose") == "evaluate" for a in vault.accesses())

    def could_not_run(reason: str) -> dict[str, Any]:
        body = {"case_id": case_id, "event": "outcome", "claim_hash": ch, "verdict": Verdict.COULD_NOT_RUN.value,
                "reason": reason, "cert_hash": None, "labels_read": labels_read(),
                "report_hash": hashlib.sha256(render_could_not_run(case_id, ch, reason, False).encode()).hexdigest(),
                "at": time.time()}
        store.append_record(RECORD_KIND, body)
        return body

    train = vault.read_public(train_id, "train.csv")
    test_x = vault.read_public(target, "test_X.csv")
    n_test = len(list(csv.reader(io.StringIO(test_x.decode("utf-8"))))) - 1
    preds: dict[tuple[str, int], list[str]] = {}
    try:  # 1. every program runs before any label is read
        strawman = comparator_problem(arms["comparator"][1], claim.metric.id, train,
                                      sandbox_factory(f"court-{case_id}-comparator-check"),
                                      claim.analysis_plan.seeds[0], timeout)
        if strawman:
            return could_not_run(strawman)
        for seed in claim.analysis_plan.seeds:
            for arm, (_, code) in arms.items():
                sb = sandbox_factory(f"court-{case_id}-{arm}-{seed}")
                p, diag = run_program(sb, code=code, entrypoint="solution.py", argv=DEFAULT_ARGV,
                                      inputs={"train": ("train.csv", train), "test": ("test_X.csv", test_x)},
                                      seed=seed, timeout_s=timeout)
                if p is None:
                    return could_not_run(f"the {arm} arm failed on seed {seed}: {diag}")  # no label was read
                if len(p) != n_test:
                    return could_not_run(f"the {arm} arm wrote {len(p)} predictions for {n_test} rows")
                problem = M.predictions_problem(claim.metric.id, p)
                if problem:
                    return could_not_run(f"the {arm} arm on seed {seed}: {problem}")
                preds[(arm, seed)] = p
    except (InfraError, OSError) as e:
        return could_not_run(f"infrastructure: {e}")
    from ..kerneld.client import KernelD, KernelDError
    refs = {"train": InputRef(dataset_id=train_id, file="train.csv",
                              blob=vault.dataset(train_id).files["train.csv"].blob),
            "test": InputRef(dataset_id=target, file="test_X.csv", blob=vault.dataset(target).files["test_X.csv"].blob)}
    ids: list[str] = []
    try:  # 2. labels stay behind the kerneld boundary: one logged 'evaluate' read per (arm, seed); the score only
        with KernelD(store.root) as kd:
            for seed in claim.analysis_plan.seeds:
                for arm, (h, _) in arms.items():
                    run_rec = Run(harness_id="chh-court", harness_hash=spec_ref(store, "chh-court"),
                                  task_id=target, seed=seed)
                    store.add_run(run_rec)
                    now = time.time()
                    store.add_step(TraceStep(run_id=run_rec.id, index=0, node_id="court_arm", kind=StepKind.TOOL,
                                             input={"arm": arm, "case_id": case_id},
                                             output={"note": "frozen arm run through the audit contract wrapper"},
                                             started_at=now, ended_at=now))
                    handle = kd.open_run(run_rec.id)
                    try:
                        score = handle.held_out_score(target, "test_y.csv", claim.metric.id, preds[(arm, seed)],
                                                      claim_hash=ch, arm=arm, seed=seed)
                    finally:
                        handle.close()
                    rec = ExecRecord(claim_hash=ch, arm=arm, artifact_hash=h, argv=DEFAULT_ARGV, inputs=refs,  # type: ignore[arg-type]
                                     target=target, label_file="test_y.csv", seed=seed, env=env, limits=limits,
                                     preds_ref=store.blobs.put_json(preds[(arm, seed)]), metric=claim.metric.id,
                                     score=score, run_id=run_rec.id)
                    ids.append(kernel.record_evaluation(rec))
                    write_run_bundle(store, run_rec.id, environment=env)  # invariant 9
    except (InfraError, KernelDError, OSError) as e:
        return could_not_run(f"infrastructure: kerneld: {e}")
    try:  # 3. the Kernel decides
        cert = kernel.certify(claim, ids)
    except InfraError as e:
        return could_not_run(f"infrastructure during certification: {e}")
    h = cert.cert_hash()
    bundle_hash = None
    if cert.label.value.startswith("CERTIFIED"):  # the one-command Reproducibility Bundle (REPRODUCIBLE needs it)
        bundle_hash = kernel.bundle(h, (bundle_root or data_dir() / "bundles") / f"court-{case_id}-{h[:16]}.zip")
    v, named = verdict(cert)
    body = {"case_id": case_id, "event": "outcome", "claim_hash": ch, "verdict": v.value, "checks": named,
            "cert_hash": h, "label": cert.label.value, "labels_read": True, "bundle_hash": bundle_hash,
            "report_hash": hashlib.sha256(render_report(cert).encode()).hexdigest(), "at": time.time()}
    store.append_record(RECORD_KIND, body)
    return body


def summary(store: EvidenceStore, case_id: str) -> dict[str, Any]:
    c = case(store, case_id)
    out = {k: c.get(k) for k in ("case_id", "role", "promotion_id", "of_case", "claim_hash", "target", "family",
                                 "n_train", "n_test", "beacon_round", "sizing")}
    out["minted"] = latest(store, case_id, "minted") is not None
    o = latest(store, case_id, "outcome")
    out["outcome"] = None if o is None else {k: o.get(k) for k in ("verdict", "label", "cert_hash", "checks",
                                                                   "reason")}
    return json.loads(json.dumps(out, default=str))
