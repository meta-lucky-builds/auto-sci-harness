"""L9 studio (GOAL §5 L9): `chh explain` (invariant 8: every value resolves to an artifact), the research graph and
limitations register (read-only), and the approval queue."""

import json
import re

import pytest
import yaml

from custom_harness import cli
from custom_harness.config import data_dir
from custom_harness.gateway import MockBackend
from custom_harness.kernel import ArtifactRef, Kernel, Label, SandboxReplayer
from custom_harness.kernel.certificate import CHECK_IDS
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from kernel_world import World
from test_research import SPEC, rings_task, scripted_llm  # noqa: F401  (rings_task is a fixture)

KEY = re.compile(r"^[a-z_]+$")
LABEL = re.compile(r"^[A-Za-z _]+$")
LINE = re.compile(r"^\s*(?P<label>[A-Za-z _]*) = (?P<value>.*)   <- (?P<artifact>[a-z]+):(?P<ref>[^:]+):"
                  r"(?P<path>\[.*\])(?P<key>#key)?$")
HEADER = re.compile(r"^\s*[A-Za-z _]+$")


def _check_every_fact_resolves(kernel, doc) -> int:
    """Walk the explanation: every leaf is a fact, every fact resolves to the value shown. Returns the fact count."""
    from custom_harness.studio.explain import Resolver
    r = Resolver(kernel)
    n = 0

    def walk(node, where):
        nonlocal n
        if isinstance(node, dict) and "src" in node and "value" in node:
            assert set(node) <= {"src", "value", "label"}, where
            assert LABEL.match(node.get("label", "x")), (where, node.get("label"))
            src = json.loads(json.dumps(node["src"]))  # as a reader of the JSON output would get it
            assert r.resolve(src) == node["value"], (where, src)
            n += 1
        elif isinstance(node, dict):
            for k, v in node.items():
                assert KEY.match(k), f"data in a key at {where}: {k!r}"
                walk(v, f"{where}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where}/{i}")
        else:
            raise AssertionError(f"a bare value with no source at {where}: {node!r}")

    walk(doc, "")
    return n


def _check_text(kernel, text) -> int:
    from custom_harness.studio.explain import Resolver
    r, n = Resolver(kernel), 0
    for line in text.splitlines():
        m = LINE.match(line)
        if m is None:
            assert HEADER.match(line), f"a line that is neither a header nor a sourced fact: {line!r}"
            continue
        ref = m["ref"]
        src = {"artifact": m["artifact"], "ref": int(ref) if m["artifact"] == "ledger" else ref,
               "path": json.loads(m["path"])}
        if m["key"]:
            src["key"] = True
        assert r.resolve(src) == json.loads(m["value"]), line
        n += 1
    return n


def _numbers(doc) -> int:
    from custom_harness.studio.explain import iter_facts
    return sum(1 for f in iter_facts(doc) if isinstance(f["value"], int | float) and not isinstance(f["value"], bool))


