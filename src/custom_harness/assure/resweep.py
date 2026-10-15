"""Resweep (GOAL §16.10): re-check archived certificates when the rules change.

A RULE is a named change to the human-owned Kernel policy (`KernelConfig` overrides; empty overrides = "the rules of
today's Kernel code"). Resweep never edits the Kernel and uses only its public API: `Kernel.certify(..., as_of=seq)`
(re-derivation on the ledger as it stood) and `Kernel.certify(..., record=False)` (today's ledger).

Per certificate, the typed status (first match wins; all matches are listed):
  INTEGRITY-FAILURE     the ledger chain is broken, the certificate does not match its ledger entry, or, under the
                        rules it was issued under (same Kernel version), its record is MORE favourable than the
                        re-derivation from its own ledger prefix (forged)
  UNCHECKABLE           it cannot be re-derived here (unknown rule fields, the replayer is unavailable, an
                        infrastructure error, a crash)
  STALE-under-new-rule  under the new rule, as of its own prefix, the verdict differs from the one its own rules give
                        (also: issued under another Kernel version whose rules today's code no longer reproduces)
  STALE-new-data        under its own rules, today's ledger (later events) gives another verdict
  VALID                 none of the above

Clawback is GRADED and REVERSIBLE. It is a hash-chained ledger next to the store (`<store>/resweep/clawbacks.jsonl`),
never a Kernel retraction (those are permanent). Grades for a CERTIFIED record: none (VALID), flag (STALE-new-data:
it stands as issued, credit stays provisional), downgrade (new rule: still certified, lower label or tier), suspend
(new rule: rejected), hold (UNCHECKABLE), revoke (INTEGRITY-FAILURE). A REJECTED record that a new rule would certify
is reported as `no-upgrade`: cheap re-derivations only deny, they never grant (invariant 21); it needs a fresh
certification. A later sweep in which a certificate grades `none` writes a `restore` entry (e.g. the rule rolled back).

VDS (predictions.py) is split, without changing the Kernel's VDS: misses always count (penalties are never clawed
back); a positive gain is SEASONED once its certificate has passed at least one applied sweep under a rule other than
the one it was issued under and is currently graded none; CLAWED BACK while it is held, suspended or revoked;
PROVISIONAL otherwise.

A rule is APPLIED only after it passes a screen on the honest corpus's SEALED half (`screen`): 0 honest flips, 0
planted-fault escapes and 0 uncheckable items (declared bound: zero, no tolerance), recorded through the look ledger
with the rule's hash; `apply` refuses without such a record. Every verdict an applied sweep reports is a typed release
(§16.5, `Kernel.record_release`, channel `report`, alphabet `count`: five statuses) on every generalization target of
the certificate, so K4 counts it for claims registered afterwards.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import InfraError
from ..kernel import Certificate, Claim, Kernel, KernelConfig, KernelError
from ..kernel.checks import Replayer
from ..kernel.predictions import score_predictions
from ..store import EvidenceStore
from . import corpus, looks

INTEGRITY = "INTEGRITY-FAILURE"
UNCHECKABLE = "UNCHECKABLE"
STALE_RULE = "STALE-under-new-rule"
STALE_DATA = "STALE-new-data"
VALID = "VALID"
STATUSES = (INTEGRITY, UNCHECKABLE, STALE_RULE, STALE_DATA, VALID)
GRADES = ("none", "flag", "downgrade", "suspend", "hold", "revoke", "no-upgrade")
CLAWED = frozenset({"hold", "suspend", "revoke"})
KERNEL_DIR = Path(__file__).resolve().parents[1] / "kernel"


class ResweepError(RuntimeError):
    pass


def kernel_code_hash() -> str:
    h = hashlib.sha256()
    for f in sorted(KERNEL_DIR.glob("*.py")):
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


@dataclass(frozen=True)
class Rule:
    """A named rule change: KernelConfig field overrides applied on top of each certificate's own rules."""

    id: str
    overrides: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def validate(self) -> None:
        if not looks_like_code(self.id):
            raise ResweepError(f"rule id {self.id!r} must be a short lowercase code ([a-z][a-z0-9_.-]{{0,40}})")
        known = {f.name for f in dataclasses.fields(KernelConfig)}
        unknown = sorted(set(self.overrides) - known)
        if unknown:
            raise ResweepError(f"rule {self.id}: unknown KernelConfig field(s) {unknown}")

    def apply_to(self, cfg: KernelConfig) -> KernelConfig:
        return dataclasses.replace(cfg, **self.overrides)

    @property
    def hash(self) -> str:
        """Binds the rule to exactly this Kernel code: a screen passed under other code does not license it."""
        return hashlib.sha256(corpus.canonical({"id": self.id, "overrides": self.overrides,
                                                "kernel": kernel_code_hash()})).hexdigest()


