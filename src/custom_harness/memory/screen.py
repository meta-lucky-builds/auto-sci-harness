"""Content screen for memory writes and renders (GOAL invariant 2, §1.2 "Prime Agent's failure, inverted").

Refused (before anything is stored):
  - a key starting with `_private` anywhere in the entry's data (the same rule as runtime/template.render and the
    engine's trace scrubber: `_private*` state never reaches a prompt, so it must never reach memory either);
  - any canary token: the generic `CHH-CANARY-` prefix and every canary registered in the Kernel vault;
  - any caller-supplied secret (e.g. the run's `_private*` values) of at least MIN_SECRET characters;
  - a byte-identical copy of any private vault file (hash match);
  - the untrusted-data wrapper marker (an entry could otherwise fake the end of its quote);
  - control characters other than newline/tab, and Unicode bidi overrides;
  - instruction-injection patterns (HEURISTIC: a few well-known shapes; rendering as quoted data is the real
    defence, this only refuses the obvious attempts).
Reasons name the category of the problem, never the offending value.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from ..kernel.vault import Vault
from ..verify.leak import CANARY_PREFIX
from .model import Proposal

WRAPPER = "UNTRUSTED-MEMORY-DATA"
MIN_SECRET = 6
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f‪-‮⁦-⁩]")
_INJECTION = (
    re.compile(r"\b(ignore|disregard|forget|override)\b[^\n]{0,40}?\b(previous|prior|above|earlier|all|system|your)"
               r"\b[^\n]{0,20}?\b(instructions?|prompts?|rules?|messages?|context)\b", re.IGNORECASE),
    re.compile(r"(^|\n)\s*(system|assistant|developer)\s*:", re.IGNORECASE),
    re.compile(r"<\|[a-z_]+\|>", re.IGNORECASE),
    re.compile(r"</?\s*(system|instructions?|assistant|developer)\s*>", re.IGNORECASE),
    re.compile(r"\byou (are|must) now\b", re.IGNORECASE),
)


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _private_keys(obj: Any) -> bool:
    if isinstance(obj, dict):
        return any(str(k).startswith("_private") or _private_keys(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(_private_keys(v) for v in obj)
    return False


class Fingerprints:
    """What must never enter memory, gathered from the Kernel vault (canaries, private-file hashes)."""

    def __init__(self, canaries: Iterable[str], private_hashes: Iterable[str]):
        self.canaries = sorted({c for c in canaries if c})
        self.private_hashes = frozenset(private_hashes)

    @classmethod
    def from_vault(cls, vault: Vault) -> Fingerprints:
        canaries: list[str] = []
        hashes: list[str] = []
        for ds in vault.datasets().values():
            if ds.canary_ref:
                canaries.append(vault.store.blobs.get(ds.canary_ref).decode())
            hashes += [ref.blob for ref in ds.files.values() if ref.private]
        return cls(canaries, hashes)


def screen_strings(strings: Iterable[str], fp: Fingerprints, secrets: Sequence[str] = ()) -> list[str]:
    secrets = [s for s in secrets if s and len(s) >= MIN_SECRET]
    problems: set[str] = set()
    for s in strings:
        if CANARY_PREFIX in s or any(c in s for c in fp.canaries):
            problems.add("contains a canary token")
        if any(x in s for x in secrets):
            problems.add("contains a private value of the current run")
        if hashlib.sha256(s.encode()).hexdigest() in fp.private_hashes:
            problems.add("is a copy of a private vault file")
        if WRAPPER.lower() in s.lower():
            problems.add("contains the untrusted-data wrapper marker")
        if _CONTROL.search(s):
            problems.add("contains control or bidi-override characters")
        if any(p.search(s) for p in _INJECTION):
            problems.add("looks like an instruction to the model (injection pattern)")
    return sorted(problems)


def screen_proposal(p: Proposal, fp: Fingerprints, secrets: Sequence[str] = ()) -> list[str]:
    problems = []
    if _private_keys(p.data):
        problems.append("data has a key starting with _private")
    try:
        json.dumps(p.data, allow_nan=False)
    except (TypeError, ValueError):
        problems.append("data is not plain finite JSON")
    strings = [p.title, p.text, *_strings(p.data), *([p.snapshot_ref] if p.snapshot_ref else [])]
    return problems + screen_strings(strings, fp, secrets)
