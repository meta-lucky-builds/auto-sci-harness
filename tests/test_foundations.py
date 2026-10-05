import pytest

from custom_harness.budget import Budget, BudgetExceeded
from custom_harness.gateway import ProviderBlocked, load_registry, redact, require_usable
from custom_harness.records import Claim, Cost, Evidence, EvidenceKind
from custom_harness.tiers import Action, PolicyDecision, Tier, chain_tier, decide


# --- tiers & policy -------------------------------------------------------
def test_chain_tier_is_weakest_link():
    assert chain_tier([Tier.T0_FORMAL, Tier.T5_LLM_JUDGE, Tier.T1_EXECUTABLE]) == Tier.T5_LLM_JUDGE
    assert chain_tier([]) == Tier.T6_NONE


def test_policy_gates_by_tier():
    assert decide(Action.PUBLISH_VERIFIED, Tier.T1_EXECUTABLE) == PolicyDecision.ALLOW
    assert decide(Action.PUBLISH_VERIFIED, Tier.T5_LLM_JUDGE) == PolicyDecision.NEEDS_HUMAN
    assert decide(Action.PUBLISH_SUPPORTED, Tier.T6_NONE, human_approved=True) == PolicyDecision.DENY
    assert decide(Action.MODIFY_VERIFIERS, Tier.T0_FORMAL) == PolicyDecision.NEEDS_HUMAN
    assert decide(Action.ADMIT_SKILL, Tier.T2_STATISTICAL) == PolicyDecision.NEEDS_HUMAN


# --- store ------------------------------------------------------------------
def test_blob_roundtrip_and_corruption(store):
    ref = store.blobs.put(b"hello")
    assert store.blobs.get(ref) == b"hello"
    store.blobs._path(ref).write_bytes(b"tampered")
    with pytest.raises(IOError):
        store.blobs.get(ref)


def test_claims_require_existing_evidence_and_take_weakest_tier(store):
    with pytest.raises(KeyError):
        store.add_claim(Claim(text="x improves y", evidence_ids=("ev_missing",)))
    ref = store.blobs.put_json({"acc": 0.91})
    e1 = Evidence(kind=EvidenceKind.TEST, tier=Tier.T1_EXECUTABLE, verifier_id="pytest",
                  produced_by_run=None, payload_ref=ref)
    e2 = Evidence(kind=EvidenceKind.JUDGMENT, tier=Tier.T5_LLM_JUDGE, verifier_id="panel",
                  produced_by_run=None, payload_ref=ref)
    store.add_evidence(e1)
    store.add_evidence(e2)
    c = Claim(text="x improves y", evidence_ids=(e1.id, e2.id))
    store.add_claim(c)
    assert store.claim_tier(c.id) == Tier.T5_LLM_JUDGE


def test_evidence_requires_blob(store):
    with pytest.raises(ValueError):
        store.add_evidence(Evidence(kind=EvidenceKind.LOG, tier=Tier.T1_EXECUTABLE, verifier_id="v",
                                    produced_by_run=None, payload_ref="0" * 64))


# --- budget -----------------------------------------------------------------
def test_budget_warns_then_stops():
    warnings = []
    b = Budget(limits={"usd": 1.0}, on_warn=lambda u, used, lim: warnings.append(u))
    b.charge(Cost(usd=0.85))
    assert warnings == ["usd"]
    with pytest.raises(BudgetExceeded):
        b.charge(Cost(usd=0.2))


def test_budget_rejects_unknown_unit():
    with pytest.raises(ValueError):
        Budget(limits={"dollars": 1})


# --- provider registry (PLAN §14.2) ------------------------------------------
def test_byos_gate():
    reg = load_registry()
    assert require_usable("openai-codex-cli", reg)["byos_status"] == "allowed"
    for blocked in ("anthropic-subscription", "openai-siwc", "google-subscription"):
        with pytest.raises(ProviderBlocked):
            require_usable(blocked, reg)
    with pytest.raises(ProviderBlocked):
        require_usable("nonexistent", reg)


def test_registry_override(tmp_path):
    override = tmp_path / "o.yaml"
    override.write_text("openai-codex-cli:\n  provider: openai\n  mode: byos\n  byos_status: prohibited\n")
    with pytest.raises(ProviderBlocked):
        require_usable("openai-codex-cli", load_registry(override))


def test_strict_schema_conversion():
    from custom_harness.gateway.base import to_strict_schema
    s = to_strict_schema({"type": "object", "properties": {
        "a": {"type": "string"},
        "b": {"type": "array", "items": {"type": "object", "properties": {"c": {"type": "integer"}}}}},
        "required": ["a"]})
    assert s["additionalProperties"] is False and sorted(s["required"]) == ["a", "b"]
    assert s["properties"]["a"]["type"] == "string"
    assert s["properties"]["b"]["type"] == ["array", "null"]
    inner = s["properties"]["b"]["items"]
    assert inner["additionalProperties"] is False and inner["properties"]["c"]["type"] == ["integer", "null"]


def test_redaction():
    s = "key sk-abcdefghijklmnopqrstuvwxyz123 and Bearer abcdefghijklmnopqrstuvwxyz"
    out = redact(s)
    assert "sk-abc" not in out and "abcdefghijklmnopqrstuvwxyz" not in out
    assert "Bearer [REDACTED]" in out
