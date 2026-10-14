"""Duplicate JSON keys (STATUS §3.5 "LP adapter duplicate-JSON-key weakness", suspected; proven here).

Python's json keeps the LAST value of a duplicated key, many other readers (and a human scanning the file) the FIRST.
A document with a duplicated key is therefore two documents: the checker certifies one while the reader sees the
other. Every JSON the Kernel, its adapters and kerneld parse must refuse such a document (fail closed)."""

from __future__ import annotations

import json
import time
import zipfile
from fractions import Fraction

import pytest

from custom_harness.kernel import (
    AnalysisPlan,
    ArtifactRef,
    Claim,
    Effect,
    Label,
    MetricRef,
    Scope,
    canonical_json,
    formal,
    lp,
)
from custom_harness.kernel.bundle import BundleError, build_bundle, reproduce_bundle
from custom_harness.kernel.claim import strict_json
from custom_harness.kernel.ledger import Entry
from custom_harness.kerneld import protocol
from kernel_world import World

# maximise x1 + x2 s.t. 2 x1 + x2 <= 4, x1 + 3 x2 <= 5, x >= 0: optimum 13/5 (x = (7/5, 6/5), y = (2/5, 1/5))
TAIL = b'"A":[["2","1"],["1","3"]],"b":["4","5"],"format":"chh-lp/1","sense":"max"}'
# a first-value reader sees c = (100, 100), whose optimum is 260; the checker (last value) sees c = (1, 1)
DUP_PROBLEM = b'{"c":["100","100"],"c":["1","1"],' + TAIL
CERT = canonical_json({"format": "chh-lp-certificate/1", "x": ["7/5", "6/5"], "y": ["2/5", "1/5"]})
# a first-value reader sees an infeasible x = (2, 1); the checker sees the feasible optimum
DUP_CERT = b'{"format":"chh-lp-certificate/1","x":["2","1"],"x":["7/5","6/5"],"y":["2/5","1/5"]}'


def _first_wins(data: bytes):
    return json.loads(data, object_pairs_hook=lambda pairs: {k: v for k, v in reversed(pairs)})


def _lp_certify(w: World, problem: bytes, cert: bytes, value: str = "13/5"):
    ref = ArtifactRef(artifact_id="inst", hash=w.vault.register_artifact("inst", problem, "problem").hash)
    try:
        statement = lp.scope_phrase(problem, Fraction(value))
    except ValueError:  # an unreadable instance has no rendering (K1 then fails it as unreadable)
        statement = "unreadable"
    doc = Claim(kind="improvement", statement=statement, subject=ref, comparator=ref,
                metric=MetricRef(id="lp_optimum", higher_is_better=True),
                effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                scope=Scope(datasets=()), development=(), generalization_targets=(),
                analysis_plan=AnalysisPlan(seeds=()), resolve_by=time.time() + 86400).model_dump(mode="json")
    claim = Claim.model_validate(doc | {"kind": "computed_value", "evidence_class": "computational", "value": value})
    w.kernel.preregister(claim)
    w.vault.register_artifact("cert", cert, "certificate")
    return w.kernel.certify(claim, [w.kernel.record_certificate(claim, "cert")])


@pytest.fixture()
def lpw(tmp_path):
    w = World(tmp_path / "lp", seed=11, n_train=30, n_test=30)
    w.kernel.approve_adapter("lp", approver="test-world")
    yield w
    w.close()


def test_duplicate_key_problem_is_not_certified_as_the_problem_a_reader_sees(lpw):
    # the attack is real: a first-value reader sees a different problem, whose optimum is not the claimed value
    seen = _first_wins(DUP_PROBLEM)
    assert seen["c"] == ["100", "100"] and json.loads(DUP_PROBLEM)["c"] == ["1", "1"]
    cert = _lp_certify(lpw, DUP_PROBLEM, CERT)
    assert cert.label == Label.REJECTED, "a problem with a duplicated key was certified"
    assert "K3" in cert.failed() and any("duplicate key 'c'" in r for r in cert.checks["K3"].reasons)


