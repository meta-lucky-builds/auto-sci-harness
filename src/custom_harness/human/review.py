"""HUMAN review of synthesized verifiers and of their fault suites (GOAL §5 L3 "Verifier synthesis").

Until a human approves BOTH a synthesized verifier (by its spec hash, against its suite's hash) AND that suite (by
hash), the verifier stays at T2 or below (verify/synthesis.py `tier`). Every suite in code and every synthesized
verifier is AGENT-AUTHORED; this module is how a human looks at one and records a decision:

- `verifier_view(store, ref)` / `suite_view(problem, store)`: what the reviewer must see. The spec (closed
  vocabulary, never executable), who wrote it, the AGENT-AUTHORED marker, and the fault-injection validation
  evidence. For a verifier the evidence is reported per half: the public half by case id, the SEALED half by counts
  only (case ids and contents of the sealed half stay out of every view a model could be shown; invariant 20). A
  suite review shows every case, both halves: the human approving a suite must see what it contains, so
  `chh review suite` runs only in an interactive terminal.
- `review_verifier(...)` / `review_suite(...)`: record approve or reject as a Kernel ledger entry of kind
  `verifier_review` (verify/synthesis.py `record_review` / `record_suite_review` own the body), naming the human
  reviewer. The caller must pass the hash it reviewed (`verifier_hash` / `suite_hash`); a different current hash is
  refused (invariant 15: the decision binds only the item it names).
- `pending_verifiers(store)` / `pending_suites(store)`: the queue's view (read-only).

Where synthesized verifiers are found: the research module `research.synthesize_verifier` stores each spec as a
blob and puts its ref in the step output (`verifier_synthesis.ref`). Module output is untrusted, so only the ref is
taken from it; the blob is re-parsed through the vocabulary, re-validated and re-hashed here at every use.
"""

from __future__ import annotations

from typing import Any

from ..kernel.ledger import Entry, Ledger
from ..store import EvidenceStore
from ..verify import l3_config as C
from ..verify import synthesis as S
from .guard import HumanActionRefused, require_human

VERIFIER_AUTHORSHIP = S.AGENT_AUTHORED  # every synthesized verifier is written by a model


def _ledger(store: EvidenceStore) -> Ledger:
    return Ledger(store)


def load_verifier(store: EvidenceStore, ref: str) -> S.SynthesizedVerifier:
    """The stored verifier at blob `ref`, re-parsed (HumanActionRefused when unreadable or tampered)."""
    try:
        return S.from_record(store.blobs.get_json(ref))
    except (S.SynthesisError, OSError, ValueError, KeyError, TypeError) as e:
        raise HumanActionRefused(f"no readable synthesized verifier at {ref!r}: {e}") from None


def _half(verifier: S.SynthesizedVerifier, cases: tuple[S.Case, ...], *, golden: bool) -> list[str]:
    """Ids of cases in `cases` the verifier gets wrong (a golden rejected, or a fault accepted)."""
    return [c.id for c in cases if bool(S.run_rules(verifier, c.subject)) == golden]


def _reviews(store: EvidenceStore) -> list[Entry]:
    return [e for e in _ledger(store).entries(S.REVIEW_KIND) if e.intact()]


def verifier_reviews(store: EvidenceStore, verifier_hash: str) -> list[dict[str, Any]]:
    return [{**e.body, "ledger_seq": e.seq} for e in _reviews(store)
            if "subject" not in e.body and e.body.get("verifier_hash") == verifier_hash]


def suite_reviews(store: EvidenceStore, suite_hash: str) -> list[dict[str, Any]]:
    return [{**e.body, "ledger_seq": e.seq} for e in _reviews(store)
            if e.body.get("subject") == S.SUITE_SUBJECT and e.body.get("suite_hash") == suite_hash]


def verifier_view(store: EvidenceStore, ref: str) -> dict[str, Any]:
    """Everything a reviewer needs to decide on one synthesized verifier (sealed half by counts only)."""
    v = load_verifier(store, ref)
    ledger = _ledger(store)
    val = S.validate(v)
    suite = S.SUITES.get(v.problem)
    evidence: dict[str, Any] = {"passed": val.passed, "suite_hash": val.suite_hash, "errors": list(val.errors),
                                "min_golden_per_half": C.SYNTH_MIN_GOLDEN_PER_HALF,
                                "min_faults_per_half": C.SYNTH_MIN_FAULTS_PER_HALF}
    if suite is not None:
        evidence.update({
            "public_golden": {"n": len(suite.public_golden),
                              "rejected": _half(v, suite.public_golden, golden=True)},
            "public_faults": {"n": len(suite.public_faults),
                              "missed": _half(v, suite.public_faults, golden=False)},
            "sealed_golden": {"n": len(suite.sealed_golden),
                              "rejected_count": len(_half(v, suite.sealed_golden, golden=True))},
            "sealed_faults": {"n": len(suite.sealed_faults),
                              "missed_count": len(_half(v, suite.sealed_faults, golden=False))},
        })
    t = S.tier(v, ledger)
    return {
        "ref": ref, "verifier_hash": v.hash(), "problem": v.problem, "kind": v.kind,
        "spec": {"format": S.SPEC_FORMAT, "problem": v.problem, "kind": v.kind, "rules": list(v.rules)},
        "author": v.author, "family": v.family, "authorship": VERIFIER_AUTHORSHIP,
        "suite_authorship": S.suite_status(v.problem, ledger),
        "validation": evidence, "tier_now": t.name if t is not None else None,
        "tier_cap_unreviewed": C.SYNTH_UNREVIEWED_TIER_CAP.name, "tier_reviewed": C.SYNTH_REVIEWED_TIER.name,
        "reviews": verifier_reviews(store, v.hash()),
        "decide": f"chh review verifier {ref} --decision approve|reject --reviewer NAME --verifier-hash {v.hash()}",
    }


