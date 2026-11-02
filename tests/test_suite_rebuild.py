"""Deterministic, verifiable rebuild of the live suite (GOAL §16.1-16.3): the suite manifest is hashed, recorded in
the ledger and anchored BEFORE any task data exist; every task's data then come from keyed streams under
draw("heldout_mint", manifest hash, sealed beacon). A second run reproduces every file byte for byte, and a verify
mode re-derives the suite and compares. Nothing secret (beacon, master key, task keys) is written where agents read.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

import pytest
import yaml

from custom_harness.commitment import as_key, draw, secret_leak_scan
from custom_harness.kernel import Kernel
from custom_harness.research import rebuild as R
from custom_harness.research import suite as S

BEACON = "sealed-beacon-" + "ab" * 24
PREFIX = Path(__file__).parent / "fixtures" / "suite_prefix_401c00a.py"  # the pre-fix seeded generators (attacker)


def _fake_real() -> dict[str, dict]:
    """Small stand-ins for the five real datasets (the trusted exporter's output format)."""
    out = {}
    for k, name in enumerate(S.REAL):
        is_cls = S.REAL[name][1] != "rmse"
        out[name] = {"columns": [f"c{j}" for j in range(4)],
                     "X": [[round(100 * k + i + j / 10, 6) for j in range(4)] for i in range(60)],
                     "y": [i % 3 if is_cls else float(i) / 7 for i in range(60)]}
    return out


@pytest.fixture
def env(tmp_path, store, monkeypatch):
    # the agent-readable root is the data dir's suites/ (conftest: CHH_DATA_DIR = tmp_path/data), a MAPPED subdir of
    # truth_layout: a suite built there keeps its truth under <data>/private/suites/..., outside the agent's reach.
    # (stage-c 2026-10-23: the first draft used an unmapped tmp_path/agent, where truth_layout rightly refused to put
    # held-out truth (TruthLayoutError) and every rebuild test failed before reaching what it tests.)
    agent = tmp_path / "data" / "suites"
    agent.mkdir(parents=True)
    monkeypatch.setenv("CHH_AGENT_READABLE", str(agent))
    record = tmp_path / "human" / "suite-m2"
    beacon = tmp_path / "human" / "beacon.txt"
    beacon.parent.mkdir(parents=True)
    beacon.write_text(BEACON + "\n", encoding="utf-8")
    return {"agent": agent, "record": record, "beacon": beacon, "vault": Kernel(store).vault, "store": store}


def _rebuild(env, out: Path, real=None):
    return R.rebuild(out, record_dir=env["record"], beacon_file=env["beacon"], vault=env["vault"],
                     store_root=env["store"].root, real_data=real)


def _labels(p: Path) -> list[str]:
    return [r[0] for r in list(csv.reader(io.StringIO(p.read_text(encoding="utf-8"))))[1:]]


# ---------------------------------------------------------------- determinism of the generator itself
def test_build_suite_with_the_same_key_is_byte_identical(tmp_path):
    """Canaries used to be fresh entropy even when the key was given, so no rebuild was reproducible."""
    a = S.build_suite(tmp_path / "a", real_data=_fake_real(), key=b"q" * 32).parent
    b = S.build_suite(tmp_path / "b", real_data=_fake_real(), key=b"q" * 32).parent
    assert R.suite_digest(a) == R.suite_digest(b)
    c = S.build_suite(tmp_path / "c", real_data=_fake_real(), key=b"r" * 32).parent
    assert R.suite_digest(a) != R.suite_digest(c)


def test_every_task_records_its_provenance_and_the_candidate_image_but_no_key(tmp_path):
    from custom_harness.sandbox.docker import CANDIDATE_IMAGE

    master = b"q" * 32
    path = S.build_suite(tmp_path / "s", real_data=_fake_real(), key=master)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert len(doc["tasks"]) == 21
    for t in doc["tasks"]:
        p = t["provenance"]
        assert t["image"] == CANDIDATE_IMAGE
        assert p["generator_version"] == S.GENERATOR_VERSION
        key = S.task_key(master, t["source"] if t["family"] in ("real", "repair") else t["id"])
        assert p["key_commitment"] == hashlib.sha256(key).hexdigest()
        assert p["generator"].split(":")[0] in ("synthetic", "real", "repair")
        if t["family"] in ("real", "repair"):
            assert p["dataset_sha256"] == R.dataset_hash(_fake_real()[t["source"]])
        # neither the master key nor any task key is written anywhere in the suite
        assert secret_leak_scan(key, [path.parent]) == []
    assert secret_leak_scan(master, [path.parent]) == []


# ---------------------------------------------------------------- freeze, then mint
def test_rebuild_anchors_the_manifest_before_minting_and_derives_the_data_from_the_draw(env, tmp_path):
    out = env["agent"] / "suites" / "m2"
    res = _rebuild(env, out, _fake_real())
    assert res["status"] == "built"
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    h = R.manifest_hash(manifest)
    assert h == res["manifest_sha256"]
    led = env["vault"].ledger.entries()
    named = [e for e in led if e.body.get("hash") == h]
    assert len(named) == 1 and named[0].body["kind"] == "suite_manifest"
    anchors = [json.loads(ln) for ln in (env["record"] / "anchor.jsonl").read_text().splitlines() if ln.strip()]
    assert max(a["length"] for a in anchors) >= named[0].seq  # anchored
    # the data are exactly the generator's output under the drawn master key
    master = as_key(draw("heldout_mint", prereg_hash=h, beacon=BEACON, secret=None))
    ref = S.build_suite(tmp_path / "ref", real_data=_fake_real(), key=master,
                        generator_extra=R.generator_block(manifest, h)).parent
    assert R.task_digest(out) == R.task_digest(ref)
    # the manifest fixed every split before the data existed
    doc = yaml.safe_load((out / "suite.yaml").read_text(encoding="utf-8"))
    assert {t["id"]: t["split"] for t in doc["tasks"]} == {t["id"]: t["split"] for t in manifest["tasks"]}
    # no secret where agents read: not the beacon, not the master key
    assert secret_leak_scan(BEACON.encode(), [env["agent"]]) == []
    assert secret_leak_scan(master, [env["agent"]]) == []


def test_rebuild_mints_nothing_when_the_freeze_is_not_anchored(env, tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise S.MintRefused("not anchored (test)")

    monkeypatch.setattr(R, "require_anchored", refuse)
    out = env["agent"] / "suites" / "m2"
    with pytest.raises(S.MintRefused):
        _rebuild(env, out, _fake_real())
    assert not out.exists() or not any(out.rglob("test_y.csv"))


def test_second_rebuild_is_identical_and_adds_no_ledger_entry_or_anchor(env, tmp_path):
    out = env["agent"] / "suites" / "m2"
    first = _rebuild(env, out, _fake_real())
    n_led = len(env["vault"].ledger.entries())
    n_anchor = len((env["record"] / "anchor.jsonl").read_text().splitlines())
    digest = R.suite_digest(out)
    second = _rebuild(env, out, _fake_real())
    assert second["status"] == "unchanged" and second["digest_sha256"] == first["digest_sha256"]
    assert R.suite_digest(out) == digest
    assert len(env["vault"].ledger.entries()) == n_led
    assert len((env["record"] / "anchor.jsonl").read_text().splitlines()) == n_anchor


def test_rebuild_never_overwrites_a_different_suite(env, tmp_path):
    out = env["agent"] / "suites" / "m2"
    S.build_suite(out, real_data=None, key=b"z" * 32)
    before = R.suite_digest(out)
    with pytest.raises(R.RebuildRefused, match="differs"):
        _rebuild(env, out, _fake_real())
    assert R.suite_digest(out) == before


def test_verify_re_derives_the_suite_and_finds_a_changed_label(env, tmp_path):
    out = env["agent"] / "suites" / "m2"
    _rebuild(env, out, _fake_real())
    ok = R.verify(out, record_dir=env["record"], beacon_file=env["beacon"],
                  ledger_entries=env["vault"].ledger.entries())
    assert ok["problems"] == [], ok
    from custom_harness.truth_layout import truth_file
    y = truth_file(out / "xor-s1", "test_y.csv")  # where the layout keeps it (outside the agent-readable tree)
    assert not (out / "xor-s1" / "private").exists()
    labels = _labels(y)
    labels[0] = "1" if labels[0] == "0" else "0"
    y.write_text("label\n" + "".join(v + "\n" for v in labels), encoding="utf-8")
    bad = R.verify(out, record_dir=env["record"], beacon_file=env["beacon"],
                   ledger_entries=env["vault"].ledger.entries())
    assert any("xor-s1/private/test_y.csv" in p for p in bad["problems"]), bad


def test_verify_refuses_a_wrong_beacon(env, tmp_path):
    out = env["agent"] / "suites" / "m2"
    _rebuild(env, out, _fake_real())
    other = tmp_path / "human" / "other.txt"
    other.write_text("not-the-beacon-" + "cd" * 24, encoding="utf-8")
    res = R.verify(out, record_dir=env["record"], beacon_file=other, ledger_entries=env["vault"].ledger.entries())
    assert any("beacon" in p for p in res["problems"]), res


@pytest.mark.parametrize("where", ["record", "beacon"])
def test_rebuild_refuses_secret_material_inside_an_agent_readable_root(env, tmp_path, where):
    if where == "record":
        env["record"] = env["agent"] / "record"
    else:
        b = env["agent"] / "beacon.txt"
        b.write_text(BEACON, encoding="utf-8")
        env["beacon"] = b
    with pytest.raises(R.RebuildRefused, match="agent-readable"):
        _rebuild(env, tmp_path / "out", _fake_real())


def test_rebuild_refuses_real_data_that_differ_from_the_recorded_export(env, tmp_path):
    out = env["agent"] / "suites" / "m2"
    _rebuild(env, out, _fake_real())
    changed = _fake_real()
    changed["iris"]["y"][0] = 2 if changed["iris"]["y"][0] != 2 else 1
    with pytest.raises(R.RebuildRefused, match="real-data export"):
        _rebuild(env, env["agent"] / "suites" / "m2b", changed)


def test_cli_frozen_rebuild_then_verify(env, tmp_path, capsys):
    from custom_harness.cli import main

    export = tmp_path / "human" / "export.json"
    export.write_text(json.dumps(_fake_real()), encoding="utf-8")
    out = env["agent"] / "suites" / "m2"
    args = ["build-suite", str(out), "--record-dir", str(tmp_path / "human" / "rec"),
            "--beacon-file", str(env["beacon"])]
    assert main([*args, "--frozen", "--real-export", str(export)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "built"
    assert main([*args, "--frozen", "--real-export", str(export)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "unchanged"
    assert main([*args, "--verify"]) == 0
    assert json.loads(capsys.readouterr().out)["problems"] == []
    with pytest.raises(SystemExit, match="agent-readable"):
        main(["build-suite", str(out), "--frozen", "--record-dir", str(env["agent"] / "rec"),
              "--beacon-file", str(env["beacon"]), "--real-export", str(export)])


# ---------------------------------------------------------------- the COUPLED-PRNG regression on a rebuilt suite
def test_the_old_seeded_generators_cannot_regenerate_rebuilt_labels(env, tmp_path):
    out = env["agent"] / "suites" / "m2"
    _rebuild(env, out, _fake_real())
    old_code = R.load_generator_module(PREFIX)
    res = R.prng_regression(out, old_code)
    assert len(res) == 12
    assert all(r["exact"] is False for r in res.values()), res
    # positive control: the same attacker regenerates a suite built with the pre-fix seeded generators exactly
    old = tmp_path / "old"
    for gen, seed, n in old_code.SYNTHETIC:
        old_code.write_task(gen(seed, n), old)
    ctrl = R.prng_regression(old, old_code)
    assert len(ctrl) == 12 and all(r["exact"] for r in ctrl.values()), ctrl
