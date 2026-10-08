"""Integrity hardening of harness evolution (2026-10-07): review of the B3 evolution pieces, the R2 fixes beyond
tests/test_review_loop.py, and three known limitations (adapter edits, tool-node taint, Socratic directives) plus the
one-shot sealed `finalize`.

Every attack test either asserts that the attack really happened (precondition) or pairs the refusal with a positive
control that differs only in the attacked element, so a refusal for an unrelated reason cannot pass the test.
Scripted mock models and LocalSandbox only (CHH_ALLOW_UNISOLATED=1, trusted test code): no real model call.
"""

from __future__ import annotations

import builtins
import copy
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.evolve import Evaluator, EvolveConfig, RunOutcome, SuiteTask, evolve_generation
from custom_harness.evolve.edits import Edit, EditOp, EditRejected, EditType, _apply_op, adapter_references, apply_edit
from custom_harness.evolve.evaluate import run_harness
from custom_harness.evolve.export import export_evidence, lineage_view, proposer_view, stats_view
from custom_harness.gateway import MockBackend
from custom_harness.records import Cost, RunStatus
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from test_evolve import seed_spec
from test_lofo import ONE_EDIT

ROOT = Path(__file__).resolve().parents[1]
ALL_ROLES = ("proposer", "executor", "judge", "replicator")


def _edges(spec: HarnessSpec) -> list[dict]:
    return spec.model_dump(mode="json")["edges"]


def _edge_index(spec: HarnessSpec, source: str, target: str) -> int:
    return next(i for i, e in enumerate(_edges(spec)) if e["source"] == source and e["target"] == target
                and e.get("when") is None)


def _insert_between(spec: HarnessSpec, source: str, target: str, nid: str, node: dict) -> list[EditOp]:
    """Ops that add node `nid` and wire it source -> nid -> target (replacing the unconditional source -> target)."""
    i = _edge_index(spec, source, target)
    return [EditOp(op="add", path=f"/nodes/{nid}", value=node),
            EditOp(op="replace", path=f"/edges/{i}/target", value=nid),
            EditOp(op="add", path="/edges/-", value={"source": nid, "target": target})]


def _rings(tmp_path: Path, name: str, seed: int = 0) -> Path:
    out = tmp_path / name
    subprocess.run([sys.executable, str(ROOT / "examples/tasks/make_rings_task.py"), str(out), "--n-train", "120",
                    "--n-test", "60", "--seed", str(seed)], check=True, capture_output=True)
    return out


# ===================================================================== B3 review: proposer-visible views
class _GateTable(Evaluator):
    """Candidate +1 everywhere, parent 0. Evolve units: CERTIFIED-VALIDATED, cost 100. Gate units: label, score
    offset and cost set per world; only the GATE differs between worlds."""

    def __init__(self, store, tasks, parent_hash, gate_label, gate_bonus, gate_cost):
        super().__init__({}, store, lambda k: None, tasks)
        self.parent_hash, self.gate_label, self.gate_bonus, self.gate_cost = parent_hash, gate_label, gate_bonus, \
            gate_cost

    def outcome(self, spec, unit):
        key = (spec.spec_hash(), unit[0], unit[1])
        if key not in self.cache:
            self.calls += 1
            gate = self.tasks[unit[0]].split == "gate"
            cand = spec.spec_hash() != self.parent_hash
            score = (1.0 + 0.001 * (int(unit[0][1:]) + 3 * (unit[1] % 1000)) + (self.gate_bonus if gate else 0)
                     ) if cand else 0.0
            label = self.gate_label if gate else "CERTIFIED-VALIDATED"
            cost = Cost(prompt_tokens=self.gate_cost if (gate and cand) else 100)
            self.cache[key] = RunOutcome(unit[0], "", unit[1], score, True, "completed", cost, [], cert_label=label)
        return self.cache[key]


def _label_tasks(tmp_path):
    tasks = [SuiteTask(f"e{i}", tmp_path, "evolve", family="A") for i in range(3)]
    tasks += [SuiteTask(f"g{i}", tmp_path, "gate", family="A") for i in range(2)]
    return tasks + [SuiteTask(f"h{i}", tmp_path, "gate", family="H") for i in range(2)]


