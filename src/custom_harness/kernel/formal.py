"""Lean 4 formal claims: the trusted record checker of the `lean` adapter (formal class, tier T0, GOAL §4.7).

The Kernel never runs Lean (agent-written proofs are untrusted code: they run only in the sandbox, invariant 1).
The adapter's sandbox runner (custom_harness.sandbox.lean) compiles the proof, then the HUMAN-APPROVED statement,
then runs the pinned comparator `chh-lean-check` (docker/lean/ChhLeanCheck.lean) and records everything in a
check record. This module re-checks that recorded, hash-bound output from the stored bytes:

  statement (the claim's subject; approved by a human, `chh approve-statement`), JSON:
    {"format": "chh-lean-statement/1", "theorem": "<Lean name>", "source": "<Lean file proving it by sorry>",
     "comment": optional}
  check record (a registered artifact linked to the claim; see `check` for every field), JSON "chh-lean-check/1".

A record proves the claim iff: it was produced for exactly these statement bytes and that theorem; the proof and
the statement pass the hygiene scan (no sorry/axiom/native_decide/metaprogramming/... in the stored source); the
toolchain output matches the pins below; both builds succeeded without errors (and the proof without `sorry`);
compiling the proof wrote nothing but its own .olean/.ilean; and the pinned checker printed exactly one result line,
bound to this record's nonce, saying the replayed theorem's type equals the approved one (statement fidelity) and
depends only on allowlisted axioms. The certificate then states: the statement holds, given Lean's kernel and the
listed axioms.

POLICY (human-owned; each is a decision for the owner, recorded in docs/adapters_lean.md): LEAN_VERSION,
MATHLIB_INPUT_REV, MATHLIB_COMMIT (a placeholder as shipped: the adapter fails its validation until it is pinned),
ALLOWED_AXIOMS, ALLOWED_IMPORT_ROOTS, ALLOWED_OPTIONS, the forbidden-token list. Stdlib only; never creates a float.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

LEAN_VERSION = "4.19.0"  # DECISION: the pinned Lean release (`lean --version`); unverified against a real install
MATHLIB_INPUT_REV = "v4.19.0"  # DECISION: the Mathlib tag the image's lakefile requires (manifest `inputRev`)
MATHLIB_COMMIT = "OWNER-MUST-PIN-the-40-hex-mathlib-commit"  # DECISION: the manifest `rev` the tag resolved to
CHECKER_SHA256 = "6d57efa224295f4b00ad5cdea2be635e4dfddbad9bd26a4711b031055735a30d"  # docker/lean/ChhLeanCheck.lean
ALLOWED_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})  # DECISION: Lean's three standard axioms
ALLOWED_IMPORT_ROOTS = ("Mathlib", "Init", "Std", "Batteries")  # DECISION: never `Lean` (metaprogramming)
ALLOWED_OPTIONS = frozenset({"maxHeartbeats", "maxRecDepth", "synthInstance.maxHeartbeats"})  # DECISION

STATEMENT_FORMAT, RECORD_FORMAT = "chh-lean-statement/1", "chh-lean-check/1"
RESULT_TAG = "CHH-LEAN-RESULT"
EXPECTED_PROOF_OUTPUTS = ("Solution.ilean", "Solution.olean")
STEPS = ("build_proof", "build_statement", "check")
MAX_BYTES = 8_000_000  # K3 refuses to parse a larger document
MAX_SOURCE_BYTES = 200_000  # K8 sanity bounds (registered background invariants of this adapter)
MAX_OUTPUT_CHARS = 2_000_000

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(\.[A-Za-z_][A-Za-z0-9_']*)*")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SALT = re.compile(r"[0-9a-f]{32,64}")
_VERSION = re.compile(r"Lean \(version ([0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?)[,)]")
_ERROR = re.compile(r"(^|:\s*)error(:|\s)", re.MULTILINE)


def _token(word: str) -> re.Pattern[str]:
    """`word` as a whole Lean identifier component (`Foo.sorry` matches, `sorryish` does not)."""
    return re.compile(r"(?<![\w'])" + word + r"(?![\w'])")


# The hygiene scan runs on the STORED source text (comments and strings included: fail closed). Each entry blocks a
# way to get a false theorem through, or to run code at compile time (which could forge outputs or files).
_FORBIDDEN: tuple[tuple[str, re.Pattern[str], str], ...] = (
    *((w, _token(w), "an unproved goal (sorryAx)") for w in ("sorry", "admit", "sorryAx")),
    ("axiom", _token("axiom"), "a new axiom declaration"),
    *((w, _token(w), "native_decide trusts compiled code (Lean.ofReduceBool)")
      for w in ("native_decide", "ofReduceBool", "reduceBool", "trustCompiler")),
    ("native", re.compile(r"native\s*:=|\+native"), "native evaluation trusts compiled code"),
    ("implemented_by", _token("implemented_by"), "replaces a definition's compiled code"),
    ("extern", _token("extern"), "replaces a definition's compiled code"),
    ("export", re.compile(r"@\[\s*export"), "exports compiled code"),
    ("unsafe", re.compile(r"(?<![\w'])unsafe[\w']*"), "unsafe code is not checked by the kernel"),
    *((w, _token(w), "user syntax/macros can change what a command means")
      for w in ("macro", "macro_rules", "syntax", "elab", "elab_rules", "notation", "infix", "infixl", "infixr",
                "prefix", "postfix", "declare_syntax_cat", "binder_predicate")),
    *((w, _token(w), "runs code at compile time")
      for w in ("initialize", "builtin_initialize", "run_cmd", "run_elab", "run_meta", "run_tac", "by_elab",
                "addDecl")),
    ("prelude", _token("prelude"), "drops the core library (redefinable foundations)"),
    ("#command", re.compile(r"#[A-Za-z_]+(?![\[\w])"), "#-commands (#exit, #eval, #print, ...) are not allowed"),
    ("guillemets", re.compile("«"), "escaped identifiers can spell forbidden names"),
)
_SET_OPTION = re.compile(r"(?<![\w'])set_option\s+([^\s]+)")
_IMPORT = re.compile(r"^\s*import\s+([^\n]*)", re.MULTILINE)


class FormalError(ValueError):
    pass


@dataclass(frozen=True)
class Statement:
    theorem: str
    source: str


def _load(data: bytes, fmt: str, keys: set[str]) -> dict[str, Any]:
    if len(data) > MAX_BYTES:
        raise FormalError(f"document is larger than {MAX_BYTES} bytes")
    try:
        doc = json.loads(data)
    except ValueError as e:
        raise FormalError(f"not JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("format") != fmt:
        raise FormalError(f"not a {fmt} document")
    extra, missing = set(doc) - keys - {"format", "comment"}, keys - set(doc)
    if extra:
        raise FormalError(f"unexpected keys {sorted(extra)}")
    if missing:
        raise FormalError(f"missing keys {sorted(missing)}")
    return doc


def parse_statement(data: bytes) -> Statement:
    doc = _load(data, STATEMENT_FORMAT, {"theorem", "source"})
    if not isinstance(doc["theorem"], str) or _NAME.fullmatch(doc["theorem"]) is None:
        raise FormalError(f"theorem {doc['theorem']!r} is not a Lean declaration name (plain ASCII components)")
    if not isinstance(doc["source"], str):
        raise FormalError("the statement source must be a string")
    return Statement(doc["theorem"], doc["source"])


def _string(doc: dict[str, Any], key: str, pattern: re.Pattern[str] | None = None, hint: str = "a string") -> None:
    v = doc[key]
    if not isinstance(v, str) or (pattern is not None and pattern.fullmatch(v) is None):
        raise FormalError(f"{key!r} must be {hint}")


def parse_record(data: bytes) -> dict[str, Any]:
    doc = _load(data, RECORD_FORMAT, {"statement_sha256", "theorem", "proof_source", "checker_sha256", "salt",
                                      "nonce", "toolchain", "proof_outputs", "steps"})
    for key in ("statement_sha256", "checker_sha256", "nonce"):
        _string(doc, key, _HEX64, "64 lowercase hex")
    _string(doc, "salt", _SALT, "32-64 lowercase hex")
    _string(doc, "theorem")
    _string(doc, "proof_source")
    outputs = doc["proof_outputs"]
    if not isinstance(outputs, list) or not all(isinstance(o, str) for o in outputs):
        raise FormalError("'proof_outputs' must be a list of strings")
    tool = doc["toolchain"]
    if not isinstance(tool, dict) or set(tool) != {"lean_version", "manifest"} or \
            not all(isinstance(v, str) for v in tool.values()):
        raise FormalError("'toolchain' must be an object with string keys 'lean_version' and 'manifest'")
    steps = doc["steps"]
    if not isinstance(steps, dict) or set(steps) != set(STEPS):
        raise FormalError(f"'steps' must be an object with keys {list(STEPS)}")
    for name in STEPS:
        step = steps[name]
        if not isinstance(step, dict) or set(step) != {"argv", "exit_code", "timed_out", "stdout", "stderr"}:
            raise FormalError(f"step {name!r} must be an object with keys argv, exit_code, timed_out, stdout, stderr")
        argv = step["argv"]
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise FormalError(f"'argv' of step {name!r} must be a list of strings")
        if not isinstance(step["exit_code"], int) or isinstance(step["exit_code"], bool):
            raise FormalError(f"'exit_code' of step {name!r} must be an integer")
        if not isinstance(step["timed_out"], bool):
            raise FormalError(f"'timed_out' of step {name!r} must be a boolean")
        for key in ("stdout", "stderr"):
            if not isinstance(step[key], str):
                raise FormalError(f"{key!r} of step {name!r} must be a string")
    return doc


def hygiene(source: str, *, statement: bool) -> list[str]:
    """Why `source` is refused (empty = clean). The approved statement may use `sorry` (it is the challenge)."""
    reasons = []
    for key, pattern, why in _FORBIDDEN:
        m = pattern.search(source)
        if m is not None and not (statement and key == "sorry"):
            reasons.append(f"forbidden {m.group(0)!r}: {why}")
    for m in _SET_OPTION.finditer(source):
        if m.group(1) not in ALLOWED_OPTIONS:
            reasons.append(f"forbidden set_option {m.group(1)!r} (not allowlisted)")
    for m in _IMPORT.finditer(source):
        for mod in m.group(1).split():
            if mod.split(".")[0] not in ALLOWED_IMPORT_ROOTS:
                reasons.append(f"forbidden import {mod!r} (not allowlisted)")
    return reasons


def nonce_for(statement_sha256: str, proof_sha256: str, salt: str) -> str:
    """Binds the checker's output to one statement, one proof and one run (salt chosen after the proof was fixed)."""
    return hashlib.sha256(f"chh-lean-nonce/1|{statement_sha256}|{proof_sha256}|{salt}".encode()).hexdigest()