def test_duplicate_key_certificate_is_refused(lpw):
    assert _first_wins(DUP_CERT)["x"] == ["2", "1"]  # the reader's x is infeasible (2*2 + 1 > 4)
    cert = _lp_certify(lpw, canonical_json(json.loads(b"{" + b'"c":["1","1"],' + TAIL)), DUP_CERT)
    assert cert.label == Label.REJECTED and any("duplicate key 'x'" in r for r in cert.checks["K3"].reasons)


def test_lp_and_formal_checkers_refuse_duplicates_directly():
    assert any("duplicate key" in r for r in lp.check(DUP_PROBLEM, CERT, Fraction(13, 5)))
    assert any("duplicate key" in r for r in lp.bounds(DUP_PROBLEM, CERT, Fraction(13, 5)))
    with pytest.raises(formal.FormalError, match="duplicate key 'theorem'"):
        formal._load(b'{"format":"f","theorem":"a","theorem":"b"}', "f", {"theorem"})


def test_strict_json_refuses_duplicates_at_any_depth():
    assert strict_json(b'{"a":{"b":1},"c":[{"d":2}]}') == {"a": {"b": 1}, "c": [{"d": 2}]}
    for bad in (b'{"a":1,"a":2}', b'{"a":{"b":1,"b":1}}', b'[{"x":1,"x":1}]'):
        with pytest.raises(ValueError, match="duplicate JSON key"):
            strict_json(bad)


def test_ledger_entry_with_a_duplicated_key_is_refused():
    with pytest.raises(ValueError, match="duplicate JSON key 'claim_hash'"):
        Entry.from_row((1, "prereg", '{"claim_hash":"a","claim_hash":"b"}', "genesis", "h", 0.0))


def test_kerneld_protocol_refuses_a_request_with_a_duplicated_key():
    good = protocol.encode_request(1, "hello", {})
    assert protocol.decode_request(good)[1] == "hello"
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_request('{"id":1,"op":"hello","op":"shutdown","args":{}}')
    assert protocol.request_id('{"id":1,"id":2,"op":"hello","args":{}}') is None


def test_evidence_record_with_a_duplicated_key_fails_k0(tmp_path):
    w = World(tmp_path / "w", seed=11)
    try:
        c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2)
        w.kernel.preregister(c)
        ids = w.evaluate_all(c)
        ev = w.store.get_evidence(ids[0])
        raw = w.store.blobs.get(ev.payload_ref).decode()
        # the same record with its score stated twice: a first-value reader sees 0.0
        forged = raw.replace('"score":', '"score":0.0,"score":', 1).encode()
        assert forged != raw.encode() and json.loads(forged) == json.loads(raw)
        ref = w.store.blobs.put(forged)
        w.store.add_evidence(ev.model_copy(update={"id": "forged-ev", "payload_ref": ref}))
        cert = w.kernel.certify(c, ["forged-ev", *ids[1:]])
        assert cert.label == Label.REJECTED and "K0" in cert.failed()
        assert any("forged-ev" in r for r in cert.checks["K0"].reasons)
    finally:
        w.close()


def test_bundle_manifest_with_a_duplicated_key_is_refused(tmp_path):
    w = World(tmp_path / "w", seed=11)
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    src, dup = tmp_path / "b.zip", tmp_path / "dup.zip"
    build_bundle(w.kernel, cert.cert_hash(), src)
    w.close()
    with zipfile.ZipFile(src) as z:
        members = {n: z.read(n) for n in z.namelist()}
    manifest = members["manifest.json"]
    members["manifest.json"] = b'{"label":"CERTIFIED-VALIDATED",' + manifest[1:]  # first-value readers see this
    with zipfile.ZipFile(dup, "w") as z:
        for n, data in members.items():
            z.writestr(n, data)
    with pytest.raises(BundleError, match="duplicated key"):
        reproduce_bundle(dup, lambda: None, tmp_path / "rp")  # type: ignore[arg-type,return-value]
