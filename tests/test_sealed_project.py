"""Seal at project start and the wider run contract (GOAL 18.2, 18.6; kerneld/seal.py, court/sealed.py,
audit/program.py). Real kerneld, real Kernel, local sandbox on throwaway stores. The human steps (adapter and role
approval, the freeze
inputs) are played by the fixture on those stores only."""

from __future__ import annotations

import csv
import io
import json
import math
import random
from datetime import date
from pathlib import Path

import pytest

from custom_harness.audit import program as PG
from custom_harness.audit.spec import Budget, ClaimPart
from custom_harness.court import health as H
from custom_harness.court import levels as L
from custom_harness.court import minted as C
from custom_harness.court import sealed as S
from custom_harness.kernel import Kernel
from custom_harness.kerneld.scope import ScopedKernel
from custom_harness.kerneld.seal import SealSpecError, seal_files
from custom_harness.lab import promote as LP
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore

ARMS = Path(__file__).parent / "fixtures" / "audit" / "arms"
BEACON = "public-beacon-value-0001"
SESSION = "s-seal-1"


def rings_csv(n: int = 1600, seed: int = 5) -> bytes:
    rng = random.Random(seed)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["x1", "x2", "n1", "outcome"])
    for _ in range(n):
        lab = rng.randrange(3)
        rad, ang = 1.0 + 1.2 * lab + rng.gauss(0, 0.3), rng.uniform(0, 6.28)
        w.writerow([f"{rad * math.cos(ang):.6f}", f"{rad * math.sin(ang):.6f}", f"{rng.gauss(0, 1):.6f}", "abc"[lab]])
    return buf.getvalue().encode()


def test_the_split_is_deterministic_keyed_and_disjoint():
    raw = rings_csv(300)
    a = seal_files(raw, "pr1", "outcome", 2, 40, "r:" + BEACON)
    assert a == seal_files(raw, "pr1", "outcome", 2, 40, "r:" + BEACON)
    assert a != seal_files(raw, "pr1", "outcome", 2, 40, "r:other-beacon-value")
    lab = a.lab.decode()
    assert lab.splitlines()[0] == "x1,x2,n1,label" and "outcome" not in lab
    assert a.n_lab == 220 and [p.n for p in a.parts] == [40, 40]
    r1 = set(a.parts[0].test_x.decode().splitlines()[1:])
    r2 = set(a.parts[1].test_x.decode().splitlines()[1:])
    labrows = {",".join(ln.split(",")[:3]) for ln in lab.splitlines()[1:]}
    assert not r1 & r2 and not r1 & labrows and not r2 & labrows


def test_a_row_repeated_in_the_lab_part_never_stays_sealed():
    rows = "x,y\n" + "".join(f"{i % 3},{i % 2}\n" for i in range(60))
    with pytest.raises(SealSpecError):  # three repeated feature values: nothing fresh remains
        seal_files(rows.encode(), "pr1", "y", 1, 5, "r:" + BEACON)


@pytest.mark.parametrize("raw,col,parts,rows", [
    (b"a,b\n1,2\n", "z", 1, 2), (b"a,b\n" + b"1,2\n" * 3, "b", 1, 5), (b"a,b\n1,2\n" * 9, "b", 3, 2),
    (b"a,b\n1,2\n", "b", 1, 1), (b"\xff\xfe", "b", 1, 2), (b"label,b\n" + b"1,2\n" * 9, "b", 1, 2),
    (b"a,b\n1\n" + b"1,2\n" * 9, "b", 1, 2), (b"", "b", 1, 2)])
def test_bad_seal_requests_are_refused(raw, col, parts, rows):
    with pytest.raises(SealSpecError):
        seal_files(raw, "pr1", col, parts, rows, "r:" + BEACON)


def health_stand_in(store: EvidenceStore) -> None:
    """A passing health record for this Court version: the fixture's stand-in, so each test need not run the plants
    (tests/test_court_rehearsal_health.py runs them for real)."""
    store.append_record(H.RECORD_KIND, {"court_version": H.court_version(), "passed": True, "health_hash": "h" * 64,
                                        "plants": [p.name for p in H.PLANTS]})


