"""s4-kernel: the Kernel-side gaps closed in round 4 (docs/kernel_s4.md). One section per brief item.

Status when written: built, NOT test-verified (tests deferred by the owner this round). Every test is designed to FAIL
on the pre-s4 Kernel (origin/int17 3ce1ba4 + these test files); the reason is stated in each docstring. Tests that call
the code under test only through new names (e.g. `reader_texts`, `inside`) fail on the old code with an ImportError or
AttributeError, which is a fail-first for the right reason only together with the behavioural assertion: those say so.
"""

from __future__ import annotations

import base64
import ctypes
import os
import sys
from pathlib import Path

import pytest

from custom_harness.kernel import (
    AnalysisPlan,
    ClaimKind,
    DatasetRole,
    Effect,
    KernelConfig,
    KernelError,
    Label,
    Scope,
    adapters,
    ml,
)
from custom_harness.kernel import checks as K
from custom_harness.kernel import ledger as L
from custom_harness.kernel.checklist import fill
from custom_harness.kernel.reasons import Code
from custom_harness.records import Cost
from custom_harness.verify import leak
from custom_harness.verify import tolerance as T
from custom_harness.verify.stats import Interval
from kernel_world import KNN, World, render

CANARY = "CHH-CANARY-world11-0"  # the canary of World(seed=11)'s held-out dataset "test"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    w = World(tmp_path_factory.mktemp("s4"), seed=11)
    # 5 rows (stage-c 2026-10-23, as int17-fix did for test_kernel_nullbound): 15 rows are NOT reliably insensitive
    # to +-0.15 (on this world's "tiny" all 11 plantings on both sides were detected and K7 passed, logs/07_alone);
    # with 5 rows K7_INSENSITIVE fires on both sides (logs/P3_s4_equivalence_debug.log)
    w.register_target("tiny", DatasetRole.TEST, n=5)
    yield w
    w.close()


def k1(w: World, claim) -> K.CheckResult:
    cx = K.Context(claim=claim, claim_hash=claim.claim_hash(), store=w.store, vault=w.vault, ledger=w.kernel.ledger,
                   config=KernelConfig(), replayer=None, evidence_ids=())
    return K.k1_well_formed(cx)


def _good_claim(w: World, **plan):
    return w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), n_boot=1000, **plan)


# ================================================================== item 1: K9 sees decoded program literals
def _escaped(text: str) -> str:
    return "".join(f"\\x{ord(ch):02x}" for ch in text)


@pytest.mark.parametrize("spelling", ["hex-escapes", "split-literals", "unicode-escapes", "bytes-literal"])
def test_a_canary_spelled_as_python_escapes_in_source_is_seen(spelling):
    """Fails first: the old K9 scanned the source bytes as UTF-8 only; none of these spellings contains the canary
    (or a 16-character fragment of it) as text, and the old code has no `reader_texts` (ImportError/AttributeError,
    plus the precondition below shows the byte scan alone misses it)."""
    lit = {"hex-escapes": f'"{_escaped(CANARY)}"',
           "split-literals": '"' + '" "'.join(CANARY[i:i + 3] for i in range(0, len(CANARY), 3)) + '"',
           "unicode-escapes": '"' + "".join(f"\\u{ord(ch):04x}" for ch in CANARY) + '"',
           "bytes-literal": f'b"{_escaped(CANARY)}"'}[spelling]
    src = f"NOTE = {lit}\nprint(len(NOTE))\n".encode()
    assert not leak.scan_text(src.decode(), "code", [CANARY], [])  # the byte scan alone cannot see it
    found = [f for t in K.reader_texts(src, source=True) for f in leak.scan_text(t, "code", [CANARY], [])]
    assert found and all("canary" in f for f in found)


