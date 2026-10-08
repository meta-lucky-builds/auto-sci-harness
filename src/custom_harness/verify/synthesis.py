"""Verifier synthesis for soft problems (GOAL §5 L3 "Verifier synthesis"; ScienceIDE row of §9).

For a problem with no ready-made verifier, a model writes one: property tests, invariants or a small simulator. A
synthesized verifier is UNTRUSTED until it passes fault-injection validation, and even then it is weaker than a
human-owned one:

1. Language. The model writes a declarative spec in a closed vocabulary (`OPS`), interpreted here by trusted code
   with exact arithmetic. It never writes executable code, so nothing model-written runs in this process (arbitrary
   code verifiers would need the sandbox: not built, stated in docs/l3_verifiers.md).
2. Validation (`validate`). Each problem has ONE fixed `FaultSuite` in `SUITES` (code, author named,
   content-hashed): golden cases the verifier must accept and planted faults it must reject. The caller cannot pass
   a suite: it is looked up by problem. AUTHORSHIP: every suite in code is AGENT-AUTHORED (`AGENT_AUTHORED`). The
   two example suites were written by an AI agent, earlier labelled with the owner's name by mistake; no code field
   can declare a suite human-written. A suite counts as human-approved only through a ledgered HUMAN suite review
   (`record_suite_review`, ledger kind `verifier_review`, subject "suite", bound to the suite hash). Each half has a PUBLIC part (shown to the author model) and a SEALED part
   (never shown), so a verifier that memorises the visible cases fails on the sealed ones. Passing = every golden
   accepted AND every planted fault rejected, with at least the configured number of each in each half.
3. Tier (`tier`). Validated and not reviewed: capped at T2 (`SYNTH_UNREVIEWED_TIER_CAP`). It reaches its natural T1
   only with BOTH a human review recorded in the Kernel ledger (kind `verifier_review`) for exactly this verifier
   hash and suite hash, AND a ledgered human approval of that suite (hash): a verifier validated against an
   agent-authored suite stays capped at T2 whatever review it has. A human rejection of the verifier or of the suite
   makes it unusable. Any content change (a new hash) voids the review.
4. Independence (`verdict`). A synthesized verifier never certifies a claim of its own author (same author id, or
   same model family) alone: an independent second verifier (different author AND family from both the claim's
   author and the conflicted verifier; a human-owned check counts) must also accept it. The verdict tier is the
   weakest link of the verifiers used.

Trust boundary (as for adapter approvals): `record_review` is meant for the human-operated CLI. The ledger entry
names the reviewer and is hash-chained (accountability, not enforcement); the reviewer cannot be the author.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from ..gateway.base import Backend, ChatRequest, Message
from ..kernel import ledger as _ledger
from ..kernel import lp
from ..kernel.claim import canonical_json
from ..kernel.ledger import Entry, Ledger
from ..policy import model_family
from ..tiers import Tier, chain_tier
from . import l3_config as C

SPEC_FORMAT = "chh-synth-verifier/1"
REVIEW_KIND = "verifier_review"  # Kernel ledger kind (requested from kstats, mailbox synth-to-kstats-01)

# op -> required keys (besides "op"). Thresholds and model constants are exact rational STRINGS.
OPS: dict[str, frozenset[str]] = {
    "range": frozenset({"field", "lo", "hi"}),  # every number in the field is in [lo, hi]
    "sorted": frozenset({"field"}),  # the list is nondecreasing
    "le": frozenset({"a", "b"}),  # number field a <= number field b
    "equals": frozenset({"field", "value"}),
    "one_of": frozenset({"field", "values"}),
    "permutation_of": frozenset({"field", "of"}),  # same multiset of numbers
    "sum_equals": frozenset({"field", "of"}),  # conservation: same sum
    # small simulator: x_{k+1} = A x_k from the subject's x0; every row of the trajectory within tolerance
    "simulate_linear": frozenset({"A", "x0", "trajectory", "tolerance"}),
}
_PROPERTY = frozenset({"range", "sorted", "le", "equals", "one_of"})
KINDS_OPS: dict[str, frozenset[str]] = {
    "property_test": _PROPERTY,
    "invariant": _PROPERTY | {"permutation_of", "sum_equals"},
    "simulator": _PROPERTY | {"permutation_of", "sum_equals", "simulate_linear"},
}
_RATIONAL_KEYS = frozenset({"lo", "hi", "tolerance"})


AGENT_AUTHORED = "AGENT-AUTHORED"  # every suite in code; human approval exists only as a ledgered suite review
SUITE_SUBJECT = "suite"  # `verifier_review` body "subject" of a suite review (verifier reviews carry no subject)


class SynthesisError(ValueError):
    pass


# ------------------------------------------------------------------ the fixed, human-owned fault suites
@dataclass(frozen=True)
class Case:
    id: str
    subject: dict[str, Any]


@dataclass(frozen=True)
class FaultSuite:
    problem: str
    description: str  # shown to the author model
    author: str  # who wrote the suite (an agent for every suite in code: see `authorship`)
    public_golden: tuple[Case, ...]  # shown to the author model
    sealed_golden: tuple[Case, ...]  # never shown
    public_faults: tuple[Case, ...]  # never shown either (named "public" only as the half a reviewer may publish)
    sealed_faults: tuple[Case, ...]

    def hash(self) -> str:
        doc = {k: getattr(self, k) for k in ("problem", "description", "author")}
        doc["authorship"] = self.authorship
        for half in ("public_golden", "sealed_golden", "public_faults", "sealed_faults"):
            doc[half] = [[c.id, c.subject] for c in getattr(self, half)]
        return hashlib.sha256(canonical_json(doc)).hexdigest()

    @property
    def authorship(self) -> str:
        """Always AGENT-AUTHORED: code cannot declare a suite human-written or human-approved. A human's approval is
        a ledgered suite review (`record_suite_review`), looked up by `suite_status`."""
        return AGENT_AUTHORED

    def author_view(self) -> str:
        return json.dumps({"problem": self.problem, "description": self.description,
                           "examples_of_correct_subjects": [c.subject for c in self.public_golden]}, sort_keys=True)


Matrix2 = tuple[tuple[float, float], tuple[float, float]]


def _decay(x0: list[float], steps: int, a: Matrix2 = ((0.5, 0.0), (0.25, 0.5))) -> list[list[float]]:
    """An honest FLOAT simulation of x_{k+1} = A x_k (how the golden trajectories were produced)."""
    traj = [list(x0)]
    for _ in range(steps):
        x = traj[-1]
        traj.append([a[0][0] * x[0] + a[0][1] * x[1], a[1][0] * x[0] + a[1][1] * x[1]])
    return traj


def _drifted(traj: list[list[float]], row: int, by: float) -> list[list[float]]:
    out = [list(r) for r in traj]
    out[row][0] += by
    return out


def _c(cid: str, **subject: Any) -> Case:
    return Case(cid, subject)


# Who wrote the two example suites below: an AI agent (workstream g-synth, 2026-10-07). They were first labelled
# "owner (custom-harness)", which was false. They are agent-authored and nobody has reviewed them.
_AGENT_SUITE_AUTHOR = "agent:g-synth (AI-written; not written, reviewed or approved by the owner)"

SUITES: dict[str, FaultSuite] = {
    "sort": FaultSuite(
        problem="sort", author=_AGENT_SUITE_AUTHOR,
        description='Subjects are JSON objects {"in": list of numbers, "out": list of numbers}. A correct subject '
                    'has "out" listing exactly the numbers of "in", in nondecreasing order.',
        public_golden=(_c("g-pub-1", **{"in": [3, 1, 2], "out": [1, 2, 3]}),
                       _c("g-pub-2", **{"in": [5, 5, 0], "out": [0, 5, 5]})),
        sealed_golden=(_c("g-sealed-1", **{"in": [], "out": []}),
                       _c("g-sealed-2", **{"in": [-2, 7, -2, 4], "out": [-2, -2, 4, 7]}),
                       _c("g-sealed-3", **{"in": [1.5, 0.5], "out": [0.5, 1.5]})),
        public_faults=(_c("unsorted-output", **{"in": [3, 1, 2], "out": [3, 1, 2]}),
                       _c("dropped-element", **{"in": [3, 1, 2], "out": [1, 2]})),
        sealed_faults=(_c("sum-preserving-change", **{"in": [1, 2, 3], "out": [0, 3, 3]}),
                       _c("extra-element", **{"in": [1, 2], "out": [1, 2, 2]}),
                       _c("reversed-order", **{"in": [1, 2, 3], "out": [3, 2, 1]}))),
    "decay": FaultSuite(
        problem="decay", author=_AGENT_SUITE_AUTHOR,
        description='Subjects are JSON objects {"x0": [a, b], "traj": [[a, b], ...]}: "traj" is a trajectory of the '
                    'linear system x_{k+1} = A x_k with A = [[1/2, 0], [1/4, 1/2]], starting at traj[0] = x0, '
                    'computed in floating point (absolute error below 1e-6).',
        public_golden=(_c("g-pub-1", x0=[1.0, 0.0], traj=_decay([1.0, 0.0], 4)),
                       _c("g-pub-2", x0=[2.0, -1.0], traj=_decay([2.0, -1.0], 3))),
        sealed_golden=(_c("g-sealed-1", x0=[0.0, 0.0], traj=_decay([0.0, 0.0], 2)),
                       _c("g-sealed-2", x0=[1.0, 1.0], traj=_decay([1.0, 1.0], 6)),
                       _c("g-sealed-3", x0=[-3.0, 0.5], traj=_decay([-3.0, 0.5], 5))),
        public_faults=(_c("uncoupled-model", x0=[1.0, 0.0], traj=_decay([1.0, 0.0], 4, ((0.5, 0.0), (0.0, 0.5)))),
                       _c("shifted-start", x0=[1.0, 0.0], traj=_decay([1.0, 0.0], 5)[1:])),
        sealed_faults=(_c("late-drift", x0=[1.0, 1.0], traj=_drifted(_decay([1.0, 1.0], 6), 5, 1e-3)),
                       _c("wrong-rate", x0=[2.0, -1.0], traj=_decay([2.0, -1.0], 3, ((0.5, 0.0), (0.25, 0.55)))))),
}


# ------------------------------------------------------------------ synthesized verifiers
@dataclass(frozen=True)
class SynthesizedVerifier:
    problem: str
    kind: str
    rules: tuple[dict[str, Any], ...]
    author: str  # "<family>/<model>" of the backend that wrote it
    family: str

    def hash(self) -> str:
        return hashlib.sha256(canonical_json({"format": SPEC_FORMAT, "problem": self.problem, "kind": self.kind,
                                              "rules": list(self.rules), "author": self.author,
                                              "family": self.family})).hexdigest()


def to_record(v: SynthesizedVerifier) -> dict[str, Any]:
    """A stored form of a verifier: the spec text plus who wrote it. Never a tier or a validation result: those are
    recomputed from the spec, the suite and the Kernel ledger at every use (`from_record` + `tier`)."""
    spec = {"format": SPEC_FORMAT, "problem": v.problem, "kind": v.kind, "rules": list(v.rules)}
    return {"spec": json.dumps(spec, sort_keys=True), "author": v.author, "family": v.family,
            "verifier_hash": v.hash()}


def from_record(doc: object) -> SynthesizedVerifier:
    """Re-parse a stored verifier through the closed vocabulary; the stored hash must match the re-parsed one."""
    if not isinstance(doc, dict) or set(doc) != {"spec", "author", "family", "verifier_hash"}:
        raise SynthesisError("not a stored synthesized verifier")
    if not all(isinstance(doc[k], str) for k in doc):
        raise SynthesisError("stored verifier fields must be strings")
    try:
        problem = json.loads(doc["spec"]).get("problem")
    except (ValueError, AttributeError):
        raise SynthesisError("stored verifier spec is not a JSON object") from None
    if not isinstance(problem, str):
        raise SynthesisError("stored verifier spec names no problem")
    v = parse_spec(doc["spec"], problem, author=doc["author"], family=doc["family"])
    if v.hash() != doc["verifier_hash"]:
        raise SynthesisError("stored verifier hash does not match its content")
    return v


def _const(v: object, what: str) -> Fraction:
    if not isinstance(v, str):
        raise SynthesisError(f"{what} must be an exact rational string 'p' or 'p/q', got {v!r}")
    try:
        return lp.parse_rational(v)
    except lp.LPError as e:
        raise SynthesisError(f"{what}: {e}") from None


def parse_spec(text: str, problem: str, author: str, family: str) -> SynthesizedVerifier:
    try:
        doc = json.loads(text)
    except ValueError:
        raise SynthesisError("the verifier spec is not JSON") from None
    if not isinstance(doc, dict) or doc.get("format") != SPEC_FORMAT:
        raise SynthesisError(f"the verifier spec must be a {SPEC_FORMAT} object (format)")
    if set(doc) != {"format", "problem", "kind", "rules"}:
        raise SynthesisError(f"spec keys must be format, problem, kind, rules; got {sorted(doc)}")
    if doc["problem"] != problem:
        raise SynthesisError(f"spec is for problem {doc['problem']!r}, asked for {problem!r}")
    kind = doc["kind"]
    if kind not in KINDS_OPS:
        raise SynthesisError(f"unknown verifier kind {kind!r} (one of {sorted(KINDS_OPS)})")
    rules = doc["rules"]
    if not isinstance(rules, list):
        raise SynthesisError("rules must be a list")
    for i, r in enumerate(rules):
        if not isinstance(r, dict) or r.get("op") not in OPS:
            raise SynthesisError(f"rule {i}: unknown op {r.get('op') if isinstance(r, dict) else r!r}")
        if r["op"] not in KINDS_OPS[kind]:
            raise SynthesisError(f"rule {i}: op {r['op']!r} is not allowed in a {kind} verifier")
        if set(r) - {"op"} != OPS[r["op"]]:
            raise SynthesisError(f"rule {i} ({r['op']}): keys must be {sorted(OPS[r['op']])} (field names)")
        for k in ("field", "of", "a", "b", "x0", "trajectory"):
            if k in r and not isinstance(r[k], str):
                raise SynthesisError(f"rule {i}: {k} must be a field name")
        for k in _RATIONAL_KEYS & set(r):
            _const(r[k], f"rule {i} {k}")
        if r["op"] == "simulate_linear":
            a = r["A"]
            if not (isinstance(a, list) and a and all(isinstance(row, list) and len(row) == len(a) for row in a)):
                raise SynthesisError(f"rule {i}: A must be a square list of rows")
            for row in a:
                for q in row:
                    _const(q, f"rule {i} A entry")
            if _const(r["tolerance"], f"rule {i} tolerance") < 0:
                raise SynthesisError(f"rule {i}: tolerance must be >= 0")
        if r["op"] == "one_of" and not isinstance(r["values"], list):
            raise SynthesisError(f"rule {i}: values must be a list")
    return SynthesizedVerifier(problem=problem, kind=kind, rules=tuple(rules), author=author, family=family)


def synthesize(backend: Backend, problem: str,
               complete: Callable[[ChatRequest], Any] | None = None) -> SynthesizedVerifier:
    """Ask `backend` (a scripted backend in tests; no live call is made here) to write a verifier for `problem`. The
    model sees the problem description, the vocabulary and the PUBLIC golden cases only. `complete` (optional) is
    the call path to use instead of `backend.complete` (the research module passes `ModuleContext.llm`, so the call
    is budget-checked and traced); the author family is still read from `backend`."""
    view = SUITES[problem].author_view() if problem in SUITES else json.dumps({"problem": problem})
    vocab = json.dumps({"format": SPEC_FORMAT, "kinds": {k: sorted(v) for k, v in KINDS_OPS.items()},
                        "op_keys": {k: sorted(v) for k, v in OPS.items()}}, sort_keys=True)
    model = getattr(backend, "model", None)
    req = ChatRequest(model=model if isinstance(model, str) else None, messages=[
        Message(role="system", content="Write a verifier for the problem as one JSON object in this vocabulary "
                                       f"(numbers as exact rational strings): {vocab}"),
        Message(role="user", content=view)])
    resp = (complete or backend.complete)(req)
    family = model_family(backend)
    return parse_spec(resp.text, problem, author=f"{family}/{resp.provenance.model}", family=family)


# ------------------------------------------------------------------ the trusted interpreter
def _num(v: object, what: str) -> Fraction:
    if isinstance(v, bool):
        raise SynthesisError(f"{what} is a boolean, not a number")
    if isinstance(v, int):
        return Fraction(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise SynthesisError(f"{what} is not finite")
        return Fraction(v)
    if isinstance(v, str):
        return lp.parse_rational(v)
    raise SynthesisError(f"{what} is not a number")


def _nums(v: object, what: str) -> list[Fraction]:
    if not isinstance(v, list):
        raise SynthesisError(f"{what} is not a list")
    return [_num(x, f"{what}[{i}]") for i, x in enumerate(v)]


def _norm(v: object, what: str) -> object:
    return [_norm(x, what) for x in v] if isinstance(v, list) else _num(v, what)


def _field(subject: dict[str, Any], name: str) -> object:
    if not isinstance(subject, dict) or name not in subject:
        raise SynthesisError(f"subject has no field {name!r}")
    return subject[name]


def _rule(r: dict[str, Any], s: dict[str, Any]) -> str | None:
    op = r["op"]
    if op == "range":
        v = _field(s, r["field"])
        xs = _nums(v, r["field"]) if isinstance(v, list) else [_num(v, r["field"])]
        lo, hi = _const(r["lo"], "lo"), _const(r["hi"], "hi")
        bad = [x for x in xs if not lo <= x <= hi]
        return f"{r['field']}: {len(bad)} value(s) outside [{lo}, {hi}]" if bad else None
    if op == "sorted":
        xs = _nums(_field(s, r["field"]), r["field"])
        return f"{r['field']} is not nondecreasing" if any(p > q for p, q in itertools.pairwise(xs)) else None
    if op == "le":
        a, b = _num(_field(s, r["a"]), r["a"]), _num(_field(s, r["b"]), r["b"])
        return f"{r['a']} > {r['b']}" if a > b else None
    if op == "equals":
        return None if _norm(_field(s, r["field"]), r["field"]) == _norm(r["value"], "value") else \
            f"{r['field']} differs from the required value"
    if op == "one_of":
        got = _norm(_field(s, r["field"]), r["field"])
        return None if any(got == _norm(v, "values") for v in r["values"]) else f"{r['field']} is not an allowed value"
    if op in ("permutation_of", "sum_equals"):
        xs, ys = _nums(_field(s, r["field"]), r["field"]), _nums(_field(s, r["of"]), r["of"])
        if op == "permutation_of":
            return None if sorted(xs) == sorted(ys) else f"{r['field']} is not a permutation of {r['of']}"
        return None if sum(xs, Fraction(0)) == sum(ys, Fraction(0)) else f"sum of {r['field']} != sum of {r['of']}"
    # simulate_linear
    mat = [[_const(q, "A") for q in row] for row in r["A"]]
    tol, n = _const(r["tolerance"], "tolerance"), len(mat)
    x0 = _nums(_field(s, r["x0"]), r["x0"])
    traj_raw = _field(s, r["trajectory"])
    if not isinstance(traj_raw, list) or not traj_raw:
        return f"{r['trajectory']} is not a non-empty list of rows"
    traj = [_nums(row, f"{r['trajectory']}[{k}]") for k, row in enumerate(traj_raw)]
    if len(x0) != n or any(len(row) != n for row in traj):
        return f"dimension mismatch with the {n}-dimensional model"
    x = x0
    for k, row in enumerate(traj):
        if k:
            x = [sum((mat[i][j] * x[j] for j in range(n)), Fraction(0)) for i in range(n)]
        if max(abs(p - q) for p, q in zip(row, x, strict=True)) > tol:
            return f"{r['trajectory']}[{k}] differs from the simulation by more than {tol}"
    return None


def run_rules(verifier: SynthesizedVerifier, subject: dict[str, Any]) -> list[str]:
    """Why the verifier rejects `subject` (empty = accepts). An unreadable subject or a failing rule rejects."""
    reasons = []
    for i, r in enumerate(verifier.rules):
        try:
            why = _rule(r, subject)
        except (SynthesisError, lp.LPError, TypeError, ValueError, KeyError, IndexError) as e:
            why = f"unreadable: {e}"
        if why is not None:
            reasons.append(f"rule {i} ({r['op']}): {why}")
    return reasons


# ------------------------------------------------------------------ validation, review, tier
@dataclass(frozen=True)
class Validation:
    verifier_hash: str
    suite_hash: str | None
    golden_rejected: tuple[str, ...]
    faults_missed: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.suite_hash is not None and not (self.golden_rejected or self.faults_missed or self.errors)


def validate(verifier: SynthesizedVerifier) -> Validation:
    """Fault-injection validation against the FIXED human-owned suite of the verifier's problem."""
    suite = SUITES.get(verifier.problem)
    if suite is None or suite.problem != verifier.problem:
        return Validation(verifier.hash(), None, (), (), (f"no human-owned fault suite for {verifier.problem!r}",))
    errors = []
    for half in ("public_golden", "sealed_golden"):
        if len(getattr(suite, half)) < C.SYNTH_MIN_GOLDEN_PER_HALF:
            errors.append(f"suite {half} has fewer than {C.SYNTH_MIN_GOLDEN_PER_HALF} golden cases")
    for half in ("public_faults", "sealed_faults"):
        if len(getattr(suite, half)) < C.SYNTH_MIN_FAULTS_PER_HALF:
            errors.append(f"suite {half} has fewer than {C.SYNTH_MIN_FAULTS_PER_HALF} planted faults")
    golden = (*suite.public_golden, *suite.sealed_golden)
    faults = (*suite.public_faults, *suite.sealed_faults)
    rejected = tuple(c.id for c in golden if run_rules(verifier, c.subject))
    missed = tuple(c.id for c in faults if not run_rules(verifier, c.subject))
    return Validation(verifier.hash(), suite.hash(), rejected, missed, tuple(errors))


