"""s4-kernel red team (s4-kernel__redteam), unit-level attack tests. NOT run by the red team (owner rule: no tests this
round). Each test asserts the SAFE behaviour; the docstring says why it is expected to fail on s4-kernel 59066ec, and
whether that was seen in a tiny python -c probe (verified) or reasoned from the code (inferred)."""

from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

from custom_harness.kernel import adapters, canonical_json
from custom_harness.kernel import checks as K
from custom_harness.kernel import ledger as L
from custom_harness.kernel.checklist import fill
from custom_harness.records import Cost
from custom_harness.verify import noninterference as ni

CANARY = "CHH-CANARY-redteam-0"


def _seen(data: bytes) -> bool:
    return bool(K._scan(K.reader_texts(data, source=True), "subject code", [CANARY], []))


# ------------------------------------------------------------------ RT-S4-1: two declared copies in one file
def test_world_files_perturbs_every_declared_copy_in_the_same_file():
    """RT-S4-1 (high, verified by probe). verify/noninterference.world_files rebuilds out[c.name] from the ORIGINAL
    files[c.name] for each copy, so with two declared truth columns in one input file only the LAST is perturbed and
    the first keeps the real truth in both counterfactual worlds: a program reading the first column passes K3's
    non-interference sub-check. Fails today: column 'leak' is unchanged in the flipped world."""
    files = {"test": b"x,leak,leak2\n1,v0,w0\n2,v1,w1\n3,v0,w1\n"}
    copies = [ni.TruthCopy("test", "leak"), ni.TruthCopy("test", "leak2")]
    for world in ni.World:
        out = ni.world_files(files, copies, world, regression=False)
        for c in copies:
            assert ni.read_column(out["test"], c.column) != ni.read_column(files["test"], c.column), (world, c)


