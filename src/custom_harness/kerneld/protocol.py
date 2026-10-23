"""Wire protocol of kerneld (GOAL §16.5.2): JSON lines over the server's stdin/stdout, utf-8, one object per line.

    launch   {"launch_secret": "<64 hex>"}                       first stdin line only (never argv/env)
    request  {"id": int, "op": <one of OPS>, "args": {...}}
    response {"id": int, "ok": true, "result": {...}}  or  {"id": int, "ok": false, "code": <one of ERRORS>}

No free text crosses the boundary in either direction of an error: a failure is a closed code (GOAL §16.5.8
spirit), never an exception message, so a message can never carry a private value out of the server.
Stdlib only: the client imports this module and must stay free of the store and the Kernel.
"""

from __future__ import annotations

import json
import re
from typing import Any

OPS = ("hello", "open_run", "issue", "score", "rescore", "leak_scan", "record_release", "issue_ingest", "ingest",
       "truth_query", "close_run", "shutdown")
ERRORS = ("bad_request", "unknown_op", "bad_secret", "bad_token", "token_scope", "token_spent", "not_registered",
          "not_target", "not_private", "vault_error", "release_error", "rate_limited", "internal",
          # private ingest (kerneld/ingest.py, 2026-10-14)
          "not_found", "data_unavailable", "already_registered", "canary_mismatch", "ingest_refused",
          "measurements_refused", "truth_layout",
          # minted Court targets (kerneld/ingest.minted_heldout, int18)
          "not_anchored")
PURPOSES = ("evaluate", "kernel", "leakscan")  # the only READ purposes a token can carry (`issue`)
INGEST = "ingest"  # the purpose of an ingest token (`issue_ingest` only; never a read)
INGEST_KINDS = ("research_task", "openml_split", "returned_measurements", "minted_heldout")  # kerneld/ingest.py
# closed problem classes of refused returned measurements (kerneld/ingest.py classifies check_measurements' messages)
TRUTH_QUERIES = ("target_id", "truth_hashes", "label_agreement", "export_scan")  # kerneld/truth.py
MEASUREMENT_PROBLEMS = ("not_utf8", "columns", "row_length", "non_finite", "replicates", "other")
HEX64 = re.compile(r"^[0-9a-f]{64}\Z")


class ProtocolError(ValueError):
    """A malformed line or object; `.code` is one of ERRORS."""

    def __init__(self, code: str = "bad_request"):
        assert code in ERRORS
        super().__init__(code)
        self.code = code


def _line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    if len({k for k, _ in pairs}) != len(pairs):
        raise ValueError("duplicate key")
    return dict(pairs)


def _loads(line: str) -> Any:
    """json.loads refusing a duplicated key (two readers would see two different requests)."""
    return json.loads(line, object_pairs_hook=_unique)


def _obj(line: str) -> dict[str, Any]:
    try:
        obj = _loads(line)
    except (ValueError, RecursionError):
        raise ProtocolError() from None
    if not isinstance(obj, dict):
        raise ProtocolError()
    return obj


# ------------------------------------------------------------------------------------------- launch line
def encode_launch(secret: str) -> str:
    if not HEX64.match(secret):
        raise ProtocolError()
    return _line({"launch_secret": secret})


def decode_launch(line: str) -> str:
    obj = _obj(line)
    secret = obj.get("launch_secret")
    if set(obj) != {"launch_secret"} or not isinstance(secret, str) or not HEX64.match(secret):
        raise ProtocolError()
    return secret


# ------------------------------------------------------------------------------------------- requests
def encode_request(rid: int, op: str, args: dict[str, Any]) -> str:
    return _line({"id": rid, "op": op, "args": args})


def request_id(line: str) -> int | None:
    """The id of a (possibly malformed) request line, if it has a usable one (to address the error reply)."""
    try:
        obj = _loads(line)
    except (ValueError, RecursionError):
        return None
    rid = obj.get("id") if isinstance(obj, dict) else None
    return rid if isinstance(rid, int) and not isinstance(rid, bool) else None


def decode_request(line: str) -> tuple[int, str, dict[str, Any]]:
    """(id, op, args). Unknown op -> ProtocolError("unknown_op"); any other malformation -> "bad_request"."""
    obj = _obj(line)
    rid, op, args = obj.get("id"), obj.get("op"), obj.get("args", {})
    if (set(obj) - {"id", "op", "args"} or not isinstance(rid, int) or isinstance(rid, bool)
            or not isinstance(op, str) or not isinstance(args, dict)):
        raise ProtocolError()
    if op not in OPS:
        raise ProtocolError("unknown_op")
    return rid, op, args


# ------------------------------------------------------------------------------------------- responses
def encode_ok(rid: int | None, result: dict[str, Any]) -> str:
    return _line({"id": rid, "ok": True, "result": result})


def encode_error(rid: int | None, code: str) -> str:
    if code not in ERRORS:
        code = "internal"
    return _line({"id": rid, "ok": False, "code": code})


def decode_response(line: str) -> tuple[int | None, bool, Any]:
    """(id, ok, result-or-code). A reply that is not exactly one of the two shapes -> ProtocolError."""
    obj = _obj(line)
    rid, ok = obj.get("id"), obj.get("ok")
    if rid is not None and (not isinstance(rid, int) or isinstance(rid, bool)):
        raise ProtocolError()
    if ok is True and set(obj) == {"id", "ok", "result"} and isinstance(obj["result"], dict):
        return rid, True, obj["result"]
    if ok is False and set(obj) == {"id", "ok", "code"} and obj["code"] in ERRORS:
        return rid, False, obj["code"]
    raise ProtocolError()


# ------------------------------------------------------------------------------------------- arg checks
def arg(args: dict[str, Any], name: str, typ: type | tuple[type, ...], *, optional: bool = False) -> Any:
    """`args[name]` of type `typ` (bool is never an int here); missing/wrong -> ProtocolError("bad_request")."""
    if name not in args or args[name] is None:
        if optional:
            return None
        raise ProtocolError()
    v = args[name]
    if isinstance(v, bool) and bool not in (typ if isinstance(typ, tuple) else (typ,)):
        raise ProtocolError()
    if not isinstance(v, typ):
        raise ProtocolError()
    return v


def str_list(v: Any) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ProtocolError()
    return v
