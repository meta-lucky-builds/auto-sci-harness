"""Provider registry loader and BYOS gate (PLAN §14.2)."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from ..config import data_dir


class ProviderBlocked(PermissionError):
    pass


def load_registry(override: Path | None = None) -> dict[str, dict[str, Any]]:
    text = resources.files("custom_harness.gateway").joinpath("providers.yaml").read_text(encoding="utf-8")
    reg: dict[str, dict[str, Any]] = yaml.safe_load(text)
    override = override if override is not None else data_dir() / "providers.override.yaml"
    if override.exists():
        reg.update(yaml.safe_load(override.read_text(encoding="utf-8")) or {})
    return reg


def require_usable(entry_key: str, registry: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Raise unless the registry entry may be used. BYOS entries must be explicitly 'allowed'."""
    reg = registry if registry is not None else load_registry()
    if entry_key not in reg:
        raise ProviderBlocked(f"'{entry_key}' is not in the provider registry")
    entry = reg[entry_key]
    if entry.get("mode") == "byos" and entry.get("byos_status") != "allowed":
        raise ProviderBlocked(
            f"subscription mode for '{entry_key}' is '{entry.get('byos_status')}'. "
            f"Use an API key or open-weights model instead. Source: {entry.get('source_url')}")
    return entry
