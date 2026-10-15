"""Provider registry loader and BYOS gate (PLAN §14.2; GOAL §10 invariant 11).

`require_usable` is the runtime gate. A subscription (BYOS) entry is usable only when ALL hold: its status is
"allowed", its key is on the human-owned BYOS_ALLOWLIST below (in code, so a local override file cannot add one), its
provider matches the allowlist's, and its `auth_kind` is the official kind the allowlist names. The registry YAML (and
its override file) can only narrow what the allowlist permits, never widen it.

`policy_problems` is the static, offline release check of the same policy over a whole registry (unknown providers,
missing licence flags, auth kinds that are not official, BYOS entries off the allowlist); `chh verify-registry` runs it
together with `costs.registry_problems` and, with --release, `costs.release_problems` (tests/test_registry_verify.py).
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from ..config import data_dir


class ProviderBlocked(PermissionError):
    pass


# HUMAN-OWNED POLICY (invariant 11). The only subscription entries that may ever be "allowed", with their provider and
# the official auth kind they use. Adding a key here is a policy decision for the owner, never an agent's edit.
BYOS_ALLOWLIST: dict[str, dict[str, str]] = {
    "openai-codex-cli": {"provider": "openai", "auth_kind": "official_cli"},
}
# Official auth kinds per access mode. Anything else (reused session tokens, cookies, scraped or shared logins) is not
# official and fails the check.
OFFICIAL_AUTH: dict[str, frozenset[str]] = {
    "byos": frozenset({"official_cli", "official_oauth"}),  # the provider's own client / sign-in flow, used by the user
    "api": frozenset({"api_key"}),  # the provider's API with the user's key
    "ow": frozenset({"none", "api_key"}),  # a local or self-hosted open-weights server
}
# A non-usable entry (blocked BYOS) may record that its auth kind is not known yet.
_BLOCKED_AUTH_OK = frozenset({"unknown"})
# Providers the registry may name: the vendors the family policy knows (policy.VENDOR_PROVIDERS, minus the test-only
# "mock") plus "local" (self-hosted open weights). An unknown provider fails the check (fail closed).
REGISTRY_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic", "google", "mistral", "meta", "alibaba",
                                                "deepseek", "microsoft", "xai", "cohere", "local"})
LICENCE_FLAGS = ("training_on_outputs",)


def load_registry(override: Path | None = None) -> dict[str, dict[str, Any]]:
    text = resources.files("custom_harness.gateway").joinpath("providers.yaml").read_text(encoding="utf-8")
    reg: dict[str, dict[str, Any]] = yaml.safe_load(text)
    override = override if override is not None else data_dir() / "providers.override.yaml"
    if override.exists():
        reg.update(yaml.safe_load(override.read_text(encoding="utf-8")) or {})
    return reg


def _byos_allowlist_problem(key: str, entry: Mapping[str, Any]) -> str | None:
    """Why an 'allowed' subscription entry is not admitted by the allowlist (None = admitted)."""
    allowed = BYOS_ALLOWLIST.get(key)
    if allowed is None:
        return f"{key}: subscription use is not on the BYOS allowlist (gateway/registry.py BYOS_ALLOWLIST)"
    if entry.get("provider") != allowed["provider"]:
        return (f"{key}: provider {entry.get('provider')!r} differs from the BYOS allowlist's "
                f"{allowed['provider']!r}")
    if entry.get("auth_kind") != allowed["auth_kind"]:
        return (f"{key}: auth_kind {entry.get('auth_kind')!r} is not the official kind the BYOS allowlist names "
                f"({allowed['auth_kind']!r})")
    return None


def require_usable(entry_key: str, registry: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Raise unless the registry entry may be used. BYOS entries must be explicitly 'allowed' AND on the allowlist."""
    reg = registry if registry is not None else load_registry()
    if entry_key not in reg:
        raise ProviderBlocked(f"'{entry_key}' is not in the provider registry")
    entry = reg[entry_key]
    if entry.get("mode") == "byos" and entry.get("byos_status") != "allowed":
        raise ProviderBlocked(
            f"subscription mode for '{entry_key}' is '{entry.get('byos_status')}'. "
            f"Use an API key or open-weights model instead. Source: {entry.get('source_url')}")
    if entry.get("mode") == "byos":
        why = _byos_allowlist_problem(entry_key, entry)
        if why is not None:
            raise ProviderBlocked(f"subscription mode refused: {why}")
    return entry


