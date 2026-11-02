"""Promote to Court: freeze, hand-off to the existing Claim Audit path, verdict-bit release (lab/promote.py).

Fail-first design, one attack per test:
- a request or targets file written inside the lab root (agent-writable) is used;
- an unpinned lock is frozen (the Court could not re-create the environment);
- the workspace is read while the agent runs (the box must be paused first);
- the frozen code names its own held-out target (deny-only screen);
- a promotion is handed off twice (two Court specs for one freeze);
- the Lab learns more than the verdict label, or learns it without a ledgered release.
A promote.py that skipped any of these steps fails the matching test.

Written 2026-10-23 by lab-agents; built, NOT run. No Docker is used (tests/_lab_fakes.FakeDocker).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from _lab_fakes import FakeDocker, docker
from custom_harness.audit.spec import load_spec
from custom_harness.commitment import agent_readable_root_of
from custom_harness.lab import promote as P
from custom_harness.lab import session as S
from custom_harness.lab.agents import AgentOptions
from custom_harness.lab.walls import lab_root
from custom_harness.pathsafe import is_within

METHOD = b"def build_model(seed):\n    from sklearn.ensemble import RandomForestClassifier\n" \
         b"    return RandomForestClassifier(random_state=seed)\n"
BASELINE = b"def build_model(seed):\n    from sklearn.linear_model import LogisticRegression\n" \
           b"    return LogisticRegression(max_iter=1000)\n"
LOCK = b"numpy==2.1.0\nscikit-learn==1.5.2\n"
REQUEST = """format: chh-lab-promote/1
title: Lab test claim
sentence: The random forest beats logistic regression on fresh data by at least 0.01 accuracy.
method: method.py
baseline: baseline.py
lock: requirements.lock
claim: {metric: accuracy, min_effect: 0.01, probability: 0.6, seeds: [0, 1, 2], resolve_by: 2027-01-01}
budget: {timeout_s: 600}
"""
TARGETS = """format: chh-lab-targets/1
datasets:
  - {openml_id: 40983, sha256: "%s", target: class, licence: CC-BY}