def test_a_canary_in_a_coding_cookie_source_and_utf16_bytes_is_seen():
    """Fails first (no reader_texts; the byte scan misses both, verified by python -c on 2026-10-15): a utf-7 cookie
    file whose COMMENT spells the canary in modified base64 (Python decodes the file before running it); UTF-16-LE
    bytes without a BOM (the NULs are dropped)."""
    hidden = "+" + base64.b64encode(CANARY.encode("utf-16-be")).decode().rstrip("=") + "-"
    cookie = f"# -*- coding: utf-7 -*-\nX = 1  # {hidden}\n".encode()
    assert not leak.scan_text(cookie.decode(), "code", [CANARY], [])
    assert any(CANARY in t for t in K.reader_texts(cookie, source=True))
    utf16 = CANARY.encode("utf-16-le")
    assert CANARY not in utf16.decode("utf-8", "ignore")
    assert any(CANARY in t for t in K.reader_texts(utf16))


def test_reader_texts_never_parses_program_inputs_as_python():
    """A numeric CSV is valid Python (a tuple per row): inputs are not AST-parsed (cost), only code is. Fails first:
    no reader_texts."""
    csv_bytes = b"x1,x2\n1.0,2.0\n3.0,4.0\n"
    assert K.reader_texts(csv_bytes) == [csv_bytes.decode()]
    assert len(K.reader_texts(b'A = "x"\n', source=True)) > 1


def test_k9_fails_a_data_claim_whose_code_hides_the_canary_in_escaped_literals(world):
    """End to end. Fails first: the old data K9 decoded the code as UTF-8 and found nothing (the hex-escaped canary
    shares no 16-character fragment with the source text), so the claim certified."""
    code = KNN.format(k=15, pre=f'_unused = "{_escaped(CANARY)}"', post="")
    subject = world.artifact("good_escaped", code=code)
    claim = world.claim(subject, world.artifact("baseline", "baseline"), n_boot=1000)
    world.kernel.preregister(claim)
    cert = world.kernel.certify(claim, world.evaluate_all(claim))
    assert cert.label == Label.REJECTED and "K9" in cert.failed()
    assert any("subject code" in r and "canary" in r for r in cert.checks["K9"].reasons)


# ================================================================== item 2: Windows path aliases
@pytest.mark.skipif(sys.platform != "win32", reason="Windows device paths")
def test_device_prefix_alias_is_inside(tmp_path):
    """Fails first (no ledger.inside; the old textual is_relative_to treated \\\\?\\C:\\... as another path)."""
    store = tmp_path / "store"
    store.mkdir()
    alias = "\\\\?\\" + str(store / "anchors.txt")
    assert not Path(alias).resolve().is_relative_to(store.resolve())  # what the old check compared
    assert L.inside(alias, store, on_error=False) and L.inside("\\\\.\\" + str(store), store, on_error=False)
    assert not L.inside(tmp_path / "elsewhere.txt", store, on_error=True)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows device paths")
def test_write_anchor_refuses_an_aliased_path_into_the_store(tmp_path, world):
    """Fails first: the old write_anchor accepted \\\\?\\<store>\\anchor.txt (an anchor INSIDE the store)."""
    store = tmp_path / "s"
    store.mkdir()
    with pytest.raises(L.AnchorError, match="outside the store"):
        L.write_anchor(world.kernel.ledger, Path("\\\\?\\" + str(store / "anchor.txt")), store)


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names")
def test_short_name_alias_is_inside(tmp_path):
    """Fails first (no ledger.inside). Skipped when the volume generates no 8.3 names."""
    d = tmp_path / "a_long_directory_name_for_83"
    d.mkdir()
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(d), buf, 1024) or buf.value.lower() == str(d).lower():
        pytest.skip("no 8.3 short name on this volume")
    assert L.inside(Path(buf.value) / "f.txt", d, on_error=False)


