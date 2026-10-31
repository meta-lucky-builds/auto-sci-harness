"""The Court health run (GOAL 18.6; UPGRADES N1): the real Court, end to end, on claims whose right answer is known.

`run_health` puts one honest toy, one planted real effect, one null claim and the planted false claims through the real
route (register, kerneld mint, run, Kernel K0-K9 with re-execution, bundle, badges), each in a throwaway store. There
are no model calls: every program is a script below. The positive and null salt ideas
(GOAL 16.9) are the planted real effect and the null claim; budgeted model-backed salts stay in `custom_harness/salts`.

`passed` is true only if every case got its expected answer. The hash covers the Court version and each case's outcome.
`require_passed` is what `register_promotion` and `register_sealed` call: a case can only be registered on a Court
version with a passing health run, and the run's hash goes into the claim's assumptions, so it is inside the
certificate. A changed Court (any file `court_version` covers) needs a new health run.

The two human steps are played on the scratch stores alone (`SCRATCH_APPROVER`). A health run is not evidence for any
claim; nothing from it enters the real ledger, only a `court_health` record.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..audit.spec import Budget, ClaimPart
from ..kerneld.scope import ScopedKernel
from ..store import EvidenceStore
from . import minted as C
from .levels import level

RECORD_KIND = "court_health"
SCRATCH_APPROVER = "health-run-scratch-store"
BEACON = "health-run-scratch-beacon-not-public"
_ROOTS = ("kernel", "kerneld", "court", "verify", "sandbox")
_FILES = ("audit/wrapper.py", "audit/program.py", "audit/claim_audit.py")

_MAJORITY = '''
class Majority:
    def fit(self, X, y):
        self.top = max(sorted(set(y)), key=y.count)
    def predict(self, X):
        return [self.top] * len(X)
def build_model(seed):
    return Majority()
'''
_KNN = '''
class Radius:
    def fit(self, X, y):
        self.rows = sorted((sum(v * v for v in r[:2]), lab) for r, lab in zip(X, y))
    def predict(self, X):
        import bisect
        keys = [k for k, _ in self.rows]
        out = []
        for r in X:
            i = bisect.bisect(keys, sum(v * v for v in r[:2]))
            near = [lab for _, lab in self.rows[max(0, i - 7): i + 7]]
            out.append(max(sorted(set(near)), key=near.count))
        return out
def build_model(seed):
    return Radius()
'''
_FLAKY = _KNN.replace("return out", "import os\n        return [v if os.urandom(1)[0] > 40 else '0' for v in out]")
_READS_LABELS = '''
class Snoop:
    def fit(self, X, y):
        self.top = max(sorted(set(y)), key=y.count)
        import glob
        self.found = [p for p in glob.glob("../**/test_y.csv", recursive=True)]
    def predict(self, X):
        if self.found:
            rows = open(self.found[0]).read().split()[1:]
            return rows[:len(X)]
        return [self.top] * len(X)
def build_model(seed):
    return Snoop()
'''
_LEAKY = '''
class Leak:
    def fit(self, X, y):
        self.top = max(sorted(set(y)), key=y.count)
        self.memo = {tuple(r): lab for r, lab in zip(X, y)}
    def predict(self, X):
        return [self.memo.get(tuple(r), self.top) for r in X]
def build_model(seed):
    return Leak()
'''
_PHACKED = '''
class Lucky:
    """The best of many random rules on Lab data: no real effect on fresh data."""
    def fit(self, X, y):
        self.top = max(sorted(set(y)), key=y.count)
    def predict(self, X):
        return [self.top if int(abs(r[0]) * 1000) % 7 else "0" for r in X]
def build_model(seed):
    return Lucky()
'''
_STRAWMAN = '''
class Broken:
    def fit(self, X, y):
        pass
    def predict(self, X):
        return ["never-a-label"] * len(X)
def build_model(seed):
    return Broken()
'''


@dataclass(frozen=True)
class Plant:
    name: str
    method: str
    baseline: str
    family: str
    min_effect: float
    certifies: bool  # the right answer
    what: str
    refused_by: str | None = None  # text of the COULD NOT RUN reason that is the right answer, for a plant stopped early


PLANTS = (
    Plant("honest_toy", _KNN, _MAJORITY, "rings", 0.1, True, "a real method on a real generator effect"),
    Plant("real_effect", _KNN, _MAJORITY, "rings", 0.2, True, "positive salt: a known large effect must be recovered"),
    Plant("null_claim", _KNN, _MAJORITY, "noise", 0.1, False, "null salt: the generator holds no effect"),
    Plant("leaky", _LEAKY, _MAJORITY, "rings", 0.1, False, "memorises training rows; fresh rows carry no leak"),
    Plant("p_hacked", _PHACKED, _MAJORITY, "rings", 0.1, False, "a rule picked on Lab data, no effect on fresh data"),
    Plant("label_reading", _READS_LABELS, _MAJORITY, "rings", 0.1, False, "searches the disk for the held-out labels"),
    Plant("flaky", _FLAKY, _MAJORITY, "rings", 0.1, False, "predictions change between runs"),
    Plant("strawman_baseline", _MAJORITY, _STRAWMAN, "rings", 0.1, False, "a comparator that can never be right",
          "strawman"),
)


def court_version() -> str:
    """Hash of the code a verdict depends on."""
    src = Path(__file__).resolve().parents[1]
    files = sorted(p for r in _ROOTS for p in (src / r).rglob("*.py"))
    files += [src / f for f in _FILES]
    h = hashlib.sha256()
    for p in files:
        h.update(p.relative_to(src).as_posix().encode() + b"\0" + p.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def _correct(plant: Plant, out: dict[str, Any], certified: bool) -> bool:
    """The right answer, for the right reason. A plant that merely failed to run has shown nothing: a broken route
    must never look like a Court that rejected every false claim."""
    if out.get("cert_hash"):
        return certified == plant.certifies
    return plant.refused_by is not None and plant.refused_by in str(out.get("reason"))


def run_plant(plant: Plant, root: Path, sandbox_factory: Any) -> dict[str, Any]:
    """One planted claim through the real route, in its own throwaway store."""
    store = EvidenceStore(root / "store")
    try:
        ScopedKernel(store).approve_adapter("ml", approver=SCRATCH_APPROVER)
        part = ClaimPart(metric="accuracy", min_effect=plant.min_effect, probability=0.6, seeds=(0, 1, 2), n_boot=1000,
                         resolve_by=date(2030, 1, 1))
        body = C.register_case(store, role="promotion", promotion_id=f"health-{plant.name}", session_id="health",
                               title=plant.name, sentence=f"health run plant {plant.name}: {plant.what}", part=part,
                               budget=Budget(timeout_s=120), method=plant.method.encode(),
                               baseline=plant.baseline.encode(), lock=b"health==1\n", family=plant.family,
                               beacon_round="health", sizing={"n_test": 800}, n_train=400,
                               anchor_file=root / "anchors" / "a.jsonl")
        C.mint(store, body["case_id"], BEACON)
        ScopedKernel(store).approve_role(body["target"], approver=SCRATCH_APPROVER, note="health run: scratch store only")
        out = C.run(store, body["case_id"], sandbox_factory(root), bundle_root=root / "bundles")
        certified = str(out.get("label", "")).startswith("CERTIFIED")
        badge = level(store, str(out["cert_hash"])).badge if out.get("cert_hash") else "NONE"
        failed = ScopedKernel(store).certificate(out["cert_hash"]).failed() if out.get("cert_hash") else []
        return {"plant": plant.name, "what": plant.what, "expected_certified": plant.certifies,
                "certified": certified,
                "correct": _correct(plant, out, certified), "verdict": out.get("verdict"), "label": out.get("label"),
                "failed_checks": failed,
                "reason": out.get("reason"), "badge": badge}
    finally:
        store.close()


def run_health(store: EvidenceStore, sandbox_factory: Any, plants: tuple[Plant, ...] = PLANTS) -> dict[str, Any]:
    """Run every plant; record the result. `sandbox_factory(root)` gives the factory for one plant's scratch root."""
    results = []
    for plant in plants:
        root = Path(tempfile.mkdtemp(prefix=f"chh-health-{plant.name}-"))
        try:
            results.append(run_plant(plant, root, sandbox_factory))
        finally:
            shutil.rmtree(root, ignore_errors=True)
    doc = {"court_version": court_version(), "results": results, "passed": all(r["correct"] for r in results),
           "plants": [p.name for p in plants]}
    body = {**doc, "health_hash": hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest(),
            "at": time.time()}
    store.append_record(RECORD_KIND, body)
    return body


def passing(store: EvidenceStore) -> dict[str, Any] | None:
    """The latest passing full health run of THIS Court version."""
    version, want = court_version(), [p.name for p in PLANTS]
    rows = [r for r in store.records(RECORD_KIND) if r.get("passed") and r.get("court_version") == version
            and r.get("plants") == want]
    return rows[-1] if rows else None


def require_passed(store: EvidenceStore) -> str:
    """The hash of the passing health run a new case rests on; refused without one."""
    row = passing(store)
    if row is None:
        raise C.CourtRefused("no passing Court health run for this Court version: run `chh court health --docker`")
    return str(row["health_hash"])
