"""Compute-backend interface (GOAL §5 L0 "Compute backends", invariant 1).

Every backend declares an ISOLATION CLASS, derived from the isolation properties it actually provides. Only a backend
whose properties meet invariant 1 (container, no secrets, a network policy of "off" or an allowlist, read-only root
filesystem, non-root user) is ISOLATED and may run agent-written code. Any other backend (a plain SSH host, a local
process, a container with full network) is TRUSTED_ONLY: it runs trusted code only, and only with a recorded human
approval bound to exactly that request (ledger kind "approval", node_id NON_ISOLATED_NODE). Agent-written code never
runs on a TRUSTED_ONLY backend, with or without an approval (GOAL L0, clarified 2026-10-08: invariant 1 wins).

Every run executes a RECORDED revision (a blob or a workspace snapshot in the content-addressed store), materialised
into a new directory of its own; a run can never be pointed at a live working tree (GOAL §5 L0 "Sandbox &
snapshots", OR). The admission check runs before anything is materialised or executed.

Trust boundary (stated honestly): the caller declares the code's origin; the default is AGENT (fail closed), so
trusted code must say so explicitly. A caller that mislabels agent code as TRUSTED is not detected here; it is
prevented by callers being human-written code, as for modules (runtime/engine.py). The approval recorder is trusted
host code (like `chh finalize`): the ledger entry names the approver and is hash-chained (accountability, not
enforcement).
"""

from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..kernel.ledger import Ledger
from ..runtime.engine import approval_item_hash
from ..sandbox.base import ExecResult, Sandbox, safe_path
from ..sandbox.snapshot import restore_snapshot, take_snapshot
from ..store import EvidenceStore
from ..store.blobs import BlobStore

NON_ISOLATED_NODE = "compute:non-isolated"
_HASH = re.compile(r"[0-9a-f]{64}")
# environment variable NAMES that look like credentials: never passed to agent code (invariant 1, "no secrets")
_SECRET_NAME = re.compile(r"(?i)(key|token|secret|passw|credential|auth|cookie|session)")


class IsolationClass(StrEnum):
    ISOLATED = "isolated"  # meets invariant 1: may run agent-written code
    TRUSTED_ONLY = "trusted_only"  # trusted code only, with a recorded human approval; agent code never


class CodeOrigin(StrEnum):
    AGENT = "agent"  # written by a model, an agent or an evolved harness
    TRUSTED = "trusted"  # human-written, reviewed code (task builders, checkers)


class IsolationRefused(PermissionError):
    """The request may not run on this backend (agent code on a non-isolated backend, trusted code there without a
    recorded human approval, or a secret-looking variable for agent code)."""


class LiveTreeRefused(TypeError):
    """A run was pointed at something other than a recorded revision (e.g. a live directory)."""


class RemoteNotBuilt(NotImplementedError):
    """A declared backend whose execution path is not built (no remote connection is ever made)."""


@dataclass(frozen=True)
class IsolationProperties:
    container: bool
    no_secrets: bool
    network_policy: str  # "off" | "allowlist" | "open" | "unknown"
    read_only_root: bool
    non_root: bool
    #: False when the environment itself holds hidden data (e.g. an image shipping bundled datasets). Invariant 2:
    #: agent code never runs there, and no approval changes that.
    hidden_data_free: bool = True

    @property
    def meets_invariant_1(self) -> bool:
        return (self.container and self.no_secrets and self.network_policy in ("off", "allowlist")
                and self.read_only_root and self.non_root)

    def unmet(self) -> list[str]:
        out = [name for name in ("container", "no_secrets", "read_only_root", "non_root") if not getattr(self, name)]
        if self.network_policy not in ("off", "allowlist"):
            out.append(f"network_policy={self.network_policy}")
        return out


