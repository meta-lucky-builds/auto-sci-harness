"""Human-owned run policy: model-family separation (GOAL §10 invariant 5, §5 L0 "same-family detection").

A role whose work JUDGES or REPLICATES another role's work must come from a different model family: the Socratic
critic and the hypothesis tournament (role `judge`) judge the `proposer`'s hypotheses; the independent replicator
(role `replicator`) re-implements the `executor`'s method. This table lives in trusted code, not in the
(evolvable) harness spec, so an evolved harness cannot relax it.

Families are derived from the backend: a vendor provider IS the family (Codex CLI -> openai); for generic hosts
(Ollama, vLLM, llama.cpp, ...) the family comes from the model name. A family that cannot be determined is
"unknown:<model>" and never counts as different (fail closed).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FAMILY_SEPARATION: tuple[tuple[str, str], ...] = (("judge", "proposer"), ("replicator", "executor"))

VENDOR_PROVIDERS = frozenset({"openai", "anthropic", "google", "mistral", "meta", "alibaba", "deepseek",
                              "microsoft", "xai", "cohere", "mock"})
# model-name prefixes for generic hosts (lower-case); gpt-oss is an OpenAI open-weights model -> openai
_MODEL_PREFIXES = (("gpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"), ("codex", "openai"),
                   ("claude", "anthropic"), ("gemini", "google"), ("gemma", "google"), ("llama", "meta"),
                   ("qwen", "alibaba"), ("qwq", "alibaba"), ("mistral", "mistral"), ("mixtral", "mistral"),
                   ("codestral", "mistral"), ("deepseek", "deepseek"), ("phi", "microsoft"), ("grok", "xai"),
                   ("command", "cohere"))


class PolicyError(ValueError):
    """A run configuration violates a human-owned policy (refused before any model call)."""


def model_family(backend: Any) -> str:
    declared = getattr(backend, "family", None)
    if declared:
        return str(declared)
    provider = str(getattr(backend, "provider", "") or "").lower()
    if provider in VENDOR_PROVIDERS:
        return provider
    model = str(getattr(backend, "model", "") or "").lower().split("/")[-1]
    for prefix, family in _MODEL_PREFIXES:
        if model.startswith(prefix):
            return family
    return f"unknown:{provider}/{model}"


def effective_bindings(backends: Mapping[str, Any]) -> dict[str, Any]:
    """Role -> backend as calls will actually resolve: a module's model call for an UNBOUND role falls back to the
    `executor` backend (runtime.modules.ModuleContext.llm), so an unbound judging role is the executor."""
    eff = dict(backends)
    if "executor" in backends:
        for a, _ in FAMILY_SEPARATION:
            eff.setdefault(a, backends["executor"])
    return eff


def family_violations(backends: Mapping[str, Any]) -> list[str]:
    """Violations among the effective bindings (a pair is checked when both roles resolve to a backend)."""
    out = []
    backends = effective_bindings(backends)
    for a, b in FAMILY_SEPARATION:
        if a in backends and b in backends:
            fa, fb = model_family(backends[a]), model_family(backends[b])
            if fa == fb or fa.startswith("unknown:") or fb.startswith("unknown:"):
                out.append(f"role {a!r} ({fa}) must be a different model family from role {b!r} ({fb})")
    return out


def model_policy(backends: Mapping[str, Any], *, allow_same_family: bool) -> dict[str, Any]:
    """The record every run carries: families per role and the invariant-5 status. Raises PolicyError on a
    violation unless explicitly overridden; an override is recorded as NOT MET, never hidden."""
    violations = family_violations(backends)
    if violations and not allow_same_family:
        raise PolicyError("invariant 5 (critic/replicator family != author family) violated: "
                          + "; ".join(violations) + ". Bind a different model family, or override explicitly "
                          "(the run is then recorded as not meeting invariant 5).")
    return {"families": {role: model_family(b) for role, b in sorted(effective_bindings(backends).items())},
            "invariant_5": "met" if not violations else "NOT MET (explicit override): " + "; ".join(violations)}
