"""Training-licence filter (GOAL §10 invariant 11: training only on licence-permitted outputs). Fail closed.

An output may become training data only if ALL hold:
1. the OWNER flag for its "provider/mode" is literally `True` (weights/config.py, or an owner JSON file); an absent key
   is False;
2. the backend's recorded terms (`Provenance.training_on_outputs`, from gateway/providers.yaml) are "allowed":
   "forbidden" and "unknown" both refuse, so an owner flag set by mistake cannot override known terms;
3. it is not replayed from a cassette (`Provenance.replayed`): a replay is a copy whose original provenance this
   filter cannot see (invariant 21 treats it as non-evidence; here it is non-data);
4. it is not mock output (AccessMode.MOCK is test scaffolding).

Keys match EXACTLY (no case folding): folding could only widen what is permitted. The owner flags are read once
into an immutable policy whose `digest()` goes into every export manifest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..records import AccessMode, Provenance
from . import config as C

_MODES = {m.value for m in AccessMode}


class LicenceConfigError(ValueError):
    """The owner's licence config is malformed. Never guessed around: a malformed config permits nothing."""


def key_of(p: Provenance) -> str:
    return f"{p.provider}/{p.mode.value}"


@dataclass(frozen=True)
class LicencePolicy:
    permitted: frozenset[str]  # "provider/mode" keys the owner set to True

    @classmethod
    def from_mapping(cls, flags: Any) -> LicencePolicy:
        if not isinstance(flags, Mapping):
            raise LicenceConfigError(f"licence flags must be a mapping of 'provider/mode' -> bool, not {type(flags)}")
        keys: set[str] = set()
        for k, v in flags.items():
            if not isinstance(k, str) or k.count("/") != 1 or k.split("/")[1] not in _MODES or not k.split("/")[0]:
                raise LicenceConfigError(f"licence key {k!r} is not 'provider/mode' with mode in {sorted(_MODES)}")
            if not isinstance(v, bool):  # "true", 1, "yes": refused, never coerced
                raise LicenceConfigError(f"licence flag for {k!r} must be true or false, not {v!r}")
            if v:
                keys.add(k)
        return cls(frozenset(keys))

    @classmethod
    def default(cls) -> LicencePolicy:
        return cls.from_mapping(C.PROVIDER_TRAINING_PERMITTED)

    @classmethod
    def load(cls, path: Path | str) -> LicencePolicy:
        """An owner file: {"permitted": {"provider/mode": true, ...}}. Missing or unreadable: an error, not a
        default."""
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise LicenceConfigError(f"licence file {path} unreadable: {e}") from e
        if not isinstance(doc, dict) or set(doc) != {"permitted"}:
            raise LicenceConfigError("licence file must be exactly {\"permitted\": {...}}")
        return cls.from_mapping(doc["permitted"])

    def permits(self, p: Provenance | None) -> bool:
        if p is None or p.replayed or p.mode is AccessMode.MOCK:
            return False
        return p.training_on_outputs == "allowed" and key_of(p) in self.permitted

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(sorted(self.permitted)).encode()).hexdigest()