def record_review(ledger: Ledger, verifier: SynthesizedVerifier, *, reviewer: str, approve: bool,
                  note: str = "") -> Entry:
    """Ledger a HUMAN review of exactly this verifier (hash) against exactly this suite (hash)."""
    name = _check_reviewer(reviewer, (verifier.author, verifier.family))
    val = validate(verifier)
    if not val.passed:
        raise SynthesisError(f"verifier {verifier.hash()[:12]} has not passed its fault-injection validation: "
                             f"golden rejected {list(val.golden_rejected)}, faults missed {list(val.faults_missed)}, "
                             f"{list(val.errors)}")
    if REVIEW_KIND not in _ledger.KINDS:
        raise SynthesisError(f"the Kernel ledger has no kind {REVIEW_KIND!r} yet (requested from kstats)")
    return ledger.append(REVIEW_KIND, {"verifier_hash": val.verifier_hash, "suite_hash": val.suite_hash,
                                       "problem": verifier.problem, "author": verifier.author,
                                       "reviewer": name, "approve": bool(approve), "note": note})


def _check_reviewer(reviewer: str, authors: Sequence[str]) -> str:
    name = reviewer.strip()
    if not name:
        raise SynthesisError("a review must name the human reviewer")
    if name in authors or name.casefold().startswith("agent"):
        # accountability-level, like adapter approvals: the CLI is human-operated; an agent identity is refused
        raise SynthesisError("an author (or any agent identity) cannot review it")
    return name