def test_inside_returns_the_callers_fail_closed_side_on_os_errors(tmp_path, monkeypatch):
    """The identity test can raise (permissions): each caller chooses the safe answer. Fails first: no inside()."""
    a, b = tmp_path / "x" / "y", tmp_path / "z"
    a.mkdir(parents=True)
    b.mkdir()

    def boom(*_: object) -> bool:
        raise PermissionError("denied")
    monkeypatch.setattr(os.path, "samefile", boom)
    assert L.inside(a, b, on_error=True) is True and L.inside(a, b, on_error=False) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows device paths")
def test_adapter_external_roots_are_compared_by_identity(monkeypatch):
    """A module under an external root spelled with a device prefix is external (not hashed as project code). Fails
    first: the old textual check classified it as project code."""
    import json
    stdlib = Path(json.__file__).resolve().parents[1]
    monkeypatch.setattr(adapters, "_external_roots", lambda: (Path("\\\\?\\" + str(stdlib)),))
    monkeypatch.setattr(adapters, "_is_external", adapters._is_external.__wrapped__)  # bypass the per-process cache
    assert adapters._source_file("json") is None


# ================================================================== item 3: equivalence (TOST) bounded nulls
def equivalence(w: World, delta: float, *, targets=("test",), **plan):
    c = w.claim(w.artifact("noisy_majority"), w.artifact("baseline", "baseline"), kind=ClaimKind.EQUIVALENCE,
                delta=delta, targets=targets, probability=0.6, n_boot=1000, **plan)
    return render(c.model_copy(update={"effect": Effect(min_effect=delta, predicted_lo=-0.05, predicted_hi=0.05,
                                                        probability=0.6)}))


def test_honest_equivalence_with_a_proven_power_is_certified_with_a_tost_statement(world):
    """Fails first: equivalence claims always failed K7 (K7_NO_RISK_MODEL) and K1 refused a declared power."""
    claim = equivalence(world, 0.1, power=0.8)
    world.kernel.preregister(claim)
    cert = world.kernel.certify(claim, world.evaluate_all(claim))
    assert cert.label == Label.CERTIFIED_INTERNAL, (cert.failed(), cert.checks["K7"].reasons)
    ex = cert.resolution["exclusion"]
    assert ex["sided"] == ml.SIDED_TOST and ex["excluded_effect"] == 0.1 and ex["plantings"] == 11
    assert ex["level"] == cert.resolution["targets"]["test"]["alpha_eff"] and ex["reported_limit"] == 0.1
    assert ex["observed_limit"] < 0.1 and ex["proven_power"] >= 0.8
    assert any("TOST" in o and "on each side" in o for o in cert.checks["K7"].observations)
    assert world.kernel.verify(cert.cert_hash()) == []


def test_equivalence_with_a_vacuous_margin_is_refused(world):
    """delta 0.9: neither arm can gain 0.9 accuracy, so no effect of +-0.9 can be planted. Fails first only in the
    code: old K7 failed with K7_NO_RISK_MODEL (and K1 with K1_POWER), not K7_NOT_PLANTABLE."""
    claim = equivalence(world, 0.9, power=0.8)
    world.kernel.preregister(claim)
    cert = world.kernel.certify(claim, world.evaluate_all(claim))
    assert cert.failed() == ["K7"] and Code.K7_NOT_PLANTABLE in cert.checks["K7"].codes


def test_equivalence_on_an_insensitive_target_is_refused(world):
    """5 examples cannot see +-0.15 (15 can: see the fixture). Fails first: the old code failed K1 (power) and K7 (no
    risk model) instead."""
    claim = equivalence(world, 0.15, targets=("tiny",), power=0.8)
    world.kernel.preregister(claim)
    cert = world.kernel.certify(claim, world.evaluate_all(claim))
    assert "K7" in cert.failed() and "K1" not in cert.failed()
    assert set(cert.checks["K7"].codes) <= {Code.K7_INSENSITIVE, Code.K7_NOT_PLANTABLE}