def test_proposer_visible_records_are_invariant_to_gate_only_outcomes(tmp_path):
    """Two campaigns identical on the evolve split, both ACCEPTED, differing only in what happened on the gate split
    (certificate labels, score margins, costs). Secure: everything the proposer can see (PAST EDITS = proposer_view,
    export lineage/ledger/generation_stats views) is identical; the gate may only leak its accept/reject bit.
    Found: `loop_label` (computed from the gate runs' certificate labels) was in both whitelists."""
    tasks = _label_tasks(tmp_path)
    parent = seed_spec()
    worlds = []
    for i, (label, bonus, cost) in enumerate((("CERTIFIED-VALIDATED", 0.0, 100), ("CERTIFIED-INTERNAL", 0.5, 180))):
        st = EvidenceStore(tmp_path / f"store{i}")
        res = evolve_generation(parent, tasks, evaluator=_GateTable(st, tasks, parent.spec_hash(), label, bonus, cost),
                                proposer=MockBackend([ONE_EDIT]), store=st,
                                cfg=EvolveConfig(seeds=(0, 1), n_edits=1, race_rounds=1))
        assert res.accepted, res.decision  # both worlds: the same aggregate decision
        worlds.append((st, res))
    raw = [w[0].records("harness_edit")[-1] for w in worlds]
    # precondition: the gate-only differences really are in the records (else the test proves nothing)
    assert raw[0]["loop_label"] != raw[1]["loop_label"] and raw[0]["actual_gate_delta"] != raw[1]["actual_gate_delta"]
    evolve_ids = {t.task_id for t in tasks if t.split == "evolve"}

    def visible(st):
        return ([{k: v for k, v in proposer_view(r, evolve_ids).items() if k != "at"}
                 for r in st.records("harness_edit")],
                [lineage_view(r) for r in st.records("lineage")],
                [stats_view(r) for r in st.records("generation_stats")])
    a, b = visible(worlds[0][0]), visible(worlds[1][0])
    assert a == b, json.dumps({"world0": a[0], "world1": b[0]}, default=str)[:2000]
    for st, _ in worlds:
        st.close()


def test_proposer_view_hides_an_old_gate_result_of_a_task_rotated_into_evolve():
    """GOAL L6.8 rotates held-out families across campaigns. A record written when task `t` was a GATE task must not
    show its gate attribution once `t` is an evolve task (the view filtered flipped/regressed by the CURRENT split
    only)."""
    rec = {"accepted": False, "rejected_on": "held_out_gate", "flipped": ["t", "e"], "regressed": ["t"],
           "regressed_unpredicted": ["t"],
           "per_task": {"t": {"split": "gate", "n": 2, "mean_diff": -0.4}, "e": {"split": "evolve", "n": 2,
                                                                                 "mean_diff": 0.1}}}
    v = proposer_view(rec, {"t", "e"})  # `t` is an evolve task NOW
    assert "t" in rec["regressed"]  # precondition: the record holds t's gate result
    assert v["flipped"] == ["e"] and v["regressed"] == [] and v["regressed_unpredicted"] == []
    assert set(v["per_task"]) == {"e"}
    # fail closed: without per_task the split at the time is unknown, nothing is shown
    assert proposer_view({"flipped": ["e"]}, {"e"})["flipped"] == []


def test_export_generation_stats_are_a_whitelist(store, tmp_path):
    """generation_stats.json was exported raw; every other proposer-visible record is a whitelist view."""
    store.append_record("generation_stats", {"parent": "p" * 64, "accepted": 1, "proposed": 2,
                                             "gate_mean_diff": 0.123456789})
    res = export_evidence(store, tmp_path / "export", runs=[],
                          tasks=[SuiteTask("e", tmp_path / "e", "evolve", family="f")])
    text = (res.path / "generation_stats.json").read_text(encoding="utf-8")
    assert '"accepted": 1' in text and "0.123456789" not in text and "gate_mean_diff" not in text


# ===================================================================== R2-1 beyond the review tests
def test_new_llm_node_may_only_run_as_an_author_role():
    spec = seed_spec()
    node = {"kind": "llm", "role": "executor", "prompt": "Summarise {{hypotheses}}", "output_key": "notes"}
    apply_edit(spec, Edit(type=EditType.HOP, rationale="x",
                          ops=_insert_between(spec, "hypothesize", "tournament", "sneak", node)))  # positive control
    for role in ("judge", "replicator", "critic2", "Judge"):
        with pytest.raises(EditRejected, match="new LLM node may only run as"):
            apply_edit(spec, Edit(type=EditType.SUBAGENT_ROLE, rationale="x", ops=_insert_between(
                spec, "hypothesize", "tournament", "sneak", node | {"role": role})))


