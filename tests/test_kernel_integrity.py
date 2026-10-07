"""Integrity hardening of the Evidence Kernel (branch m3-fix-kernel, 2026-10-07).

One test per finding / limitation. Each security assertion was run against the code BEFORE its fix and failed
there (the evidence is in the commit message and the report). Tests assert the attack really happened where that
matters (a defence that holds because the scenario was too weak proves nothing).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

from custom_harness.cli import main
from custom_harness.config import data_dir
from custom_harness.kernel import AdapterError, DatasetRole, Kernel, KernelConfig, Label, Ledger, VaultError
from custom_harness.kernel import adapters as A
from custom_harness.kernel import checks as K
from custom_harness.kernel.bundle import BundleError, ReadLimits, build_bundle, reproduce_bundle
from custom_harness.kernel.claim import EvidenceClass
from custom_harness.kernel.execution import SandboxReplayer
from custom_harness.kernel.ledger import AnchorError, check_anchors, write_anchor
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from custom_harness.verify import leak
from kernel_world import PROGRAMS, World

os.environ.setdefault("CHH_ALLOW_UNISOLATED", "1")


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "w", seed=31, n_train=120, n_test=120)
    yield world
    world.close()


def _honest(w: World, subject: str = "good", **kw):
    return w.claim(w.artifact(subject), w.artifact("baseline", "baseline"), delta=0.2, n_boot=1000, **kw)


def _fresh(root: Path):
    return lambda: SandboxReplayer(lambda key: LocalSandbox(root / key, i_understand_no_isolation=True))


def _ctx(w: World, claim, ids) -> K.Context:
    c = K.Context(claim=claim, claim_hash=claim.claim_hash(), store=w.store, vault=w.vault, ledger=w.kernel.ledger,
                  config=KernelConfig(), replayer=w.replayer, evidence_ids=tuple(ids))
    K.k0_provenance(c)
    return c


def _log_read(w: World, dataset: str, claim_hash: str, purpose: str = "evaluate") -> None:
    """A data_access entry as an older vault (before the R1-F1 binding), or any other ledger writer, could leave."""
    w.kernel.ledger.append("data_access", {"dataset_id": dataset, "dataset_hash": w.vault.dataset(dataset).hash,
                                           "file": "test_y.csv", "private": True, "purpose": purpose, "run_id": None,
                                           "claim_hash": claim_hash, "arm": "subject", "seed": 0})


# ================================================================== R1-F1: label reads bound to the claim's targets
def test_evaluate_and_kernel_reads_are_bound_to_the_named_claims_targets(w):
    cover = _honest(w, targets=("test2",))
    w.kernel.preregister(cover)
    for purpose, kw in (("evaluate", {"arm": "subject", "seed": 0}), ("kernel", {})):
        with pytest.raises(VaultError, match="not a generalization target"):
            w.vault.read("test", "test_y.csv", purpose=purpose, claim_hash=cover.claim_hash(), **kw)
    assert w.vault.accesses("test") == []  # refused BEFORE anything is logged or returned
    assert w.vault.read("test2", "test_y.csv", purpose="kernel", claim_hash=cover.claim_hash())  # its own target
    with pytest.raises(VaultError, match="not pre-registered"):
        w.vault.read("test", "test_y.csv", purpose="kernel", claim_hash="0" * 64)


def test_k2_counts_an_early_read_logged_for_a_claim_on_an_unrelated_target_as_peeking(w):
    """Defence in depth behind the vault binding: an early 'evaluate' read of `test` logged for a claim registered on
    the unrelated target `test2` is peeking (K4's family never charges it, since that claim does not name `test`)."""
    cover = _honest(w, targets=("test2",))
    w.kernel.preregister(cover)
    _log_read(w, "test", cover.claim_hash())
    c = _honest(w, subject="good_k17")
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c), record=False)
    assert "K2" in cert.failed() and "read 1 time(s) before pre-registration" in " ".join(cert.checks["K2"].reasons)