def record_suite_review(ledger: Ledger, problem: str, *, reviewer: str, approve: bool, note: str = "") -> Entry:
    """Ledger a HUMAN review of exactly this fault suite (hash). Only this makes an (agent-authored) suite count as
    human-approved; a rejection makes every verifier validated against it unusable."""
    suite = SUITES.get(problem)
    if suite is None:
        raise SynthesisError(f"no fault suite for {problem!r}")
    name = _check_reviewer(reviewer, (suite.author,))
    if REVIEW_KIND not in _ledger.KINDS:
        raise SynthesisError(f"the Kernel ledger has no kind {REVIEW_KIND!r}")
    return ledger.append(REVIEW_KIND, {"subject": SUITE_SUBJECT, "suite_hash": suite.hash(), "problem": problem,
                                       "author": suite.author, "authorship": suite.authorship, "reviewer": name,
                                       "approve": bool(approve), "note": note})


def _reviews(ledger: Ledger | None) -> list[dict[str, Any]]:
    return [e.body for e in ledger.entries(REVIEW_KIND)] if ledger is not None else []


def suite_status(problem: str, ledger: Ledger | None) -> str:
    """"human-rejected" | "human-approved" | AGENT_AUTHORED (no ledgered human approval of this exact suite hash).
    A rejection wins over any approval."""
    suite = SUITES.get(problem)
    if suite is None:
        return "unknown"
    h = suite.hash()
    mine = [b for b in _reviews(ledger) if b.get("subject") == SUITE_SUBJECT and b.get("suite_hash") == h
            and "verifier_hash" not in b]
    if any(b.get("approve") is not True for b in mine):
        return "human-rejected"
    return "human-approved" if mine else suite.authorship


