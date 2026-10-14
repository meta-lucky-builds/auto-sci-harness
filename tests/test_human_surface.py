"""The human-review surface (custom_harness/human; GOAL §5 L3/L9, invariants 13, 15, 22; docs/human_actions.md).

Written by s4-human to FAIL FIRST: on the parent (origin/int17 4567e6c) the package `custom_harness.human` does not
exist, so every test here errors at its first import of it; the behavioural ones also need what the package adds
(ledgered provider sign-offs, human-check registration, anchor-by-default rulings, the module-kernel refusal of every
human-only kind). The imports sit inside the tests so each one fails on its own line. NOT RUN (owner: no tests this
phase); see agent_worklogs/s4-human/RESUME.md for the command.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
from pathlib import Path

import pytest

from custom_harness import cli
from custom_harness.config import data_dir
from custom_harness.kernel import Ledger
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec, register
from custom_harness.store import EvidenceStore

SORT_SPEC = json.dumps({"format": "chh-synth-verifier/1", "problem": "sort", "kind": "invariant",
                        "rules": [{"op": "sorted", "field": "out"},
                                  {"op": "permutation_of", "field": "out", "of": "in"}]})


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def dstore():
    """The store the CLI opens (data_dir()/store)."""
    s = EvidenceStore(data_dir() / "store")
    yield s
    s.close()


def _verifier():
    from custom_harness.verify import synthesis as S
    return S.parse_spec(SORT_SPEC, "sort", author="openai/gpt-x", family="openai")


def _store_verifier(store) -> str:
    from custom_harness.verify import synthesis as S
    return store.blobs.put_json(S.to_record(_verifier()))


# ------------------------------------------------------------------ who may act
@pytest.mark.parametrize("name", ["", "   ", "agent:g-synth", "claude", "Claude Opus", "gpt-5", "openai/gpt-5",
                                  "s4-human", "w2-taint", "b-evolve", "s3-verifiers__review", "integrator", "main",
                                  "my-bot", "LLM judge", "sub-agent 3", "codex"])
def test_agent_and_empty_names_are_refused(name):
    from custom_harness.human.guard import HumanActionRefused, check_human_name
    with pytest.raises(HumanActionRefused):
        check_human_name(name)


@pytest.mark.parametrize("name", ["alice", "pytest", "owner", "h", "Ai Wei", "operator", "Dr. Jane Roe",
                                  "jane@lab.example"])
def test_human_names_pass(name):
    from custom_harness.human.guard import check_human_name
    assert check_human_name(f"  {name} ") == name


@register("hs.call_human_api")
def _call_human_api(state, params, ctx):
    from custom_harness.human import actions
    from custom_harness.kernel.adapters import REGISTRY, adapter_hash
    actions.approve_adapter(ctx._store, "ml", approver="alice", adapter_hash=adapter_hash(REGISTRY["ml"][0]))
    return {"done": True}


def _one(module: str) -> HarnessSpec:
    return HarnessSpec.model_validate({"id": "hs", "entry": "a", "nodes": {"a": {"kind": "module", "module": module}},
                                       "edges": [{"source": "a", "target": "END"}]})


def test_a_module_cannot_call_the_human_api_even_with_a_human_name(dstore):
    res = Engine(_one("hs.call_human_api"), backends={}, store=dstore).run("t", {})
    assert res.status != RunStatus.COMPLETED and "done" not in res.state
    assert any("inside a run" in (st.error or "") for st in dstore.trace(res.run_id))
    assert not Ledger(dstore).entries("adapter_approval")


def _forge(kind: str, body: dict):
    def fn(state, params, ctx):
        ctx.kernel().ledger.append(kind, body)
        return {"forged": True}
    return fn


def test_module_kernel_refuses_every_human_only_kind(dstore):
    from custom_harness.human.catalog import MODULE_FORBIDDEN_KINDS
    from custom_harness.kernel.ledger import KINDS
    from custom_harness.studio.kill import _ModuleLedger
    assert {"feature_ruling", "verifier_review", "approval", "alpha_open"} <= MODULE_FORBIDDEN_KINDS <= KINDS
    assert MODULE_FORBIDDEN_KINDS <= _ModuleLedger.HUMAN_ONLY
    cases = [(k, {"x": 1}) for k in sorted(MODULE_FORBIDDEN_KINDS)]
    cases.append(("alpha_retire", {"account": 1, "dataset_id": "d", "code": "human", "approver": "alice"}))
    for i, (kind, body) in enumerate(cases):
        mid = f"hs.forge_{i}"
        register(mid)(_forge(kind, body))
        res = Engine(_one(mid), backends={}, store=dstore).run("t", {})
        assert "forged" not in res.state, kind
        assert any("is a human action" in (st.error or "") for st in dstore.trace(res.run_id)), kind
    assert [e.kind for e in Ledger(dstore).entries() if e.kind in MODULE_FORBIDDEN_KINDS | {"alpha_retire"}] == []


# ------------------------------------------------------------------ the queue
def test_queue_is_read_only_and_every_item_names_what_it_binds(dstore):
    from custom_harness.human.queue import KIND_ORDER, pending
    before = len(Ledger(dstore).entries())
    items = pending(dstore)
    assert len(Ledger(dstore).entries()) == before
    assert items and all(i["kind"] in KIND_ORDER and isinstance(i["binds"], dict) and i["decide"] for i in items)
    assert not [i for i in items if i["kind"] == "queue_error"]
    suites = {i["problem"]: i for i in items if i["kind"] == "suite_review"}
    from custom_harness.verify import synthesis as S
    assert {p: i["binds"]["suite_hash"] for p, i in suites.items()} == {p: s.hash() for p, s in S.SUITES.items()}
    assert all(i["authorship"] == S.AGENT_AUTHORED for i in suites.values())


# ------------------------------------------------------------------ verifier and suite reviews
def test_suite_review_binds_its_hash_and_refuses_agents(store):
    from custom_harness.human import review as R
    from custom_harness.human.guard import HumanActionRefused
    from custom_harness.verify import synthesis as S
    h = S.SUITES["sort"].hash()
    with pytest.raises(HumanActionRefused, match="you reviewed"):
        R.review_suite(store, "sort", reviewer="alice", approve=True, suite_hash="0" * 64)
    with pytest.raises(HumanActionRefused, match="not a human"):
        R.review_suite(store, "sort", reviewer="agent:g-synth", approve=True, suite_hash=h)
    assert not Ledger(store).entries(S.REVIEW_KIND)
    e = R.review_suite(store, "sort", reviewer="alice", approve=True, suite_hash=h)
    assert e.kind == "verifier_review" and e.body["suite_hash"] == h and e.body["reviewer"] == "alice"
    assert S.suite_status("sort", Ledger(store)) == "human-approved"
    assert [i["problem"] for i in R.pending_suites(store)] == ["decay"]


def test_verifier_review_lifts_t2_to_t1_only_with_both_reviews(store):
    from custom_harness.human import review as R
    from custom_harness.human.guard import HumanActionRefused
    from custom_harness.tiers import Tier
    from custom_harness.verify import synthesis as S
    ref = _store_verifier(store)
    v = _verifier()
    view = R.verifier_view(store, ref)
    assert view["validation"]["passed"] and view["authorship"] == S.AGENT_AUTHORED
    assert view["tier_now"] == Tier.T2_STATISTICAL.name
    sealed_ids = {c.id for c in (*S.SUITES["sort"].sealed_golden, *S.SUITES["sort"].sealed_faults)}
    assert not any(sid in json.dumps(view) for sid in sealed_ids)  # the sealed half is shown by counts only
    with pytest.raises(HumanActionRefused, match="you reviewed"):
        R.review_verifier(store, ref, reviewer="alice", approve=True, verifier_hash="0" * 64)
    with pytest.raises(HumanActionRefused):  # the author's own family/model id is an agent identity
        R.review_verifier(store, ref, reviewer=v.author, approve=True, verifier_hash=v.hash())
    R.review_verifier(store, ref, reviewer="alice", approve=True, verifier_hash=v.hash())
    assert S.tier(v, Ledger(store)) == Tier.T2_STATISTICAL  # the suite is still agent-authored
    R.review_suite(store, "sort", reviewer="bob", approve=True, suite_hash=S.SUITES["sort"].hash())
    assert S.tier(v, Ledger(store)) == Tier.T1_EXECUTABLE


@register("hs.synth_ref")
def _synth_ref(state, params, ctx):
    return {"verifier_synthesis": {"ref": params["ref"], "problem": "sort"}}


def test_queue_finds_synthesized_verifiers_from_run_output_and_drops_them_once_decided(dstore):
    from custom_harness.human import review as R
    ref = _store_verifier(dstore)
    spec = HarnessSpec.model_validate({"id": "hs", "entry": "a", "nodes": {
        "a": {"kind": "module", "module": "hs.synth_ref", "params": {"ref": ref}}},
        "edges": [{"source": "a", "target": "END"}]})
    Engine(spec, backends={}, store=dstore).run("t", {})
    (item,) = R.pending_verifiers(dstore)
    assert item["ref"] == ref and item["binds"]["verifier_hash"] == _verifier().hash()
    R.review_verifier(dstore, ref, reviewer="alice", approve=False, verifier_hash=_verifier().hash())
    assert R.pending_verifiers(dstore) == []


def test_cli_review_needs_a_terminal_and_the_typed_hash(monkeypatch, capsys):
    from custom_harness.verify import synthesis as S
    h = S.SUITES["decay"].hash()
    argv = ["review", "suite", "decay", "--decision", "reject", "--reviewer", "alice", "--suite-hash", h]
    monkeypatch.setattr("sys.stdin", io.StringIO(h[:12] + "\n"))
    with pytest.raises(SystemExit, match="interactive terminal"):
        cli.main(argv)
    monkeypatch.setattr("sys.stdin", _Tty("000000000000\n"))
    with pytest.raises(SystemExit, match="confirmation"):
        cli.main(argv)
    monkeypatch.setattr("sys.stdin", _Tty(h[:12] + "\n"))
    assert cli.main(argv) == 0
    s = EvidenceStore(data_dir() / "store")
    try:
        assert S.suite_status("decay", Ledger(s)) == "human-rejected"
    finally:
        s.close()


# ------------------------------------------------------------------ feature rulings
def test_feature_ruling_needs_an_anchor_and_the_reviewed_report(tmp_path, store):
    from custom_harness.human import features as F
    from custom_harness.human.guard import HumanActionRefused
    from custom_harness.kernel.ledger import write_anchor
    from custom_harness.verify import provenance as prov
    from test_feature_provenance import _routed_task
    d, col = _routed_task(tmp_path)
    report = prov.screen(d)
    F.note_screen(store, d, report)
    (item,) = F.pending_rulings(store)
    assert item["feature"] == col and item["binds"]["report_hash"] == report["report_hash"]
    with pytest.raises(HumanActionRefused, match="anchor"):
        F.rule_feature(store, d, col, "legitimate", approver="alice", report_hash=report["report_hash"])
    anchor = tmp_path / "anchor.jsonl"
    write_anchor(Ledger(store), anchor, tmp_path / "store")  # the conftest `store` lives at tmp_path/store
    with pytest.raises(HumanActionRefused, match="you reviewed"):
        F.rule_feature(store, d, col, "legitimate", approver="alice", report_hash="0" * 64, anchors=[anchor])
    with pytest.raises(HumanActionRefused, match="not a human"):
        F.rule_feature(store, d, col, "legitimate", approver="claude", report_hash=report["report_hash"],
                       anchors=[anchor])
    out = F.rule_feature(store, d, col, "legitimate", approver="alice", report_hash=report["report_hash"],
                         anchors=[anchor])
    assert out["anchors_checked"] == 1 and out["approver"] == "alice"
    assert F.pending_rulings(store) == []
    assert prov.gate(prov.screen(d), prov.load_rulings(store)) == []


# ------------------------------------------------------------------ provider sign-off
def _override(tmp_path: Path, training: str = "forbidden", verified_by: str | None = None) -> Path:
    import yaml
    p = tmp_path / "providers.override.yaml"
    p.write_text(yaml.safe_dump({"openai-api": {
        "provider": "openai", "mode": "api", "auth_kind": "api_key", "byos_status": "n/a",
        "training_on_outputs": training, "source_url": "https://openai.com/policies/",
        "verified_on": "2026-10-01" if verified_by else None, "verified_by": verified_by}}), encoding="utf-8")
    return p


def test_provider_signoff_is_ledgered_bound_to_the_entry_and_counted_by_the_release_check(tmp_path, store):
    from custom_harness.gateway.costs import release_problems
    from custom_harness.gateway.registry import load_registry
    from custom_harness.human import providers as P
    from custom_harness.human.guard import HumanActionRefused
    today = dt.date(2026, 10, 14)
    ov = _override(tmp_path, verified_by="someone")  # a YAML verified_by alone no longer counts
    reg = load_registry(ov)
    eff = P.with_ledgered_signoffs(reg, store)
    assert eff["openai-api"]["verified_by"] is None
    assert any(p.startswith("openai-api: not verified by a human") for p in release_problems(eff, today))
    h = P.entry_hash(reg["openai-api"])
    assert h == P.entry_hash({**reg["openai-api"], "verified_by": "x", "verified_on": "2020-01-01"})
    with pytest.raises(HumanActionRefused, match="you reviewed"):
        P.sign_off_provider(store, "openai-api", by="alice", entry_hash_reviewed="0" * 64, override=ov)
    with pytest.raises(HumanActionRefused, match="not a human"):
        P.sign_off_provider(store, "openai-api", by="codex", entry_hash_reviewed=h, override=ov)
    P.sign_off_provider(store, "openai-api", by="alice", entry_hash_reviewed=h, override=ov, today=today)
    eff = P.with_ledgered_signoffs(load_registry(ov), store)
    assert (eff["openai-api"]["verified_by"], eff["openai-api"]["verified_on"]) == ("alice", "2026-10-14")
    assert not any(p.startswith("openai-api: not verified") or p.startswith("openai-api: verified ")
                   for p in release_problems(eff, today))
    assert "openai-api" not in {i["key"] for i in P.pending_signoffs(store, today=today, override=ov)}
    ov2 = _override(tmp_path, training="allowed")  # the terms changed: the old sign-off no longer counts
    assert P.with_ledgered_signoffs(load_registry(ov2), store)["openai-api"]["verified_by"] is None
    assert "openai-api" in {i["key"] for i in P.pending_signoffs(store, today=today, override=ov2)}
    assert P.pending_signoffs(store, today=today + dt.timedelta(days=200), override=ov)  # too old for a release


# ------------------------------------------------------------------ human-held check registration
def test_human_check_registration_binds_the_file_hash_and_never_records_content(tmp_path, store):
    from custom_harness.assure import humancheck as H
    from custom_harness.human import checks as K
    from custom_harness.human.guard import HumanActionRefused
    repo, ddir = tmp_path / "repo", tmp_path / "data"
    (repo / "redteam").mkdir(parents=True)
    assert [i["action"] for i in K.pending_checks(store, repo_root=repo, data_dir=ddir)] == ["write_and_register"]
    cdir = H.checks_dir(ddir)
    cdir.mkdir(parents=True)
    secret = b"# SECRET-CHECK-CONTENT-7f3a\nimport sys\nsys.exit(0)\n"
    (cdir / "m4-a.py").write_bytes(secret)
    sha = hashlib.sha256(secret).hexdigest()
    with pytest.raises(HumanActionRefused, match="hashes to"):
        K.register_check(store, "m4-a", by="alice", sha256="0" * 64, repo_root=repo, data_dir=ddir)
    with pytest.raises(HumanActionRefused, match="not a human"):
        K.register_check(store, "m4-a", by="agent-7", sha256=sha, repo_root=repo, data_dir=ddir)
    e = K.register_check(store, "m4-a", by="alice", sha256=sha, repo_root=repo, data_dir=ddir)
    assert e.body["check"]["sha256"] == sha and e.body["approver"] == "alice"
    assert "SECRET-CHECK-CONTENT" not in json.dumps([x.body for x in Ledger(store).entries()])
    assert json.loads((repo / H.LOCK).read_text()) == {"m4-a": sha}
    assert H.verify_hashes(repo, ddir).status == "OK"
    kinds = [(i["kind"], i["action"]) for i in K.pending_checks(store, repo_root=repo, data_dir=ddir)]
    assert kinds == [("human_check_run", "run")]  # registered and ledgered; it has not been run yet
    (cdir / "m4-a.py").write_bytes(secret + b"# changed\n")
    with pytest.raises(HumanActionRefused, match="already registered"):
        K.register_check(store, "m4-a", by="alice", sha256=hashlib.sha256(secret + b"# changed\n").hexdigest(),
                         repo_root=repo, data_dir=ddir)


# ------------------------------------------------------------------ approval nodes and the older commands
def test_approve_node_binds_the_named_item_and_refuses_agents(dstore):
    from custom_harness.human.guard import HumanActionRefused
    from custom_harness.human.nodes import approve_node, node_item
    from test_approval import spec
    res = Engine(spec(), backends={}, store=dstore).run("t", {})
    it = node_item(dstore, res.run_id, "review")
    with pytest.raises(HumanActionRefused, match="does not match"):
        approve_node(dstore, res.run_id, "review", decision="approve", approver="alice", item_hash="f" * 64)
    with pytest.raises(HumanActionRefused, match="not a human"):
        approve_node(dstore, res.run_id, "review", decision="approve", approver="model",
                     item_hash=it.item_hash)
    assert not Ledger(dstore).entries("approval")
    e = approve_node(dstore, res.run_id, "review", decision="approve", approver="alice", item_hash=it.item_hash)
    assert e.body["item_hash"] == it.item_hash and e.body["approver"] == "alice"


def test_older_human_commands_refuse_agent_names_and_check_the_named_hash(dstore):
    with pytest.raises(SystemExit, match="not a human"):
        cli.main(["approve-adapter", "ml", "--approver", "claude"])
    with pytest.raises(SystemExit, match="you reviewed"):
        cli.main(["approve-adapter", "ml", "--approver", "alice", "--adapter-hash", "0" * 64])
    assert not Ledger(dstore).entries("adapter_approval")


def test_catalogue_covers_every_queued_kind_and_the_doc_lists_every_action():
    from custom_harness.human.catalog import ACTIONS
    from custom_harness.human.queue import KIND_ORDER
    names = {a.name for a in ACTIONS}
    assert set(KIND_ORDER) - {"queue_error"} <= names
    doc = (Path(__file__).resolve().parents[1] / "docs" / "human_actions.md").read_text(encoding="utf-8")
    missing = [a.name for a in ACTIONS if f"`{a.name}`" not in doc]
    assert missing == []