def test_explain_of_a_scripted_research_run_resolves_every_value(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    from custom_harness.studio.explain import explain, render_text
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    llm = MockBackend(scripted_llm)
    res = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    h = res.state["certificate"]["hash"]
    kernel = Kernel(store, replayer=SandboxReplayer(
        lambda key: LocalSandbox(tmp_path / "ex" / key, i_understand_no_isolation=True)))
    target = kernel.certificate(h).claim["generalization_targets"][0]
    kernel.record_release(target, "report", "verdict_bit", "report:studio-test")  # a release on its target
    doc = explain(kernel, h[:16])
    assert doc["certificate"]["value"] == h
    assert doc["status"]["verdict"]["value"] in ("valid", "stale")  # honest: never forged
    assert doc["summary"]["label"]["value"] == "CERTIFIED-INTERNAL"
    assert [c["check"]["value"] for c in doc["checks"]] == list(CHECK_IDS)
    assert all(c["status"]["value"] == "pass" for c in doc["checks"])
    chain = doc["evidence_chain"]
    assert chain["prereg"] and chain["reads"] and chain["records"] and chain["certificate"]
    assert any(f["src"]["path"][-1] == "score" for row in chain["records"] for f in row)
    assert [f["value"] for f in doc["adapters"][0] if f["src"]["path"][-1] == "name"] == ["ml"]
    assert doc["assumptions"]["untested"]
    loopholes = {x["finding"]["value"] for x in doc["assumptions"]["open_loopholes"]}
    assert {"KD1", "KD2", "KD3", "KD4"} <= loopholes  # data-class loopholes apply to this data certificate
    assert any(f["value"] == "report:studio-test" for row in doc["releases"] for f in row)
    assert doc["prediction"] and doc["prediction"][2]["value"] is True  # outcome
    n = _check_every_fact_resolves(kernel, doc)
    assert n > 100 and _numbers(doc) > 30  # the walk really checked the numbers (it did not pass on an empty doc)
    assert _check_text(kernel, render_text(doc)) == n


def _world_cert(w):
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"))
    w.kernel.preregister(c)
    return w.kernel.certify(c, w.evaluate_all(c))


def test_tampered_certificate_is_shown_as_forged(tmp_path, capsys):
    from custom_harness.studio.explain import explain
    w = World(data_dir())  # its store is the one the CLI opens
    try:
        honest = _world_cert(w)
        assert honest.label == Label.CERTIFIED_INTERNAL
        doc = explain(w.kernel, honest.cert_hash())
        assert doc["status"]["verdict"]["value"] == "valid"
        _check_every_fact_resolves(w.kernel, doc)
        # 1. a self-consistent forgery (content hashes correctly; lookup alone cannot tell)
        bad = w.claim(w.artifact("nondet"), w.artifact("baseline", "baseline"), delta=0.2)
        w.kernel.preregister(bad)
        rejected = w.kernel.certify(bad, w.evaluate_all(bad))
        assert rejected.label == Label.REJECTED
        fake = rejected.model_copy(update={"label": Label.CERTIFIED_INTERNAL, "checks": {
            k: v.model_copy(update={"status": "pass", "reasons": (), "codes": ()})
            for k, v in rejected.checks.items()}})
        blob = w.store.blobs.put_json(fake.model_dump(mode="json"))
        w.kernel.ledger.append("certificate", {"cert_hash": fake.cert_hash(), "claim_hash": fake.claim_hash,
                                               "label": fake.label.value, "blob": blob, "resolution": {}})
        assert w.kernel.certificate(fake.cert_hash()).label == Label.CERTIFIED_INTERNAL  # the attack is in place
        doc = explain(w.kernel, fake.cert_hash())
        assert doc["status"]["verdict"]["value"] == "forged"
        assert any("recorded" in p["value"] for p in doc["status"]["problems"])
        _check_every_fact_resolves(w.kernel, doc)
        # 2. a certificate filed under a hash its content does not have
        w.kernel.ledger.append("certificate", {"cert_hash": "f" * 64, "claim_hash": honest.claim_hash,
                                               "label": "CERTIFIED-INTERNAL", "blob": blob, "resolution": {}})
        doc = explain(w.kernel, "f" * 64)
        assert doc["status"]["verdict"]["value"] == "forged"
        assert "does not match" in doc["status"]["problems"][0]["value"]
        _check_every_fact_resolves(w.kernel, doc)
        # 3. the honest certificate's blob edited on disk
        entry = next(e for e in w.kernel.ledger.entries("certificate") if e.body["cert_hash"] == honest.cert_hash())
        path = w.store.blobs._path(entry.body["blob"])
        path.write_bytes(path.read_bytes().replace(b'"CERTIFIED-INTERNAL"', b'"CERTIFIED-VALIDATED"'))
        doc = explain(w.kernel, honest.cert_hash())
        assert doc["status"]["verdict"]["value"] == "forged" and "corrupted" in doc["status"]["problems"][0]["value"]
        # the CLI shows the same, and exits non-zero
        assert cli.main(["explain", fake.cert_hash()[:12], "--dev-sandbox", "--json"]) == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"]["verdict"]["value"] == "forged"
        with pytest.raises(SystemExit, match="--no-verify"):
            cli.main(["explain", fake.cert_hash()[:12]])
        assert cli.main(["explain", rejected.cert_hash()[:12], "--no-verify"]) == 0
        assert "UNVERIFIED" in capsys.readouterr().out
        with pytest.raises(SystemExit, match="no certificate"):
            cli.main(["explain", "0" * 12, "--no-verify"])
    finally:
        w.close()


def _counts(store):
    rows = store._rows("SELECT (SELECT count(*) FROM ledger), (SELECT count(*) FROM records), "
                       "(SELECT count(*) FROM evidence), (SELECT count(*) FROM run_status)")
    return rows[0]


def test_research_graph_and_limitations_register_are_read_only(tmp_path):
    from custom_harness.research.tree import ExperimentTree, RunContract, RunOutcome, code_hash
    from custom_harness.studio.graph import research_graph
    from custom_harness.studio.limitations import UNREADABLE, register
    w = World(tmp_path / "w")
    try:
        honest = _world_cert(w)
        bad = w.claim(w.artifact("weak"), w.artifact("baseline", "baseline"))
        w.kernel.preregister(bad)
        rejected = w.kernel.certify(bad, w.evaluate_all(bad))
        assert rejected.label == Label.REJECTED
        contract = RunContract.make(["python", "s.py"], {}, {}, {"tokens": 1})
        t = ExperimentTree.create(w.store, "tree-1", contract, root_code="print(1)", root_config={},
                                  metric="accuracy", higher_is_better=True, dev_labels=["0", "1"])
        t.record(t.root, RunOutcome(contract, code_hash("print(1)"), 0, "ok", 0.5, ["0", "0"]))
        rnd = t.open_round(t.root, "which k")
        t.add_option(rnd, "which k", "print(2)", {"k": 2})
        before = _counts(w.store)
        g = research_graph(w.store)
        assert _counts(w.store) == before  # read-only: nothing was written
        nodes = {n["id"]: n for n in g["nodes"]}
        ch, rh = honest.cert_hash(), rejected.cert_hash()
        assert nodes[f"cert:{ch}"]["label"] == "CERTIFIED-INTERNAL"
        assert nodes[f"claim:{honest.claim_hash}"]["prediction"]["outcome"] is True
        assert nodes[f"claim:{bad.claim_hash()}"]["prediction"]["outcome"] is False
        assert nodes["tree:tree-1"]["events"] == t.seq and len([n for n in nodes if n.startswith("treenode:")]) == 2
        rels = {(e["from"].split(":")[0], e["rel"], e["to"].split(":")[0]) for e in g["edges"]}
        assert {("cert", "certifies", "claim"), ("cert", "uses", "evidence"), ("evidence", "produced_by", "run"),
                ("claim", "target", "dataset"), ("claim", "subject", "artifact"),
                ("treenode", "child_of", "treenode"), ("treenode", "root_of", "tree")} <= rels
        loopholes = set(nodes[f"cert:{ch}"]["untested_open_loopholes"])
        assert {"KD1", "KD2", "KD3", "KD4", "ASOF", "ADAPTER-EDIT"} <= loopholes
        assert "untested_open_loopholes" not in nodes[f"cert:{rh}"]  # a rejected certificate certifies nothing
        assert g["problems"] == []
        # the register follows the findings file: a new open finding with no scope entry applies to every certificate
        from custom_harness.studio.limitations import FINDINGS_PATH
        doc = yaml.safe_load(FINDINGS_PATH.read_text(encoding="utf-8"))
        opened = [f["id"] for f in doc["findings"] if f.get("status") == "open"]
        assert [x["finding_id"] for x in g["limitations"]] == opened  # machine-readable, one row per open finding
        assert all(x["tested_by"] is None for x in g["limitations"])
        custom = tmp_path / "findings.yaml"
        custom.write_text(yaml.safe_dump({"findings": [
            {"id": "NEW-1", "title": "t", "invariant": "i", "capability": "A3", "tests": [], "status": "open"},
            {"id": "OLD", "title": "t", "invariant": "i", "capability": "A3", "tests": [], "status": "fixed"}]}))
        (lim,) = register(w.kernel, custom)
        assert lim.finding_id == "NEW-1" and lim.certificates == (ch,)
        (lim,) = register(w.kernel, tmp_path / "missing.yaml")
        assert lim.finding_id == UNREADABLE and lim.certificates == (ch,)
        # a tampered certificate is marked, not dropped
        entry = next(e for e in w.kernel.ledger.entries("certificate") if e.body["cert_hash"] == ch)
        path = w.store.blobs._path(entry.body["blob"])
        path.write_bytes(path.read_bytes() + b" ")
        g = research_graph(w.store)
        assert {n["id"]: n for n in g["nodes"]}[f"cert:{ch}"]["tampered"] is True and g["problems"]
    finally:
        w.close()


def test_queue_lists_every_pending_human_decision(capsys):
    from custom_harness.kernel import DatasetRole
    from custom_harness.studio.queue import pending
    from test_approval import spec as approval_spec
    from test_kernel_formal import formal_claim
    w = World(data_dir(), approve_adapters=False)
    try:
        kinds = lambda: sorted({(i["kind"], i.get("name") or i.get("dataset_id") or i.get("run_id")  # noqa: E731
                                 or i.get("artifact_id")) for i in pending(w.store)})
        assert ("adapter", "ml") in kinds()
        assert cli.main(["approve-adapter", "ml", "--approver", "alice"]) == 0
        assert ("adapter", "ml") not in kinds()
        w.register_target("ext", DatasetRole.EXTERNAL)
        assert ("role", "ext") in kinds()
        assert cli.main(["approve-role", "ext", "--approver", "alice"]) == 0
        assert ("role", "ext") not in kinds()
        p1 = Engine(approval_spec(), backends={}, store=w.store).run("t", {})
        p2 = Engine(approval_spec(), backends={}, store=w.store).run("t", {})
        assert {("approval_node", p1.run_id), ("approval_node", p2.run_id)} <= set(kinds())
        assert cli.main(["approve", p1.run_id, "review", "--decision", "approve", "--approver", "alice"]) == 0
        assert cli.main(["kill", p2.run_id, "--by", "alice"]) == 0
        assert not [k for k in kinds() if k[0] == "approval_node"]  # decided, or killed
        art = w.vault.register_artifact("thm", b'{"format": "chh-lean-statement/1"}', "lean-statement")
        claim = formal_claim(w, ArtifactRef(artifact_id="thm", hash=art.hash))
        w.kernel.preregister(claim)
        (st,) = [i for i in pending(w.store) if i["kind"] == "statement"]
        assert st["statement_hash"] == art.hash and "--statement-hash" in st["decide"]
        assert cli.main(["queue"]) == 0
        assert "[statement]" in capsys.readouterr().out
    finally:
        w.close()


def test_queue_and_graph_are_cli_commands(capsys):
    store = EvidenceStore(data_dir() / "store")
    store.close()
    assert cli.main(["queue", "--json"]) == 0
    items = json.loads(capsys.readouterr().out)
    assert {i["kind"] for i in items} == {"adapter"}  # a fresh store: only the adapters wait for a human
    assert cli.main(["graph", "--limitations"]) == 0
    assert all(x["certificates"] == [] for x in json.loads(capsys.readouterr().out))
