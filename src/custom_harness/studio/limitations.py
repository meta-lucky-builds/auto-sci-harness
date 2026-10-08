"""Limitations register (GOAL §5 L9; invariant 19's findings registry read the other way round).

Every OPEN finding in `redteam/findings.yaml` is a loophole the Kernel does not close today. Each one is attached to
every certificate it could affect, as an UNTESTED assumption: "this certificate is only as good as the absence of
loophole X". Which certificates a finding affects is stated in SCOPE below, per evidence class. A finding missing from
SCOPE affects EVERY certified certificate (fail closed: a new loophole is never silently out of scope). REJECTED
certificates are not affected (they certify nothing). If the findings file cannot be read, the register says so and
every certified certificate carries that as an untested assumption.

The register is rebuilt from the store and the findings file; it writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from ..kernel import Kernel, KernelError
from ..kernel.certificate import Label
from ..kernel.claim import EvidenceClass

FINDINGS_PATH = Path(__file__).resolve().parents[3] / "redteam" / "findings.yaml"
ALL = frozenset(EvidenceClass)
DATA = frozenset({EvidenceClass.DATA})
# Which certificates an open finding can affect, by the claim's evidence class (from each finding's invariant/check).
SCOPE: dict[str, frozenset[EvidenceClass]] = {
    "KD1": DATA,  # K6: a development dataset holding the held-out examples (data claims only)
    "KD2": DATA,  # K7 on a subset of targets (data claims only)
    "KD3": DATA,  # held-out target whose public input carries its labels (data claims only)
    "KD4": DATA,  # K2 counts reads of the target id only (data claims only)
    "ASOF": ALL,  # verify() as-of semantics: any certificate's verify status
    "ADAPTER-EDIT": ALL,  # adapters are the trusted base of every class
}
UNREADABLE = "REGISTER-UNREADABLE"


@dataclass(frozen=True)
class Limitation:
    finding_id: str
    title: str
    invariant: str
    capability: str
    classes: frozenset[EvidenceClass]
    certificates: tuple[str, ...]  # cert hashes affected

    @property
    def assumption(self) -> str:
        return f"open loophole {self.finding_id} is not exploited here: {self.title} ({self.invariant})"

    def to_json(self) -> dict[str, Any]:
        return {"finding_id": self.finding_id, "title": self.title, "invariant": self.invariant,
                "capability": self.capability, "classes": sorted(c.value for c in self.classes),
                "certificates": list(self.certificates), "tested_by": None, "assumption": self.assumption}


@cache
def _load(path: Path, mtime: float) -> tuple[dict[str, Any], ...]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return tuple(f for f in doc["findings"] if isinstance(f, dict))


def findings(path: Path | None = None) -> tuple[dict[str, Any], ...] | None:
    """All findings, or None if the file is missing or malformed."""
    path = path or FINDINGS_PATH
    try:
        return _load(path, path.stat().st_mtime)
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return None


def open_findings(path: Path | None = None) -> list[dict[str, Any]] | None:
    fs = findings(path)
    return None if fs is None else [f for f in fs if f.get("status") == "open"]


def _certified(kernel: Kernel) -> list[tuple[str, EvidenceClass | None]]:
    out: list[tuple[str, EvidenceClass | None]] = []
    for e in kernel.ledger.entries("certificate"):
        if e.body.get("label") == Label.REJECTED.value:
            continue
        h = e.body["cert_hash"]
        cls: EvidenceClass | None
        try:
            cls = EvidenceClass(kernel.certificate(h).claim.get("evidence_class", "data"))
        except (KernelError, OSError, ValueError):
            cls = None  # unreadable: affected by everything (fail closed)
        out.append((h, cls))
    return out


def register(kernel: Kernel, path: Path | None = None) -> list[Limitation]:
    certs = _certified(kernel)
    opened = open_findings(path)
    if opened is None:
        return [Limitation(UNREADABLE, f"the findings register {path or FINDINGS_PATH} could not be read: open "
                                       f"loopholes are unknown", "§10 inv. 19", "unknown", ALL,
                           tuple(h for h, _ in certs))]
    out = []
    for f in opened:
        fid = str(f.get("id"))
        classes = SCOPE.get(fid, ALL)
        out.append(Limitation(fid, str(f.get("title", "")), str(f.get("invariant", "")), str(f.get("capability", "")),
                              classes, tuple(h for h, c in certs if c is None or c in classes)))
    return out


def for_certificate(kernel: Kernel, cert_hash: str, path: Path | None = None) -> list[Limitation]:
    return [lim for lim in register(kernel, path) if cert_hash in lim.certificates]