def lean_version(output: str) -> str | None:
    m = _VERSION.match(output.strip())
    return m.group(1) if m else None


def pinned() -> list[str]:
    """Why the toolchain pins are not usable (validation check: the adapter cannot be approved until pinned)."""
    reasons = []
    if lean_version(f"Lean (version {LEAN_VERSION})") != LEAN_VERSION:
        reasons.append(f"LEAN_VERSION {LEAN_VERSION!r} is not a Lean release number")
    if re.fullmatch(r"[0-9a-f]{40}", MATHLIB_COMMIT) is None:
        reasons.append(f"MATHLIB_COMMIT {MATHLIB_COMMIT!r} is not a 40-hex commit: the owner must pin Mathlib")
    return reasons


def _toolchain(tool: dict[str, str]) -> list[str]:
    reasons = []
    version = lean_version(tool["lean_version"])
    if version is None:
        reasons.append(f"Lean version output unreadable: {tool['lean_version'][:80]!r}")
    elif version != LEAN_VERSION:
        reasons.append(f"wrong toolchain: Lean {version}, the adapter pins {LEAN_VERSION}")
    try:
        packages = json.loads(tool["manifest"])["packages"]
        mathlib = [p for p in packages if p.get("name") == "mathlib"]
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        return [*reasons, f"lake manifest unreadable: {type(e).__name__}"]
    if len(mathlib) != 1:
        return [*reasons, f"the lake manifest does not list exactly one mathlib package ({len(mathlib)})"]
    if mathlib[0].get("rev") != MATHLIB_COMMIT:
        reasons.append(f"wrong Mathlib commit {mathlib[0].get('rev')!r}, the adapter pins {MATHLIB_COMMIT!r}")
    if mathlib[0].get("inputRev") != MATHLIB_INPUT_REV:
        reasons.append(f"wrong Mathlib input revision {mathlib[0].get('inputRev')!r}, the adapter pins "
                       f"{MATHLIB_INPUT_REV!r}")
    return reasons