def looks_like_code(s: str) -> bool:
    return re.fullmatch(r"[a-z][a-z0-9_.-]{0,40}", s) is not None


# ------------------------------------------------------------------ comparison
def _verdict(c: Certificate) -> dict[str, Any]:
    return {"label": c.label.value, "tier": c.tier, "checks": {k: v.status for k, v in c.checks.items()}}


def _diff(a: dict[str, Any], b: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(ways `a` is MORE favourable than `b`, ways it is less). Mirrors the Kernel's own comparison rule."""
    better: list[str] = []
    worse: list[str] = []
    for k in sorted(a["checks"]):
        if a["checks"][k] != b["checks"].get(k):
            (better if b["checks"].get(k) == "fail" else worse).append(f"{k}: {a['checks'][k]} vs {b['checks'].get(k)}")
    if a["label"] != b["label"]:
        (better if b["label"] == "REJECTED" else worse).append(f"label {a['label']} vs {b['label']}")
    if a["tier"] != b["tier"]:
        (better if a["tier"] < b["tier"] else worse).append(f"tier {a['tier']} vs {b['tier']}")
    return better, worse


def _rank(label: str) -> int:
    return {"REJECTED": 0, "SUPPORTED": 1, "CERTIFIED-INTERNAL": 2}.get(label, 3 if label.startswith("CERTIFIED")
                                                                        else 0)


@dataclass
class CertResult:
    cert_hash: str
    claim_hash: str
    targets: tuple[str, ...]
    recorded: dict[str, Any]
    status: str
    statuses: list[str]
    reasons: list[str]
    new: dict[str, Any] | None = None  # the verdict under the new rule (as of the certificate's prefix)
    grade: str = "none"


def grade_of(r: CertResult) -> str:
    certified = r.recorded["label"].startswith("CERTIFIED")
    if r.status == INTEGRITY:
        return "revoke" if certified else "none"
    if r.status == UNCHECKABLE:
        return "hold" if certified else "none"
    if r.status == STALE_RULE and r.new is not None:
        if not certified:
            return "no-upgrade" if r.new["label"].startswith("CERTIFIED") else "none"
        if not r.new["label"].startswith("CERTIFIED"):
            return "suspend"
        if _rank(r.new["label"]) < _rank(r.recorded["label"]) or r.new["tier"] > r.recorded["tier"]:
            return "downgrade"
        return "flag"
    if r.status == STALE_RULE:  # another Kernel version re-derives differently, no new-rule verdict to compare
        return "hold" if certified else "none"
    if r.status == STALE_DATA:
        return "flag" if certified else "none"
    return "none"


# ------------------------------------------------------------------ classification
ReplayerFactory = Callable[[], Replayer | None]


def classify(store: EvidenceStore, cert_hash: str, rule: Rule, replayer: ReplayerFactory) -> CertResult:
    today = KernelConfig()
    base = Kernel(store, replayer=replayer())
    chain = base.ledger.verify()
    try:
        cert = base.certificate(cert_hash)
    except (KernelError, ValueError, OSError):
        return CertResult(cert_hash, "", (), {"label": "?", "tier": 0, "checks": {}}, INTEGRITY, [INTEGRITY],
                          ["the certificate does not match its ledger entry (or its blob is missing)"])
    claim = Claim.model_validate(cert.claim)
    rec = _verdict(cert)
    out = CertResult(cert_hash, cert.claim_hash, tuple(claim.generalization_targets), rec, VALID, [], [])
    if chain:
        out.statuses.append(INTEGRITY)
        out.reasons += [f"ledger: {p}" for p in chain[:3]]
    seq = next(e.seq for e in base.ledger.entries("certificate") if e.body.get("cert_hash") == cert_hash)
    ids = list(cert.evidence_ids)
    try:
        own = KernelConfig(**cert.config)
    except TypeError:
        out.statuses.append(UNCHECKABLE)
        out.reasons.append("the certificate's rules name fields today's Kernel does not have")
        return _finish(out)
    try:
        old_prefix = _verdict(Kernel(store, config=own, replayer=replayer()).certify(claim, ids, as_of=seq - 1))
        old_today = _verdict(Kernel(store, config=own, replayer=replayer()).certify(claim, ids, record=False))
        new_prefix = _verdict(Kernel(store, config=rule.apply_to(own), replayer=replayer()).certify(
            claim, ids, as_of=seq - 1))
    except (InfraError, corpus.Uncheckable) as e:
        out.statuses.append(UNCHECKABLE)
        out.reasons.append(f"re-derivation impossible here: {type(e).__name__}")
        return _finish(out)
    except Exception as e:  # noqa: BLE001  fail closed: a crash never yields VALID
        out.statuses.append(UNCHECKABLE)
        out.reasons.append(f"re-derivation raised {type(e).__name__}")
        return _finish(out)
    better, _worse = _diff(rec, old_prefix)
    if better:
        if cert.kernel_version == today.version:
            out.statuses.append(INTEGRITY)
            out.reasons.append("forged: the record is more favourable than its own ledger prefix supports under its "
                               "own rules: " + "; ".join(better))
        else:
            out.statuses.append(STALE_RULE)
            out.reasons.append(f"issued under Kernel {cert.kernel_version}; today's code ({today.version}) re-derives "
                               f"differently (a forgery cannot be told apart from a rule change here): "
                               + "; ".join(better))
    rb, rw = _diff(old_prefix, new_prefix)
    if rb or rw:
        out.statuses.append(STALE_RULE)
        out.new = new_prefix
        out.reasons.append(f"rule {rule.id}: " + "; ".join(rb + rw))
    db, dw = _diff(old_prefix, old_today)
    if db or dw:
        out.statuses.append(STALE_DATA)
        out.reasons.append("later ledger events: " + "; ".join(db + dw))
    return _finish(out)


def _finish(r: CertResult) -> CertResult:
    r.status = next((s for s in STATUSES if s in r.statuses), VALID)
    r.statuses = [s for s in STATUSES if s in r.statuses] or [VALID]
    r.grade = grade_of(r)
    return r


def certificates(store: EvidenceStore) -> list[str]:
    seen: list[str] = []
    for e in Kernel(store).ledger.entries("certificate"):
        h = e.body.get("cert_hash")
        if isinstance(h, str) and h not in seen:
            seen.append(h)
    return seen


# ------------------------------------------------------------------ clawback ledger (reversible)
def clawback_path(store: EvidenceStore) -> Path:
    return Path(store.root) / "resweep" / "clawbacks.jsonl"


def current_grades(path: Path) -> dict[str, str]:
    grades: dict[str, str] = {}
    for line in (path.read_text(encoding="utf-8").splitlines() if path.exists() else []):
        rec = json.loads(line)
        if rec.get("kind") in ("clawback", "restore", "notice"):
            grades[rec["cert_hash"]] = rec["grade"]
    return grades


def seasoned_by(path: Path) -> dict[str, set[str]]:
    """cert hash -> ids of the applied sweeps (rule hashes) it passed with grade none."""
    out: dict[str, set[str]] = {}
    for line in (path.read_text(encoding="utf-8").splitlines() if path.exists() else []):
        rec = json.loads(line)
        if rec.get("kind") == "sweep":
            for h in rec.get("passed", []):
                out.setdefault(h, set()).add(rec["rule_hash"])
    return out


def write_clawbacks(path: Path, rule: Rule, results: list[CertResult], at: str) -> list[dict[str, Any]]:
    problems = looks.check_chain(path)
    if problems:
        raise ResweepError(f"the clawback ledger is not intact: {problems[:3]}")
    cur = current_grades(path)
    written = []
    for r in results:
        before = cur.get(r.cert_hash, "none")
        if r.grade == before:
            continue
        kind = "restore" if r.grade == "none" else "notice" if r.grade == "no-upgrade" else "clawback"
        rec = {"kind": kind, "cert_hash": r.cert_hash, "grade": r.grade, "previous": before, "status": r.status,
               "rule": rule.id, "rule_hash": rule.hash, "at": at}
        looks.append(path, rec)
        written.append(rec)
    passed = [r.cert_hash for r in results if r.grade == "none" and r.recorded["label"].startswith("CERTIFIED")
              and rule.overrides]
    looks.append(path, {"kind": "sweep", "rule": rule.id, "rule_hash": rule.hash, "at": at, "n": len(results),
                        "passed": passed})
    return written


def vds_split(store: EvidenceStore, path: Path, now: float) -> dict[str, float]:
    kernel = Kernel(store)
    latest: dict[str, str] = {}
    for e in kernel.ledger.entries("certificate"):
        if isinstance(e.body.get("claim_hash"), str) and isinstance(e.body.get("cert_hash"), str):
            latest[e.body["claim_hash"]] = e.body["cert_hash"]
    grades = current_grades(path)
    seasoned = seasoned_by(path)
    out = {"seasoned": 0.0, "provisional": 0.0, "clawed_back": 0.0, "misses": 0.0}
    for s in score_predictions(kernel.ledger, now):
        g = s.info_gain_bits
        if g is None:
            continue
        if g <= 0:
            out["misses"] += g  # penalties always count
            continue
        cert = latest.get(s.claim_hash, "")
        grade = grades.get(cert, "none")
        if grade in CLAWED:
            out["clawed_back"] += g
        elif grade == "none" and seasoned.get(cert):
            out["seasoned"] += g
        else:
            out["provisional"] += g
    out["vds_kernel"] = out["seasoned"] + out["provisional"] + out["clawed_back"] + out["misses"]
    out["vds_seasoned"] = out["seasoned"] + out["misses"]
    return out


# ------------------------------------------------------------------ screen on the sealed half
def clopper_pearson_upper(failures: int, n: int, conf: float = 0.95) -> float:
    """One-sided upper confidence bound on a rate from `failures` of `n` (exact for failures = 0)."""
    if n == 0:
        return 1.0
    if failures == 0:
        return 1 - (1 - conf) ** (1 / n)
    lo, hi = failures / n, 1.0  # bisection on the binomial tail P(X <= failures | p) = 1 - conf
    for _ in range(60):
        mid = (lo + hi) / 2
        tail = sum(math.comb(n, k) * mid ** k * (1 - mid) ** (n - k) for k in range(failures + 1))
        lo, hi = (mid, hi) if tail > 1 - conf else (lo, mid)
    return hi


def screen(rule: Rule, by: str, reason: str, *, ledger: Path = corpus.DEFAULT_LOOKS,
           root: Path | None = None, man: dict[str, Any] | None = None, head: str = "",
           progress: Callable[[corpus.Recheck], None] | None = None) -> dict[str, Any]:
    """Re-check the honest corpus's SEALED half under the rule (one ledgered look). Passes only with 0 honest flips,
    0 fault escapes and 0 uncheckable items. Only counts are printed and ledgered, never which items flipped."""
    rule.validate()
    man = man or corpus.manifest()
    prior = sum(1 for line in (ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else [])
                if json.loads(line).get("purpose") == "resweep-screen" and json.loads(line).get("kind") == "look")
    look = corpus.open_sealed(by, reason, ledger=ledger, man=man, head=head, purpose="resweep-screen")
    items = corpus.sealed_items(look, root, man)
    res = corpus.check_all(items, overrides=rule.overrides, progress=progress)
    s = corpus.summary(res)
    n_honest = s["honest"]
    flips, escapes, unch = len(s["honest_flips"]), len(s["fault_escapes"]), len(s["uncheckable"])
    passed = flips == 0 and escapes == 0 and unch == 0 and n_honest > 0
    out = {"rule": rule.id, "rule_hash": rule.hash, "passed": passed, "items": s["items"], "honest": n_honest,
           "faults": s["faults"], "honest_flips": flips, "fault_escapes": escapes, "uncheckable": unch,
           "bound": "0 honest flips, 0 fault escapes, 0 uncheckable (no tolerance)",
           "flip_rate_upper95": round(clopper_pearson_upper(flips, n_honest), 4),
           "escape_rate_upper95": round(clopper_pearson_upper(escapes, s["faults"]), 4),
           "earlier_screen_looks": prior}
    looks.record_result(look, purpose="resweep-screen", **{k: v for k, v in out.items() if k != "bound"})
    return out


def screen_passed(rule: Rule, ledger: Path = corpus.DEFAULT_LOOKS) -> bool:
    """True iff the intact look ledger holds a passing screen result for exactly this rule hash."""
    if looks.check_chain(ledger):
        return False
    lines = ledger.read_text(encoding="utf-8").splitlines() if ledger.exists() else []
    looked = {looks.line_hash(x) for x in lines if json.loads(x).get("kind") == "look"
              and json.loads(x).get("purpose") == "resweep-screen"}
    for x in lines:
        r = json.loads(x)
        if (r.get("kind") == "result" and r.get("purpose") == "resweep-screen" and r.get("look") in looked
                and r.get("rule_hash") == rule.hash and r.get("passed") is True):
            return True
    return False


# ------------------------------------------------------------------ apply
def apply(store: EvidenceStore, rule: Rule, replayer: ReplayerFactory, *, ledger: Path = corpus.DEFAULT_LOOKS,
          release: bool = True, now: float | None = None) -> dict[str, Any]:
    """Classify every archived certificate under the rule, write graded (reversible) clawbacks, emit each verdict
    as a release on the certificate's targets, and report the VDS split. Refuses without a passing screen."""
    rule.validate()
    if not screen_passed(rule, ledger):
        raise ResweepError(f"rule {rule.id} ({rule.hash[:12]}) has no passing screen on the honest corpus's sealed "
                           f"half in {ledger}: run `resweep --screen` first")
    at = dt.datetime.now(dt.UTC).isoformat()
    results = [classify(store, h, rule, replayer) for h in certificates(store)]
    path = clawback_path(store)
    written = write_clawbacks(path, rule, results, at)
    released = 0
    if release:
        kernel = Kernel(store)
        datasets = set(kernel.vault.datasets())
        for r in results:
            for t in sorted(set(r.targets) & datasets):
                kernel.record_release(t, "report", "count", f"resweep.{rule.id}")
                released += 1
    return {"rule": rule.id, "rule_hash": rule.hash, "certificates": len(results),
            "by_status": {s: sum(r.status == s for r in results) for s in STATUSES},
            "by_grade": {g: sum(r.grade == g for r in results) for g in GRADES},
            "clawback_entries": len(written), "releases": released,
            "vds": vds_split(store, path, time.time() if now is None else now),
            "results": [dataclasses.asdict(r) for r in results]}
