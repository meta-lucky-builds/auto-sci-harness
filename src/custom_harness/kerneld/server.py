"""kerneld server (GOAL §16.5.2): the one local process that opens the evidence store for the vault's private-read
path. Run as `python -m custom_harness.kerneld.server --store ROOT`; the client (`client.py`) spawns it.

Transport: JSON lines on stdin/stdout (`protocol.py`). The first stdin line is the launch secret; `open_run` must
present it. Every private read is a `Vault.read` made HERE, so its `data_access` ledger entry is written by this
process before any label is used, and labels never leave it: `score`/`rescore` return one float, `leak_scan`
returns location strings only (`verify.leak.leak_audit` never reports values), and errors are closed codes.

Trust boundary (stated, not hidden): the parent holds the launch secret and the pipe, so it can open runs and
issue tokens for any purpose the token table allows. What the boundary buys is that host code which only holds a
RunHandle can make exactly the reads its tokens name, every read is logged by a process that host code cannot
skip, and no private byte crosses the pipe. Host code that still opens the store itself can still read the blobs
directly; moving those reads here is the migration this module enables.
"""

from __future__ import annotations

import argparse
import hmac
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

from . import protocol as P
from .tokens import TokenError, TokenTable

MAX_EXTRA_TEXTS = 16  # UNCALIBRATED (owner decision, GOAL §13.8): extra texts per leak scan (each is one probe)


class _Stop(Exception):
    pass


def _labels(raw: bytes) -> list[str]:
    """Same parsing as research/claims.py `_labels`: csv, first column, header skipped."""
    import csv
    import io
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
    return [r[0] for r in rows[1:] if r]


def _trace(v: Any) -> list[SimpleNamespace]:
    if not isinstance(v, list):
        raise P.ProtocolError()
    out = []
    for step in v:
        if not isinstance(step, dict) or set(step) - {"index", "node_id", "output", "error"}:
            raise P.ProtocolError()
        index, node_id, error = step.get("index"), step.get("node_id"), step.get("error")
        if (not isinstance(index, int) or isinstance(index, bool) or not isinstance(node_id, str)
                or (error is not None and not isinstance(error, str))):
            raise P.ProtocolError()
        out.append(SimpleNamespace(index=index, node_id=node_id, output=step.get("output"), error=error))
    return out