""" % ("a" * 64)


@pytest.fixture
def lab(store, tmp_path):
    fake = FakeDocker()
    S.start(store, "opencode", AgentOptions(model="m", local_model_server=True), None,
            session_id="s-prom", docker=docker(fake))
    ws = S.workspace_of("s-prom")
    (ws / "method.py").write_bytes(METHOD)
    (ws / "baseline.py").write_bytes(BASELINE)
    (ws / "requirements.lock").write_bytes(LOCK)
    req = tmp_path / "promote.yaml"
    req.write_text(REQUEST, encoding="utf-8")
    tg = tmp_path / "targets.yaml"
    tg.write_text(TARGETS, encoding="utf-8")
    return SimpleNamespace(fake=fake, ws=ws, req=req, targets=tg, docker=docker(fake))


def test_freeze_pauses_the_box_and_records_hashes_outside_the_lab(store, lab):
    rec = P.freeze(store, "s-prom", lab.req, docker=lab.docker)
    subs = lab.fake.subcommands()
    assert subs.index("pause") < subs.index("unpause")
    assert rec["workspace_snapshot"] and rec["n_files"] == 3
    intake = P.intake_root() / rec["promotion_id"]
    assert (intake / "method.py").read_bytes() == METHOD
    # path containment, not a string prefix: `<data>/lab_court` starts with the string `<data>/lab` but is outside it
    # (the string test failed on the correct layout, stage-c 2026-10-23); and it is under NO agent-readable root
    assert not is_within(intake, lab_root()) and not is_within(lab_root(), intake)
    assert agent_readable_root_of(intake) is None
    assert store.blobs.get(rec["frozen"]["lock"]["blob"]) == LOCK
    assert S.events(store, "s-prom")[-1]["event"] == "promoted"


def test_request_inside_the_lab_root_is_refused(store, lab):
    inside = lab.ws / "promote.yaml"
    inside.write_text(REQUEST, encoding="utf-8")
    with pytest.raises(P.PromoteError):
        P.freeze(store, "s-prom", inside, docker=lab.docker)


@pytest.mark.parametrize("lock", [b"numpy>=2\n", b"numpy\n", b"-e git+https://x/y.git#egg=y\n",
                                  b"--extra-index-url https://evil.invalid\nnumpy==2.1.0\n", b"y @ https://x/y.whl\n"])
def test_unpinned_or_redirected_locks_are_refused(store, lab, lock):
    (lab.ws / "requirements.lock").write_bytes(lock)
    with pytest.raises(P.PromoteError):
        P.freeze(store, "s-prom", lab.req, docker=lab.docker)


def test_lock_with_hashes_passes():
    assert P.lock_problems("numpy==2.1.0 \\\n    --hash=sha256:" + "0" * 64 + "\n") == []


def test_box_that_cannot_be_paused_is_refused(store, lab):
    lab.fake.state = "running"
    orig = lab.fake.__call__

    def no_pause(argv, env, stdin, timeout):
        if argv[1] == "pause":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return orig(argv, env, stdin, timeout)

    with pytest.raises(P.PromoteError):
        P.freeze(store, "s-prom", lab.req, docker=docker(no_pause))  # type: ignore[arg-type]


def test_handoff_writes_a_valid_court_spec_once(store, lab):
    rec = P.freeze(store, "s-prom", lab.req, docker=lab.docker)
    out = P.handoff(store, rec["promotion_id"], lab.targets)
    loaded = load_spec(P.intake_root() / rec["promotion_id"] / "audit.yaml")
    assert out["spec_hash"] == loaded.spec_hash and out["claim_hash"] == loaded.claim().claim_hash()
    assert out["targets"] == ["openml-40983-" + "a" * 16]
    with pytest.raises(P.PromoteError):
        P.handoff(store, rec["promotion_id"], lab.targets)


def test_code_naming_its_target_is_refused(store, lab):
    (lab.ws / "method.py").write_bytes(METHOD + b"# fetch_openml(data_id=40983)\n")
    rec = P.freeze(store, "s-prom", lab.req, docker=lab.docker)
    with pytest.raises(P.PromoteError, match="names Court target"):
        P.handoff(store, rec["promotion_id"], lab.targets)


def test_release_needs_a_court_outcome(store, lab):
    rec = P.freeze(store, "s-prom", lab.req, docker=lab.docker)
    with pytest.raises(P.PromoteError):
        P.release_verdict(store, rec["promotion_id"], "owner", docker=lab.docker)
    P.handoff(store, rec["promotion_id"], lab.targets)
    with pytest.raises(P.PromoteError):
        P.release_verdict(store, rec["promotion_id"], "owner", docker=lab.docker)


def test_release_is_the_label_only_and_is_ledgered(store, lab, monkeypatch):
    import custom_harness.audit.claim_audit as CA
    import custom_harness.kerneld.releases as R
    from custom_harness.kernel import Kernel

    rec = P.freeze(store, "s-prom", lab.req, docker=lab.docker)
    hand = P.handoff(store, rec["promotion_id"], lab.targets)
    monkeypatch.setattr(CA, "latest_outcome", lambda s, aid: {"cert_hash": "c" * 64, "verdict": "NOT CONFIRMED",
                                                             "checks": ["K6"], "labels_read": True})
    cert = SimpleNamespace(label=SimpleNamespace(value="REJECTED"),
                           claim={"generalization_targets": hand["targets"]})
    monkeypatch.setattr(Kernel, "certificate", lambda self, h: cert)
    emitted: list[dict] = []

    class Rec:
        def emit(self, value, **kw):
            emitted.append({"value": value, **kw})
            return value

    monkeypatch.setattr(R.Releases, "live", classmethod(lambda cls, s: Rec()))
    # int18 (stage-a): the verification badges (court/levels.py, code only; LevelResult.release_value) are released
    # with the label, as their own ledgered `count` release; stubbed here because the certificate above is a stub
    import custom_harness.court.levels as L
    monkeypatch.setattr(L, "level", lambda s, h: SimpleNamespace(release_value=lambda: "NONE"))
    P.release_verdict(store, rec["promotion_id"], "owner", docker=lab.docker)
    assert emitted == [{"value": "REJECTED", "targets": hand["targets"], "channel": "prompt",
                        "alphabet": "verdict_bit", "note": "lab:promotion_verdict",
                        "key": f"lab:{rec['promotion_id']}"},
                       {"value": "NONE", "targets": hand["targets"], "channel": "prompt",
                        "alphabet": "count", "note": "lab:promotion_level",
                        "key": f"lab:{rec['promotion_id']}"}]
    written = json.loads((lab.ws / ".court" / f"verdict-{rec['promotion_id']}.json").read_text(encoding="utf-8"))
    assert written["label"] == "REJECTED" and written["level"] == "NONE"
    assert "K6" not in json.dumps(written) and "NOT CONFIRMED" not in json.dumps(written)