def tier(verifier: SynthesizedVerifier, ledger: Ledger | None) -> Tier | None:
    """None = unusable (failed validation, or a human rejected the verifier or its suite). Else T2 (cap), lifted to
    T1 only by a ledgered human approval of this verifier AND a ledgered human approval of its suite: an
    agent-authored suite cannot lift the cap, whatever review the verifier has."""
    val = validate(verifier)
    if not val.passed:
        return None
    suite = suite_status(verifier.problem, ledger)
    if suite == "human-rejected":
        return None
    mine = [b for b in _reviews(ledger) if "subject" not in b and b.get("verifier_hash") == val.verifier_hash
            and b.get("suite_hash") == val.suite_hash]
    if any(b.get("approve") is not True for b in mine):
        return None
    cap = max(C.SYNTH_REVIEWED_TIER, C.SYNTH_UNREVIEWED_TIER_CAP)
    return C.SYNTH_REVIEWED_TIER if mine and suite == "human-approved" else cap


# ------------------------------------------------------------------ verdicts and independence
@dataclass(frozen=True)
class Author:
    id: str
    family: str


@dataclass(frozen=True)
class HumanVerifier:
    """A human-owned check (e.g. an approved adapter's), usable as an independent second verifier."""
    name: str
    owner: str
    tier: Tier
    check: Callable[[dict[str, Any]], list[str]]

    @property
    def author(self) -> str:
        return f"human:{self.owner}"

    @property
    def family(self) -> str:
        return "human"


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    tier: Tier | None
    reasons: tuple[str, ...]


