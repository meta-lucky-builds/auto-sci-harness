"""K9 and the exact adapters' readers must agree on what a document says (reported by s3-domain's red team for the
circuit adapter, 2026-10-14; the same hole in the `lp` reader). Python's json reads UTF-16/32, a BOM and \\u escapes;
K9 scanned the raw bytes as UTF-8, so a held-out canary in a certificate `comment` was invisible to K9 while the
reader accepted the document. Now the readers take canonical UTF-8 only (no BOM), and K9 also scans every parsed
string of a JSON document. Status when written: built, NOT test-verified (tests not run this round per owner)."""

from __future__ import annotations

import json
import time
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
    sim,
)
from kernel_world import World

PROBLEM = canonical_json({"format": "chh-lp/1", "sense": "max", "c": ["1", "1"], "A": [["2", "1"], ["1", "3"]],
                          "b": ["4", "5"]})
CANARY = "CHH-CANARY-world11-0"  # the canary of World(seed=11)'s held-out dataset "test"


def _cert(comment: str) -> dict:
    return {"format": "chh-lp-certificate/1", "x": ["7/5", "6/5"], "y": ["2/5", "1/5"], "comment": comment}


def _certify(w: World, cert: bytes):
    ref = ArtifactRef(artifact_id="inst", hash=w.vault.register_artifact("inst", PROBLEM, "problem").hash)
    doc = Claim(kind="improvement", statement=lp.scope_phrase(PROBLEM, Fraction(13, 5)), subject=ref, comparator=ref,
                metric=MetricRef(id="lp_optimum", higher_is_better=True),
                effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                scope=Scope(datasets=()), development=(), generalization_targets=(),
                analysis_plan=AnalysisPlan(seeds=()), resolve_by=time.time() + 86400).model_dump(mode="json")
    claim = Claim.model_validate(doc | {"kind": "computed_value", "evidence_class": "computational", "value": "13/5"})
    w.kernel.preregister(claim)
    aid = f"cert-{len(w.vault.artifacts())}"
    w.vault.register_artifact(aid, cert, "certificate")
    return w.kernel.certify(claim, [w.kernel.record_certificate(claim, aid)])


@pytest.fixture()
def world(tmp_path):
    w = World(tmp_path / "w", seed=11, n_train=30, n_test=30)
    w.kernel.approve_adapter("lp", approver="test-world")
    yield w
    w.close()


@pytest.mark.parametrize("encode", ["utf-16", "utf-32", "utf-8-sig"])
def test_a_canary_in_a_non_utf8_certificate_is_not_certified(world, encode):
    data = json.dumps(_cert(CANARY)).encode(encode)
    # UTF-16/32 hid the canary from the old K9 byte scan; UTF-8 with a BOM did not (the bytes after the BOM are plain
    # UTF-8). The BOM case stays because the reader must refuse it (K3) while K9 still flags the canary.
    assert (CANARY in data.decode("utf-8", errors="ignore")) == (encode == "utf-8-sig")
    cert = _certify(world, data)
    assert cert.label == Label.REJECTED and "K9" in cert.failed() and "K3" in cert.failed(), cert.failed()


def test_a_unicode_escaped_canary_is_found_by_k9(world):
    data = json.dumps(_cert(CANARY)).replace("CHH", "\\u0043HH").encode()  # valid UTF-8, the reader decodes it
    assert CANARY not in data.decode() and json.loads(data)["comment"] == CANARY
    cert = _certify(world, data)
    assert cert.failed() == ["K9"] and any("certificate" in r for r in cert.checks["K9"].reasons)


def test_readers_refuse_non_canonical_encodings():
    good = json.dumps(_cert("ok")).encode()
    assert lp.check(PROBLEM, good, Fraction(13, 5)) == []
    for bad in (good.decode().encode("utf-16"), b"\xef\xbb\xbf" + good):
        assert any("not JSON" in r for r in lp.check(PROBLEM, bad, Fraction(13, 5)))
    with pytest.raises(formal.FormalError, match="byte-order mark"):
        formal._load(b"\xef\xbb\xbf{}", "f", set())
    with pytest.raises(sim.SimError, match="not JSON"):
        sim._load('{"format": "x"}'.encode("utf-16"), "x", set())
