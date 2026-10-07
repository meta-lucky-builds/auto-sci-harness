"""Invariant 5 (GOAL §10): a role that judges or replicates another role's work comes from a different model
family. Enforced before any model call; an explicit override is recorded as NOT MET, never hidden."""

import pytest
import yaml

import custom_harness.research  # noqa: F401
from custom_harness.gateway import MockBackend
from custom_harness.gateway.codex_cli import CodexCLIBackend
from custom_harness.policy import PolicyError, family_violations, model_family
from custom_harness.runtime import Engine, HarnessSpec
from test_research import SPEC


class _Host:  # a generic OpenAI-compatible host (Ollama / vLLM): family from the model name
    def __init__(self, provider, model):
        self.provider, self.model = provider, model


def _mock(family):
    m = MockBackend(["{}"])
    m.family = family
    return m


def test_model_family_detection():
    assert model_family(CodexCLIBackend()) == "openai"
    assert model_family(_Host("ollama", "llama3.1:8b")) == "meta"
    assert model_family(_Host("vllm", "Qwen/Qwen2.5-7B-Instruct")) == "alibaba"
    assert model_family(_Host("ollama", "gpt-oss:20b")) == "openai"  # OpenAI open weights: still openai
    assert model_family(_Host("ollama", "mystery-7b")).startswith("unknown:")


def test_violations_cover_unbound_roles_that_fall_back_to_the_executor():
    codex, llama = CodexCLIBackend(), _Host("ollama", "llama3.1:8b")
    assert family_violations({"proposer": codex, "executor": codex, "judge": llama, "replicator": llama}) == []
    # judge unbound: the tournament's module call falls back to the executor (same family as the proposer)
    v = family_violations({"proposer": codex, "executor": codex, "replicator": llama})
    assert len(v) == 1 and "'judge'" in v[0]
    # unknown families never count as different (fail closed)
    assert family_violations({"proposer": codex, "judge": _Host("ollama", "mystery-7b")})


def test_engine_refuses_a_same_family_configuration_unless_explicitly_overridden(store, monkeypatch):
    monkeypatch.delenv("CHH_ALLOW_SAME_FAMILY", raising=False)
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    same = MockBackend(["{}"])
    with pytest.raises(PolicyError, match="invariant 5"):
        Engine(spec, backends={"proposer": same, "executor": same, "judge": same, "replicator": same}, store=store)
    ok = Engine(spec, backends={"proposer": _mock("a"), "executor": _mock("a"), "judge": _mock("b"),
                                "replicator": _mock("b")}, store=store)
    assert ok.model_policy["invariant_5"] == "met"
    over = Engine(spec, backends={"proposer": same, "executor": same, "judge": same, "replicator": same},
                  store=store, allow_same_family=True)
    assert over.model_policy["invariant_5"].startswith("NOT MET (explicit override)")