class Project:
    def __init__(self, root: Path, *, parts: int = 2, project: str = "cohort-1", rows: int = 400,
                 health: bool = True):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.store = EvidenceStore(root / "store")
        Kernel(self.store).approve_adapter("ml", approver="fixture-human")
        if health:
            health_stand_in(self.store)
        self.data = root / "private-data.csv"
        self.data.write_bytes(rings_csv())
        self.project = project
        self.seal = S.seal_project(self.store, project, self.data, label_column="outcome", rows=rows, parts=parts,
                                   beacon_round="r1", beacon_value=BEACON, provenance="private")
        self.n = 0

    def session(self, sid: str = SESSION) -> None:
        S.bind_session(self.store, self.project, sid)

    def freeze(self, sid: str = SESSION, contract: str = "build_model", method: bytes | None = None,
               baseline: bytes | None = None, metric: str = "accuracy") -> str:
        """The stored form of `lab.promote.freeze` (the box and workspace parts are covered by test_lab_promote)."""
        put = self.store.blobs.put
        self.n += 1
        pid = f"p{self.n}"
        req = {"format": "chh-lab-promote/1", "title": "fixture", "sentence": "the fixture method beats the baseline",
               "method": "m.py", "baseline": "b.py", "lock": "r.lock", "contract": contract,
               "claim": ClaimPart(metric=metric, min_effect=0.1, probability=0.6, seeds=(0, 1, 2), n_boot=1000,
                                  resolve_by=date(2030, 1, 1)).model_dump(mode="json"),
               "budget": Budget(timeout_s=120).model_dump(mode="json"),
               "time_window": {"start": "2024-01-01", "end": "2025-12-31"}}
        self.store.append_record(LP.RECORD_KIND, {
            "promotion_id": pid, "session_id": sid, "event": "frozen", "request": req,
            "frozen": {"method": {"blob": put(method or (ARMS / "knn_radius.py").read_bytes())},
                       "baseline": {"blob": put(baseline or (ARMS / "majority.py").read_bytes())},
                       "lock": {"blob": put(b"x==1\n")}}})
        return pid

    def approve(self, body: dict) -> None:
        Kernel(self.store).approve_role(body["target"], approver="fixture-human", note="fixture")

    def factory(self, key: str) -> LocalSandbox:
        self.n += 1
        return LocalSandbox(self.root / "sb" / f"{self.n}-{key}", i_understand_no_isolation=True)

    def run(self, body: dict) -> dict:
        self.approve(body)
        return C.run(self.store, body["case_id"], self.factory)

    def anchor(self) -> Path:
        return self.root / "anchors" / "a.jsonl"

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def pj(tmp_path):
    p = Project(tmp_path / "p")
    yield p
    p.close()


def test_the_seal_is_in_the_ledger_and_no_sealed_label_is_public(pj):
    k = ScopedKernel(pj.store)
    ds = k.vault.datasets()
    t1, t2 = pj.seal["targets"]
    assert ds[pj.seal["train_id"]].role.value == "dev"
    for t in (t1, t2):
        assert ds[t].role.value == "external" and ds[t].files["test_y.csv"].private
    regs = {e.body["dataset_id"]: e.seq for e in k.ledger.entries("register_dataset")}
    assert pj.seal["seal_seq"] == max(regs[t1], regs[t2])
    lab = S.lab_part(pj.store, pj.project)
    for t in (t1, t2):
        assert not (set(k.vault.read_public(t, "test_X.csv").decode().splitlines()[1:])
                    & {",".join(r.split(",")[:3]) for r in lab.decode().splitlines()[1:]})


def test_a_project_is_sealed_once(pj):
    with pytest.raises(S.SealRefused, match="already exists"):
        S.seal_project(pj.store, pj.project, pj.data, label_column="outcome", rows=100, parts=1, beacon_round="r2",
                       beacon_value=BEACON, provenance="private")


def test_a_session_needs_a_sealed_project(pj):
    with pytest.raises(S.SealRefused, match="not sealed"):
        S.bind_session(pj.store, "other-project", "s-x")
    with pytest.raises(S.SealRefused):
        S.lab_part(pj.store, "other-project")


def test_a_file_in_the_lab_root_cannot_be_sealed(tmp_path, monkeypatch):
    monkeypatch.setenv("CHH_LAB_ROOT", str(tmp_path / "lab"))
    (tmp_path / "lab").mkdir()
    f = tmp_path / "lab" / "d.csv"
    f.write_bytes(rings_csv(50))
    store = EvidenceStore(tmp_path / "store")
    try:
        with pytest.raises(S.SealRefused, match="lab root"):
            S.seal_project(store, "pr-a", f, label_column="outcome", rows=5, parts=1, beacon_round="r",
                           beacon_value=BEACON, provenance="private")
        with pytest.raises(S.SealRefused, match="project id"):
            S.seal_project(store, "BAD ID", tmp_path / "x", label_column="o", rows=5, parts=1, beacon_round="r",
                           beacon_value=BEACON, provenance="private")
    finally:
        store.close()