def test_the_minus_side_is_planted_into_the_comparator():
    """side = -1 improves the comparator (subject - comparator falls by delta); nothing is degraded. A comparator that
    is already perfect cannot gain: NOT_PLANTABLE on the minus side only. Fails first: plasmode had no `side`."""
    labels = [str(i % 3) for i in range(60)]
    perfect, poor = list(labels), ["0"] * 60
    st = ml.TargetStats(Interval(0.0, 0.0, 0.0, 3, 0.1, "t", degenerate=True),
                        Interval(0.0, -0.05, 0.05, 60, 0.1, "bca"), alpha_eff=0.05, met=True)
    minus = ml.plasmode("accuracy", True, labels, [poor] * 3, [perfect] * 3, 0.1, st, 1000, 2, b"s", side=-1)
    assert minus is not None and minus[0] == Code.K7_NOT_PLANTABLE and "-0.1" in minus[1]
    plus = ml.plasmode("accuracy", True, labels, [perfect] * 3, [poor] * 3, 0.1, st, 1000, 2, b"s", side=1)
    assert plus is not None and plus[0] == Code.K7_NOT_PLANTABLE  # the perfect subject cannot gain either


# ================================================================== item 4: the data-class scope vocabulary
def test_a_data_statement_must_be_the_ml_rendering(world):
    """Fails first: the old K1 never read a data claim's statement (prose rode on certificates)."""
    good = _good_claim(world)
    assert good.statement == ml.scope_phrase(good) and Code.K1_STATEMENT not in k1(world, good).codes
    prose = good.model_copy(update={"statement": good.statement + "; it cures every dataset"})
    assert Code.K1_STATEMENT in k1(world, prose).codes
    stale = good.model_copy(update={"effect": good.effect.model_copy(update={"min_effect": 0.3})})  # not re-rendered
    assert Code.K1_STATEMENT in k1(world, stale).codes


def test_scope_conditions_other_than_provenance_notes_are_refused(world):
    """Fails first: data claims could carry any scope condition ('population: all patients')."""
    good = _good_claim(world)
    wide = good.model_copy(update={"scope": Scope(datasets=good.scope.datasets, conditions={"population": "all"})})
    assert Code.K1_SCOPE in k1(world, wide).codes
    noted = good.model_copy(update={"scope": Scope(datasets=good.scope.datasets, conditions={"paper_doi": "10.1/x"})})
    assert Code.K1_SCOPE not in k1(world, noted).codes  # an audit provenance note, never scope


def test_scope_datasets_must_be_targets_or_development(world):
    """Fails first: scope.datasets could name any registered dataset the claim never tested ('train_small')."""
    good = _good_claim(world)
    wider = good.model_copy(update={"scope": Scope(datasets=(*good.scope.datasets, "train_small"))})
    assert Code.K1_SCOPE in k1(world, wider).codes


def test_identifiers_rendered_into_the_statement_must_be_plain(world):
    """An artifact id is claimant-chosen text; ids with spaces or newlines would carry prose into the rendering.
    Fails first: no id rule."""
    prose_id = world.artifact("cures cancer\nreally", code="print(1)\n")
    c = render(world.claim(prose_id, world.artifact("baseline", "baseline"), n_boot=1000))
    assert Code.K1_STATEMENT in k1(world, c).codes


def test_the_rendering_is_exact_and_never_rounds():
    """min_effect by repr: 0.1 + 0.2 is not rendered as 0.3. Fails first: no scope_phrase."""
    from custom_harness.kernel import ArtifactRef, Claim, MetricRef
    ref = ArtifactRef(artifact_id="m", hash="1" * 64)
    c = Claim(kind=ClaimKind.NEGATIVE_RESULT, statement="", subject=ref, comparator=ArtifactRef(artifact_id="b",
              hash="2" * 64), metric=MetricRef(id="rmse", higher_is_better=False),
              effect=Effect(min_effect=0.1 + 0.2, predicted_lo=0.0, predicted_hi=0.0, probability=0.5),
              scope=Scope(datasets=("t",)), development=(), generalization_targets=("t", "u"),
              analysis_plan=AnalysisPlan(seeds=(0, 1, 2)), resolve_by=1.0)
    phrase = ml.scope_phrase(c)
    assert "does not beat b by 0.30000000000000004 or more in rmse (lower is better)" in phrase
    assert "held-out test examples of t, u;" in phrase


