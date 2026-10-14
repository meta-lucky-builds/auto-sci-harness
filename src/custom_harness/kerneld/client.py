"""kerneld client (GOAL §16.5.2): spawns the server process and talks to it over a pipe.

This module holds no store and imports neither `custom_harness.store`, `custom_harness.kernel` nor the server
(tests/test_kerneld.py checks that by AST). Host code gets private-data results only as what the server returns:
a score, or leak-problem location strings.

    with KernelD(store_root) as kd:
        run = kd.open_run(run_id)
        s = run.held_out_score("test", "test_y.csv", "accuracy", preds, claim_hash=h, arm="subject", seed=0)
        run.close()
"""

from __future__ import annotations

import math
import os
import secrets
import signal
import subprocess
import sys
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import IO, Any

import custom_harness

from ..errors import InfraError
from . import protocol as P


class KernelDUnavailable(InfraError):
    """The kerneld process is gone or its pipe broke (not transient: a restarted kerneld knows no token)."""

    def __init__(self, msg: str = "kerneld is unavailable"):
        super().__init__(msg, transient=False)


class KernelDError(Exception):
    """kerneld refused a request; `.code` is the closed protocol code (no other detail exists)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _wire_safe(v: Any) -> Any:
    """A trace output as strict JSON for the wire (allow_nan=False): a non-finite float becomes None. Lossless for
    the leak scan, which reads only strings (and dict keys, kept as str) plus the truthiness of
    `_redacted_private_values` (a non-finite float there becomes a truthy marker, so the flag is never lost)."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {str(k): (True if k == "_redacted_private_values" and isinstance(x, float) and not math.isfinite(x)
                         else _wire_safe(x)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_wire_safe(x) for x in v]
    return v


def _package_parent() -> str:
    return str(Path(custom_harness.__file__).resolve().parent.parent)


class KernelD:
    def __init__(self, store_root: Path | str, *, log_path: Path | str | None = None, timeout_s: float = 10.0):
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._next = 0
        self._secret = secrets.token_hex(32)
        self.last_reply: str | None = None  # the raw last reply line (diagnostics and tests)
        env = dict(os.environ)
        parent = _package_parent()
        env["PYTHONPATH"] = parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONUNBUFFERED"] = "1"
        self._log: IO[bytes] | None = open(log_path, "ab") if log_path is not None else None
        self._proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "custom_harness.kerneld.server", "--store", str(store_root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._log if self._log is not None else subprocess.DEVNULL, env=env)
        try:
            self._write(P.encode_launch(self._secret))
            self.hello = self._call("hello", {})
        except BaseException:
            self.kill()
            raise

    # ----------------------------------------------------------------------------------- lifecycle
    @property
    def pid(self) -> int:
        """The server's own pid as it reports it (on a Windows venv, `sys.executable` is a launcher whose child
        is the real interpreter, so the Popen pid is not the process that holds the store)."""
        return int(self.hello["pid"])

    def alive(self) -> bool:
        return self._proc.poll() is None

    def __enter__(self) -> KernelD:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self.alive():
            try:
                self._call("shutdown", {})
            except (KernelDUnavailable, KernelDError):
                pass
            try:
                self._proc.wait(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                self.kill()
                return
        self._cleanup()

    def kill(self) -> None:
        if self.alive():
            server_pid = getattr(self, "hello", {}).get("pid")
            if isinstance(server_pid, int) and server_pid != self._proc.pid:  # the launcher's child (Windows venv)
                try:
                    os.kill(server_pid, signal.SIGTERM)
                except OSError:
                    pass
            self._proc.kill()
            self._proc.wait(timeout=self.timeout_s)
        self._cleanup()

    def _cleanup(self) -> None:
        for f in (self._proc.stdin, self._proc.stdout):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        if self._log is not None:
            self._log.close()
            self._log = None

    # ----------------------------------------------------------------------------------- transport
    def _write(self, line: str) -> None:
        stdin = self._proc.stdin
        if stdin is None or stdin.closed or not self.alive():
            raise KernelDUnavailable()
        try:
            stdin.write(line.encode("utf-8"))
            stdin.flush()
        except (OSError, ValueError):
            raise KernelDUnavailable() from None

    def _call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._next += 1
            rid = self._next
            self._write(P.encode_request(rid, op, args))
            stdout = self._proc.stdout
            try:
                raw = stdout.readline() if stdout is not None and not stdout.closed else b""
            except (OSError, ValueError):
                raw = b""
            if not raw:
                raise KernelDUnavailable()
            line = raw.decode("utf-8", errors="replace")
            self.last_reply = line
            try:
                got, ok, value = P.decode_response(line)
            except P.ProtocolError:
                raise KernelDUnavailable("kerneld sent a malformed reply") from None
            if got != rid:
                raise KernelDUnavailable("kerneld reply out of sequence")
            if not ok:
                raise KernelDError(value)
            return dict(value)

    # ----------------------------------------------------------------------------------- runs
    def open_run(self, run_id: str, *, secret: str | None = None) -> RunHandle:
        """A run capability. `secret` overrides the launch secret (only to test the refusal)."""
        res = self._call("open_run", {"secret": self._secret if secret is None else secret, "run_id": run_id})
        return RunHandle(self, run_id, str(res["run_cap"]))


class RunHandle:
    """Per-run capability holder: no store, no labels; only tokens and the server's answers."""

    def __init__(self, kd: KernelD, run_id: str, run_cap: str):
        self._kd = kd
        self.run_id = run_id
        self._cap = run_cap

    def _call(self, op: str, **args: Any) -> dict[str, Any]:
        return self._kd._call(op, {"run_cap": self._cap, **args})

    def token(self, purpose: str, dataset_id: str, file: str, *, claim_hash: str | None = None,
              arm: str | None = None, seed: int | None = None) -> str:
        return str(self._call("issue", purpose=purpose, dataset_id=dataset_id, file=file, claim_hash=claim_hash,
                              arm=arm, seed=seed)["token"])

    def score(self, token: str, metric: str, preds: Sequence[str]) -> float:
        return float(self._call("score", token=token, metric=metric, preds=list(preds))["score"])

    def rescore(self, token: str, metric: str, preds: Sequence[str]) -> float:
        return float(self._call("rescore", token=token, metric=metric, preds=list(preds))["score"])

    def leak_scan(self, token: str, trace: Iterable[Any], workspace: Path | str | None,
                  extra_texts: dict[str, str] | None = None, extra_canaries: Iterable[str] = ()) -> list[str]:
        """`trace` items are dicts {index,node_id,output,error} or objects with those attributes (TraceStep)."""
        steps = [s if isinstance(s, dict) else {"index": s.index, "node_id": s.node_id, "output": s.output,
                                                "error": s.error} for s in trace]
        steps = [{**s, "output": _wire_safe(s.get("output"))} for s in steps]
        res = self._call("leak_scan", token=token, trace=steps, workspace=str(workspace) if workspace else None,
                         extra_texts=dict(extra_texts or {}), extra_canaries=list(extra_canaries))
        return [str(p) for p in res["problems"]]

    def record_release(self, target_dataset_id: str, channel: str, alphabet: str, note: str) -> None:
        self._call("record_release", target_dataset_id=target_dataset_id, channel=channel, alphabet=alphabet,
                   note=note)

    def held_out_score(self, dataset_id: str, file: str, metric: str, preds: Sequence[str], *, claim_hash: str,
                       arm: str, seed: int) -> float:
        tok = self.token("evaluate", dataset_id, file, claim_hash=claim_hash, arm=arm, seed=seed)
        return self.score(tok, metric, preds)

    def close(self) -> None:
        self._call("close_run")