def test_kerneld_refusal_is_a_refusal(tmp_path):
    store = EvidenceStore(tmp_path / "store")
    try:
        with pytest.raises(S.SealRefused, match="not_found"):
            S.seal_project(store, "pr-b", tmp_path / "missing.csv", label_column="o", rows=5, parts=1,
                           beacon_round="r", beacon_value=BEACON, provenance="private")
        (tmp_path / "bad.csv").write_bytes(b"a,b\n1,2\n")
        with pytest.raises(S.SealRefused, match="bad_request"):
            S.seal_project(store, "pr-b", tmp_path / "bad.csv", label_column="z", rows=5, parts=1,
                           beacon_round="r", beacon_value=BEACON, provenance="private")
    finally:
        store.close()


def test_sealed_case_validates_as_a_ledger_fact_and_second_part_revalidates(pj):
    pj.session()
    body = C.register_sealed(pj.store, pj.freeze(), pj.project, anchor_file=pj.anchor(), revalidate=True)
    assert body["route"] == "sealed" and body["target"] == pj.seal["targets"][0]
    out = pj.run(body)
    assert out["cert_hash"] and out["label"] == "CERTIFIED-VALIDATED", out
    res = L.level(pj.store, out["cert_hash"])
    assert res.held["VALIDATED"], res.missing
    fact = res.facts["targets"][body["target"]]
    assert fact["freshness"] == "sealed-at-start" and fact["seal"]["at_start"]
    # the order is a ledger fact, so the only thing left untested is the researcher's statement of provenance
    assert [a.split(": ", 1)[1] for a in res.facts["untested_assumptions"]] == [
        "the data is private, as the researcher stated at the seal (not checked by code)"]
    rv = pj.run(body["revalidation"])
    assert rv["cert_hash"], rv
    res2 = L.level(pj.store, out["cert_hash"])
    assert res2.held["RE-VALIDATED"], res2.missing
    assert res2.revalidation == "passed"


def test_a_session_before_the_seal_means_never_fresh(tmp_path):
    p = Project(tmp_path / "p", parts=1)
    try:
        p.session()
        # a second seal of the same project is refused, so the ledger order is forced here: a session whose
        # artifact sits before the target's registration (a seal after Lab access)
        ledger = ScopedKernel(p.store).ledger
        seq = max(e.seq for e in ledger.entries("register_dataset"))
        fact = S.seal_fact(p.store, ScopedKernel(p.store), p.seal["targets"][0], SESSION)
        assert fact["at_start"] and fact["sessions"][SESSION] > seq
        k = ScopedKernel(p.store)
        arts = k.vault.artifacts()
        forged = {a: v.model_copy(update={"seq": 0}) if v.kind == S.ARTIFACT_KIND else v for a, v in arts.items()}

        class V:
            ledger = k.ledger
            vault = type("X", (), {"artifacts": staticmethod(lambda: forged)})
        late = S.seal_fact(p.store, V, p.seal["targets"][0], SESSION)  # type: ignore[arg-type]
        assert not late["at_start"] and "before the seal" in late["why"]
        assert L.classify_target("sealed_split", False, False, None, False) == "seal-after-lab"
        assert L.classify_target("sealed_split", False, False, None, True) == "sealed-at-start"
    finally:
        p.close()


def test_a_session_outside_the_project_cannot_use_its_seal(pj):
    pj.session()
    with pytest.raises(C.CourtRefused, match="not a session of project"):
        C.register_sealed(pj.store, pj.freeze(sid="s-elsewhere"), pj.project, anchor_file=pj.anchor())
    k = ScopedKernel(pj.store)
    assert "not a session" in S.seal_fact(pj.store, k, pj.seal["targets"][0], "s-elsewhere")["why"]


def test_sessions_of_another_project_do_not_count(pj):
    S.seal_project(pj.store, "other-project", pj.data, label_column="outcome", rows=100, parts=1, beacon_round="r2",
                   beacon_value=BEACON, provenance="private")
    S.bind_session(pj.store, "other-project", "s-other")
    fact = S.seal_fact(pj.store, ScopedKernel(pj.store), pj.seal["targets"][0], None)
    assert "no Lab session" in fact["why"]


def test_seal_fact_without_a_seal_or_sessions(pj):
    k = ScopedKernel(pj.store)
    assert "no seal" in S.seal_fact(pj.store, k, "nothing", None)["why"]
    assert "no Lab session" in S.seal_fact(pj.store, k, pj.seal["targets"][0], None)["why"]


