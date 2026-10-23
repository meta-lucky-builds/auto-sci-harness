"""Campaign-scope cassette replay (GOAL §16.5.5): the decisive check that the release log is complete.

A small real campaign (one evolution generation over evolve + gate tasks; every research run reads its task's
private labels through the vault, scores a constant predictor and asks an LLM for a summary) is recorded with real
labels, then replayed in a fresh store with FLIPPED labels, cassette-served model calls and pinned releases.
- Honest harness: 0 cassette misses, 0 decision changes, every pin used, although the flipped labels really change
  every verdict (asserted: the attack surface is live).
- Leaky harness A (a module copies a label-derived number into a prompt, no release): flagged.
- Leaky harness B (the pre-embargo continuous held-out score term re-introduced): flagged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _kerneld_standin import kerneld_vault
from custom_harness.evolve import evaluate as ev_mod
from custom_harness.evolve.evaluate import SuiteTask
from custom_harness.evolve.loop import Evaluator, EvolveConfig, evolve_generation
from custom_harness.gateway import BackendError, MockBackend
from custom_harness.kerneld.campaign import (
    RecordingBackend,
    ReplayBackend,
    fake_labels,
    load_cassette,
    replay_report,
)
from custom_harness.kerneld.releases import Releases
from custom_harness.runtime import HarnessSpec, register
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore

LABELS = ["1"] * 16 + ["0"] * 4  # a constant "1" predictor: 0.8 real, 0.2 after flipping


@register("test.campaign_score")
def _score(state, params, ctx):
    from custom_harness.kernel import DatasetRole
    task_dir = Path(state["input"]["task_dir"])
    raw = (task_dir / "private" / "y.csv").read_bytes()
    ds = "held-" + __import__("hashlib").sha256(raw).hexdigest()[:12]
    with kerneld_vault(ctx) as vault:  # kerneld's private ingest + logged read (tests/_kerneld_standin.py)
        if ds not in vault.datasets():
            vault.register_dataset(ds, DatasetRole.TEST, public={"x.csv": b"x\n1\n"}, private={"y.csv": raw})
        labels = vault.read(ds, "y.csv", purpose="leakscan", run_id=ctx.run_id).decode().split()[1:]
    acc = sum(v == "1" for v in labels) / len(labels)
    out = {"audit": {"passed": True}, "best": {"dev_score": 0.8}, "baseline_dev": 0.7, "task": {"metric": "accuracy"},
           "certificate": {"label": "CERTIFIED-INTERNAL" if acc > 0.5 else "REJECTED", "subject_mean": acc,
                           "comparator_mean": 0.5},
           "hint": f"{acc:.3f}" if params.get("leak") else "none"}
    return out


def _spec(leak: bool) -> HarnessSpec:
    return HarnessSpec.model_validate({
        "id": "camp", "entry": "score",
        "nodes": {"score": {"kind": "module", "module": "test.campaign_score", "params": {"leak": leak}},
                  "ask": {"kind": "llm", "role": "executor", "prompt": "Summarise the run. Note: {{hint}}",
                          "output_key": "summary"}},
        "edges": [{"source": "score", "target": "ask"}]})


def _tasks(root: Path) -> list[SuiteTask]:
    out = []
    for name, split, fam in (("e0", "evolve", "A"), ("e1", "evolve", "A"), ("g0", "gate", "A"), ("h0", "gate", "H")):
        d = root / name
        (d / "private").mkdir(parents=True)
        (d / "private" / "y.csv").write_text("label\n" + "\n".join(LABELS) + "\n", encoding="utf-8", newline="")
        out.append(SuiteTask(name, d, split, family=fam))
    return out


def _decline(req):
    return json.dumps({"edits": [], "decline": "no supported edit"})


def _campaign(root: Path, tasks, spec, backend, proposer, releases) -> list:
    store = EvidenceStore(root / "store")
    from custom_harness.kernel import Kernel
    Kernel(store).approve_adapter("ml", approver="pytest")
    evaluator = Evaluator({"executor": backend}, store,
                          lambda k: LocalSandbox(root / "ws" / k, i_understand_no_isolation=True), tasks,
                          releases=releases)
    try:
        res = evolve_generation(spec, tasks, evaluator=evaluator, proposer=proposer, store=store,
                                cfg=EvolveConfig(seeds=(0,), n_edits=1, race_rounds=1))
    except BackendError:  # a proposer cassette miss: the generation could not repeat (flagged by the miss)
        labels = sorted((t, o.cert_label) for (_, t, _s), o in evaluator.cache.items())
        store.close()
        return [["generation aborted"], labels]
    decisions = [{"accepted": res.accepted, "decision": res.decision},
                 [{k: c.get(k) for k in ("status", "reason")} for c in res.candidates],
                 sorted((t, s, o.score) for (_, t, s), o in evaluator.cache.items())]
    labels = sorted((t, o.cert_label) for (_, t, _s), o in evaluator.cache.items())
    store.close()
    return [decisions, labels]


def _record_and_replay(tmp_path, leak: bool):
    tasks = _tasks(tmp_path / "tasks")
    cas = tmp_path / "cassette.jsonl"
    rec = Releases(None, mode="record")
    llm = RecordingBackend(MockBackend(lambda r: "ok"), cas)
    prop = RecordingBackend(MockBackend(_decline), cas)
    recorded, real_labels = _campaign(tmp_path / "rec", tasks, _spec(leak), llm, prop, rec)
    rec.save(tmp_path / "pins.jsonl")

    fake = [SuiteTask(t.task_id, fake_labels(t.task_dir, tmp_path / "fake" / t.task_id), t.split, family=t.family)
            for t in tasks]
    misses: list[str] = []
    cassette = load_cassette(cas)
    pins = Releases.replaying(tmp_path / "pins.jsonl", None)
    seen: dict = {}

    def run():
        dec, labels = _campaign(tmp_path / "rep", fake, _spec(leak), ReplayBackend(cassette, misses),
                                ReplayBackend(cassette, misses), pins)
        seen["labels"] = labels
        return dec
    report = replay_report(recorded, run, misses, pins)
    # the attack surface is live: the fake labels really changed the unpinned verdicts
    assert seen["labels"] != real_labels and all(lb == "REJECTED" for _, lb in seen["labels"]), seen["labels"]
    assert all(lb == "CERTIFIED-INTERNAL" for _, lb in real_labels)
    return report


def test_honest_campaign_replays_identically_with_fake_labels(tmp_path):
    report = _record_and_replay(tmp_path, leak=False)
    assert not report.flagged, report


def test_label_derived_value_in_a_prompt_is_flagged(tmp_path):
    report = _record_and_replay(tmp_path, leak=True)
    assert report.flagged and report.cassette_misses, report


def test_continuous_held_out_score_term_is_flagged(tmp_path, monkeypatch):
    """Re-introduce the pre-embargo term: the score depends on the held-out subject mean, unreleased."""
    real = ev_mod.dev_term
    monkeypatch.setattr(ev_mod, "dev_term", lambda st: real(st) + float((st.get("certificate") or {})
                                                                         .get("subject_mean") or 0.0))
    report = _record_and_replay(tmp_path, leak=False)
    assert report.flagged and (report.decision_changes or report.cassette_misses), report


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
