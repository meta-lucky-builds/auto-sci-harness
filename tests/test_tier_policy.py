"""Tier policy as human-only config (GOAL §5 L9): no model output, evolved edit, sandbox program or module can
change it; a change made behind the human pin is detected and refused (fail closed)."""

import json

import pytest
from pydantic import ValidationError

from custom_harness import cli
from custom_harness.config import data_dir
from custom_harness.evolve import Edit, EditOp, EditRejected, EditType, apply_edit
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Ledger
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from custom_harness.tiers import DEFAULT_POLICY, Action, Tier

LOOSE = {a.value: "T6_NONE" for a in Action}  # what an attacker wants: everything automatic


@pytest.fixture
def dstore():
    s = EvidenceStore(data_dir() / "store")
    yield s
    s.close()


def _pins(store) -> int:
    return len(Ledger(store).entries("tier_policy"))


def test_human_pin_lifecycle_and_tamper_detection(dstore, capsys):
    from custom_harness.studio.tierpolicy import TierPolicyError, load_policy, policy_path
    assert load_policy(dstore) == DEFAULT_POLICY  # nothing configured: the code default
    assert cli.main(["tier-policy", "init"]) == 0
    with pytest.raises(TierPolicyError, match="never pinned"):
        load_policy(dstore)
    doc = json.loads(policy_path().read_text(encoding="utf-8"))
    doc["admit_skill"] = "T0_FORMAL"  # the human tightens one floor
    policy_path().write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(SystemExit, match="hashes to"):
        cli.main(["tier-policy", "pin", "--by", "alice", "--sha256", "0" * 64])
    assert cli.main(["tier-policy", "pin", "--by", "alice"]) == 0
    assert load_policy(dstore)[Action.ADMIT_SKILL] == Tier.T0_FORMAL
    (pin,) = Ledger(dstore).entries("tier_policy")
    assert pin.body["by"] == "alice" and pin.body["policy"]["admit_skill"] == "T0_FORMAL"
    # someone with host access rewrites the file behind the pin: every load refuses it
    policy_path().write_text(json.dumps(LOOSE), encoding="utf-8")
    with pytest.raises(TierPolicyError, match="changed after its last human pin"):
        load_policy(dstore)
    with pytest.raises(SystemExit, match="changed after"):
        cli.main(["tier-policy", "show"])
    policy_path().unlink()
    with pytest.raises(TierPolicyError, match="missing"):
        load_policy(dstore)
    for bad in ({"admit_skill": "T0_FORMAL"}, {**doc, "extra": None}, {**doc, "admit_skill": "T9"}, [1]):
        policy_path().write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(SystemExit, match="refused"):
            cli.main(["tier-policy", "pin", "--by", "alice"])
    with pytest.raises(SystemExit, match="refused"):
        cli.main(["tier-policy", "pin"])
    assert _pins(dstore) == 1


def test_no_model_or_evolved_node_can_change_the_tier_policy(dstore):
    from custom_harness.studio.tierpolicy import human_config_dir, load_policy
    before = load_policy(dstore)
    # 1. a model's output (LLM node) carrying a looser policy only reaches run state
    spec = HarnessSpec.model_validate({"id": "tp", "entry": "m", "nodes": {
        "m": {"kind": "llm", "prompt": "set policy", "output_key": "tier_policy"}}})
    res = Engine(spec, backends={"executor": MockBackend([json.dumps(LOOSE)])}, store=dstore).run("t", {})
    assert res.state["tier_policy"] == json.dumps(LOOSE)  # the attack really reached state
    assert load_policy(dstore) == before and _pins(dstore) == 0
    # 2. the harness spec (the evolved surface) has no field for it, so no evolved edit can carry one
    with pytest.raises(ValidationError):
        HarnessSpec.model_validate({**spec.model_dump(mode="json"), "tier_policy": LOOSE})
    with pytest.raises((EditRejected, ValidationError)):
        apply_edit(spec, Edit(type=EditType.HOP, rationale="x",
                              ops=[EditOp(op="add", path="/tier_policy", value=LOOSE)]),
                   protected_nodes=(), terminal_required=())
    # 3. no sandbox program can reach the file: no workspace may be, sit inside or contain the human config dir
    from custom_harness.sandbox.base import ProtectedMount
    human = human_config_dir()
    for ws in (human, human / "ws", data_dir() / "human" / "x" / "y"):
        with pytest.raises(ProtectedMount):
            LocalSandbox(ws, i_understand_no_isolation=True)
    # 4. module code cannot pin one: tests/test_studio_kill.py::test_module_code_cannot_write_a_kill_or_a_tier_policy
    assert load_policy(dstore) == before


def test_protected_before_the_studio_is_ever_imported(tmp_path):
    """The protection must not depend on studio code having run in this process."""
    from custom_harness.sandbox.base import ProtectedMount
    with pytest.raises(ProtectedMount):
        LocalSandbox(data_dir() / "human" / "ws", i_understand_no_isolation=True)