def test_an_early_read_for_a_claim_on_a_related_target_is_reuse_charged_by_k4(w):
    """R1-F4 + the K2 excuse: `test_alias` carries byte-identical private labels (public file projected to fewer
    columns, so no shared example rows). A read of `test` logged for a claim on `test_alias`, registered first, is
    test-set reuse: K2 excuses it and K4 counts that claim in the Bonferroni family (2 claims)."""
    lines = w.vault.read_public("test", "test_X.csv").decode().splitlines()
    proj = "\n".join(",".join(ln.split(",")[:2]) for ln in lines) + "\n"
    labels = w.store.blobs.get(w.vault.dataset("test").files["test_y.csv"].blob)
    w.vault.register_dataset("test_alias", DatasetRole.TEST, public={"test_X.csv": proj.encode()},
                             private={"test_y.csv": labels})
    assert K.related_targets(w.vault, "test") == {"test", "test_alias"}
    assert K.related_targets(w.vault, "ghost") == {"ghost"}  # unregistered: related to nothing
    alias = _honest(w, targets=("test_alias",))
    w.kernel.preregister(alias)
    _log_read(w, "test", alias.claim_hash())
    c = _honest(w, subject="good_k17")
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c), record=False)
    assert cert.checks["K2"].status == "pass", cert.checks["K2"].reasons
    assert any("2 claim(s) tested on this target" in o for o in cert.checks["K4"].observations)


# ================================================================== K2-TWO-RUN: integrity reads
def test_k2_lists_leakscan_reads_but_counts_reproduce_reads_as_peeking(w):
    w.vault.read("test", "test_y.csv", purpose="leakscan")  # e.g. an earlier run's audit building fingerprints
    c = _honest(w)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c), record=False)
    assert "K2" not in cert.failed(), cert.checks["K2"].reasons
    assert any("1 integrity read(s) (leakscan)" in o for o in cert.checks["K2"].observations)
    w.vault.read("test", "test_y.csv", purpose="reproduce")
    c2 = _honest(w, subject="good_k17")
    w.kernel.preregister(c2)
    cert2 = w.kernel.certify(c2, w.evaluate_all(c2), record=False)
    assert "K2" in cert2.failed() and "purpose 'reproduce'" in " ".join(cert2.checks["K2"].reasons)


# ================================================================== R1-F2: records outside the plan
def test_records_outside_the_plan_neither_steer_the_replay_seed_nor_pass_k4(w):
    c = _honest(w)
    w.kernel.preregister(c)
    ids = w.evaluate_all(c)
    base = _ctx(w, c, ids)
    want = {(r.arm, r.target): K.replay_seed(base, r) for _, r in base.records}
    extras: list[str] = []
    for i in range(6):  # non-target dataset and unplanned seeds, written without a vault read
        extras.append(w.evaluate(c, "comparator", seed=100 + i, target="test2" if i % 2 else "test", log_read=False))
        ctx = _ctx(w, c, ids + extras)
        got = {(r.arm, r.target): K.replay_seed(ctx, r) for _, r in ctx.records if r.target == "test"}
        assert got == want, f"{len(extras)} extra record(s) moved the replay seed"
    k4 = " ".join(K.k4_statistics(_ctx(w, c, ids + extras)).reasons)
    assert "6 evaluation record(s) outside the analysis plan" in k4


# ================================================================== R1-F3: certification shopping
def test_a_claim_that_failed_k3_once_is_never_certified_by_retrying(w, tmp_path):
    c = _honest(w)
    w.kernel.preregister(c)
    ids = w.evaluate_all(c)
    honest = w.replayer

    class FailsOnce:  # the first re-execution differs, every later one reproduces exactly
        calls = 0

        def replay(self, record, vault):
            self.calls += 1
            return ["9"] * 120 if self.calls == 1 else honest.replay(record, vault)

    k = Kernel(w.store, replayer=FailsOnce())
    first = k.certify(c, ids)
    assert "K3" in first.failed()
    second = k.certify(c, ids)  # this replay reproduces: only the history can reject it
    assert second.label == Label.REJECTED and second.failed() == ["K3"], second.failed()
    assert "an earlier certificate of this claim" in " ".join(second.checks["K3"].reasons)
    bundle = tmp_path / "b.zip"  # the earlier certificate travels with the bundle, so the verdict reproduces
    build_bundle(w.kernel, second.cert_hash(), bundle)
    rep = reproduce_bundle(bundle, _fresh(tmp_path / "rp"))
    assert rep.reproduced and rep.reproduced_label == "REJECTED", rep.problems


