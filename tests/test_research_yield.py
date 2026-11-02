"""research_v2 (GOAL §16.8): execution as ranker, packed implement calls, tie-aware selection, deny-only preflight."""

import json
from pathlib import Path

import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.gateway import MockBackend
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from test_research import KNN_RADIUS, rings_task, scripted_llm  # noqa: F401  (fixture re-export)

ROOT = Path(__file__).resolve().parents[1]
SPEC_V2 = ROOT / "examples/research_v2.yaml"
# the same method with a cosmetic difference: identical dev behaviour, more code, so tie-aware selection skips it
KNN_VERBOSE = "import time\ntime.sleep(1.0)\n" + KNN_RADIUS +"\n# " + "padding " * 40 + "\n"


def packed_llm(req):
    text = req.messages[-1].content
    if "Propose 5 DISTINCT hypotheses" in text:
        return json.dumps({"hypotheses": [
            {"id": "h_verbose", "idea": "kNN on radius, verbose", "rationale": "rings"},
            {"id": "h_radius", "idea": "kNN on radial feature", "rationale": "classes are rings"},
            {"id": "h_noise", "idea": "drop noise columns", "rationale": "n1..n4 are noise"}]})
    if "Implement EACH of these hypotheses" in text:
        return json.dumps({"solutions": [
            {"id": "h_verbose", "code": KNN_VERBOSE, "notes": "a"},
            {"id": "h_radius", "code": KNN_RADIUS, "notes": "b"},
            {"id": "h_noise", "code": "", "notes": "malformed member"}]})
    if "Rank ALL of these hypotheses" in text:
        raise AssertionError("research_v2 must not call the tournament")
    return scripted_llm(req)


def _engine(store, tmp_path, llm):
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC_V2.read_text(encoding="utf-8")))
    return Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                  sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True), allow_same_family=True)


def test_v2_packs_ranks_by_execution_and_selects_tie_aware(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    llm = MockBackend(packed_llm)
    res = _engine(store, tmp_path, llm).run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    prompts = [r.messages[-1].content for r in llm.requests]
    assert sum("Implement EACH of these hypotheses" in p for p in prompts) == 1  # one call wrote the scripts
    assert not any("Implement this hypothesis" in p for p in prompts)
    assert s["pack_failures"] == [{"hypothesis_id": "h_noise", "reason": "empty or non-string code"}]
    ids = [a["hypothesis_id"] for a in s["attempts"]]
    assert ids[:2] == ["h_verbose", "h_radius"]
    # both scored identically on dev; the smaller/faster equal candidate is selected, not the first maximum
    assert s["attempts"][0]["dev_score"] == s["attempts"][1]["dev_score"]
    assert s["selection"]["rule"] == "tie_aware" and set(s["selection"]["tied"]) >= {1, 2}
    assert s["best"]["hypothesis_id"] in ("h_radius", "improve-1", "improve-2")
    assert s["preflight"]["verdict"] == "no objection"
    assert s["ensemble"]["used"] is False
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL", s["certificate"]


def test_v2_preflight_denial_registers_nothing_and_reads_no_label(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    import custom_harness.research.preflight as pf
    monkeypatch.setattr(pf, "DEFAULT_CONFIG", pf.PreflightConfig(theta=1.01, n_sims=20))  # force a denial
    monkeypatch.setattr(pf.preflight, "__defaults__", (pf.DEFAULT_CONFIG,))
    res = _engine(store, tmp_path, MockBackend(packed_llm)).run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    assert s["preflight"]["verdict"] == "deny" and "claim" not in s and "certificate" not in s
    from custom_harness.kernel import Kernel
    k = Kernel(store)
    assert k.ledger.entries("prereg") == []
    test_id = s["datasets"]["test"]
    reads = [a.body.get("purpose") for a in k.vault.accesses(test_id) if a.body.get("private")]
    # no scoring read of the held-out labels; the only private read is the audit's leak-fingerprint scan (I5),
    # which every run makes and which never scores or models (K2 lists it apart)
    assert set(reads) <= {"leakscan"}, reads
    assert "certifiability preflight" in s["report"]