def _build(name: str, what: str, step: dict[str, Any], *, sorry_ok: bool) -> list[str]:
    if step["timed_out"]:
        return [f"building {what} timed out"]
    reasons = [] if step["exit_code"] == 0 else [f"building {what} failed (exit {step['exit_code']})"]
    text = step["stdout"] + "\n" + step["stderr"]
    if _ERROR.search(text):
        reasons.append(f"building {what} reported an error ({name})")
    if not sorry_ok and "declaration uses 'sorry'" in text:
        reasons.append(f"building {what} reported a sorry")
    return reasons


def _result(rec: dict[str, Any], theorem: str) -> tuple[list[str], dict[str, Any] | None]:
    step = rec["steps"]["check"]
    if step["timed_out"]:
        return ["the checker timed out"], None
    if step["exit_code"] != 0:
        return [f"the checker failed (exit {step['exit_code']}): {step['stderr'][-200:]!r}"], None
    lines = step["stdout"].splitlines()
    if len(lines) != 1 or not lines[0].startswith(RESULT_TAG + " "):
        return [f"the checker output is not exactly one result line ({len(lines)} line(s))"], None
    parts = lines[0].split(" ", 2)
    if len(parts) != 3 or parts[1] != rec["nonce"]:
        return ["the checker result is not bound to this record's nonce"], None
    try:
        res = json.loads(parts[2])
        if not isinstance(res, dict) or set(res) != {"theorem", "kind", "statement_match", "axioms", "replayed",
                                                     "problems"}:
            raise ValueError("unexpected keys")
        if not (isinstance(res["axioms"], list) and all(isinstance(a, str) for a in res["axioms"])
                and isinstance(res["problems"], list) and all(isinstance(p, str) for p in res["problems"])
                and isinstance(res["replayed"], int) and not isinstance(res["replayed"], bool)
                and isinstance(res["statement_match"], bool)):
            raise ValueError("wrong field types")
    except ValueError as e:
        return [f"checker result unreadable: {e}"], None
    reasons = []
    if res["theorem"] != theorem:
        reasons.append(f"checker result is for theorem {res['theorem']!r}, the approved statement names {theorem!r}")
    if res["kind"] != "theorem":
        reasons.append(f"{theorem} is a {res['kind']!r}, not a theorem")
    if res["statement_match"] is not True:
        reasons.append("statement drift: the proved theorem's type differs from the approved statement (comparator)")
    reasons += [f"checker reported: {p}" for p in res["problems"]]
    if res["replayed"] < 1:
        reasons.append("the checker replayed no declaration of the proof through the kernel")
    for ax in res["axioms"]:
        if ax == "sorryAx":
            reasons.append("the theorem uses sorry (sorryAx)")
        elif ax in ("Lean.ofReduceBool", "Lean.trustCompiler"):
            reasons.append(f"the theorem uses native_decide ({ax})")
        elif ax not in ALLOWED_AXIOMS:
            reasons.append(f"the theorem depends on a non-allowlisted axiom {ax!r}")
    return reasons, res