def test_k3_history_ignores_unregistered_attempts_and_fails_closed_on_unreadable_ones(w):
    c = _honest(w)
    early = w.kernel.certify(c, [])  # before registration: recorded; K3 "not evaluated", not a failed replay
    assert early.prereg_seq is None and early.checks["K3"].status == "fail"
    w.kernel.preregister(c)
    ids = w.evaluate_all(c)
    good = w.kernel.certify(c, ids)
    assert good.label == Label.CERTIFIED_INTERNAL, good.failed()
    blob = w.kernel.ledger.entries("certificate")[-1].body["blob"]
    for bogus in ({"cert_hash": "1" * 64, "blob": blob},  # a real certificate filed under another hash
                  {"cert_hash": "2" * 64, "blob": w.store.blobs.put_json({"not": "a certificate"})}):
        w.kernel.ledger.append("certificate", {**bogus, "claim_hash": c.claim_hash(), "label": "REJECTED",
                                               "resolution": {}})
    k3 = w.kernel.certify(c, ids, record=False).checks["K3"]
    assert k3.status == "fail" and " ".join(k3.reasons).count("is unreadable") == 2, k3.reasons


# ================================================================== R1-F5: adapter identity covers its imports
def _package(root: Path) -> None:
    pkg = root / "chh_adapt_pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .core import ok\n")
    (pkg / "core.py").write_text(
        "import json\nimport sys\n\nimport pydantic\n\nfrom os.path import *  # noqa\nfrom . import helper\n"
        "from .sub import deep\n\n\ndef ok(x):\n    return x >= helper.THRESHOLD + deep.VALUE - 2\n\n\n"
        "def lazy():\n    from .missing_mod import nothing  # never imported at runtime\n")
    (pkg / "helper.py").write_text("THRESHOLD = 1\n")
    (pkg / "sub" / "__init__.py").write_text("")
    (pkg / "sub" / "deep.py").write_text("import chh_adapt_pkg.helper as h\n\nVALUE = 2\n")


def test_adapter_identity_covers_the_project_modules_it_imports(tmp_path, monkeypatch):
    _package(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = A.AdapterSpec("ml", "1", frozenset({EvidenceClass.DATA}), ("chh_adapt_pkg.core",), frozenset({"accuracy"}))
    monkeypatch.setitem(A.REGISTRY, "ml", (spec, (("trivially true", lambda: True),)))
    w = World(tmp_path / "w", approve_adapters=False)
    try:
        w.kernel.approve_adapter("ml", approver="alice")
        assert A.resolve(w.kernel.ledger, EvidenceClass.DATA, "accuracy")[0] is not None
        (tmp_path / "chh_adapt_pkg" / "helper.py").write_text("THRESHOLD = -1e9  # unapproved change\n")
        adapter, why = A.resolve(w.kernel.ledger, EvidenceClass.DATA, "accuracy")
        assert adapter is None and "changed since approval" in why[0]
    finally:
        w.close()
    assert set(A.covered_modules(spec)) == {"chh_adapt_pkg", "chh_adapt_pkg.core", "chh_adapt_pkg.helper",
                                            "chh_adapt_pkg.sub", "chh_adapt_pkg.sub.deep"}  # no stdlib/site-packages
    assert A.adapter_hash(spec) == A.adapter_hash(spec)  # deterministic (second call uses the parse cache)
    gone = A.AdapterSpec("ml", "1", frozenset({EvidenceClass.DATA}), ("chh_adapt_pkg.nope",), frozenset({"accuracy"}))
    with pytest.raises(AdapterError, match="no project source"):
        A.adapter_hash(gone)
    for m in [m for m in sys.modules if m.startswith("chh_adapt_pkg")]:
        sys.modules.pop(m)


# ================================================================== dataset roles: a human approves external/future
def test_external_role_needs_a_human_approval_to_earn_validated(tmp_path):
    w = World(tmp_path / "ext", seed=8, n_train=120, n_test=120)
    try:
        w.register_target("external_lab", DatasetRole.EXTERNAL, n=120)
        c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2, targets=("external_lab",),
                    n_boot=1000)
        w.kernel.preregister(c)
        ids = w.evaluate_all(c)
        cert = w.kernel.certify(c, ids, record=False)
        assert cert.label == Label.CERTIFIED_INTERNAL, cert.label  # the registrant's role alone: fail closed
        assert any("NOT human-approved" in o for o in cert.checks["K6"].observations)
        with pytest.raises(VaultError, match="only external/future"):
            w.kernel.approve_role("test", approver="alice")
        with pytest.raises(VaultError, match="name the human"):
            w.kernel.approve_role("external_lab", approver="  ")
        w.kernel.ledger.append("role_approval", {"dataset_id": "external_lab", "dataset_hash": "0" * 64,
                                                 "role": "external", "approver": "mallory", "note": ""})
        assert w.kernel.certify(c, ids, record=False).label == Label.CERTIFIED_INTERNAL  # other content: no
        w.kernel.approve_role("external_lab", approver="alice", note="lab B data, never used in development")
        cert = w.kernel.certify(c, ids)
        assert cert.label == Label.CERTIFIED_VALIDATED, cert.failed()
        assert any("approved by a human ('alice'" in o for o in cert.checks["K6"].observations)
        build_bundle(w.kernel, cert.cert_hash(), tmp_path / "b.zip")
    finally:
        w.close()
    rep = reproduce_bundle(tmp_path / "b.zip", _fresh(tmp_path / "rp"))
    assert rep.reproduced and rep.reproduced_label == "CERTIFIED-VALIDATED", rep.problems


