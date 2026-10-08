"""Tier policy as human-only config (GOAL §5 L9; tiers.py "changing this table is a human-only action").

The policy maps each autonomy action (tiers.Action) to the weakest tier that may proceed without a human, or None
(never automatic). It lives in ONE file outside the repository and outside every sandbox:
`<data_dir>/human/tier_policy.json`. A human edits that file, then runs `chh tier-policy pin --by NAME`, which shows
the policy, validates it and records its sha256 in the Kernel ledger (kind `tier_policy`). `load_policy` returns:

- `tiers.DEFAULT_POLICY` when no file and no pin exist (the code default);
- the file's policy when its bytes hash to the LATEST human pin;
- otherwise it raises `TierPolicyError` (fail closed): a file that changed after its pin, a pin without its file, a
  file that was never pinned, or a malformed file.

Why no model or evolved node can change it:
- the harness spec has no field for it (HarnessSpec forbids extra fields), so an evolved edit cannot carry one;
- LLM output only reaches run state, and nothing reads the policy from state;
- the directory is a protected path: no sandbox workspace may be, contain or sit inside it;
- module code cannot write a `tier_policy` ledger entry through its Kernel (studio/kill.py `_ModuleLedger`);
- a direct write to the file by anything else (an agent with host access) is not prevented, but it is detected: the
  file no longer matches its pin and every load refuses it.

Honest limits: whoever controls both the host files and the store can rewrite the file and append a pin; the ledger
is tamper-evident, not authenticated (see kernel/ledger.py anchoring). The `by` field is not checked to be a human.
Consumers: `memory_floors` turns the loaded policy into L5 memory floors. The memory store built by
research/memory_wiring.py still uses `tiers.DEFAULT_POLICY` until it is wired to `load_policy` (not done here).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..config import data_dir
from ..kernel.ledger import Entry, Ledger
from ..store import EvidenceStore
from ..tiers import DEFAULT_POLICY, Action, Tier

PIN_KIND = "tier_policy"
FILE_NAME = "tier_policy.json"


class TierPolicyError(ValueError):
    pass


def human_config_dir() -> Path:
    """The human-only config directory. Registered as a protected path, so no sandbox workspace may overlap it."""
    from ..sandbox.base import protect_path
    d = data_dir() / "human"
    d.mkdir(parents=True, exist_ok=True)
    protect_path(d)
    return d


def policy_path() -> Path:
    return human_config_dir() / FILE_NAME


def to_json(policy: dict[Action, Tier | None]) -> dict[str, str | None]:
    return {a.value: (None if policy[a] is None else policy[a].name) for a in Action}  # type: ignore[union-attr]


def parse(data: bytes) -> dict[Action, Tier | None]:
    """Strict: a JSON object naming EVERY action exactly once, each mapped to a tier name (e.g. "T2_STATISTICAL")
    or null (never automatic). Anything else is refused."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise TierPolicyError(f"tier policy is not JSON ({type(e).__name__})") from None
    if not isinstance(doc, dict):
        raise TierPolicyError("tier policy must be a JSON object")
    want = {a.value for a in Action}
    if set(doc) != want:
        raise TierPolicyError(f"tier policy must name exactly the actions {sorted(want)}; "
                              f"missing {sorted(want - set(doc))}, unknown {sorted(set(doc) - want)}")
    out: dict[Action, Tier | None] = {}
    for key, value in doc.items():
        if value is None:
            out[Action(key)] = None
        elif isinstance(value, str) and value in Tier.__members__:
            out[Action(key)] = Tier[value]
        else:
            raise TierPolicyError(f"{key}: {value!r} is not a tier name {sorted(Tier.__members__)} or null")
    return out


def _pins(store: EvidenceStore) -> list[Entry]:
    return [e for e in Ledger(store).entries(PIN_KIND) if e.intact() and str(e.body.get("by", "")).strip()
            and isinstance(e.body.get("sha256"), str)]


def load_policy(store: EvidenceStore, path: Path | None = None) -> dict[Action, Tier | None]:
    path = path or policy_path()
    pins = _pins(store)
    if not pins and not path.exists():
        return dict(DEFAULT_POLICY)
    if not pins:
        raise TierPolicyError(f"{path} exists but was never pinned by a human: run `chh tier-policy pin --by NAME`")
    if not path.exists():
        raise TierPolicyError(f"the pinned tier policy file {path} is missing")
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != pins[-1].body["sha256"]:
        raise TierPolicyError(f"{path} changed after its last human pin (ledger seq {pins[-1].seq}): refused. Review "
                              f"it and pin it again, or restore the pinned version")
    return parse(data)


def write_default(path: Path | None = None) -> Path:
    """Write the code default as a starting file for a human to edit (not pinned; refuses to overwrite)."""
    path = path or policy_path()
    if path.exists():
        raise TierPolicyError(f"{path} exists; edit it instead")
    path.write_text(json.dumps(to_json(DEFAULT_POLICY), indent=1, sort_keys=True) + "\n", encoding="utf-8",
                    newline="")
    return path


def pin(store: EvidenceStore, by: str, path: Path | None = None, expect_sha256: str | None = None) -> Entry:
    """HUMAN action: validate the file and record its hash (and content) in the ledger."""
    if not by.strip():
        raise TierPolicyError("--by must name the human pinning the policy")
    path = path or policy_path()
    if not path.exists():
        raise TierPolicyError(f"no tier policy file at {path} (create it with `chh tier-policy init`)")
    data = path.read_bytes()
    policy = parse(data)
    digest = hashlib.sha256(data).hexdigest()
    if expect_sha256 is not None and expect_sha256 != digest:
        raise TierPolicyError(f"the file hashes to {digest}, not the {expect_sha256} you reviewed")
    return Ledger(store).append(PIN_KIND, {"sha256": digest, "policy": to_json(policy), "by": by.strip()})


def memory_floors(policy: dict[Action, Tier | None]) -> dict[Any, Tier]:
    """The L5 memory floors this policy implies (same rule as memory.model.default_floors)."""
    from ..memory.model import SKILL_KINDS, Kind
    floors: dict[Any, Tier] = {}
    for kind in Kind:
        floor = policy[Action.ADMIT_SKILL if kind in SKILL_KINDS else Action.PUBLISH_VERIFIED]
        if floor is not None:
            floors[kind] = floor
    return floors