@dataclass(frozen=True)
class Revision:
    """A recorded revision: `kind` "file" (one blob, written as `entrypoint`) or "snapshot" (a workspace snapshot
    manifest). Only hashes: a path cannot be a revision."""

    kind: str
    ref: str
    entrypoint: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("file", "snapshot"):
            raise ValueError(f"unknown revision kind {self.kind!r}")
        if not isinstance(self.ref, str) or not _HASH.fullmatch(self.ref):
            raise LiveTreeRefused(f"a revision is a recorded content hash, not {self.ref!r}")
        if self.kind == "file" and not self.entrypoint:
            raise ValueError("a file revision needs an entrypoint name")

    @staticmethod
    def record_tree(tree: Path, blobs: BlobStore) -> Revision:
        """Record the current state of a working tree as a snapshot revision (the tree itself is never run)."""
        return Revision("snapshot", take_snapshot(Path(tree), blobs))

    @staticmethod
    def record_file(entrypoint: str, data: bytes, blobs: BlobStore) -> Revision:
        return Revision("file", blobs.put(data), entrypoint)


def fresh_dir(root: Path, key: str) -> Path:
    """A path under `root` that does not exist yet and never will be reused (key + random suffix)."""
    safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:80] or "run"
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f"{safe_key}-{uuid.uuid4().hex[:12]}"
    if dest.exists():  # pragma: no cover - uuid collision
        raise FileExistsError(dest)
    return dest


def unique_workspaces(maker: Callable[[Path], Sandbox], root: Path) -> Callable[[str], Sandbox]:
    """Wrap a sandbox constructor as a key -> Sandbox factory whose every call gets a NEW directory (so a retried
    run with the same key never inherits the previous attempt's files)."""
    return lambda key: maker(fresh_dir(root, key))