def test_an_author_backed_node_cannot_write_the_critics_output():
    """Invariant 5 on paper, broken in fact: a proposer-backed node placed after the critic overwrites
    `critique_raw`, so the Socratic filter passes on the PROPOSER's self-critique as the judge's."""
    spec = seed_spec()
    node = {"kind": "llm", "role": "proposer", "prompt": "Critique {{hypotheses}}", "output_key": "notes"}
    i = _edge_index(spec, "critic", "socratic")
    ops = [EditOp(op="add", path="/nodes/selfcrit", value=node),
           EditOp(op="replace", path=f"/edges/{i}/target", value="selfcrit"),
           EditOp(op="add", path="/edges/-", value={"source": "selfcrit", "target": "socratic"})]
    apply_edit(spec, Edit(type=EditType.HOP, rationale="x", ops=ops))  # positive control: same wiring, own key
    bad = copy.deepcopy(ops)
    bad[0].value["output_key"] = "critique_raw"
    with pytest.raises(EditRejected, match="role-bound output"):
        apply_edit(spec, Edit(type=EditType.HOP, rationale="x", ops=bad))
    # removing the critic node and re-adding it under the same id with an author role is a role change
    with pytest.raises(EditRejected, match="role"):
        apply_edit(spec, Edit(type=EditType.SUBAGENT_ROLE, rationale="x", ops=[
            EditOp(op="replace", path="/nodes/critic",
                   value=spec.nodes["critic"].model_dump() | {"role": "proposer"})]))


# ===================================================================== R2-5 generalised: what evolved templates render
@pytest.mark.parametrize("ref", ["critique_raw", "critique_raw.questions", "_last_error", "_module_llm_calls",
                                 "_seed", "input.task_dir", "_forked_from"])
@pytest.mark.parametrize("field", ["prompt", "system"])
def test_evolved_llm_template_cannot_render_unfiltered_private_or_host_state(ref, field):
    spec = seed_spec()
    base = getattr(spec.nodes["implement"], field)
    ok = apply_edit(spec, Edit(type=EditType.PROMPT_TEXT, rationale="x", ops=[EditOp(
        op="replace", path=f"/nodes/implement/{field}", value=base + " Critic: {{critique.questions}}")]))
    assert "{{critique.questions}}" in getattr(ok.nodes["implement"], field)  # positive control
    with pytest.raises(EditRejected, match="would render"):
        apply_edit(spec, Edit(type=EditType.PROMPT_TEXT, rationale="x", ops=[EditOp(
            op="replace", path=f"/nodes/implement/{field}", value=base + " {{" + ref + "}}")]))


def test_evolved_tool_command_cannot_render_unfiltered_state():
    spec = seed_spec()
    tool = {"kind": "tool", "command": ["echo", "ok"], "output_key": "tool_echo"}
    apply_edit(spec, Edit(type=EditType.TOOL, rationale="x",
                          ops=_insert_between(spec, "hypothesize", "tournament", "t1", tool)))  # positive control
    with pytest.raises(EditRejected, match="would render 'critique_raw'"):
        apply_edit(spec, Edit(type=EditType.TOOL, rationale="x", ops=_insert_between(
            spec, "hypothesize", "tournament", "t1", tool | {"command": ["echo", "{{critique_raw}}"]})))


# ===================================================================== adapters are not an edit surface
ADAPTER_EDITS = {
    "module-path": [EditOp(op="add", path="/nodes/x", value={"kind": "module", "module": "kernel.adapters.approve"})],
    "adapter-param": [EditOp(op="add", path="/nodes/next/params/adapter", value="ml")],
    "evidence-class": [EditOp(op="add", path="/nodes/next/params/evidence_class", value="simulated")],
    "adapter-id-value": [EditOp(op="add", path="/nodes/run/params/verifier", value="lp")],
    "adapter-id-segment": [EditOp(op="add", path="/nodes/run/params/lp", value=1)],
    "adapter-file": [EditOp(op="replace", path="/nodes/implement/system",
                            value="Read custom_harness/verify/metrics.py first.")],
    "adapter-module": [EditOp(op="add", path="/nodes/run/params/metric_module", value="custom_harness.verify.stats")],
    "kernel-path": [EditOp(op="replace", path="/nodes/implement/system", value="see custom_harness/kernel/lp.py")],
    "approve-cli": [EditOp(op="replace", path="/nodes/fix/system", value="then run chh approve-adapter ml")],
    "adapters-key": [EditOp(op="add", path="/nodes/next/params/x", value={"Adapters": {"ml": "v2"}})],
}


