"""The machine's product settings: one small file in the data folder, written by `chh exp setup`. None of them grants
anything. A missing file means the defaults below; a damaged file is refused, because the spending limit lives here and
a silent default could raise it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import data_dir

LAB_DEFAULT_MODEL = "claude-haiku-5-5"  # owner decision: the cheap model unless a session names another
SESSION_SPEND_LIMIT_USD = 5.0  # UNCALIBRATED (owner decision, GOAL §13.8): model spend one Lab session may reach
SPEND_WARN_SHARE = 0.8  # UNCALIBRATED (owner decision, GOAL §13.8): share of the limit at which a warning starts
UNKNOWN_COST_CALLS = 10  # UNCALIBRATED (owner decision, GOAL §13.8): calls of unknown cost a session may run up
DEFAULTS: dict[str, Any] = {"lab_default_model": LAB_DEFAULT_MODEL, "session_spend_limit_usd": SESSION_SPEND_LIMIT_USD,
                            "unknown_cost_calls": UNKNOWN_COST_CALLS}


class SettingsRefused(ValueError):
    pass


def settings_file() -> Path:
    return data_dir() / "settings.json"


def _valid(doc: dict[str, Any]) -> dict[str, Any]:
    unknown = set(doc) - set(DEFAULTS)
    if unknown:
        raise SettingsRefused(f"{settings_file()}: unknown settings {sorted(unknown)}")
    limit, calls, model = doc.get("session_spend_limit_usd", 1.0), doc.get("unknown_cost_calls", 1), \
        doc.get("lab_default_model", "x")
    if not (type(limit) in (int, float) and limit > 0):
        raise SettingsRefused("the spending limit must be a number above 0")
    if not (type(calls) is int and calls >= 0):
        raise SettingsRefused("the limit on calls of unknown cost must be a whole number, 0 or more")
    if not (isinstance(model, str) and model.strip()):
        raise SettingsRefused("the default model must be a name")
    return doc


def load() -> dict[str, Any]:
    path = settings_file()
    if not path.is_file():
        return dict(DEFAULTS)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SettingsRefused(f"{path} cannot be read ({type(e).__name__}). Fix it or delete it.") from None
    if not isinstance(doc, dict):
        raise SettingsRefused(f"{path} is not a settings file. Fix it or delete it.")
    return {**DEFAULTS, **_valid(doc)}


def save(**changes: Any) -> dict[str, Any]:
    """Merge `changes` (None = leave as it is) into the file. Only named keys are ever written."""
    doc = {**load(), **{k: v for k, v in changes.items() if v is not None}}
    _valid(doc)
    settings_file().write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return doc