def materialise(revision: Revision, blobs: BlobStore, root: Path, key: str) -> Path:
    """Restore `revision` into a NEW directory under `root` (unique name; an existing path is never reused).
    Every blob is hash-checked before use (BlobStore.get, restore_snapshot's preflight)."""
    if not isinstance(revision, Revision):
        raise LiveTreeRefused(f"runs execute recorded revisions only, not {type(revision).__name__} {revision!r}")
    dest = fresh_dir(root, key)
    if revision.kind == "snapshot":
        manifest = blobs.get_json(revision.ref)
        if not isinstance(manifest, dict) or manifest.get("kind", "workspace_snapshot") != "workspace_snapshot":
            raise LiveTreeRefused(f"{revision.ref} is not a workspace snapshot")
        restore_snapshot(revision.ref, dest, blobs)
    else:
        data = blobs.get(revision.ref)
        dest.mkdir(parents=False, exist_ok=False)
        target = safe_path(dest, revision.entrypoint or "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dest


@dataclass(frozen=True)
class RunRequest:
    revision: Revision
    command: tuple[str, ...]
    origin: CodeOrigin = CodeOrigin.AGENT  # fail closed: trusted code must say so
    timeout_s: float = 600.0
    env: Mapping[str, str] = field(default_factory=dict)
    key: str = "run"

    def item(self, backend: str) -> dict[str, Any]:
        """What a human approval binds to (everything that decides what runs where)."""
        return {"backend": backend, "revision": {"kind": self.revision.kind, "ref": self.revision.ref,
                                                 "entrypoint": self.revision.entrypoint},
                "command": list(self.command), "origin": self.origin.value, "env": dict(sorted(self.env.items()))}

    def item_hash(self, backend: str) -> str:
        return approval_item_hash(self.item(backend))


@dataclass
class RunResult:
    result: ExecResult
    run_dir: Path
    revision: Revision
    backend: str
    isolation_class: IsolationClass
    origin: CodeOrigin
    approval_seq: int | None  # ledger seq of the human approval, when trusted code ran on a non-isolated backend
    environment: dict[str, str]


def record_non_isolated_approval(store: EvidenceStore, *, approver: str, backend: str, request: RunRequest,
                                 note: str = "") -> int:
    """HUMAN action: approve exactly this TRUSTED-code request on this non-isolated backend. Returns the ledger seq.
    Recording an approval for agent code is allowed (it is only a ledger entry) but never admits it (see admit)."""
    if not approver.strip():
        raise ValueError("an approval must name a human approver")
    h = request.item_hash(backend)
    e = Ledger(store).append("approval", {"run_id": f"{NON_ISOLATED_NODE}:{h[:16]}", "node_id": NON_ISOLATED_NODE,
                                          "item_hash": h, "decision": "approved", "approver": approver.strip(),
                                          "note": note, "compute": request.item(backend)})
    return e.seq


def find_approval(store: EvidenceStore, item_hash: str) -> int | None:
    """Ledger seq of an intact human approval for exactly `item_hash` (latest decision wins), else None."""
    for e in reversed(Ledger(store).entries("approval")):
        b = e.body
        if b.get("node_id") == NON_ISOLATED_NODE and b.get("item_hash") == item_hash and e.intact():
            ok = b.get("decision") == "approved" and isinstance(b.get("approver"), str) and b["approver"].strip()
            return e.seq if ok else None
    return None


class ComputeBackend(ABC):
    """One place where runs execute. Subclasses declare `name`, `isolation` and how to make a sandbox for a run
    directory; `run` enforces the policy (admission before anything is materialised or executed)."""

    name: str
    isolation: IsolationProperties

    def __init__(self, store: EvidenceStore, runs_root: Path):
        self.store, self.runs_root = store, Path(runs_root)

    @property
    def isolation_class(self) -> IsolationClass:
        ok = self.isolation.meets_invariant_1 and self.isolation.hidden_data_free
        return IsolationClass.ISOLATED if ok else IsolationClass.TRUSTED_ONLY

    def admit(self, request: RunRequest) -> int | None:
        """Raise IsolationRefused unless `request` may run here; return the approval seq used (if any)."""
        if not isinstance(request, RunRequest) or not isinstance(request.revision, Revision):
            raise LiveTreeRefused("a run needs a RunRequest with a recorded Revision")
        if not isinstance(request.origin, CodeOrigin):
            raise IsolationRefused(f"unknown code origin {request.origin!r}")
        if request.origin is CodeOrigin.TRUSTED:
            if self.isolation_class is IsolationClass.ISOLATED:
                return None
            seq = find_approval(self.store, request.item_hash(self.name))
            if seq is None:
                raise IsolationRefused(
                    f"backend {self.name!r} is {self.isolation_class.value}: trusted code runs there only with a "
                    f"recorded human approval of exactly this request (item {request.item_hash(self.name)})")
            return seq
        if not self.isolation.hidden_data_free:
            raise IsolationRefused(f"backend {self.name!r} holds hidden data in its environment (invariant 2): agent "
                                   "code never runs there, approved or not")
        secret = sorted(k for k in request.env if _SECRET_NAME.search(k))
        if secret:
            raise IsolationRefused(f"agent code never receives credentials: refusing variables {secret}")
        if self.isolation_class is IsolationClass.ISOLATED:
            return None
        raise IsolationRefused(
            f"backend {self.name!r} is {self.isolation_class.value} (does not meet invariant 1: "
            f"{', '.join(self.isolation.unmet())}); agent-written code never runs on a non-isolated backend, with or "
            f"without a human approval (GOAL L0)")

    def run(self, request: RunRequest) -> RunResult:
        seq = self.admit(request)  # before anything is touched
        run_dir = materialise(request.revision, self.store.blobs, self.runs_root, request.key)
        sandbox = self._sandbox(run_dir)
        res = sandbox.exec(list(request.command), timeout_s=request.timeout_s, env=dict(request.env) or None)
        return RunResult(res, run_dir, request.revision, self.name, self.isolation_class, request.origin, seq,
                         sandbox.environment())

    @abstractmethod
    def _sandbox(self, run_dir: Path) -> Sandbox:
        """A sandbox whose workspace is exactly `run_dir` (fresh, holding only the materialised revision)."""