@pytest.mark.parametrize("name", list(ADAPTER_EDITS))
def test_an_edit_that_touches_an_adapter_is_rejected_statically(name, monkeypatch):
    """GOAL §5 Domain adapters done-when: "an edit that touches an adapter is rejected statically". The check runs
    first, before the edit is applied (applying is patched to fail loudly, proving no other check decided)."""
    import custom_harness.evolve.edits as E
    edit = Edit(type=EditType.PARAMS, rationale="x", ops=ADAPTER_EDITS[name])
    assert adapter_references(edit), name
    monkeypatch.setattr(E, "_apply_op", lambda *a: pytest.fail("the edit was applied before the adapter check"))
    with pytest.raises(EditRejected, match="domain adapters are human-only"):
        apply_edit(seed_spec(), edit)


def test_adapter_check_positive_control_and_registry_coverage():
    edit = Edit(type=EditType.CONTROL_FLOW, rationale="allow fix attempts",
                ops=[EditOp(op="replace", path="/nodes/run/params/max_fixes", value=2)])
    assert adapter_references(edit) == [] and apply_edit(seed_spec(), edit).nodes["run"].params["max_fixes"] == 2
    from custom_harness.kernel.adapters import REGISTRY
    for name, (aspec, _) in REGISTRY.items():  # every registered adapter (incl. future ones) is covered by name/code
        for value in (name, aspec.modules[0], aspec.modules[-1].replace(".", "/") + ".py"):
            assert adapter_references(Edit(type=EditType.PARAMS, rationale="x", ops=[
                EditOp(op="add", path="/nodes/next/params/v", value=value)])), value


