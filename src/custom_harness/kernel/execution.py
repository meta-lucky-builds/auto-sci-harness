"""Held-out evaluation records and their re-execution (GOAL §4.3 K3, K5).

An ExecRecord is the complete, typed account of one evaluation of one arm on one target with one seed:
which code (by hash), which input files (by hash), which command, which environment and limits, which
predictions (by hash) and which reported score. Everything needed to re-run it exists in the store.

The command contract is the ML solution CLI (research/task.py), expressed as an argv template:
  {python} {entrypoint} --train {train} --test {test} --out {out} --seed {seed}
"""

from __future__ import annotations

import csv
import io
import secrets
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, SerializerFunctionWrapHandler, model_serializer

from ..sandbox.base import Sandbox, WorkspaceEscape, safe_path
from .vault import Vault

DEFAULT_ARGV = ("{python}", "{entrypoint}", "--train", "{train}", "--test", "{test}", "--out", "{out}",
                "--seed", "{seed}")
OUT_PATH = "out/preds.csv"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InputRef(_Model):
    dataset_id: str
    file: str
    blob: str  # hash of the exact bytes the program received


class ExecRecord(_Model):
    schema_version: Literal[1] = 1
    claim_hash: str
    arm: Literal["subject", "comparator"]
    artifact_hash: str
    entrypoint: str = "solution.py"
    argv: tuple[str, ...] = DEFAULT_ARGV
    inputs: dict[str, InputRef]  # role ("train", "test") -> public input file
    target: str  # dataset id whose PRIVATE labels score this run
    label_file: str
    seed: int
    env: dict[str, str]  # e.g. sandbox isolation description, image
    limits: dict[str, float]  # e.g. timeout_s
    preds_ref: str  # blob of the predictions (JSON list of strings)
    metric: str
    score: float
    run_id: str
    # invariant 21: the program came from REPLAYED model output (a cassette). Such a record is never evidence (K0
    # refuses it). Omitted from dumps when false, so records written before the field existed keep their bytes.
    replayed: bool = False

    @model_serializer(mode="wrap")
    def _omit_not_replayed(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        doc: dict[str, Any] = handler(self)
        if not doc.get("replayed"):
            doc.pop("replayed", None)
        return doc


class ExecutionError(RuntimeError):
    pass


def parse_predictions(data: bytes) -> list[str]:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    if not rows or rows[0] != ["prediction"]:
        raise ExecutionError("predictions file must start with the header 'prediction'")
    if any(len(r) != 1 for r in rows[1:]):
        raise ExecutionError("predictions file must have exactly one column")
    return [r[0] for r in rows[1:]]


def run_program(sandbox: Sandbox, *, code: bytes, entrypoint: str, argv: tuple[str, ...],
                inputs: dict[str, tuple[str, bytes]], seed: int, timeout_s: float) -> tuple[list[str] | None, str]:
    """Run one program in a sandbox workspace. Returns (predictions or None, diagnostic text).
    Every host-side workspace path goes through safe_path (agent code may plant links)."""
    ws = sandbox.workspace
    safe_path(ws, entrypoint).write_bytes(code)
    paths: dict[str, str] = {}
    for role, (name, data) in inputs.items():
        rel = f"in/{role}/{name}"
        p = safe_path(ws, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        paths[role] = rel
    safe_path(ws, "out").mkdir(exist_ok=True)
    out = safe_path(ws, OUT_PATH)
    if out.exists():
        out.unlink()
    values = {"python": sandbox.python, "entrypoint": entrypoint, "out": OUT_PATH, "seed": str(seed), **paths}
    try:
        cmd = [part.format(**values) for part in argv]
    except (KeyError, IndexError) as e:
        raise ExecutionError(f"argv template refers to an unknown placeholder: {e}") from e
    res = sandbox.exec(cmd, timeout_s=timeout_s)
    diag = f"rc={res.returncode} timed_out={res.timed_out} stderr_tail={res.stderr[-1500:]!r}"
    if res.returncode != 0:
        return None, diag
    try:
        out = safe_path(ws, OUT_PATH)
        return parse_predictions(out.read_bytes()), diag
    except (OSError, WorkspaceEscape, ExecutionError, UnicodeDecodeError) as e:
        return None, f"{diag}; unreadable predictions: {e}"


SandboxFactory = Callable[[str], Sandbox]


class SandboxReplayer:
    """Re-executes an ExecRecord from stored bytes in a FRESH sandbox (K3). Inputs are re-read from the
    vault and must still hash to what the record says the program received."""

    def __init__(self, sandbox_factory: SandboxFactory):
        self.sandbox_factory = sandbox_factory
        self.calls = 0
        # a per-replayer nonce in every key: callers build a NEW replayer per certify/reproduce, and a factory that maps
        # key -> root / key would otherwise hand a second replayer the directory (and files) of the first one
        self.nonce = secrets.token_hex(4)

    def replay(self, record: ExecRecord, vault: Vault) -> list[str] | None:
        self.calls += 1
        inputs: dict[str, tuple[str, bytes]] = {}
        for role, ref in record.inputs.items():
            data = vault.read_public(ref.dataset_id, ref.file)
            if vault.store.blobs.put(data) != ref.blob:
                raise ExecutionError(f"input {role} ({ref.dataset_id}/{ref.file}) no longer matches the record")
            inputs[role] = (ref.file, data)
        sandbox = self.sandbox_factory(f"replay-{record.arm}-{record.seed}-{record.artifact_hash[:10]}-{self.nonce}-"
                                       f"{self.calls}")
        preds, _ = run_program(sandbox, code=vault.store.blobs.get(record.artifact_hash), entrypoint=record.entrypoint,
                               argv=record.argv, inputs=inputs, seed=record.seed,
                               timeout_s=float(record.limits.get("timeout_s", 900)))
        return preds
