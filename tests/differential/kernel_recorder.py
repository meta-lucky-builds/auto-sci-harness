"""Records every Evidence Kernel decision made in a process, RAW, as JSONL (differential testing of a Kernel refactor).

Used two ways (see docs/kernel_differential.md):
- as a pytest plugin: `scripts/kernel_differential.py suite` loads this module into pytest.main(plugins=[...]) so the
  Kernel tests of the tree under test run unchanged while every call is captured;
- by the seeded-history generator (`scripts/kernel_differential.py histories`), through `install()`/`set_scope()`.

What is recorded (raw: NO normalisation here, normalisation is done by the diff tool, applied identically to both
sides, so it can be proved necessary by diffing two raw runs of the same commit):
- every `Kernel.certify` call (nested ones too, e.g. inside `verify_status` and bundle reproduction): its keyword
  arguments, then the whole certificate (`model_dump(mode="json")`: label, tier, K0-K9 status/reasons/codes/
  observations, resolution, verifier_ids, assumptions, adapters, config, ...) or the exception it raised;
- every `Kernel.preregister` call: the ledger body (claim hash and the FIXED terms p0, forecast, K3 reliability) or the
  refusal (exception type and message);
- every `Kernel.verify_status` call: status, forged, stale (or the exception);
- in pytest mode, each test's outcome per phase (setup/call/teardown).
Each record carries `scope` (the pytest node id, or the history id) and `i` (call index within that scope).

The patch wraps the class attributes of `custom_harness.kernel.Kernel` (whatever module defines it), calls the original
unchanged and only reads its return value, so it cannot change a verdict. This file must be the SAME file for the OLD
and the NEW run: it lives in the harness tree, never in the trees under test.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
from pathlib import Path
from typing import Any, TextIO

ENV = "CHH_KERNEL_RECORD"

_sink: TextIO | None = None
_scope = "<none>"
_counter: dict[str, int] = {}
_installed = False


def set_scope(scope: str) -> None:
    global _scope
    _scope = scope


def _emit(rec: dict[str, Any]) -> None:
    if _sink is None:
        return
    _sink.write(json.dumps(rec, sort_keys=True, default=_default) + "\n")
    _sink.flush()


def _default(o: Any) -> Any:
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=repr)
    if isinstance(o, Path):
        return str(o)
    return repr(o)


def _call_record(api: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    i = _counter.get(_scope, 0)
    _counter[_scope] = i + 1
    return {"t": "call", "scope": _scope, "i": i, "api": api, "kwargs": kwargs}


def _error(e: BaseException) -> dict[str, Any]:
    return {"type": type(e).__name__, "message": str(e)}


def install(sink: TextIO) -> None:
    """Patch Kernel.certify / preregister / verify_status to record into `sink`. Idempotent."""
    global _sink, _installed
    _sink = sink
    if _installed:
        return
    from custom_harness.kernel import Kernel

    orig_certify = Kernel.certify
    orig_prereg = Kernel.preregister
    orig_verify = Kernel.verify_status

    @functools.wraps(orig_certify)
    def certify(self: Any, claim: Any, evidence_ids: Any, **kw: Any) -> Any:
        rec = _call_record("certify", {"evidence_ids": list(evidence_ids), **kw})
        try:
            cert = orig_certify(self, claim, evidence_ids, **kw)
        except BaseException as e:
            rec["error"] = _error(e)
            _emit(rec)
            raise
        rec["result"] = cert.model_dump(mode="json")
        _emit(rec)
        return cert

    @functools.wraps(orig_prereg)
    def preregister(self: Any, claim: Any) -> Any:
        rec = _call_record("preregister", {"claim": claim.model_dump(mode="json")})
        try:
            entry = orig_prereg(self, claim)
        except BaseException as e:
            rec["error"] = _error(e)
            _emit(rec)
            raise
        rec["result"] = {"kind": entry.kind, "body": entry.body}
        _emit(rec)
        return entry

    @functools.wraps(orig_verify)
    def verify_status(self: Any, cert_hash: str) -> Any:
        rec = _call_record("verify_status", {"cert_hash": cert_hash})
        try:
            res = orig_verify(self, cert_hash)
        except BaseException as e:
            rec["error"] = _error(e)
            _emit(rec)
            raise
        rec["result"] = {"status": res.status, "forged": list(res.forged), "stale": list(res.stale)}
        _emit(rec)
        return res

    Kernel.certify = certify
    Kernel.preregister = preregister
    Kernel.verify_status = verify_status
    _installed = True


def write_header(info: dict[str, Any]) -> None:
    _emit({"t": "header", **info})


# ---------------------------------------------------------------------- pytest plugin hooks (active only with ENV set)
def pytest_configure(config: Any) -> None:
    path = os.environ.get(ENV)
    if not path:
        return
    import custom_harness

    f = open(path, "w", encoding="utf-8")
    config._kernel_record_file = f
    install(f)
    write_header({"mode": "suite", "custom_harness": str(Path(custom_harness.__file__).resolve()),
                  "args": [str(a) for a in config.invocation_params.args]})


def pytest_unconfigure(config: Any) -> None:
    f = getattr(config, "_kernel_record_file", None)
    if f is not None:
        f.close()


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:  # before setup: fixtures' calls belong to the test
    set_scope(nodeid)


def pytest_runtest_logreport(report: Any) -> None:
    if _sink is None:
        return
    if report.when == "call" or report.outcome != "passed":
        _emit({"t": "test", "scope": report.nodeid, "when": report.when, "outcome": report.outcome})
