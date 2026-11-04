"""Seal at project start (GOAL 18.2, 18.6, UPGRADES N7): "sealed" as a ledger fact, not an attestation.

    chh lab seal PROJECT --data FILE --label-column C --rows N [--parts 2] --beacon-round R --beacon VALUE
        --provenance private|public
        kerneld reads FILE, draws the split and registers the parts. The host never holds a sealed label.
    chh lab start ... --project PROJECT
        registers a `lab_session` artifact (a ledger entry) and puts the Lab's part into the workspace.
    chh court register-sealed PROMOTION --project PROJECT [--revalidate]

The order is read from the ledger: each sealed part's `register_dataset` entry and each session's `register_artifact`
entry have sequence numbers in one hash chain. A target counts as sealed at start (`seal_fact`) only when every session
of the project sits after the seal, and the case's own session is one of them.

What this shows: the Court's registered copy existed, and was held by kerneld, before any session of this project. What
it cannot show: that nobody gave the raw file to a Lab some other way, inside or outside this project, and that the data
is not public. Both rest on the researcher, who states the provenance at the seal; the role approval of each part (`chh
approve-role`, human) still applies, and it is listed as an assumption on the claim.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ..kerneld.scope import ScopedKernel
from ..store import EvidenceStore

RECORD_KIND = "lab_project"
ARTIFACT_KIND = "lab_session"
PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
PROVENANCE = ("private", "public")


class SealRefused(RuntimeError):
    pass


def project_rows(store: EvidenceStore, project: str) -> list[dict[str, Any]]:
    return store.records_where(RECORD_KIND, "project", project)


def sealed_record(store: EvidenceStore, project: str) -> dict[str, Any] | None:
    return next((r for r in project_rows(store, project) if r.get("event") == "sealed"), None)


def seal_project(store: EvidenceStore, project: str, data_file: Path, *, label_column: str, rows: int, parts: int,
                 beacon_round: str, beacon_value: str, provenance: str) -> dict[str, Any]:
    """`provenance`, the researcher's statement: `private` (no Lab could get this data elsewhere) or `public`. Sealing a
    copy of public data hides nothing from a networked Lab, so a `public` seal is capped as RETROSPECTIVE."""
    from ..kerneld.client import KernelD, KernelDError
    from ..lab.walls import lab_root, overlaps
    if provenance not in PROVENANCE:
        raise SealRefused(f"provenance {provenance!r}: say where this data comes from, one of {list(PROVENANCE)}")
    if not PROJECT.match(project):
        raise SealRefused(f"project id {project!r}: lower-case letters, digits and '-', 3-40 characters")
    if project_rows(store, project):
        raise SealRefused(f"project {project!r} already exists: a project is sealed once, before any Lab session")
    if overlaps(data_file, lab_root()):
        raise SealRefused("the data file is inside the lab root: the Lab can already read it, so nothing is sealed")
    try:
        with KernelD(store.root) as kd:
            run = kd.open_run(f"seal-{project}")
            try:
                res = run.ingest_sealed_split(project, data_file, label_column=label_column, n_parts=parts,
                                              rows_per_part=rows, beacon_round=beacon_round,
                                              beacon_value=beacon_value)
            finally:
                run.close()
    except KernelDError as e:
        raise SealRefused(f"kerneld refused the seal ({e.code})") from None
    body = {"project": project, "event": "sealed", **res, "beacon_round": beacon_round, "provenance": provenance,
            "at": time.time()}
    store.append_record(RECORD_KIND, body)
    return body


def bind_session(store: EvidenceStore, project: str, session_id: str) -> dict[str, Any]:
    """A new Lab session of a sealed project: its ledger entry first, before the box exists."""
    seal = sealed_record(store, project)
    if seal is None:
        raise SealRefused(f"project {project!r} is not sealed (`chh lab seal`, before the first session)")
    doc = {"project": project, "session_id": session_id, "seal_seq": seal["seal_seq"]}
    art = ScopedKernel(store).vault.register_artifact(f"lab-session-{session_id}",
                                                      json.dumps(doc, sort_keys=True).encode(), ARTIFACT_KIND)
    body = {"project": project, "event": "session", "session_id": session_id, "ledger_seq": art.seq, "at": time.time()}
    store.append_record(RECORD_KIND, body)
    return body


def lab_part(store: EvidenceStore, project: str) -> bytes:
    """The bytes the Lab receives: the public remainder."""
    seal = sealed_record(store, project)
    if seal is None:
        raise SealRefused(f"project {project!r} is not sealed")
    return bytes(ScopedKernel(store).vault.read_public(seal["train_id"], "train.csv"))


def seal_fact(store: EvidenceStore, kernel: ScopedKernel, target: str, session_id: str | None) -> dict[str, Any]:
    """Whether `target` was sealed before any Lab session of its project, read from the ledger. `at_start` is the
    answer; the other fields say why."""
    seal = next((r for r in store.records(RECORD_KIND)
                 if r.get("event") == "sealed" and target in (r.get("targets") or [])), None)
    if seal is None:
        return {"project": None, "provenance": None, "at_start": False, "why": "no seal names this target"}
    project = str(seal["project"])
    seal_seq = max(e.seq for e in kernel.ledger.entries("register_dataset") if e.body.get("dataset_id") == target)
    sessions: dict[str, int] = {}
    for art in kernel.vault.artifacts().values():
        if art.kind == ARTIFACT_KIND:
            doc = json.loads(store.blobs.get(art.hash))
            if doc.get("project") == project:
                sessions[str(doc["session_id"])] = art.seq
    late = sorted(s for s, seq in sessions.items() if seq < seal_seq)
    why = ""
    if not sessions:
        why = "no Lab session of this project is in the ledger"
    elif late:
        why = f"sessions {late} are in the ledger before the seal"
    elif session_id not in sessions:
        why = "the case's session is not a session of this project"
    return {"project": project, "provenance": seal.get("provenance"), "seal_seq": seal_seq, "sessions": sessions,
            "at_start": not why, "why": why}