# ================================================================== item 5: unknown cost is never $0
def test_a_subscription_cost_is_rendered_unknown_not_zero(world):
    """Fails first: the old checklist rendered '$0.0000' for a BYOS run (usd recorded 0.0 = not reported)."""
    c = _good_claim(world, seeds=(5, 6, 7))
    cert = world.kernel.certify(c, [])  # unregistered: a REJECTED certificate is enough to fill the checklist
    out = fill(cert, run_costs={"r": Cost(prompt_tokens=10, quota_units=2.0)}, models=["codex (byos)"],
               bundle_hash=None, selection_policy=None, human_in_the_loop=None, novelty=None)["cost"]
    assert out is not None and "$0" not in out and "USD unknown / UNCALIBRATED" in out
    paid = fill(cert, run_costs={"r": Cost(usd=0.25, estimated=True)}, models=[], bundle_hash=None,
                selection_policy=None, human_in_the_loop=None, novelty=None)["cost"]
    assert paid is not None and "$0.2500 (estimated)" in paid


# ================================================================== item 6: truth-copy declaration hook
def test_declare_truth_copy_is_idempotent_and_reads_a_real_csv_header(tmp_path):
    """Fails first: the old hook appended a second identical entry on every suite registration, and split the header
    on ',' (a quoted column name with a comma was refused)."""
    w = World(tmp_path / "w", seed=5, n_train=30, n_test=30)
    try:
        w.vault.register_dataset("pub", DatasetRole.DEV, public={"f.csv": b'id,"y, coded"\n1,0\n'}, private={})
        first = w.kernel.declare_truth_copy("test", "pub", "f.csv", "y, coded", declarer="task-registration")
        again = w.kernel.declare_truth_copy("test", "pub", "f.csv", "y, coded", declarer="someone else")
        assert again == first and len(w.kernel.ledger.entries("truth_copy")) == 1
        with pytest.raises(KernelError):
            w.kernel.declare_truth_copy("test", "pub", "f.csv", "y", declarer="task-registration")
    finally:
        w.close()


# ================================================================== item 7: K1 tolerance evidence
def _floor() -> T.NoiseFloor:
    return T.NoiseFloor(0.8, tuple(0.8 + 0.001 * i for i in range(1, 10)),
                        tuple(0.8 - 0.0005 * i for i in range(1, 10)), (), ())


def test_a_tolerance_needs_a_noise_floor_record_that_justifies_it(world):
    """Fails first: the old K1 accepted any reproduce_tolerance in [0, 0.02] with no evidence (a hand-set 0.02
    loosens K3, the s3-verifiers drift test), and AnalysisPlan had no tolerance_evidence field."""
    got = T.claim_tolerance(_good_claim(world).evidence_class, _floor()).value
    assert got is not None and 0 < got < 0.02
    ref = world.kernel.record_noise_floor(_floor())
    assert Code.K1_TOLERANCE in k1(world, _good_claim(world, reproduce_tolerance=got)).codes  # nothing cited
    assert Code.K1_TOLERANCE not in k1(world, _good_claim(world, reproduce_tolerance=got,
                                                          tolerance_evidence=ref)).codes
    assert Code.K1_TOLERANCE not in k1(world, _good_claim(world, reproduce_tolerance=got / 2,
                                                          tolerance_evidence=ref)).codes  # stricter is fine
    assert Code.K1_TOLERANCE in k1(world, _good_claim(world, reproduce_tolerance=min(0.02, got * 2),
                                                      tolerance_evidence=ref)).codes  # inflated
    assert Code.K1_TOLERANCE not in k1(world, _good_claim(world)).codes  # 0.0 needs nothing