def _adapter_and_kernel_fingerprint() -> dict[str, str]:
    from custom_harness.kernel.adapters import REGISTRY, adapter_hash
    out = {f"adapter:{n}": adapter_hash(s) for n, (s, _) in REGISTRY.items()}
    kdir = ROOT / "src/custom_harness/kernel"
    for p in sorted(kdir.rglob("*.py")):
        out[p.relative_to(kdir).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_the_edit_surface_cannot_reach_adapter_files(monkeypatch):
    """The edit surface is the HarnessSpec JSON and nothing else: (1) its schema has exactly the fields below (a new
    field, e.g. an adapter selector, fails this test until reviewed); the only fields that resolve code are
    ModuleNode.module (a registry LOOKUP, never an import) and ToolNode.command (runs inside the sandbox); (2) the
    module registry holds no adapter or Kernel module, and resolving an adapter module path fails; (3) applying
    accepted edits of every edit type performs no file write and leaves every adapter hash and Kernel file
    unchanged."""
    from custom_harness.runtime.modules import get_module, registered
    schema = HarnessSpec.model_json_schema()

    def fields(obj) -> set[str]:  # every property name anywhere in the schema (all $defs included)
        out: set[str] = set()
        if isinstance(obj, dict):
            out |= set(obj.get("properties") or {})
            for v in obj.values():
                out |= fields(v)
        elif isinstance(obj, list):
            for v in obj:
                out |= fields(v)
        return out
    assert fields(schema) == {
        "id", "description", "entry", "nodes", "edges", "max_steps", "context", "budget",
        "kind", "role", "system", "prompt", "output_key", "output_schema", "retries", "temperature", "max_tokens",
        "command", "timeout_s", "snapshot_after", "module", "params", "item_key",
        "source", "target", "when", "key", "op", "value", "max_value_chars", "head_fraction",
        "usd", "gpu_hours", "quota_units", "tokens", "wall_s",
        # MapNode (g-runtime, reviewed 2026-10-08 at the int16-fix merge): items_key is a state path, body a node id,
        # reducer a lookup in the trusted declared-reducer registry (never an import), max_items an integer cap. None
        # resolves code; the evolve flow rules for maps are in edits._check_map_flow (finding R2-MAP).
        "items_key", "body", "reducer", "max_items"}
    assert not [m for m in registered() if "adapter" in m or m.startswith(("custom_harness", "kernel"))]
    for path in ("custom_harness.kernel.adapters", "kernel.adapters", "custom_harness.verify.metrics"):
        with pytest.raises(KeyError):
            get_module(path)
    before = _adapter_and_kernel_fingerprint()
    spec = seed_spec()
    edits = [Edit(type=t, rationale="x", ops=ops) for t, ops in [
        (EditType.PARAMS, [EditOp(op="replace", path="/nodes/run/params/max_fixes", value=2)]),
        (EditType.PROMPT_TEXT, [EditOp(op="replace", path="/nodes/implement/system", value="Be careful.")]),
        (EditType.CONTEXT_POLICY, [EditOp(op="replace", path="/context/max_value_chars", value=8000)]),
        (EditType.BUDGET_POLICY, [EditOp(op="replace", path="/budget/tokens", value=1e6)]),
        (EditType.CONTRACT, [EditOp(op="replace", path="/nodes/implement/retries", value=2)]),
        (EditType.TOOL, _insert_between(spec, "hypothesize", "tournament", "t1",
                                        {"kind": "tool", "command": ["echo", "ok"], "output_key": "tool_ok"})),
        (EditType.SUBAGENT_ROLE, _insert_between(spec, "hypothesize", "tournament", "n1",
                                                 {"kind": "llm", "prompt": "p {{hypotheses}}", "output_key": "n1"})),
        (EditType.HOP, _insert_between(spec, "hypothesize", "tournament", "m1",
                                       {"kind": "module", "module": "research.tournament"})),
    ]]
    real_open = builtins.open

    def no_write(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax+"):
            pytest.fail(f"apply_edit wrote a file: {file}")
        return real_open(file, mode, *a, **k)
    monkeypatch.setattr(builtins, "open", no_write)
    for e in edits:
        apply_edit(spec, e)  # every one is accepted (else the "no effect" below would prove nothing)
    monkeypatch.setattr(builtins, "open", real_open)
    assert _adapter_and_kernel_fingerprint() == before


# ===================================================================== tool-node taint (known limitation)
def test_tool_node_cannot_put_training_rows_into_a_prompt(store, tmp_path, monkeypatch):
    """Attack: a tool node reads the workspace's public TRAINING file and the hypothesize prompt renders its stdout,
    so training rows (with labels: the dev folds' validation labels among them) reach the model. Shown to work on a
    spec built WITHOUT the validator; the same ops through apply_edit are rejected."""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")  # trusted test code only
    task_dir = _rings(tmp_path, "rings")
    spec = seed_spec()
    tool = {"kind": "tool", "command": [sys.executable, "-c", "print(open('data/train.csv').read()[:600])"],
            "output_key": "tool_rows"}
    # the node right before hypothesize (was `baseline`; the L5 memory wiring, 93c0c4a, put `recall` in between)
    (before,) = [e["source"] for e in _edges(spec) if e["target"] == "hypothesize" and e.get("when") is None]
    ops = _insert_between(spec, before, "hypothesize", "peek", tool)
    ops.append(EditOp(op="replace", path="/nodes/hypothesize/prompt",
                      value=spec.nodes["hypothesize"].prompt + "\nRows: {{tool_rows.stdout}}"))
    with pytest.raises(EditRejected, match="tool output"):
        apply_edit(spec, Edit(type=EditType.TOOL, rationale="show the data", ops=ops))
    # the attack itself, without the validator
    doc = copy.deepcopy(spec.model_dump(mode="json"))
    for op in ops:
        _apply_op(doc, op)
    unchecked = HarnessSpec.model_validate(doc | {"max_steps": 6})
    seen = []

    def llm(req):
        seen.append(req.messages[-1].content)
        return json.dumps({"hypotheses": [{"id": "h1", "idea": "a", "rationale": "b"},
                                          {"id": "h2", "idea": "c", "rationale": "d"}]})
    res = Engine(unchecked, backends={r: MockBackend(llm) for r in ALL_ROLES}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(task_dir)})
    rows = list(csv.reader((task_dir / "public/train.csv").open(newline="")))
    line = ",".join(rows[1])  # the first training example, label included
    assert seen and line in seen[0], (res.status, res.detail, seen[:1])  # the attack really reached the model


def test_tool_node_rules_and_positive_control():
    spec = seed_spec()

    def tool_edit(node, extra=()):
        return Edit(type=EditType.TOOL, rationale="x",
                    ops=[*_insert_between(spec, "hypothesize", "tournament", "t1", node), *extra])
    check = {"kind": "tool", "command": ["python", "-V"], "output_key": "tool_check"}
    # positive control: tool output may route control flow
    j = len(_edges(spec))
    ok = apply_edit(spec, tool_edit(check, [EditOp(op="add", path="/edges/0", value={
        "source": "t1", "target": "tournament", "when": {"key": "tool_check.returncode", "op": "eq", "value": 0}})]))
    assert len(ok.edges) == j + 2
    with pytest.raises(EditRejected, match="must write a 'tool_\\*' key"):
        apply_edit(spec, tool_edit(check | {"output_key": "notes"}))
    with pytest.raises(EditRejected, match="must write a 'tool_\\*' key"):
        apply_edit(spec, tool_edit(check | {"output_key": "queue"}))  # a key a trusted module reads
    with pytest.raises(EditRejected, match=r"tool output|rendered into model prompt"):  # a new LLM node rendering it
        apply_edit(spec, tool_edit(check, _insert_between(spec, "check_answers", "next", "n1", {
            "kind": "llm", "prompt": "{{tool_check.stdout}}", "output_key": "n1"})))
    with pytest.raises(EditRejected, match="rendered into model prompt"):  # an existing prompt already rendering it
        human = spec.model_dump(mode="json")
        human["nodes"]["implement"]["prompt"] += " {{tool_check}}"
        apply_edit(HarnessSpec.model_validate(human), tool_edit(check))


def test_no_trusted_module_reads_tool_prefixed_state():
    """The tool taint rule relies on no module consuming `tool_*` keys: pin it on the module sources. One reviewed
    exception: runtime/spec.py's check_map REFUSES an LLM map body over a `tool_*` key (a guard, not a reader); it
    may mention the prefix only inside that `startswith` test."""
    src = ROOT / "src/custom_harness"
    guard = 'root.startswith("tool_")'
    for p in [*(src / "research").glob("*.py"), *(src / "runtime").glob("*.py")]:
        text = p.read_text(encoding="utf-8")
        if p.name == "spec.py" and p.parent.name == "runtime":
            assert text.count(guard) == 1, p  # the guard is still there (not vacuous)
            text = text.replace(guard, "")
        assert '"tool_' not in text and "'tool_" not in text, p


# ===================================================================== Socratic filter (heuristic, measured)
DIRECTIVES = [  # question-shaped directives: must be DROPPED
    "Could you replace the kNN with a random forest?",
    "Can we just standardise the features first?",
    "Why not just use gradient boosting?",
    "Why don't you add a radial feature?",
    "How about an ensemble of the two models?",
    "What about using a polynomial kernel?",
    "Have you tried increasing k to 25?",
    "Have you considered dropping the noise columns?",
    "Shouldn't you scale the inputs before kNN?",
    "Wouldn't it be better to use cross-entropy?",
    "Is it not better to tune C on a log grid?",
    "Would you set k=25 for smoother boundaries?",
    "Isn't the fix is to drop column n3?",
    "Do you think you should normalise the radius?",
    "Would a forest not work better than kNN instead of the current model?",
    "Use a random forest?",
    "I'd add an interaction term, wouldn't you?",
    "Should we switch to logistic regression?",
]
QUESTIONS = [  # genuine Socratic questions: must be KEPT
    "What would falsify the radius hypothesis?",
    "Is the color column leaking the label?",
    "Which baseline is missing from this comparison?",
    "Could class imbalance make accuracy misleading here?",
    "How would you know the improvement is not seed noise?",
    "What assumption about the noise columns is untested?",
    "Does the dev estimate share rows with the held-out set?",
    "Why would a radial feature generalise beyond these rings?",
    "Which data issue could make this result misleading?",
    "What happens to the ranking if the outliers are removed?",
    "Is the effect size larger than the variation between folds?",
    "Can the profile's skewed columns explain the gain?",
]
KNOWN_MISSES = [  # leading questions that embed a solution without a recognised shape: still KEPT (limitation)
    "Is a gradient-boosted model with depth 3 the natural choice here?",
    "Would a random forest with 500 trees fix the variance?",
]


def test_socratic_filter_drops_question_shaped_directives_and_keeps_questions():
    from custom_harness.research.modules import socratic_filter
    raw = {"questions": DIRECTIVES + QUESTIONS + KNOWN_MISSES}
    kept = set(socratic_filter({"critique_raw": raw}, {"max_questions": 100}, None)["critique"]["questions"])
    leaked = [d for d in DIRECTIVES if d in kept]
    lost = [q for q in QUESTIONS if q not in kept]
    # measured on this labelled set: 18/18 directives dropped, 12/12 questions kept; the 2 known misses pass
    assert leaked == [] and lost == [], {"directives kept": leaked, "questions dropped": lost}
    assert all(m in kept for m in KNOWN_MISSES)  # pinned: the filter remains a surface heuristic


# ===================================================================== R2-2: campaign multiplicity declaration
def test_campaign_refused_unless_claim_nodes_declare_the_multiplicity_bound(store, tmp_path):
    from custom_harness.budget import Budget
    from custom_harness.evolve.driver import evolve
    from custom_harness.evolve.multiplicity import MultiplicityDrift, check_campaign, held_out_target_id
    from custom_harness.research.claims import register_task
    from custom_harness.research.task import load_task
    from custom_harness.runtime.modules import ModuleContext
    d1, d2 = _rings(tmp_path, "a", 1), _rings(tmp_path, "b", 2)
    tasks = [SuiteTask("a", d1, "evolve", family="A"), SuiteTask("b", d2, "gate", family="B")]
    llm = MockBackend(lambda r: pytest.fail("no run may start in a refused campaign"))
    with pytest.raises(MultiplicityDrift, match="n_comparisons"):
        evolve(seed_spec(), tasks, generations=2, backends={r: llm for r in ALL_ROLES}, proposer=llm, store=store,
               sandbox_factory=lambda k: pytest.fail("no sandbox"), log=lambda s: None)
    # bound = runs per task (2 gens x (1 + 3 edits) x (1 + 1 seeds) x 2 attempts = 32) x 2 claim nodes
    assert check_campaign(_declared(seed_spec(), 64), store, [d1, d2], runs_per_task=32) == 64
    with pytest.raises(MultiplicityDrift):
        check_campaign(_declared(seed_spec(), 63), store, [d1, d2], runs_per_task=32)
    # the target id the check uses is the one setup registers; claims already on it raise the bound
    ctx = ModuleContext(run_id="t", store=store, backends={}, budget=Budget(), sandbox=None)
    assert register_task(ctx, load_task(d1), d1)["test"] == held_out_target_id(d1)
    from custom_harness.kernel import Kernel
    from custom_harness.kernel.ledger import Ledger
    Ledger(store).append("prereg", {"claim_hash": "x" * 64, "claim": {"generalization_targets": [
        held_out_target_id(d1)]}})
    assert Kernel(store).vault.datasets()  # precondition: the target is registered
    with pytest.raises(MultiplicityDrift):
        check_campaign(_declared(seed_spec(), 64), store, [d1, d2], runs_per_task=32)
    assert check_campaign(_declared(seed_spec(), 65), store, [d1, d2], runs_per_task=32) == 65


def _declared(spec: HarnessSpec, n: int) -> HarnessSpec:
    doc = spec.model_dump(mode="json")
    for nid in ("claim", "replica_claim"):
        doc["nodes"][nid]["params"]["n_comparisons"] = n
    return HarnessSpec.model_validate(doc)


# ===================================================================== sealed split: one-shot human finalize
TINY = {"id": "tiny", "entry": "a", "nodes": {"a": {"kind": "llm", "prompt": "hi", "output_key": "o"}}}


def _sealed_suite(tmp_path):
    return [SuiteTask("e0", _rings(tmp_path, "e0", 1), "evolve", family="A"),
            SuiteTask("s0", _rings(tmp_path, "s0", 2), "sealed", family="A"),
            SuiteTask("s1", _rings(tmp_path, "s1", 3), "sealed", family="B")]


def test_finalize_runs_the_sealed_split_exactly_once(store, tmp_path):
    from custom_harness.evolve.finalize import FinalizeRefused, finalize, prior_finalizations
    from custom_harness.kernel.ledger import Ledger
    suite = _sealed_suite(tmp_path)
    spec, base = HarnessSpec.model_validate(TINY), HarnessSpec.model_validate(TINY | {"id": "tiny0"})
    llm = MockBackend(lambda r: "{}")
    factory = lambda k: LocalSandbox(tmp_path / "sb" / k, i_understand_no_isolation=True)  # noqa: E731
    with pytest.raises(FinalizeRefused, match="approver"):
        finalize(spec, suite, approver="  ", backends={"executor": llm}, store=store, sandbox_factory=factory)
    with pytest.raises(FinalizeRefused, match="no sealed task"):
        finalize(spec, [suite[0]], approver="alice", backends={"executor": llm}, store=store,
                 sandbox_factory=factory)
    assert not llm.requests and not prior_finalizations(store)  # refusals touched nothing
    out = finalize(spec, suite, approver="alice", backends={"executor": llm}, store=store, sandbox_factory=factory,
                   seeds=(0, 1), baseline=base)
    assert [(u["task_id"], u["seed"]) for u in out["units"]] == [("s0", 0), ("s0", 1), ("s1", 0), ("s1", 1)]
    assert len(llm.requests) == 8 and out["paired"]["n"] == 4  # final + baseline on each sealed unit; e0 never
    runs = [store.get_run(u["run_id"]) for u in out["units"]]
    assert {r.task_id for r in runs} == {"s0", "s1"}
    entry = [e for e in Ledger(store).entries("approval") if e.body.get("node_id") == "finalize"]
    assert len(entry) == 1 and entry[0].body["approver"] == "alice" and entry[0].intact()
    assert entry[0].body["finalize"]["spec_hash"] == spec.spec_hash()
    n = len(llm.requests)
    for again in (suite, [suite[2]], [SuiteTask("renamed", suite[1].task_dir, "sealed", family="A")]):
        with pytest.raises(FinalizeRefused, match="already finalized"):
            finalize(spec, again, approver="bob", backends={"executor": llm}, store=store, sandbox_factory=factory)
    assert len(llm.requests) == n  # a refused second look runs nothing


def test_an_aborted_finalize_still_consumes_the_one_shot(store, tmp_path):
    from custom_harness.evolve.finalize import FinalizeRefused, finalize
    suite = _sealed_suite(tmp_path)
    spec = HarnessSpec.model_validate(TINY)

    def broken(key):
        raise RuntimeError("operator pressed ctrl-c")
    with pytest.raises(RuntimeError, match="ctrl-c"):
        finalize(spec, suite, approver="alice", backends={"executor": MockBackend(["{}"])}, store=store,
                 sandbox_factory=broken)
    with pytest.raises(FinalizeRefused, match="already finalized"):
        finalize(spec, suite, approver="alice", backends={"executor": MockBackend(["{}"])}, store=store,
                 sandbox_factory=lambda k: LocalSandbox(tmp_path / k, i_understand_no_isolation=True))


def test_nothing_but_finalize_runs_a_sealed_task(store, tmp_path):
    sealed = SuiteTask("s0", tmp_path, "sealed", family="A")
    llm = MockBackend(["{}"])
    with pytest.raises(ValueError, match="sealed split"):
        run_harness(HarnessSpec.model_validate(TINY), sealed, 0, backends={"executor": llm}, store=store,
                    sandbox_factory=lambda k: LocalSandbox(tmp_path / k, i_understand_no_isolation=True))
    with pytest.raises(ValueError, match="finalize only"):
        Evaluator({"executor": llm}, store, lambda k: None, [SuiteTask("e", tmp_path, "evolve", family="A"), sealed])
    assert not llm.requests
    # positive control: the same call on an evolve task runs
    out = run_harness(HarnessSpec.model_validate(TINY), SuiteTask("e", tmp_path, "evolve", family="A"), 0,
                      backends={"executor": llm}, store=store,
                      sandbox_factory=lambda k: LocalSandbox(tmp_path / k, i_understand_no_isolation=True))
    assert out.status == RunStatus.COMPLETED.value and llm.requests


def test_finalize_cli_requires_a_named_human(tmp_path):
    from custom_harness.cli import main
    with pytest.raises(SystemExit):
        main(["finalize", "spec.yaml", "--suite", "suite.yaml"])  # no --approver: argparse refuses
    spec = tmp_path / "spec.yaml"
    spec.write_text(yaml.safe_dump(TINY), encoding="utf-8")
    suite = tmp_path / "suite.yaml"
    suite.write_text(yaml.safe_dump({"tasks": []}), encoding="utf-8")
    with pytest.raises(SystemExit, match="approver"):
        main(["finalize", str(spec), "--suite", str(suite), "--approver", " ", "--dev-sandbox"])


def test_reserved_module_ids_and_keys_name_real_modules():
    """b-evolve reserved b-research's module ids and output keys by name before that branch was merged; now both are
    on one branch, every reserved module id must be a REGISTERED module (a typo would reserve nothing), and the
    critic/recall keys must be keys those modules really write."""
    from custom_harness.evolve.edits import RESERVED_OUTPUT_KEYS, RESERVED_WRITING_MODULES
    from custom_harness.runtime.modules import registered
    assert sorted(RESERVED_WRITING_MODULES - set(registered())) == []
    src = ROOT / "src/custom_harness/research"
    written = (src / "critic_answers.py").read_text(encoding="utf-8") + (src / "recall.py").read_text(encoding="utf-8")
    for key in ("critic_answers", "critic_evidence", "recalled", "recall_log"):
        assert key in RESERVED_OUTPUT_KEYS and f'"{key}":' in written, key