def suite_view(problem: str, store: EvidenceStore) -> dict[str, Any]:
    """The whole suite (both halves): a human approving a suite must see every case it contains."""
    suite = S.SUITES.get(problem)
    if suite is None:
        raise HumanActionRefused(f"no fault suite for problem {problem!r}; known: {sorted(S.SUITES)}")
    h = suite.hash()

    def cases(half: tuple[S.Case, ...]) -> list[dict[str, Any]]:
        return [{"id": c.id, "subject": c.subject} for c in half]

    counts_ok = (len(suite.public_golden) >= C.SYNTH_MIN_GOLDEN_PER_HALF
                 and len(suite.sealed_golden) >= C.SYNTH_MIN_GOLDEN_PER_HALF
                 and len(suite.public_faults) >= C.SYNTH_MIN_FAULTS_PER_HALF
                 and len(suite.sealed_faults) >= C.SYNTH_MIN_FAULTS_PER_HALF)
    return {
        "problem": problem, "suite_hash": h, "author": suite.author, "authorship": suite.authorship,
        "status": S.suite_status(problem, _ledger(store)), "description": suite.description,
        "public_golden": cases(suite.public_golden), "sealed_golden": cases(suite.sealed_golden),
        "public_faults": cases(suite.public_faults), "sealed_faults": cases(suite.sealed_faults),
        "validation": {"meets_minimum_counts": counts_ok, "min_golden_per_half": C.SYNTH_MIN_GOLDEN_PER_HALF,
                       "min_faults_per_half": C.SYNTH_MIN_FAULTS_PER_HALF},
        "reviews": suite_reviews(store, h),
        "decide": f"chh review suite {problem} --decision approve|reject --reviewer NAME --suite-hash {h}",
    }


def review_verifier(store: EvidenceStore, ref: str, *, reviewer: str, approve: bool, verifier_hash: str,
                    note: str = "") -> Entry:
    """HUMAN action: approve or reject exactly the verifier at `ref` whose hash the reviewer names."""
    name = require_human(reviewer, what="the reviewer")
    v = load_verifier(store, ref)
    if verifier_hash != v.hash():
        raise HumanActionRefused(f"the verifier at {ref} hashes to {v.hash()}, not the {verifier_hash} you reviewed")
    try:
        return S.record_review(_ledger(store), v, reviewer=name, approve=approve, note=note)
    except S.SynthesisError as e:
        raise HumanActionRefused(str(e)) from None


def review_suite(store: EvidenceStore, problem: str, *, reviewer: str, approve: bool, suite_hash: str,
                 note: str = "") -> Entry:
    """HUMAN action: approve or reject exactly the fault suite of `problem` whose hash the reviewer names."""
    name = require_human(reviewer, what="the reviewer")
    suite = S.SUITES.get(problem)
    if suite is None:
        raise HumanActionRefused(f"no fault suite for problem {problem!r}")
    if suite_hash != suite.hash():
        raise HumanActionRefused(f"the {problem} suite hashes to {suite.hash()}, not the {suite_hash} you reviewed")
    try:
        return S.record_suite_review(_ledger(store), problem, reviewer=name, approve=approve, note=note)
    except S.SynthesisError as e:
        raise HumanActionRefused(str(e)) from None


# ------------------------------------------------------------------ queue views (read-only)
def synthesized_refs(store: EvidenceStore) -> list[str]:
    """Blob refs of synthesized verifiers named in any run's step outputs (oldest run first, deduplicated)."""
    seen: dict[str, None] = {}
    for run in reversed(store.list_runs(-1)):
        for step in store.trace(run.id):
            out = step.output.get("verifier_synthesis") if isinstance(step.output, dict) else None
            ref = out.get("ref") if isinstance(out, dict) else None
            if isinstance(ref, str) and ref:
                seen.setdefault(ref, None)
    return list(seen)


def pending_verifiers(store: EvidenceStore) -> list[dict[str, Any]]:
    """Validated, usable synthesized verifiers with no human decision on (this verifier hash, this suite hash)."""
    ledger = _ledger(store)
    out: list[dict[str, Any]] = []
    for ref in synthesized_refs(store):
        try:
            v = load_verifier(store, ref)
        except HumanActionRefused:
            continue
        val = S.validate(v)
        if not val.passed or S.tier(v, ledger) is None:
            continue  # failed validation or rejected: nothing for a human to approve
        decided = [r for r in verifier_reviews(store, v.hash()) if r.get("suite_hash") == val.suite_hash]
        if decided:
            continue
        out.append({"kind": "verifier_review", "ref": ref, "problem": v.problem, "author": v.author,
                    "authorship": VERIFIER_AUTHORSHIP, "binds": {"verifier_hash": v.hash(),
                                                                 "suite_hash": val.suite_hash},
                    "decide": f"chh review verifier {ref} --decision approve|reject --reviewer NAME "
                              f"--verifier-hash {v.hash()}"})
    return out


def pending_suites(store: EvidenceStore) -> list[dict[str, Any]]:
    """Fault suites with no human decision on their current hash (all are AGENT-AUTHORED)."""
    out: list[dict[str, Any]] = []
    for problem, suite in sorted(S.SUITES.items()):
        h = suite.hash()
        if suite_reviews(store, h):
            continue
        out.append({"kind": "suite_review", "problem": problem, "author": suite.author,
                    "authorship": suite.authorship, "binds": {"suite_hash": h},
                    "decide": f"chh review suite {problem} --decision approve|reject --reviewer NAME "
                              f"--suite-hash {h}"})
    return out
