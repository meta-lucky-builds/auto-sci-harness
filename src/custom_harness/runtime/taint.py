"""Engine-recorded, persisted taint labels (GOAL §16.5.3, invariant 18; design note docs/taint.md).

Every top-level run-state key carries a LABEL: the set of held-out target ids its value may derive from. The engine,
not the module, decides what a step read:
  - a module gets its state as a `TrackedState`, which records every key it reads (and fingerprints each mutable value
    it hands out, so an in-place mutation is seen); a bulk access (iteration, items(), copy, `{**state}`,
    json.dumps, ==, repr ...) counts as reading EVERY key;
  - `ModuleContext` records which other surfaces the module touched (the store, the Kernel, the sandbox, the run's
    secrets); `ContextAccess` below says what each one adds;
  - template references give the exact keys an LLM prompt or tool command renders.
The label of whatever a step writes = the union of the labels of what it read, the private vault reads attributed to
this run during the step (kerneld/releases.attributed_reads: reads naming this run, no run, or an unknown run; fail
closed), and the run's CONTROL label (targets that earlier routing decisions depended on: an implicit flow).

Labels are written into every checkpoint (`TaintState.doc`) and restored by resume and fork exactly; a checkpoint
written before labels existed is restored fail-closed (every key labelled with every target the run read).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

TAINT_DOC_VERSION = 1
# ContextAccess: what touching a ModuleContext surface means for the step's label
STORE_SURFACES = frozenset({"store", "kernel"})  # the run's trace, evidence and blobs: every target the run read
SANDBOX_SURFACE = "sandbox"  # the workspace: every label written into it so far
SECRETS_SURFACE = "secrets"  # the run's `_private*` values: the labels of the `_private*` keys


def fingerprint(value: Any) -> str | None:
    """Content fingerprint of a MUTABLE value (None for immutable scalars, which cannot change in place)."""
    if value is None or isinstance(value, (str, int, float, bool, bytes, tuple, frozenset)):
        return None
    try:
        text = json.dumps(value, sort_keys=True, default=repr, ensure_ascii=False)
    except (TypeError, ValueError):  # e.g. a cycle: the content cannot be compared, so it counts as changed
        return f"unfingerprintable:{id(value)}:{object()!r}"
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


class TrackedState(dict):  # type: ignore[type-arg]
    """A module's view of run state: a shallow copy of it whose reads, writes and in-place mutations the engine
    records. Overriding `__iter__` also routes `dict(x)`, `{**x}` and `f(**x)` through `keys()` (CPython), so those
    count as bulk reads. Not a security boundary against hostile code (`dict.__getitem__(x, k)` bypasses it): modules
    are trusted, registry-only code; it records what honest code reads, including through library calls."""

    def __init__(self, state: Mapping[str, Any]):
        super().__init__(state)
        self.reads: set[str] = set()
        self.bulk = False
        self.written: set[str] = set()
        self.deleted: set[str] = set()
        self._fp: dict[str, str | None] = {}

    # ------------------------------------------------------------ recording
    def _read(self, key: Any) -> None:
        k = str(key)
        self.reads.add(k)
        if k not in self._fp and dict.__contains__(self, k):
            self._fp[k] = fingerprint(dict.__getitem__(self, k))

    def _read_all(self) -> None:
        self.bulk = True
        for k in list(dict.keys(self)):
            self._read(k)

    def _write(self, key: Any) -> None:
        self.written.add(str(key))
        self.deleted.discard(str(key))

    def _delete(self, key: Any) -> None:
        self.deleted.add(str(key))
        self.written.discard(str(key))

    def mutated(self) -> set[str]:
        """Keys whose value was changed IN PLACE (not by assignment) since the module first read them."""
        out = set()
        for k, fp in self._fp.items():
            if fp is not None and dict.__contains__(self, k) and fingerprint(dict.__getitem__(self, k)) != fp:
                out.add(k)
        return out

    # ------------------------------------------------------------ single-key reads
    def __getitem__(self, key: Any) -> Any:
        self._read(key)
        return dict.__getitem__(self, key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._read(key)
        return dict.get(self, key, default)

    def __contains__(self, key: object) -> bool:
        self._read(key)
        return dict.__contains__(self, key)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        self._read(key)
        if not dict.__contains__(self, key):
            self._write(key)
        return dict.setdefault(self, key, default)

    def pop(self, key: Any, *default: Any) -> Any:
        self._read(key)
        if dict.__contains__(self, key):
            self._delete(key)
        return dict.pop(self, key, *default)

    # ------------------------------------------------------------ writes
    def __setitem__(self, key: Any, value: Any) -> None:
        self._write(key)
        dict.__setitem__(self, key, value)

    def __delitem__(self, key: Any) -> None:
        self._delete(key)
        dict.__delitem__(self, key)

    def update(self, *args: Any, **kwargs: Any) -> None:
        other = dict(*args, **kwargs)
        for k in other:
            self._write(k)
        dict.update(self, other)

    def __ior__(self, other: Any) -> TrackedState:
        self.update(other)
        return self

    def popitem(self) -> tuple[Any, Any]:
        self._read_all()
        k, v = dict.popitem(self)
        self._delete(k)
        return k, v

    def clear(self) -> None:
        self._read_all()
        for k in list(dict.keys(self)):
            self._delete(k)
        dict.clear(self)

    # ------------------------------------------------------------ bulk reads: every key
    def __iter__(self):  # type: ignore[no-untyped-def]
        self._read_all()
        return dict.__iter__(self)

    def __reversed__(self):  # type: ignore[no-untyped-def]
        self._read_all()
        return dict.__reversed__(self)

    def keys(self):  # type: ignore[no-untyped-def]
        self._read_all()
        return dict.keys(self)

    def values(self):  # type: ignore[no-untyped-def]
        self._read_all()
        return dict.values(self)

    def items(self):  # type: ignore[no-untyped-def]
        self._read_all()
        return dict.items(self)

    def __len__(self) -> int:
        self._read_all()
        return dict.__len__(self)

    def __bool__(self) -> bool:
        self._read_all()
        return dict.__len__(self) > 0

    def copy(self) -> dict[str, Any]:
        self._read_all()
        return dict(dict.items(self))

    def __copy__(self) -> dict[str, Any]:
        return self.copy()

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
        import copy as _copy
        self._read_all()
        return _copy.deepcopy(dict(dict.items(self)), memo)

    def __reduce_ex__(self, protocol: Any) -> Any:
        self._read_all()
        return (dict, (dict(dict.items(self)),))

    def __eq__(self, other: object) -> bool:
        self._read_all()
        return dict.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        self._read_all()
        return dict.__ne__(self, other)

    __hash__ = None  # type: ignore[assignment]

    def __or__(self, other: Any) -> dict[str, Any]:
        self._read_all()
        return dict(dict.items(self)) | dict(other)

    def __ror__(self, other: Any) -> dict[str, Any]:
        self._read_all()
        return dict(other) | dict(dict.items(self))

    def __repr__(self) -> str:
        self._read_all()
        return dict.__repr__(self)

    def __str__(self) -> str:
        return self.__repr__()

    def __format__(self, spec: str) -> str:
        return format(self.__repr__(), spec)


def _targets(v: Any) -> frozenset[str]:
    if not isinstance(v, (list, tuple, set, frozenset)):
        raise ValueError("a label is a list of target ids")
    return frozenset(str(t) for t in v)


@dataclass
class TaintState:
    """The persisted label state of one run (engine-owned; never in run state, so no evolved node can write it)."""

    labels: dict[str, frozenset[str]] = field(default_factory=dict)  # state key -> targets
    alphabets: dict[str, str] = field(default_factory=dict)  # allowlisted declarations (engine.py)
    control: frozenset[str] = frozenset()  # targets earlier routing decisions depended on
    pending: list[dict[str, Any]] = field(default_factory=list)  # tainted decisions not yet released
    decisions: list[dict[str, Any]] = field(default_factory=list)  # every tainted decision (cumulative)
    run: frozenset[str] = frozenset()  # every target attributed to this run so far
    workspace: frozenset[str] = frozenset()  # every label written into the sandbox workspace
    seq: int = 0  # last data_access ledger seq this state has accounted for

    def label_of(self, keys: Iterable[str]) -> frozenset[str]:
        out: set[str] = set()
        for k in keys:
            out |= self.labels.get(k, frozenset())
        return frozenset(out)

    def set_label(self, key: str, targets: Iterable[str]) -> None:
        t = frozenset(targets)
        if t:
            self.labels[key] = t
            self.run |= t
        else:
            self.labels.pop(key, None)

    def doc(self, step: Iterable[str] = ()) -> dict[str, Any]:
        return {"v": TAINT_DOC_VERSION, "labels": {k: sorted(v) for k, v in sorted(self.labels.items()) if v},
                "alphabets": dict(sorted(self.alphabets.items())), "control": sorted(self.control),
                "pending": [dict(p) for p in self.pending], "decisions": [dict(d) for d in self.decisions],
                "run": sorted(self.run), "workspace": sorted(self.workspace), "seq": int(self.seq),
                "step": sorted(set(step))}

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> TaintState:
        """Raises ValueError on anything malformed (the caller then restores fail closed)."""
        if not isinstance(doc, Mapping) or doc.get("v") != TAINT_DOC_VERSION:
            raise ValueError("unknown taint doc")
        labels = doc.get("labels")
        alphabets = doc.get("alphabets")
        if not isinstance(labels, Mapping) or not isinstance(alphabets, Mapping):
            raise ValueError("malformed taint doc")
        pending, decisions = doc.get("pending"), doc.get("decisions")
        if not isinstance(pending, list) or not isinstance(decisions, list):
            raise ValueError("malformed taint doc")
        if not all(isinstance(p, Mapping) for p in [*pending, *decisions]):
            raise ValueError("malformed taint doc")
        seq = doc.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise ValueError("malformed taint doc")
        return cls(labels={str(k): _targets(v) for k, v in labels.items() if v},
                   alphabets={str(k): str(a) for k, a in alphabets.items()},
                   control=_targets(doc.get("control", [])),
                   pending=[{**p, "targets": sorted(_targets(p.get("targets", [])))} for p in pending],
                   decisions=[{**d, "targets": sorted(_targets(d.get("targets", [])))} for d in decisions],
                   run=_targets(doc.get("run", [])), workspace=_targets(doc.get("workspace", [])), seq=seq)

    @classmethod
    def fail_closed(cls, state: Mapping[str, Any], targets: Iterable[str], seq: int) -> TaintState:
        """For a checkpoint without labels: every key carries every target, every routing decision is unknown."""
        t = frozenset(targets)
        if not t:
            return cls(seq=seq)
        pending = [{"node": "unlabelled_checkpoint", "targets": sorted(t), "alphabet": "continuous"}]
        return cls(labels={str(k): t for k in state}, control=t, pending=pending, decisions=list(pending), run=t,
                   workspace=t, seq=seq)

    def copy(self) -> TaintState:
        return TaintState(labels=dict(self.labels), alphabets=dict(self.alphabets), control=self.control,
                          pending=[dict(p) for p in self.pending], decisions=[dict(d) for d in self.decisions],
                          run=self.run, workspace=self.workspace, seq=self.seq)


def decision_alphabet(n_outcomes: int, count_alphabet: int = 16) -> str:
    """What one routing decision among `n_outcomes` distinct targets can tell."""
    if n_outcomes <= 2:
        return "verdict_bit"
    return "count" if n_outcomes <= count_alphabet else "continuous"


__all__ = ["SANDBOX_SURFACE", "SECRETS_SURFACE", "STORE_SURFACES", "TaintState", "TrackedState", "decision_alphabet",
           "fingerprint"]
