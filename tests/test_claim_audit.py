"""Claim Audit (GOAL §16.10): human-typed claim, fidelity approval, pre-registration before fresh data, both arms
under one contract, a verdict computed only from the certificate, and the publication gate.

Fixture world: a synthetic 3-class "rings" dataset written as an OpenML-style ARFF file into a local cache (no
download anywhere). The method arm (k-NN on the radius) beats the baseline (majority class) by about 0.45 accuracy
on fresh data. Planted inflated claims: (1) the paper's margin is overstated (claims >= 0.7); (2) the method
memorised the benchmark's test labels (its code carries them), so it "wins" by a mile and must still not certify.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from custom_harness.audit import claim_audit as CA
from custom_harness.audit import data as D
from custom_harness.audit import gate as G
from custom_harness.audit.spec import SpecError, load_spec
from custom_harness.kernel import Kernel, SandboxReplayer
from custom_harness.kernel.bundle import build_bundle, reproduce_bundle
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore

ARMS = Path(__file__).parent / "fixtures" / "audit" / "arms"
OPENML_ID = 990001  # fictional id: the fixture is local, nothing is fetched


def rings_arff(seed: int = 3, n: int = 240, with_id: bool = False) -> bytes:
    rng = random.Random(seed)
    lines = ["% synthetic rings fixture (chh audit tests)", "@relation rings", "@attribute x1 numeric",
             "@attribute x2 numeric", "@attribute n1 numeric", *(["@attribute sample_id numeric"] if with_id else []),
             "@attribute class {a,b,c}", "@data"]
    for i in range(n):
        lab = rng.randrange(3)
        rad = 1.0 + 1.2 * lab + rng.gauss(0, 0.55)
        ang = rng.uniform(0, 2 * math.pi)
        sid = f"{i}," if with_id else ""
        lines.append(f"{rad * math.cos(ang):.5f},{rad * math.sin(ang):.5f},{rng.gauss(0, 1):.5f},{sid}{'abc'[lab]}")
    return ("\n".join(lines) + "\n").encode()


class Audit:
    """One audit set up on disk: spec dir, local OpenML cache, store with the ML adapter approved."""

    def __init__(self, root: Path, *, method: str | bytes = "knn_radius.py", min_effect: float = 0.2,
                 arff: bytes | None = None, extra: dict | None = None, n: int = 240, cache: bool = True):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.arff = arff if arff is not None else rings_arff(n=n)
        self.pin = hashlib.sha256(self.arff).hexdigest()
        self.cache = D.OpenMLCache(root / "cache")
        if cache:
            self.cache.root.mkdir(parents=True, exist_ok=True)
            (self.cache.root / f"openml-{OPENML_ID}-{self.pin}.arff").write_bytes(self.arff)
        mcode = method if isinstance(method, bytes) else (ARMS / method).read_bytes()
        (root / "method.py").write_bytes(mcode)
        (root / "baseline.py").write_bytes((ARMS / "majority.py").read_bytes())
        doc = {"format": "chh-audit/1",
               "paper": {"doi": "10.5555/audit.2024.001", "title": "Radius neighbours beat majority voting",
                         "sentence": "Our radius k-NN improves accuracy over the majority baseline by at least "
                                     f"{min_effect} points.", "datasets": ["openml:61"], "reported_effect": 0.75},
               "claim": {"metric": "accuracy", "min_effect": min_effect, "probability": 0.6, "seeds": [0, 1, 2],
                         "n_boot": 1000, "resolve_by": "2027-06-30"},
               "method": {"path": "method.py"}, "baseline": {"path": "baseline.py"},
               "budget": {"timeout_s": 120},
               "datasets": [{"openml_id": OPENML_ID, "sha256": self.pin, "target": "class", "licence": "CC0"}]}
        doc.update(extra or {})
        (root / "audit.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        self.loaded = load_spec(root / "audit.yaml")
        self.store = EvidenceStore(root / "store")
        Kernel(self.store).approve_adapter("ml", approver="pytest-human")
        self.claim_hash = self.loaded.claim().claim_hash()

    def factory(self, key: str) -> LocalSandbox:
        return LocalSandbox(self.root / "sb" / key, i_understand_no_isolation=True)

    def approve_fidelity(self) -> int:
        return G.approve_fidelity(self.store, audit_id=self.loaded.audit_id, spec_hash=self.loaded.spec_hash,
                                  claim_hash=self.claim_hash, sentence=self.loaded.spec.paper.sentence,
                                  approver="pytest-human", reviewed_hash=self.loaded.spec_hash)

    def prepare(self, fetcher=None) -> dict:
        return CA.prepare(self.store, self.loaded, self.cache, fetcher)

    def approve_roles(self) -> None:
        k = Kernel(self.store)
        for ds in self.loaded.spec.datasets:
            k.approve_role(ds.dataset_id, approver="pytest-human", note="fixture: fresh synthetic data")

    def full(self, resolver=None) -> dict:
        self.approve_fidelity()
        self.prepare()
        self.approve_roles()
        return CA.run(self.store, self.loaded, self.factory, resolver=resolver)

    def cert(self, outcome: dict):
        return Kernel(self.store).certificate(outcome["cert_hash"])

    def test_labels(self) -> list[str]:
        sp = D.split(D.to_table(self.arff, "class"), self.pin)
        return [ln for ln in sp.test_y.decode().splitlines()[1:]]

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def honest(tmp_path):
    a = Audit(tmp_path / "honest")
    out = a.full()
    yield a, out
    a.close()


# ------------------------------------------------------------------ acceptance: honest claim certifies
def test_honest_claim_certifies_on_fixture_data(honest):
    a, out = honest
    assert out["verdict"] == "CERTIFIED", (out, CA.current_report(a.store, out))
    cert = a.cert(out)
    assert cert.label.value == "CERTIFIED-VALIDATED"
    assert cert.claim["scope"]["conditions"]["paper_doi"] == "10.5555/audit.2024.001"


# ------------------------------------------------------------------ acceptance: planted inflated claims
def test_planted_overstated_margin_is_not_confirmed_by_K6(tmp_path):
    a = Audit(tmp_path / "inflated", min_effect=0.7)  # true margin ~0.45; the "paper" claims >= 0.7
    out = a.full()
    cert = a.cert(out)
    assert out["verdict"] == "NOT CONFIRMED" and out["checks"] == ["K6"], out
    (st,) = cert.resolution["targets"].values()
    assert st["met"] is False and st["hi"] < 0.7  # the data rule the claimed margin out
    assert st["estimate"] > 0.2  # the attack: a real but smaller effect, inflated in the claim
    assert "upper bound below the claimed effect" in CA.current_report(a.store, out)
    a.close()


def _memorised(labels: list[str], literal: bool) -> bytes:
    answers = json.dumps(labels) if literal else json.dumps(",".join(labels)) + '.split(",")'
    return ("_ANSWERS = " + answers + "\n\n"
            "class Memorised:\n    def fit(self, X, y):\n        return self\n\n"
            "    def predict(self, X):\n        return list(_ANSWERS[: len(X)])\n\n\n"
            "def build_model(seed):\n    return Memorised()\n").encode()


def _probe_labels(tmp_path: Path) -> list[str]:
    probe = Audit(tmp_path / "probe")
    labels = probe.test_labels()
    probe.close()
    return labels


def test_planted_label_memorising_method_is_not_confirmed_by_K9(tmp_path):
    a = Audit(tmp_path / "memorised", method=_memorised(_probe_labels(tmp_path), literal=False))
    out = a.full()
    cert = a.cert(out)
    (st,) = cert.resolution["targets"].values()
    assert st["met"] is True and st["estimate"] > 0.6  # the attack worked statistically (perfect accuracy)
    assert out["verdict"] == "NOT CONFIRMED" and "K9" in out["checks"], out
    assert cert.checks["K9"].status == "fail"
    a.close()


def test_planted_label_memorising_method_quoted_literal_k9_gap(tmp_path):
    """Was a strict xfail (auditor-to-kstats-01) until 2026-10-08: K9's label-run scan now also tokenises across
    quote and bracket characters (verify/leak._LITERAL_SPLIT)."""
    a = Audit(tmp_path / "memorised_literal", method=_memorised(_probe_labels(tmp_path), literal=True))
    out = a.full()
    (st,) = a.cert(out).resolution["targets"].values()
    assert st["met"] is True  # the attack worked statistically
    assert out["verdict"] == "NOT CONFIRMED" and "K9" in out["checks"], out
    a.close()


def test_planted_id_column_leak_is_not_confirmed_by_K6(tmp_path):
    """The paper's dataset had a row id that tracked the label; the method keys on it. On fresh data the id
    carries nothing, the edge vanishes, and the claimed margin is ruled out."""
    a = Audit(tmp_path / "idleak", method="knn_id_column.py", arff=rings_arff(with_id=True))
    out = a.full()
    cert = a.cert(out)
    (st,) = cert.resolution["targets"].values()
    assert out["verdict"] == "NOT CONFIRMED" and out["checks"] == ["K6"], out
    assert st["met"] is False and st["hi"] < 0.2 and abs(st["estimate"]) < 0.15
    a.close()


def test_straddling_interval_is_inconclusive_not_a_refutation(tmp_path):
    a = Audit(tmp_path / "straddle", min_effect=0.45)  # the true margin is about the claimed one
    out = a.full()
    cert = a.cert(out)
    (st,) = cert.resolution["targets"].values()
    assert st["lo"] < 0.45 <= st["hi"], st
    assert out["verdict"] == "INCONCLUSIVE" and out["checks"] == ["K6"], out
    a.close()


def test_verdict_names_unrecognised_K6_failure_as_not_confirmed(honest):
    a, out = honest
    cert = a.cert(out)
    from custom_harness.kernel import CheckResult, Label
    bad = cert.model_copy(update={"label": Label.REJECTED, "checks": {
        **cert.checks, "K6": CheckResult(status="fail", reasons=("x: used for development (1 logged read)",))}})
    assert CA.verdict(bad) == (CA.Verdict.NOT_CONFIRMED, ["K6"])


def test_bundles_reproduce_for_certified_and_not_confirmed(tmp_path, honest):
    a, out = honest
    b = Audit(tmp_path / "inflated2", min_effect=0.7)
    out_b = b.full()
    for audit, o, name in ((a, out, "honest"), (b, out_b, "inflated")):
        path = tmp_path / f"{name}.zip"
        build_bundle(Kernel(audit.store), o["cert_hash"], path)
        rep = reproduce_bundle(path, lambda name=name: SandboxReplayer(
            lambda key, name=name: LocalSandbox(tmp_path / "rp" / name / key, i_understand_no_isolation=True)))
        assert rep.reproduced, rep.problems
        assert rep.original_label == audit.cert(o).label.value
    b.close()


# ------------------------------------------------------------------ order: approval, prereg, then data
def test_run_and_prepare_refuse_without_fidelity_approval(tmp_path):
    a = Audit(tmp_path / "noapprove")
    with pytest.raises(G.AuditRefused, match="fidelity"):
        a.prepare()
    with pytest.raises(G.AuditRefused, match="fidelity"):
        CA.run(a.store, a.loaded, a.factory)
    assert not Kernel(a.store).ledger.entries("prereg")
    assert not Kernel(a.store).vault.datasets()
    a.close()


def test_fidelity_approval_is_bound_to_the_spec_and_arm_code(tmp_path):
    a = Audit(tmp_path / "edit")
    a.approve_fidelity()
    with pytest.raises(G.AuditRefused, match="not the current spec"):
        G.approve_fidelity(a.store, audit_id=a.loaded.audit_id, spec_hash=a.loaded.spec_hash,
                           claim_hash=a.claim_hash, sentence="x", approver="h", reviewed_hash="0" * 64)
    with pytest.raises(G.AuditRefused, match="human"):
        G.approve_fidelity(a.store, audit_id=a.loaded.audit_id, spec_hash=a.loaded.spec_hash,
                           claim_hash=a.claim_hash, sentence="x", approver="  ", reviewed_hash=a.loaded.spec_hash)
    (a.root / "method.py").write_bytes((ARMS / "knn_radius.py").read_bytes() + b"\n# tuned after approval\n")
    edited = load_spec(a.root / "audit.yaml")
    assert edited.spec_hash != a.loaded.spec_hash
    with pytest.raises(G.AuditRefused, match="fidelity"):
        CA.prepare(a.store, edited, a.cache)
    a.close()


def test_claim_is_preregistered_before_fresh_data_are_touched(honest):
    a, _ = honest
    led = Kernel(a.store).ledger
    prereg = next(e.seq for e in led.entries("prereg") if e.body["claim_hash"] == a.claim_hash)
    fid = next(e.seq for e in led.entries("approval") if e.body.get("node_id") == "audit_fidelity")
    regs = [e.seq for e in led.entries("register_dataset")]
    reads = [e.seq for e in led.entries("data_access") if e.body.get("private")]
    assert regs and reads
    assert fid < prereg < min(regs) <= max(regs) < min(reads)


def test_ingest_refuses_an_unregistered_claim(tmp_path):
    a = Audit(tmp_path / "ingest")
    ds = a.loaded.spec.datasets[0]
    with pytest.raises(D.DataUnavailable, match="not pre-registered"):
        D.ingest(Kernel(a.store), a.claim_hash, ds, a.arff)
    assert not Kernel(a.store).vault.datasets()
    a.close()


def test_run_refuses_until_roles_are_human_approved(tmp_path):
    a = Audit(tmp_path / "roles")
    a.approve_fidelity()
    a.prepare()
    with pytest.raises(G.AuditRefused, match="external role"):
        CA.run(a.store, a.loaded, a.factory)
    assert not [e for e in Kernel(a.store).vault.accesses() if e.body.get("private")]
    a.close()


def test_one_look_per_claim(honest):
    a, _ = honest
    with pytest.raises(G.AuditRefused, match="already evaluated"):
        CA.run(a.store, a.loaded, a.factory)


# ------------------------------------------------------------------ identical contract
def test_both_arms_ran_one_contract_through_the_same_wrapper(honest):
    a, out = honest
    from custom_harness.audit import wrapper
    from custom_harness.kernel import ExecRecord
    cert = a.cert(out)
    assert cert.checks["K5"].status == "pass"
    recs = [ExecRecord.model_validate(a.store.blobs.get_json(a.store.get_evidence(e).payload_ref))
            for e in cert.evidence_ids]
    assert {r.arm for r in recs} == {"subject", "comparator"}
    for r in recs:
        assert r.env["audit_wrapper"] == wrapper.wrapper_hash() and r.limits == {"timeout_s": 120.0}
        assert wrapper.arm_part(a.store.blobs.get(r.artifact_hash)) is not None
    assert len({(r.argv, tuple(sorted(r.inputs)), r.limits["timeout_s"]) for r in recs}) == 1


def test_spec_cannot_give_one_arm_its_own_budget_or_reuse_paper_data(tmp_path):
    with pytest.raises(SpecError):
        Audit(tmp_path / "b1", extra={"method": {"path": "method.py", "timeout_s": 999}})
    with pytest.raises(SpecError, match="not fresh"):
        Audit(tmp_path / "b2", extra={"paper": {"doi": "d", "title": "t", "sentence": "s",
                                                "datasets": [f"openml:{OPENML_ID}"]}})


# ------------------------------------------------------------------ COULD NOT RUN
def test_missing_cache_is_could_not_run_and_needs_owner(tmp_path):
    a = Audit(tmp_path / "nocache", cache=False)
    a.approve_fidelity()
    out = a.prepare()
    cnr = out["could_not_run"]
    assert cnr["verdict"] == "COULD NOT RUN" and cnr["needs_owner"] is True and "owner decision" in cnr["reason"]
    assert not Kernel(a.store).vault.datasets()
    a.close()


def test_tampered_cache_and_wrong_download_are_refused(tmp_path):
    a = Audit(tmp_path / "tamper")
    ds = a.loaded.spec.datasets[0]
    a.cache.path(ds).write_bytes(a.arff + b"1,1,1,a\n")
    with pytest.raises(D.DataUnavailable, match="pinned"):
        a.cache.load(ds)
    a.cache.path(ds).unlink()
    with pytest.raises(D.DataUnavailable, match="not the pinned"):
        a.cache.ensure(ds, lambda i: (b"other bytes", {}))
    assert not a.cache.path(ds).exists()
    assert a.cache.ensure(ds, lambda i: (a.arff, {"licence": "CC0"})) == a.arff  # owner-approved fetch, pinned
    a.close()


def test_crashing_arm_is_could_not_run_and_reads_no_label(tmp_path):
    a = Audit(tmp_path / "crash", method="crash.py")
    out = a.full()
    assert out["verdict"] == "COULD NOT RUN" and out["cert_hash"] is None
    assert not [e for e in Kernel(a.store).vault.accesses() if e.body.get("private")]
    assert not Kernel(a.store).ledger.entries("certificate")
    a.close()


# ------------------------------------------------------------------ report only from the certificate
def test_report_is_a_pure_function_of_the_certificate(honest):
    a, out = honest
    cert = a.cert(out)
    r1 = CA.render_report(cert)
    a.store.append_record("audit", {**out, "verdict": "NOT CONFIRMED"})  # mutable run state cannot change it
    assert CA.render_report(cert) == r1 == CA.current_report(a.store, out)
    assert hashlib.sha256(r1.encode()).hexdigest() == out["report_hash"]
    assert cert.cert_hash() in r1 and "CERTIFIED" in r1.splitlines()[0]


# ------------------------------------------------------------------ citations
def test_paper_citations_are_checked_by_the_literature_module(tmp_path):
    from custom_harness.literature.client import LiteratureClient
    from lit_world import world
    good = {"key": "okafor2021", "id": "10.5555/lit.2021.001",
            "title": "Sparse gradient routing for tabular ensembles", "year": 2021,
            "quotes": ["the method improves accuracy by 1.8 points over gradient boosting"]}
    bad = {**good, "key": "okafor2021-misquoted", "quotes": ["the method improves accuracy by 9 points"]}
    a = Audit(tmp_path / "cite", extra={"citations": [good, bad]})
    out = a.full(resolver=LiteratureClient(world(), sleep=lambda s: None, max_attempts=2))
    c = out["citations"]
    assert c["I3"] is False and c["counts"] == {"verified": 1, "failed": 1, "unverified": 0}, c
    a.close()


# ------------------------------------------------------------------ publication gate
def test_publication_needs_reply_then_human_approval_of_this_exact_report(tmp_path, honest):
    a, out = honest
    report = CA.current_report(a.store, out)
    rh = out["report_hash"]
    dest = tmp_path / "export"
    with pytest.raises(G.AuditRefused, match="no right-of-reply record for this report"):
        G.publish(a.store, out, report, dest)
    with pytest.raises(G.AuditRefused, match="right-of-reply"):
        G.approve_publication(a.store, audit_id=out["audit_id"], report_hash=rh, approver="h", reviewed_hash=rh)
    with pytest.raises(G.AuditRefused, match="days"):
        G.record_reply(a.store, audit_id=out["audit_id"], report_hash=rh, recorded_by="h",
                       notified="corresponding author, email", notified_on=date.today(), reply_text=None)
    G.record_reply(a.store, audit_id=out["audit_id"], report_hash=rh, recorded_by="h",
                   notified="corresponding author, email", notified_on=date.today() - timedelta(days=1),
                   reply_text="We used a different preprocessing.")
    with pytest.raises(G.AuditRefused, match="publication approval"):
        G.publish(a.store, out, report, dest)
    with pytest.raises(G.AuditRefused, match="not the current report"):
        G.approve_publication(a.store, audit_id=out["audit_id"], report_hash=rh, approver="h", reviewed_hash="0" * 64)
    assert not dest.exists()
    G.approve_publication(a.store, audit_id=out["audit_id"], report_hash=rh, approver="pytest-human",
                          reviewed_hash=rh)
    with pytest.raises(G.AuditRefused, match="no longer renders"):
        G.publish(a.store, out, report + "edited", dest)
    bpath = CA.bundle(a.store, out, tmp_path / "b.zip")
    man = G.publish(a.store, out, report, dest, bundle_path=bpath, redistribution={"code": True, "data": False})
    assert man["publication_approval"]["approver"] == "pytest-human" and man["right_of_reply"]["reply"]
    assert man["bundle"] is None and (dest / "report.md").read_text(encoding="utf-8") == report
    assert not (dest / "bundle.zip").exists()  # data not declared redistributable: hash-only


def test_retracted_certificate_is_not_published(honest, tmp_path):
    a, out = honest
    rh = out["report_hash"]
    G.record_reply(a.store, audit_id=out["audit_id"], report_hash=rh, recorded_by="h", notified="author, email",
                   notified_on=date.today() - timedelta(days=30), reply_text=None)
    G.approve_publication(a.store, audit_id=out["audit_id"], report_hash=rh, approver="h", reviewed_hash=rh)
    Kernel(a.store).retract(out["cert_hash"], "planted error found")
    with pytest.raises(G.AuditRefused, match="retracted"):
        G.publish(a.store, out, CA.current_report(a.store, out), tmp_path / "x")


# ------------------------------------------------------------------ CLI end to end
def test_cli_flow_human_steps_and_gated_publish(tmp_path, capsys):
    from custom_harness.cli import main
    from custom_harness.config import data_dir
    a = Audit(tmp_path / "cli")
    a.close()
    s = EvidenceStore(data_dir() / "store")
    Kernel(s).approve_adapter("ml", approver="pytest-human")
    s.close()
    spec = str(a.root / "audit.yaml")

    def run(*args):
        capsys.readouterr()
        rc = main(list(args))
        out = capsys.readouterr().out
        return rc, out

    rc, out = run("audit", "show", spec)
    shown = json.loads(out)
    assert rc == 0 and shown["spec_hash"] == a.loaded.spec_hash
    assert run("audit", "approve-fidelity", spec, "--approver", "h", "--spec-hash", "0" * 64)[0] == 1
    assert run("audit", "run", spec, "--dev-sandbox")[0] == 1  # refused: no fidelity approval
    assert run("audit", "approve-fidelity", spec, "--approver", "h", "--spec-hash", shown["spec_hash"])[0] == 0
    rc, out = run("audit", "prepare", spec, "--cache", str(a.cache.root))
    (ds,) = json.loads(out)["datasets"]
    assert run("approve-role", ds["dataset_id"], "--approver", "h")[0] == 0
    rc, out = run("audit", "run", spec, "--dev-sandbox")
    res = json.loads(out)
    assert rc == 0 and res["verdict"] == "CERTIFIED"
    rc, report = run("audit", "report", spec)
    assert report.startswith("# Claim Audit") and res["cert_hash"] in report
    dest = tmp_path / "pub"
    assert run("audit", "publish", spec, "--out", str(dest))[0] == 1 and not dest.exists()
    assert run("audit", "reply", spec, "--recorded-by", "h", "--notified", "author, email", "--notified-on",
               (date.today() - timedelta(days=20)).isoformat(), "--no-reply")[0] == 0
    assert run("audit", "approve-publication", spec, "--approver", "h", "--report-hash", res["report_hash"])[0] == 0
    rc, out = run("audit", "publish", spec, "--out", str(dest))
    assert rc == 0 and json.loads(out)["bundle"] is None and (dest / "report.md").exists()


def test_cli_labeldep_reports_a_flag_as_json(tmp_path, capsys):
    from custom_harness.cli import main
    from test_audit_labeldep import DATA, FIXTURES
    data = tmp_path / "data.csv"
    data.write_bytes(DATA)
    capsys.readouterr()
    rc = main(["audit", "labeldep", str(FIXTURES / "leaky_target_encoding.py"), str(data), "--id-column", "id",
               "--label-column", "label", "--dev-sandbox"])
    res = json.loads(capsys.readouterr().out)
    assert rc == 0 and res["verdict"] == "flagged" and "reads_test_labels_flipped" in res["codes"]