def check(statement: bytes, record: bytes) -> list[str]:
    """K3: why the record does NOT prove the approved statement (empty = proven, given Lean's kernel and the axioms)."""
    try:
        st, rec = parse_statement(statement), parse_record(record)
    except FormalError as e:
        return [f"unreadable: {e}"]
    reasons = []
    digest = hashlib.sha256(statement).hexdigest()
    if rec["statement_sha256"] != digest:
        reasons.append(f"statement drift: the record was produced for statement {rec['statement_sha256'][:12]}, "
                       f"the approved statement is {digest[:12]}")
    if rec["theorem"] != st.theorem:
        reasons.append(f"statement drift: the record checks {rec['theorem']!r}, the statement names {st.theorem!r}")
    if rec["checker_sha256"] != CHECKER_SHA256:
        reasons.append(f"the comparator {rec['checker_sha256'][:12]} is not the adapter's pinned checker")
    reasons += [f"statement: {r}" for r in hygiene(st.source, statement=True)]
    reasons += [f"proof: {r}" for r in hygiene(rec["proof_source"], statement=False)]
    reasons += _toolchain(rec["toolchain"])
    proof_sha = hashlib.sha256(rec["proof_source"].encode()).hexdigest()
    if rec["nonce"] != nonce_for(digest, proof_sha, rec["salt"]):
        reasons.append("the record's nonce does not bind this statement and proof")
    extra = sorted(set(rec["proof_outputs"]) - set(EXPECTED_PROOF_OUTPUTS))
    if extra or sorted(rec["proof_outputs"]) != list(EXPECTED_PROOF_OUTPUTS):
        reasons.append(f"building the proof wrote unexpected files {extra}: compile-time code ran, or the proof did "
                       f"not build")
    reasons += _build("build_proof", "the proof", rec["steps"]["build_proof"], sorry_ok=False)
    reasons += _build("build_statement", "the approved statement", rec["steps"]["build_statement"], sorry_ok=True)
    reasons += _result(rec, st.theorem)[0]
    return reasons


def axioms_used(record: bytes) -> list[str]:
    """The axioms the checker reported (for the certificate's assumption list); [] when the record is unusable."""
    try:
        rec = parse_record(record)
    except FormalError:
        return []
    res = _result(rec, rec["theorem"])[1]
    return list(res["axioms"]) if res is not None else []


def bounds(statement: bytes, record: bytes) -> list[str]:
    """K8: the adapter's registered sanity bounds on source and output sizes."""
    try:
        st, rec = parse_statement(statement), parse_record(record)
    except FormalError as e:
        return [f"unreadable: {e}"]
    reasons = []
    for what, text in (("statement source", st.source), ("proof source", rec["proof_source"])):
        if len(text.encode()) > MAX_SOURCE_BYTES:
            reasons.append(f"{what} is larger than {MAX_SOURCE_BYTES} bytes")
    for name in STEPS:
        step = rec["steps"][name]
        if len(step["stdout"]) + len(step["stderr"]) > MAX_OUTPUT_CHARS:
            reasons.append(f"output of step {name!r} exceeds {MAX_OUTPUT_CHARS} characters")
    return reasons