def policy_problems(reg: Mapping[str, Any], *, owner_training: Mapping[str, bool] | None = None) -> list[str]:
    """Static policy check of a provider registry (offline; empty = passes). `owner_training`: the owner's licence
    flags ("provider/mode" -> bool, weights/config.py by default); a flag that permits training where the registry
    records the terms as not "allowed" is reported (the L8 filter refuses it anyway; the check says why)."""
    out: list[str] = []
    if not isinstance(reg, Mapping) or not reg:
        return ["registry is empty or not a mapping"]
    for key, e in reg.items():
        if not isinstance(e, Mapping):
            out.append(f"{key}: entry is not a mapping")
            continue
        prov, mode, status = e.get("provider"), e.get("mode"), e.get("byos_status")
        if prov not in REGISTRY_PROVIDERS:
            out.append(f"{key}: unknown provider {prov!r} (known: {sorted(REGISTRY_PROVIDERS)})")
        for flag in LICENCE_FLAGS:
            if flag not in e or e.get(flag) is None:
                out.append(f"{key}: missing licence flag {flag!r}")
        usable = mode in ("api", "ow") or (mode == "byos" and status == "allowed")
        auth = e.get("auth_kind")
        official = OFFICIAL_AUTH.get(str(mode), frozenset())
        if auth is None:
            if usable:
                out.append(f"{key}: missing auth_kind (official kinds for {mode}: {sorted(official)})")
        elif auth not in official and not (not usable and auth in _BLOCKED_AUTH_OK):
            out.append(f"{key}: auth_kind {auth!r} is not an official auth kind for mode {mode!r} "
                       f"({sorted(official)})")
        if mode == "byos" and status == "allowed":
            why = _byos_allowlist_problem(str(key), e)
            if why is not None:
                out.append(why)
            if not str(e.get("source_url") or "").startswith("https://"):
                out.append(f"{key}: an allowed subscription entry needs an https source_url for its terms")
        if key in BYOS_ALLOWLIST and mode != "byos":
            out.append(f"{key}: a BYOS-allowlisted key must have mode 'byos', not {mode!r} (the subscription backend "
                       "would bypass the BYOS gate)")
    if owner_training is None:
        from ..weights.config import PROVIDER_TRAINING_PERMITTED
        owner_training = PROVIDER_TRAINING_PERMITTED
    for flag_key, permitted in sorted(owner_training.items()):
        if permitted is not True:
            continue
        prov, _, mode = str(flag_key).partition("/")
        terms = [e.get("training_on_outputs") for e in reg.values()
                 if isinstance(e, Mapping) and e.get("provider") == prov and e.get("mode") == mode]
        if not terms:
            out.append(f"owner licence flag {flag_key!r} permits training, but no registry entry has provider "
                       f"{prov!r} and mode {mode!r}")
        elif any(t != "allowed" for t in terms):
            out.append(f"owner licence flag {flag_key!r} permits training, but the registry records the terms as "
                       f"{sorted(set(map(str, terms)))}")
    return out


def verify_registry(reg: Mapping[str, Any], *, release: bool, today: Any = None,
                    owner_training: Mapping[str, bool] | None = None) -> list[str]:
    """Every problem of a registry: structure + policy, and with `release` the per-release human verification and cost
    rows (costs.release_problems, which already includes the structural check)."""
    import datetime as _dt

    from .costs import registry_problems, release_problems
    if release:
        base = release_problems(reg, today or _dt.date.today())
    else:
        base = registry_problems(reg)
    return base + policy_problems(reg, owner_training=owner_training)