def _label(v: SynthesizedVerifier | HumanVerifier) -> str:
    return f"{v.name} ({v.owner})" if isinstance(v, HumanVerifier) else f"{v.kind} {v.hash()[:12]} by {v.author}"


def verdict(problem: str, subject: dict[str, Any], claim_author: Author,
            verifiers: Sequence[SynthesizedVerifier | HumanVerifier], ledger: Ledger | None) -> Verdict:
    reasons: list[str] = []
    accepted: list[tuple[SynthesizedVerifier | HumanVerifier, Tier]] = []
    if not verifiers:
        reasons.append("no verifier")
    for v in verifiers:
        if isinstance(v, SynthesizedVerifier):
            if v.problem != problem:
                reasons.append(f"{_label(v)} is for problem {v.problem!r}, not {problem!r}")
                continue
            t = tier(v, ledger)
            if t is None:
                reasons.append(f"{_label(v)} is unusable: it failed fault-injection validation or a human rejected it")
                continue
            why = run_rules(v, subject)
        else:
            t = v.tier
            try:
                got = v.check(subject)
                why = [str(x) for x in got] if isinstance(got, list) else [f"check returned {type(got).__name__}"]
            except Exception as e:  # noqa: BLE001  a crashing check rejects
                why = [f"check crashed: {type(e).__name__}"]
        if why:
            reasons.append(f"{_label(v)} rejects: {'; '.join(why)}")
        else:
            accepted.append((v, t))

    def conflicted(v: SynthesizedVerifier | HumanVerifier) -> bool:
        same_author = claim_author.id == v.author
        same_family = claim_author.family == v.family
        return isinstance(v, SynthesizedVerifier) and (same_author or same_family)

    def independent(w: SynthesizedVerifier | HumanVerifier, v: SynthesizedVerifier | HumanVerifier) -> bool:
        return w is not v and not conflicted(w) and w.author not in (v.author, claim_author.id) and \
            w.family not in (v.family, claim_author.family)

    for v, _ in accepted:
        if conflicted(v) and not any(independent(w, v) for w, _ in accepted):
            reasons.append(f"{_label(v)} checks a claim of its own author/family: no independent second verifier "
                           "accepted it")
    ok = not reasons
    return Verdict(ok, chain_tier(t for _, t in accepted) if ok else None, tuple(reasons))