@pytest.mark.parametrize("doc", [
    {"format": "chh-noise-floor/1", "reference": 0.8, "nominal": [True] * 9, "perturbed": [0.8] * 9, "rebuild": [],
     "seed": []},
    {"format": "chh-noise-floor/1", "reference": 1, "nominal": [0.81] * 9, "perturbed": [0.8] * 9, "rebuild": [],
     "seed": []},
    {"format": "other/1", "reference": 0.8, "nominal": [0.81] * 9, "perturbed": [0.8] * 9, "rebuild": [], "seed": []},
    {"format": "chh-noise-floor/1", "reference": 0.8, "nominal": [0.8] * 9, "perturbed": [0.8] * 9, "rebuild": [],
     "seed": []},  # identical nominal re-runs: the exact check stays, so no tolerance > 0
    ["not", "a", "record"],
])
def test_a_malformed_or_strict_noise_floor_record_justifies_no_tolerance(world, doc):
    """Bools, ints, another format, a deterministic floor, a non-object: each refused. Fails first (no check)."""
    from custom_harness.kernel import canonical_json
    ref = world.store.blobs.put(canonical_json(doc))
    c = _good_claim(world, reproduce_tolerance=0.005, tolerance_evidence=ref)
    assert Code.K1_TOLERANCE in k1(world, c).codes