def test_sealed_cases_are_not_minted_and_parts_are_bounded(pj):
    pj.session()
    body = C.register_sealed(pj.store, pj.freeze(), pj.project, anchor_file=pj.anchor())
    with pytest.raises(C.CourtRefused, match="nothing is minted"):
        C.mint(pj.store, body["case_id"], BEACON)
    with pytest.raises(C.CourtRefused, match="part 3"):
        C.sealed_route(pj.store, pj.project, 3)
    with pytest.raises(C.CourtRefused, match="not sealed"):
        C.sealed_route(pj.store, "none-such", 1)


# ---------------------------------------------------------------------------------------- the program contract
PROGRAM = b"""import csv, os, helper
rows = list(csv.DictReader(open(os.path.join(os.environ["CHH_INPUTS"], "train.csv"))))
test = list(csv.DictReader(open(os.path.join(os.environ["CHH_INPUTS"], "test.csv"))))
pred = [helper.classify(float(r["x1"]), float(r["x2"]), rows) for r in test]
with open(os.path.join(os.environ["CHH_OUTPUTS"], "predictions.csv"), "w") as f:
    f.write("prediction\\n" + "\\n".join(pred) + "\\n")
"""
HELPER = b"""def classify(x, y, rows):
    r = (x * x + y * y) ** 0.5
    near = sorted(rows, key=lambda t: abs((float(t["x1"]) ** 2 + float(t["x2"]) ** 2) ** 0.5 - r))[:15]
    labels = [t["label"] for t in near]
    return max(sorted(set(labels)), key=labels.count)
"""
CONSTANT = b"""import os, csv
n = len(list(csv.reader(open(os.path.join(os.environ["CHH_INPUTS"], "test.csv"))))) - 1
with open(os.path.join(os.environ["CHH_OUTPUTS"], "predictions.csv"), "w") as f:
    f.write("prediction\\n" + "a\\n" * n)
"""


def test_a_program_contract_case_certifies_and_the_court_computes_the_metric(pj):
    pj.session()
    m = PG.bundle("main.py", {"main.py": PROGRAM, "helper.py": HELPER})
    b = PG.bundle("main.py", {"main.py": CONSTANT})
    body = C.register_sealed(pj.store, pj.freeze(contract="program", method=m, baseline=b), pj.project,
                             anchor_file=pj.anchor())
    assert body["contract"] == "program"
    out = pj.run(body)
    assert out["cert_hash"] and out["label"] == "CERTIFIED-VALIDATED", out
    cert = ScopedKernel(pj.store).certificate(out["cert_hash"])
    assert cert.claim["scope"]["conditions"]["wrapper_sha256"] == PG.shim_hash()
    assert cert.checks["K3"].status == "pass"


def test_a_program_that_claims_a_score_gets_no_help_from_it(pj):
    pj.session()
    liar = CONSTANT.replace(b'"a\\n" * n', b'"a\\n" * n)\nprint("accuracy=1.0")\n(0')
    m = PG.bundle("main.py", {"main.py": liar})
    b = PG.bundle("main.py", {"main.py": CONSTANT.replace(b'"a\\n"', b'"b\\n"')})
    body = C.register_sealed(pj.store, pj.freeze(contract="program", method=m, baseline=b), pj.project,
                             anchor_file=pj.anchor())
    out = pj.run(body)
    assert out["cert_hash"] and not str(out["label"]).startswith("CERTIFIED"), out


def test_a_program_with_no_predictions_could_not_run_and_keeps_the_target(pj):
    pj.session()
    m = PG.bundle("main.py", {"main.py": b"print('forgot')\n"})
    b = PG.bundle("main.py", {"main.py": CONSTANT})
    body = C.register_sealed(pj.store, pj.freeze(contract="program", method=m, baseline=b), pj.project,
                             anchor_file=pj.anchor())
    out = pj.run(body)
    assert out["verdict"] == "COULD NOT RUN" and out["labels_read"] is False


def test_bundle_rules():
    with pytest.raises(PG.ProgramError, match="entry"):
        PG.bundle("x.py", {"y.py": b""})
    with pytest.raises(PG.ProgramError, match="UTF-8"):
        PG.bundle("x.py", {"x.py": b"\xff"})
    with pytest.raises(PG.ProgramError, match="not a frozen"):
        PG.compose(json.dumps({"format": "other"}).encode())
    composed = PG.compose(PG.bundle("x.py", {"x.py": b"SECRET_LITERAL = 1\n"}))
    assert b"SECRET_LITERAL" in composed  # the source stays readable for the Kernel's static checks
    assert composed.endswith(PG.SHIM.encode())