def test_cli_approve_role_and_ledger_anchor(tmp_path, capsys):
    s = EvidenceStore(data_dir() / "store")
    try:
        Kernel(s).vault.register_dataset("lab", DatasetRole.FUTURE, public={"a.csv": b"x\n1\n"},
                                         private={"y.csv": b"y\n1\n"})
        Kernel(s).vault.register_dataset("dev", DatasetRole.DEV, public={"b.csv": b"x\n2\n"}, private={})
    finally:
        s.close()
    assert main(["approve-role", "lab", "--approver", "alice", "--note", "returned by lab B"]) == 0
    assert '"approver": "alice"' in capsys.readouterr().out
    with pytest.raises(SystemExit, match="NOT approved"):
        main(["approve-role", "dev", "--approver", "alice"])
    anchor = tmp_path / "elsewhere" / "anchors.jsonl"
    assert main(["ledger-anchor", str(anchor)]) == 0
    with pytest.raises(SystemExit, match="outside the store"):
        main(["ledger-anchor", str(data_dir() / "store" / "a.jsonl")])
    capsys.readouterr()
    assert main(["ledger-verify", "--anchor", str(anchor)]) == 0
    assert main(["ledger-verify", "--anchor", str(tmp_path / "missing.jsonl")]) == 1
    assert "unreadable" in capsys.readouterr().out


# ================================================================== external ledger anchoring
def test_external_anchor_detects_a_rewritten_or_truncated_ledger(tmp_path):
    root = tmp_path / "store"
    s = EvidenceStore(root)
    lg = Ledger(s)
    anchors = tmp_path / "anchors" / "a.jsonl"
    with pytest.raises(AnchorError, match="outside the store"):
        write_anchor(lg, root / "a.jsonl", root)
    assert write_anchor(lg, anchors, root)["head"] == "genesis"
    for i in range(3):
        lg.append("bundle", {"x": i})
    assert write_anchor(lg, anchors, root)["length"] == 3
    lg.append("bundle", {"x": 3})
    assert check_anchors(lg.entries(), anchors) == []  # extending the chain is fine
    s2 = EvidenceStore(tmp_path / "rewritten")  # a whole new chain: self-consistent, same length
    lg2 = Ledger(s2)
    for i in range(4):
        lg2.append("bundle", {"x": 10 + i})
    assert lg2.verify() == []
    assert any("was rewritten at or before entry 3" in p for p in check_anchors(lg2.entries(), anchors))
    assert any("fewer than the 3 anchored" in p for p in check_anchors(lg.entries()[:2], anchors))
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"format": "x", "length": 1, "head": "h"}\nnot json\n'
                   '{"format": "chh-anchor/1", "length": -1, "head": "h"}\n')
    assert len([p for p in check_anchors(lg.entries(), bad) if "unreadable" in p]) == 3
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n")
    assert "holds no anchor" in check_anchors(lg.entries(), empty)[0]
    s3 = EvidenceStore(tmp_path / "broken")
    s3.ledger_import((1, "bundle", "{}", "genesis", "not-the-hash", 0.0))
    with pytest.raises(AnchorError, match="broken chain"):
        write_anchor(Ledger(s3), tmp_path / "b.jsonl", tmp_path / "broken")
    for st in (s, s2, s3):
        st.close()


