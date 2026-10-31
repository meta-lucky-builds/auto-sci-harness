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
    """kerneld refused a request; `.code` is the closed protocol code. The only other detail that ever exists is
    `.problems`: the closed problem classes of refused returned measurements (protocol.MEASUREMENT_PROBLEMS)."""

    def __init__(self, code: str, problems: tuple[str, ...] = ()):
        super().__init__(code)
        self.code = code
        self.problems = problems


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


def process_started_at(pid: int) -> int | None:
    """A process's creation time in the OS's own units, or None if it does not exist or cannot be queried. With the
    pid it identifies one process: a pid can be reused, a (pid, creation time) pair cannot."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            code = wintypes.DWORD()
            times = k32.GetProcessTimes(handle, created, exited, kernel, user)
            if not times or not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            k32.CloseHandle(handle)
    try:  # field 22 of /proc/<pid>/stat, counted after the command name (which may hold spaces)
        return int(Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _package_parent() -> str:
    return str(Path(custom_harness.__file__).resolve().parent.parent)


class KernelD:
    def __init__(self, store_root: Path | str, *, log_path: Path | str | None = None, timeout_s: float = 10.0):
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._next = 0
        self._secret = secrets.token_hex(32)
        self.last_reply: str | None = None  # the raw last reply line (diagnostics and tests)
        self._server: tuple[int, int | None] | None = None  # (pid, creation time) of the server, taken at hello
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
            self._server = (self.pid, process_started_at(self.pid))  # identity, while the server is known to be alive
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
            if isinstance(server_pid, int) and server_pid != self._proc.pid and self._is_server(server_pid):
                try:  # the launcher's child (Windows venv)
                    os.kill(server_pid, signal.SIGTERM)
                except OSError:
                    pass
            self._proc.kill()
            self._proc.wait(timeout=self.timeout_s)
        self._cleanup()

    def _is_server(self, pid: int) -> bool:
        """Still the process that answered hello? Unknown counts as no: never signal a process that may be another's
        (a reused pid). The launcher's death below ends the server anyway: closing stdin ends its serve loop."""
        known = self._server
        return known is not None and known[0] == pid and known[1] is not None and process_started_at(pid) == known[1]

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

    # ------------------------------------------------------------------------------- private ingest (kerneld/ingest.py)
    def ingest_token(self, kind: str, subject: str, *, claim_hash: str | None = None) -> str:
        return str(self._call("issue_ingest", kind=kind, subject=subject, claim_hash=claim_hash)["token"])

    def _ingest(self, kind: str, subject: str, bind_claim: str | None = None, **args: Any) -> dict[str, Any]:
        tok = self.ingest_token(kind, subject, claim_hash=bind_claim)
        res = self._call("ingest", token=tok, kind=kind, **args)
        if "refused" in res:
            problems = res.get("problems")
            if (res["refused"] != "measurements_refused" or not isinstance(problems, list)
                    or not all(p in P.MEASUREMENT_PROBLEMS for p in problems)):
                raise KernelDUnavailable("kerneld sent a malformed ingest refusal")
            raise KernelDError("measurements_refused", tuple(problems))
        return res

    def ingest_research_task(self, task_id: str, task_dir: Path | str) -> dict[str, str]:
        """Register a research task's data (kerneld reads the files; the labels never enter this process). Returns
        the vault ids {train, test, baseline}."""
        ids = self._ingest("research_task", task_id, task_dir=str(Path(task_dir).resolve()))["ids"]
        if not isinstance(ids, dict) or set(ids) != {"train", "test", "baseline"}:
            raise KernelDUnavailable("kerneld sent a malformed ingest reply")
        return {str(k): str(v) for k, v in ids.items()}

    def ingest_openml_split(self, claim_hash: str, cache_root: Path | str, spec: dict[str, Any]) -> dict[str, int]:
        """Split and register a Claim Audit's pinned fresh dataset (kerneld loads it from the cache and splits it).
        `spec` is the OpenMLDataset fields. Returns {n_train, n_test}."""
        subject = f"openml-{spec.get('openml_id')}-{str(spec.get('sha256', ''))[:16]}"  # OpenMLDataset.dataset_id
        res = self._ingest("openml_split", subject, bind_claim=claim_hash, cache_root=str(Path(cache_root).resolve()),
                           spec=dict(spec))
        return {"n_train": int(res["n_train"]), "n_test": int(res["n_test"])}

    def ingest_minted_heldout(self, claim_hash: str, target: str, *, family: str, n_train: int, n_test: int,
                              beacon_round: str, beacon_value: str, anchor_file: Path | str) -> dict[str, Any]:
        """Mint a Court case's held-out target IN kerneld after its claim is frozen and anchored (kerneld/mint.py).
        The labels never enter this process. Returns {n_train, n_test, task_id, mint_seq, anchor_length}."""
        res = self._ingest("minted_heldout", target, bind_claim=claim_hash, family=family, n_train=int(n_train),
                           n_test=int(n_test), beacon_round=beacon_round, beacon_value=beacon_value,
                           anchor_file=str(Path(anchor_file).resolve()))
        return {"n_train": int(res["n_train"]), "n_test": int(res["n_test"]), "task_id": str(res["task_id"]),
                "mint_seq": int(res["mint_seq"]), "anchor_length": int(res["anchor_length"])}

    def ingest_sealed_split(self, project: str, file: Path | str, *, label_column: str, n_parts: int,
                            rows_per_part: int, beacon_round: str, beacon_value: str) -> dict[str, Any]:
        """Seal part of a project's dataset IN kerneld (kerneld/seal.py): kerneld reads the file, this process only
        names it. Returns ids, counts, the data's sha256 and the ledger position of the seal."""
        return self._ingest("sealed_split", project, file=str(Path(file).resolve()), label_column=label_column,
                            n_parts=int(n_parts), rows_per_part=int(rows_per_part), beacon_round=beacon_round,
                            beacon_value=beacon_value)

    def ingest_returned_measurements(self, dataset_id: str, file: Path | str, protocol: dict[str, Any], *,
                                     claim_hash: str, attestation: str, approved_protocol_seq: int) -> str:
        """Register measurements humans returned (`chh return-data`) as a FUTURE dataset; kerneld reads the file and
        checks it against the protocol. Returns the data's sha256."""
        res = self._ingest("returned_measurements", dataset_id, bind_claim=claim_hash, file=str(Path(file).resolve()),
                           protocol=dict(protocol), claim_hash=claim_hash, attestation=attestation,
                           approved_protocol_seq=int(approved_protocol_seq))
        return str(res["data_sha256"])

    # ------------------------------------------------------------------------------- truth queries (kerneld/truth.py)
    def truth_query(self, kind: str, **args: Any) -> dict[str, Any]:
        """A fact about a task's held-out truth computed in kerneld: ids, hashes, counts or location strings."""
        return self._call("truth_query", kind=kind, **args)

    def held_out_score(self, dataset_id: str, file: str, metric: str, preds: Sequence[str], *, claim_hash: str,
                       arm: str, seed: int) -> float:
        tok = self.token("evaluate", dataset_id, file, claim_hash=claim_hash, arm=arm, seed=seed)
        return self.score(tok, metric, preds)

    def close(self) -> None:
        self._call("close_run")


def truth_query(kind: str, *, store_root: Path | str | None = None, run_id: str = "truth-query",
                **args: Any) -> dict[str, Any]:
    """One truth query in a short-lived kerneld (GOAL §16.5.2): for host code that has no kerneld session of its own
    (evolve/export.py, evolve/multiplicity.py, research/rebuild.py). `store_root` defaults to the data dir's store
    (the record of the query goes there). kerneld unavailable -> KernelDUnavailable (no fallback: the host never
    reads the truth itself)."""
    from ..config import data_dir
    root = Path(store_root) if store_root is not None else data_dir() / "store"
    root.mkdir(parents=True, exist_ok=True)  # kerneld serves only an existing store directory
    with KernelD(root) as kd:
        run = kd.open_run(run_id)
        try:
            return run.truth_query(kind, **args)
        finally:
            run.close()