class Server:
    def __init__(self, root: Path, launch_secret: str):
        from ..kernel import Kernel
        from ..store import EvidenceStore
        self.store = EvidenceStore(root)
        self.kernel = Kernel(self.store)
        self.vault = self.kernel.vault
        self.table = TokenTable(self.vault)
        self._secret = launch_secret

    def close(self) -> None:
        self.store.close()

    # ----------------------------------------------------------------------------------------- dispatch
    def handle(self, line: str) -> str:
        rid = P.request_id(line)
        try:
            rid, op, args = P.decode_request(line)
            return P.encode_ok(rid, getattr(self, f"op_{op}")(args))
        except _Stop:
            raise
        except (P.ProtocolError, TokenError) as e:
            return P.encode_error(rid, e.code)
        except Exception as e:  # noqa: BLE001 - closed codes only: never str(e) across the boundary
            return P.encode_error(rid, self._code(e))

    @staticmethod
    def _code(e: BaseException) -> str:
        from ..kernel import VaultError
        return "vault_error" if isinstance(e, VaultError) else "internal"

    # ----------------------------------------------------------------------------------------- ops
    def op_hello(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"pid": os.getpid(), "ops": list(P.OPS)}

    def op_shutdown(self, args: dict[str, Any]) -> dict[str, Any]:
        raise _Stop()

    def op_open_run(self, args: dict[str, Any]) -> dict[str, Any]:
        secret = P.arg(args, "secret", str)
        run_id = P.arg(args, "run_id", str)
        if not hmac.compare_digest(secret.encode(), self._secret.encode()):
            raise TokenError("bad_secret")
        return {"run_cap": self.table.open_run(run_id)}

    def op_close_run(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"revoked": self.table.close_run(P.arg(args, "run_cap", str))}

    def op_issue(self, args: dict[str, Any]) -> dict[str, Any]:
        token = self.table.issue(P.arg(args, "run_cap", str), P.arg(args, "purpose", str),
                                 P.arg(args, "dataset_id", str), P.arg(args, "file", str),
                                 claim_hash=P.arg(args, "claim_hash", str, optional=True),
                                 arm=P.arg(args, "arm", str, optional=True),
                                 seed=P.arg(args, "seed", int, optional=True))
        return {"token": token}

    def _scored(self, args: dict[str, Any], purpose: str) -> dict[str, Any]:
        from ..verify import metrics as M
        cap = P.arg(args, "run_cap", str)
        metric = P.arg(args, "metric", str)
        preds = P.str_list(args.get("preds"))
        tok = self.table.use(cap, P.arg(args, "token", str), purpose)
        run_id = self.table.run_id(cap)
        if purpose == "evaluate":
            raw = self.vault.read(tok.dataset_id, tok.file, purpose="evaluate", run_id=run_id,
                                  claim_hash=tok.claim_hash, arm=tok.arm, seed=tok.seed)
        else:
            raw = self.vault.read(tok.dataset_id, tok.file, purpose="kernel", run_id=run_id,
                                  claim_hash=tok.claim_hash)
        return {"score": float(M.score(metric, _labels(raw), preds))}

    def op_score(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._scored(args, "evaluate")

    def op_rescore(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._scored(args, "kernel")

    def op_leak_scan(self, args: dict[str, Any]) -> dict[str, Any]:
        from ..verify.leak import label_windows, leak_audit
        cap = P.arg(args, "run_cap", str)
        trace = _trace(args.get("trace", []))
        workspace = P.arg(args, "workspace", str, optional=True)
        extra = args.get("extra_texts") or {}
        if not isinstance(extra, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                  for k, v in extra.items()):
            raise P.ProtocolError()
        extra_canaries = P.str_list(args.get("extra_canaries") or [])
        tok = self.table.use(cap, P.arg(args, "token", str), "leakscan")
        if len(extra) > MAX_EXTRA_TEXTS:  # many probes in one call would bypass the per-run call budget
            raise TokenError("rate_limited")
        self.table.charge_leak_scan(cap)  # membership-oracle budget, charged before the private read
        ds = self.vault.dataset(tok.dataset_id)
        raw = self.vault.read(tok.dataset_id, tok.file, purpose="leakscan", run_id=self.table.run_id(cap),
                              claim_hash=tok.claim_hash)
        fp = {"hashes": {f.blob: f"{tok.dataset_id}/{name}" for name, f in ds.files.items() if f.private},
              "canaries": [], "label_windows": label_windows(_labels(raw)), "contents": [raw]}
        canaries = [c for c in (self.vault.canary(tok.dataset_id), *extra_canaries) if c]
        res = leak_audit(trace, Path(workspace) if workspace else None, fp, canaries, extra_texts=extra)
        return {"problems": [str(p) for p in res["problems"]]}

    def op_record_release(self, args: dict[str, Any]) -> dict[str, Any]:
        from ..kernel import KernelError
        from .releases import LedgerSink
        self.table.run_id(P.arg(args, "run_cap", str))
        target, channel = P.arg(args, "target_dataset_id", str), P.arg(args, "channel", str)
        alphabet, note = P.arg(args, "alphabet", str), P.arg(args, "note", str)
        try:
            LedgerSink(self.store).record_release(target, channel, alphabet, note)
        except (ValueError, KernelError):
            raise TokenError("release_error") from None
        return {"ok": True}


def _readline(inp: BinaryIO) -> str | None:
    b = inp.readline()
    if not b:
        return None
    return b.decode("utf-8", errors="replace")


def serve(root: Path, inp: BinaryIO, out: BinaryIO) -> int:
    first = _readline(inp)
    if first is None:
        return 2
    try:
        secret = P.decode_launch(first)
    except P.ProtocolError:
        return 2
    if not root.is_dir():
        return 2
    server = Server(root, secret)
    try:
        while True:
            line = _readline(inp)
            if line is None:
                return 0
            if not line.strip():
                continue
            try:
                reply = server.handle(line)
            except _Stop:
                out.write(P.encode_ok(P.request_id(line), {"bye": True}).encode("utf-8"))
                out.flush()
                return 0
            out.write(reply.encode("utf-8"))
            out.flush()
    finally:
        server.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="custom_harness.kerneld.server")
    ap.add_argument("--store", required=True, type=Path)
    a = ap.parse_args(argv)
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # nothing but protocol lines may ever reach the real stdout
    return serve(a.store, sys.stdin.buffer, out)


if __name__ == "__main__":
    sys.exit(main())