# ================================================================== bundles: unbroken chain, v1 label, bounded reads
def _certified_bundle(w: World, tmp_path: Path) -> tuple[Path, int]:
    w.vault.read("test2", "test_y.csv", purpose="audit")  # an entry on another dataset: v1 excerpts dropped it
    c = _honest(w)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    assert cert.label == Label.CERTIFIED_INTERNAL
    path = tmp_path / "b.zip"
    build_bundle(w.kernel, cert.cert_hash(), path)
    seq = next(e.seq for e in w.kernel.ledger.entries("certificate") if e.body["cert_hash"] == cert.cert_hash())
    return path, seq


def _rewrite_manifest(src: Path, dst: Path, edit) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for n in zin.namelist():
            data = zin.read(n)
            zout.writestr(n, json.dumps(edit(json.loads(data))).encode() if n == "manifest.json" else data)


def test_bundle_ledger_is_an_unbroken_chain_so_an_omitted_entry_is_detected(w, tmp_path):
    bundle, seq = _certified_bundle(w, tmp_path)
    rep = reproduce_bundle(bundle, _fresh(tmp_path / "r0"))
    assert rep.reproduced, rep.problems
    assert rep.ledger_check.startswith(f"unbroken hash chain from genesis: {seq} entries")
    assert rep.ledger_head == w.kernel.ledger.entries()[seq - 1].hash

    def drop_audit_read(m):  # a producer leaving one entry out of the bundled ledger
        rows = [r for r in m["ledger"] if not (r[1] == "data_access" and '"audit"' in r[2])]
        assert len(rows) == len(m["ledger"]) - 1
        return {**m, "ledger": rows}

    _rewrite_manifest(bundle, tmp_path / "holed.zip", drop_audit_read)
    rep = reproduce_bundle(tmp_path / "holed.zip", _fresh(tmp_path / "r1"))
    assert not rep.reproduced and any("chain broken" in p for p in rep.problems), rep.problems


def test_legacy_v1_bundle_is_rechecked_but_labelled_excerpt_only(w, tmp_path):
    bundle, _ = _certified_bundle(w, tmp_path)
    _rewrite_manifest(bundle, tmp_path / "v1.zip", lambda m: {**m, "format": "chh-bundle/1"})
    rep = reproduce_bundle(tmp_path / "v1.zip", _fresh(tmp_path / "r"))
    assert rep.reproduced and rep.ledger_check.startswith("EXCERPT ONLY (chh-bundle/1)"), rep.ledger_check


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def test_bundle_reads_are_bounded_and_refuse_zip_bombs(w, tmp_path):
    """A zip-bomb member was read whole into memory. Now: a highly compressed manifest is refused by the DEFAULT
    ratio cap before it is read, and every other cap (per member, total, member count) refuses with its name."""
    bomb = _zip(tmp_path / "bomb.zip", {"manifest.json": b'{"format": "chh-bundle/2"' + b" " * (9 * 2**20) + b"}"})
    with pytest.raises(BundleError, match="compression ratio over 200"):
        reproduce_bundle(bomb, _fresh(tmp_path / "r"))
    bundle, _ = _certified_bundle(w, tmp_path)
    for limits, why in ((ReadLimits(member_bytes=100), "per-member cap of 100"),
                        (ReadLimits(total_bytes=1000), "total cap of 1000"),
                        (ReadLimits(members=3), "more than the cap of 3")):
        with pytest.raises(BundleError, match=why):
            reproduce_bundle(bundle, _fresh(tmp_path / "r"), limits=limits)
    with zipfile.ZipFile(bundle) as z:  # the manifest fits, a blob after it does not: refused mid-way, store closed
        fits = z.getinfo("manifest.json").file_size + 10
    with pytest.raises(BundleError, match=f"total cap of {fits} bytes .at member blobs/"):
        reproduce_bundle(bundle, _fresh(tmp_path / "r"), limits=ReadLimits(total_bytes=fits))
    with pytest.raises(BundleError, match="no manifest"):
        reproduce_bundle(_zip(tmp_path / "empty.zip", {"x": b""}), _fresh(tmp_path / "r"))