# ------------------------------------------------------------------ RT-S4-5 / RT-S4-6: K9 source scan
def _zipapp(body: str, method: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        z.writestr("__main__.py", body)
    return buf.getvalue()


@pytest.mark.parametrize("method", [zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_k9_sees_a_canary_inside_a_zipapp_code_artifact(method):
    """RT-S4-5 (high, verified by probe). `python solution.py` runs a ZIP archive with __main__.py whatever the file
    name (probe: a deflated zip named solution.py ran), and run_program writes the code artifact to the entrypoint
    as-is. reader_texts sees neither text (compressed) nor Python (ast.parse fails), so K9 finds no canary. Fails
    today. Fix: refuse a code artifact that is not UTF-8 Python source (zip magic 'PK\\x03\\x04' anywhere, or
    ast.parse failure) at K9/K0, or scan every member of a zip after decompression."""
    data = _zipapp(f"MARK = {CANARY!r}\nprint(MARK)\n", method)
    assert CANARY.encode() not in data  # compressed: the attack precondition
    assert _seen(data)


# owner decision 2026-10-28 (stage-c4): open findings are strict xfails until fixed; the attack below is unchanged
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="open red-team finding RT-S4-6 (low): the K9 source "
                   "scan does not join canary fragments separated by another literal")
def test_k9_sees_a_canary_split_by_an_unrelated_literal():
    """RT-S4-6 (low, verified by probe). reader_texts joins literals in source order, so 'a' 'b' adjacency is caught,
    but ANY literal in between defeats the join (the program concatenates at runtime). Stated limit: runtime
    composition is out of static scope; this test documents the gap and fails today. Possible fix: also scan all
    pairwise concatenations of short literals (bounded), or refuse code whose literals contain canary fragments of
    >= 8 characters (leak._found(fragments=True) on each literal)."""
    src = f"a = {CANARY[:9]!r}\nsep = 'zz'\nb = {CANARY[9:]!r}\nprint(a + b)\n".encode()
    assert _seen(src)


def test_k9_still_sees_escaped_and_cookie_encoded_canaries():
    """Regression (expected to PASS today; verified by probe): \\x escapes and a utf-7 coding cookie are decoded."""
    assert _seen(b"X = '\\x43HH-CANARY-redteam-0'\n")
    utf7 = b"# -*- coding: utf-7 -*-\nX = '+AEM-HH-CANARY-redteam-0'\n"  # +AEM- is 'C' in UTF-7
    assert CANARY.encode() not in utf7 and _seen(utf7)


# ------------------------------------------------------------------ RT-S4-7: anchor in an alternate data stream
@pytest.mark.skipif(sys.platform != "win32", reason="NTFS alternate data streams")
def test_write_anchor_refuses_an_alternate_data_stream_of_the_store_directory(tmp_path):
    """RT-S4-7 (medium, verified by probe). `<store>:anchor` names a named stream of the store DIRECTORY itself: it is
    writable (probe), resolve() keeps it, it is neither the store nor under it by path, and it does not exist before
    the first write, so samefile on its ancestors never matches. ledger.inside returns False and write_anchor
    accepts an anchor that lives in the store's own metadata. Fails today. Fix: in inside(), strip an NTFS stream
    suffix (':name[:type]') from every component before comparing, or refuse any ':' after the drive."""
    root = (tmp_path / "store").resolve()
    root.mkdir()
    assert L.inside(Path(str(root) + ":anchor"), root, on_error=True)


# ------------------------------------------------------------------ RT-S4-8: adapter hash cache
def test_adapter_hash_sees_a_same_size_edit_with_the_mtime_restored(tmp_path, monkeypatch):
    """RT-S4-8 (low, inferred). adapter_hash trusts (mtime_ns, size, inode): an in-place edit of a covered module that
    keeps the size and restores the mtime (os.utime, or a coarse-mtime filesystem) is a cache hit, so resolve() keeps
    honouring the old approval for changed adapter code in a long-lived process. Fails today. Fix: cache the import
    walk only and always re-hash the (small) covered files' bytes."""
    monkeypatch.syspath_prepend(str(tmp_path))
    mod = tmp_path / "rt_s4k_cache_mod.py"
    mod.write_text("X = 1\n", encoding="utf-8")
    spec = adapters.AdapterSpec(name="rtprobe", version="1", classes=frozenset(), modules=("rt_s4k_cache_mod",),
                                metrics=frozenset())
    first = adapters.adapter_hash(spec)
    st = mod.stat()
    mod.write_text("X = 2\n", encoding="utf-8")  # same size
    os.utime(mod, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert adapters.adapter_hash(spec) != first


# ------------------------------------------------------------------ RT-S4-9: cost rendering
def test_a_mix_of_paid_and_unreported_costs_is_not_rendered_as_a_known_total():
    """RT-S4-9 (low, inferred). checklist.fill sums usd and renders '$0.2500' when one run paid $0.25 and another was a
    subscription call (usd 0.0 = NOT reported): the unknown part silently disappears. Fails today. Fix: track whether
    any run with model usage reported no usd, and render '$x + unknown'."""
    from custom_harness.kernel.certificate import CHECK_IDS, Certificate, Label

    claim = {"kind": "improvement", "metric": {"id": "accuracy"}, "effect": {"min_effect": 0.1},
             "analysis_plan": {"seeds": [0], "test": "t", "alpha": 0.05}, "generalization_targets": ["t"],
             "subject": {"hash": "a" * 64}, "comparator": {"hash": "b" * 64}}
    cert = Certificate(kernel_version="1.3", claim_hash="c" * 64, claim=claim, label=Label.REJECTED, tier=6,
                       checks={k: K.CheckResult(status="fail", reasons=("x",)) for k in CHECK_IDS}, assumptions=(),
                       evidence_ids=(), verifier_ids=(), prereg_seq=None, resolution={}, issued_at=0.0,
                       vg_ladder="x")
    out = fill(cert, run_costs={"paid": Cost(usd=0.25, prompt_tokens=5),
                                "byos": Cost(prompt_tokens=10, quota_units=2.0)}, models=["m"], bundle_hash=None,
               selection_policy=None, human_in_the_loop=None, novelty=None)["cost"]
    assert out is not None and "unknown" in out.lower(), out


# ------------------------------------------------------------------ duplicate keys (regression)
def test_a_noise_floor_record_with_a_duplicated_key_is_refused():
    """Regression (expected to PASS today, inferred): tolerance_reasons reads the record with strict_json, so a
    document whose 'reference' appears twice (two readers, two tolerances) raises ValueError."""
    doc = canonical_json({"format": "chh-noise-floor/1", "reference": 0.8, "nominal": [0.81] * 9,
                          "perturbed": [0.8] * 9, "rebuild": [], "seed": []}).decode()
    dup = doc.replace('"reference":0.8', '"reference":0.8,"reference":0.5')
    assert dup != doc
    with pytest.raises(ValueError, match="duplicate"):
        K.strict_json(dup)