# ================================================================== item 9: the adapter hash is cached safely
def test_adapter_hash_is_cached_and_recomputed_after_an_edit(tmp_path, monkeypatch):
    """Fails first: the old adapter_hash walked and hashed the import closure on every call (calls == 2 below)."""
    monkeypatch.syspath_prepend(str(tmp_path))
    mod = tmp_path / "s4k_cache_probe_mod.py"
    mod.write_text("X = 1\n", encoding="utf-8")
    spec = adapters.AdapterSpec(name="probe", version="1", classes=frozenset(), modules=("s4k_cache_probe_mod",),
                                metrics=frozenset())
    calls = []
    real = adapters.covered_modules
    monkeypatch.setattr(adapters, "covered_modules", lambda s: calls.append(1) or real(s))
    first = adapters.adapter_hash(spec)
    n = len(calls)
    assert adapters.adapter_hash(spec) == first and len(calls) == n  # a hit: nothing walked again
    mod.write_text("X = 22\n", encoding="utf-8")
    st = mod.stat()
    os.utime(mod, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert adapters.adapter_hash(spec) != first  # the edit is seen


def test_k1_accepts_a_declared_power_on_equivalence_claims(world):
    """Item 3 at K1. Fails first: K1_POWER was raised for any equivalence claim with a power."""
    c = equivalence(world, 0.1, power=0.8)
    assert Code.K1_POWER not in k1(world, c).codes
    assert Code.K1_POWER in k1(world, equivalence(world, 0.1, power=0.99)).codes



# ================================================================== s4-station: run-bound prereg/certificate entries
def test_a_run_bound_kernel_records_its_run_on_prereg_and_certificate_entries(tmp_path):
    """Fails first: neither entry named a run, so a concurrent process writing to the same store could have its
    certificates credited to another agent's turn (s4-station red team, HIGH). Unbound callers add no key (old entries
    keep their bytes and hashes), and the run comes from the binding only: no Kernel method takes a run id."""
    import inspect

    from custom_harness.kernel import Kernel
    from custom_harness.records import Run
    from custom_harness.studio.kill import RunGuardedKernel
    w = World(tmp_path / "w", seed=6, n_train=30, n_test=30)
    try:
        c = _good_claim(w)
        w.kernel.preregister(c)
        assert "run_id" not in w.kernel.ledger.entries("prereg")[-1].body
        w.kernel.certify(c, [])
        assert "run_id" not in w.kernel.ledger.entries("certificate")[-1].body
        run = Run(harness_id="h", harness_hash="h", task_id="t", seed=0)
        w.store.add_run(run)
        bound = RunGuardedKernel(w.store, run_id=run.id)
        c2 = _good_claim(w, seeds=(3, 4, 5))
        assert bound.preregister(c2).body["run_id"] == run.id
        bound.certify(c2, [])
        assert bound.ledger.entries("certificate")[-1].body["run_id"] == run.id
        for method in (Kernel.preregister, Kernel.certify, Kernel.record_certificate, Kernel.record_evaluation):
            assert "run_id" not in inspect.signature(method).parameters
    finally:
        w.close()


# ================================================================== s4-kerneld #7: keyed digests of private files
def test_the_ledger_names_private_files_by_a_keyed_digest_only(tmp_path):
    """Fails first: the register_dataset entry carried the plain sha256 of every private file, so anyone reading the
    ledger could confirm a guessed (low-entropy) label file offline by hashing guesses."""
    import hashlib

    from custom_harness.kernel.vault import KEYED
    w = World(tmp_path / "w", seed=8, n_train=30, n_test=30)
    try:
        labels = b"label\n0\n1\n1\n0\n"
        w.vault.register_dataset("small", DatasetRole.TEST, public={"x.csv": b"x\n1\n2\n3\n4\n"},
                                 private={"y.csv": labels})
        body = next(e.body for e in w.kernel.ledger.entries("register_dataset") if e.body["dataset_id"] == "small")
        plain = hashlib.sha256(labels).hexdigest()
        assert plain not in str(body) and body["files"]["y.csv"]["blob"].startswith(KEYED)
        assert w.vault.dataset("small").files["y.csv"].blob == plain  # the Vault resolves it internally
        again = w.vault.register_dataset("small", DatasetRole.TEST, public={"x.csv": b"x\n1\n2\n3\n4\n"},
                                         private={"y.csv": labels})  # idempotent: the same keyed record
        assert again.hash == body["hash"]
        assert str(Path(w.store.root) / "private" / "vault.key") not in str(body)
    finally:
        w.close()


def test_a_private_file_registered_before_keyed_digests_still_resolves(tmp_path):
    """Old entries keep verifying: a register_dataset body with a plain sha256 (as before s4) is read as before."""
    from custom_harness.kernel.vault import DatasetRecord, FileRef, dataset_hash
    w = World(tmp_path / "w", seed=9, n_train=30, n_test=30)
    try:
        blob = w.store.blobs.put(b"label\n1\n0\n")
        files = {"y.csv": FileRef(blob=blob, private=True)}
        rec = DatasetRecord(dataset_id="legacy", role=DatasetRole.TEST, files=files, hash=dataset_hash(files))
        w.kernel.ledger.append("register_dataset", rec.model_dump(mode="json", exclude={"seq"}))
        assert w.vault.dataset("legacy").files["y.csv"].blob == blob
        w.vault.register_dataset("legacy", DatasetRole.TEST, public={}, private={"y.csv": b"label\n1\n0\n"})  # same
    finally:
        w.close()


# ================================================================== s4-kerneld #15: private bytes as code
def test_a_private_file_of_another_dataset_used_as_code_is_a_coded_k9_finding(world):
    """Fails first: K9 only knew the TARGETS' private files; a non-target's private bytes given as code were not a
    K9 finding (and the reviewer saw such a case fail as a raised check, CHECK_RAISED, not with a closed code)."""
    labels = "label\n" + "\n".join(world.labels["test2"]) + "\n"
    stolen = world.artifact("stolen_labels", code=labels)
    claim = world.claim(stolen, world.artifact("baseline", "baseline"), n_boot=1000, seeds=(11, 12, 13))
    cx = K.Context(claim=claim, claim_hash=claim.claim_hash(), store=world.store, vault=world.vault,
                   ledger=world.kernel.ledger, config=KernelConfig(), replayer=None, evidence_ids=())
    res = K.k9_leaks(cx)
    assert Code.K9_PRIVATE_CODE in res.codes and Code.CHECK_RAISED not in res.codes