# ================================================================== K9: encoded leaks and long label runs
def _canary_texts(canary: str) -> dict[str, str]:
    return {"base64": base64.b64encode(f"token={canary};".encode()).decode(),
            "url-safe base64 glued to a word": "blob" + base64.urlsafe_b64encode(canary.encode()).decode(),
            "hex": "0x" + canary.encode().hex().upper(),
            "reversed": canary[::-1],
            "url-encoded": "".join(f"%{b:02X}" for b in canary.encode()),
            "fragment of its random part": "id = " + canary[-24:-4].upper()}


def test_k9_finds_encoded_canaries_and_canary_fragments():
    canary = leak.new_canary()
    for how, text in _canary_texts(canary).items():
        assert canary not in text
        assert leak.scan_text(f"x = '{text}'", "code", [canary], []), how
    short = "CHH-CANARY-world31-0"  # too short a random part for fragments: only the whole token counts
    assert leak.scan_text("world31-0", "code", [short], []) == []
    assert leak.scan_text(base64.b64encode(short.encode()).decode(), "code", [short], [])


def test_k9_finds_long_label_runs_anywhere_and_encoded_but_not_short_or_uninformative_ones(w):
    labels = w.labels["test"]
    runs = [leak.label_runs(labels)]
    run = labels[50:110]
    leaks = {"semicolon run at offset 50": ";".join(run), "reversed order": ", ".join(run[::-1]),
             "base64 of a newline run": base64.b64encode("\n".join(run).encode()).decode(),
             "hex of a comma run": ",".join(run).encode().hex()}
    for how, text in leaks.items():
        assert any("consecutive labels" in p for p in leak.scan_text(f"# {text}\n", "code", [], [], runs)), how
    # no false positives: a run too short, labels of the WRONG examples, a constant run, random hex and base64
    rng_hex = os.urandom(64).hex()
    clean = [" ".join(labels[50:85]), " ".join(labels[1:61][::-2] * 2), ",".join(["0"] * 200), rng_hex,
             base64.b64encode(os.urandom(90)).decode(), " ".join(w.labels["test2"][:120])]
    for text in clean:
        assert leak.scan_text(text, "code", [], [], runs) == [], text[:60]
    imbalanced = ["0"] * 190 + ["1"] * 10  # low information: 60 consecutive labels carry < 64 bits
    assert leak.scan_text(" ".join(imbalanced[:60]), "code", [], [], [leak.label_runs(imbalanced)]) == []
    assert leak.label_runs(labels[:30]).index == {}  # fewer labels than a run


def test_k9_rejects_a_program_carrying_base64_encoded_labels_and_keeps_perfect_predictions_clean(w, tmp_path):
    labels = "\n".join(w.labels["test"][40:120])
    code = PROGRAMS["good"] + f"\n# {base64.b64encode(labels.encode()).decode()}\n"
    c = w.claim(w.artifact("leaky", code=code), w.artifact("baseline", "baseline"), delta=0.2, n_boot=1000)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c), record=False)
    assert "K9" in cert.failed() and "base64-encoded" in " ".join(cert.checks["K9"].reasons), cert.checks["K9"]
    ws = tmp_path / "ws"  # the workspace check never applies label runs: a perfect predictions file is not a leak
    ws.mkdir()
    (ws / "preds.csv").write_text("prediction\n" + "\n".join(w.labels["test"]))
    fp = {"hashes": {}, "label_windows": leak.label_windows(w.labels["test"]),
          "label_runs": [leak.label_runs(w.labels["test"])]}
    assert leak.leak_audit([], ws, fp, [])["passed"]
    trace = [type("Step", (), {"index": 0, "node_id": "n", "output": {"t": ";".join(w.labels["test"][7:90])},
                               "error": None})()]
    assert not leak.leak_audit(trace, None, fp, [])["passed"]  # but in a trace, a long run is a leak
