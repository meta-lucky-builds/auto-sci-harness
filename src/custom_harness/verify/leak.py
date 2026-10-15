"""Canary + leak audit (GOAL §4.3 K9 precursor, invariant 2; CR: grader/label visibility is common).

What counts as "agent-visible" for a research run:
  - every string recorded in the run's trace: rendered prompts, model responses, module outputs,
    module LLM requests/responses, error messages (this is also what the harness proposer will read);
  - every file in the sandbox workspace (agent code reads and writes it).
What must never appear there:
  - a canary token (a per-run token held only in `_private` state, and the task's private canary file);
  - a byte-identical copy of any file under the task's private/ directory (hash match);
  - in TRACE text only: a window of the hidden test labels in order (a heuristic for labels pasted into
    a prompt or hard-coded into code: the first 40 labels joined by a common separator) and, when the caller
    passes `label_runs`, ANY run of consecutive hidden labels (at any offset, any common separator, also in
    reverse order) that carries at least MIN_RUN_BITS of information under the labels' own frequencies. Not
    applied to workspace files, where a good predictions file legitimately matches.
  - also in common encodings: every text is also scanned reversed, URL-decoded, and with each hex run and each
    base64 run (standard or URL-safe alphabet) decoded; a canary also matches by any MIN_FRAGMENT-character
    fragment of its random part (case-insensitive), when that part is at least that long.
Also flagged: an attempt by agent code to plant a link the host would follow (escape marker).

False positives are kept out by minimum lengths: a label run must be >= RUN_MIN labels AND >= MIN_RUN_BITS bits
(so short or low-information sequences such as "0,0,0,..." never match), a canary fragment >= MIN_FRAGMENT
characters of a random token, an encoded run >= 24 (base64) or 32 (hex) characters.

Limits (stated, not hidden): it does not detect labels that were otherwise transformed (re-mapped, nested or
compressed encodings, partial use below the thresholds), labels of a single class (zero information), nor
information that flowed through the metric itself (e.g. many probing submissions); those need the
evaluator-side defences planned for L3.

Encodings and fragments (since 2026-10-07; the engine's trace scrubber uses the same patterns): a canary or label
window also matches when it appears reversed, as hex (lower/upper), base64 (standard or URL-safe, at any byte
alignment inside a longer encoded text: only the characters that depend on the secret alone are matched), URL /
percent encoded, or upper/lower-cased; a high-entropy secret (a canary's random part, a blob hash) also matches by
any FRAGMENT_LEN-character substring of it (or of its reversal). Thresholds, so short values cannot cause false
positives (also the values to align with Kernel K9):
  - MIN_VERBATIM_LEN = 8: shorter private values are not matched at all by the engine scrubber;
  - MIN_ENCODED_LEN = 16: encodings only for values at least this long;
  - FRAGMENT_LEN = 16 characters of a random core at least MIN_FRAGMENT_SOURCE_LEN = 24 long (>= 64 bits of entropy
    for hex), never fragments of label windows (short label runs occur in honest predictions);
  - private file contents are matched as substrings of workspace files only when >= MIN_CONTENT_LEN = 64 bytes.

Limits (stated, not hidden): other transformations (compression, encryption, XOR, a different label separator or
order, labels mapped to other symbols, a few labels at a time) are not detected, nor information that flowed through
the metric itself (e.g. many probing submissions); those need the evaluator-side defences planned for L3.
"""

from __future__ import annotations

import ast
import base64
import contextlib
import functools
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import secrets
import urllib.parse
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterable, Sequence
from itertools import accumulate
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ..sandbox.base import is_link_like

CANARY_PREFIX = "CHH-CANARY-"
ESCAPE_MARKER = "[harness] refused to read output"
_SEPARATORS = ("\n", "\r\n", ",", ", ", " ", ";")
_WINDOW = 40
_MAX_SCAN_BYTES = 50 * 1024 * 1024
RUN_MIN = 40  # a leaked label run is at least this many consecutive labels ...
MIN_RUN_BITS = 64.0  # ... and at least this much information (-sum log2 p(label)); chance match <= 2^-64
MAX_RUN = 4000  # runs needing more labels than this to reach MIN_RUN_BITS are not indexed (too little information)
MIN_FRAGMENT = 16  # characters of a canary's random part that identify it
_B64_RUN = re.compile(r"[A-Za-z0-9+/_-]{24,}={0,2}")
_HEX_RUN = re.compile(r"[0-9a-fA-F]{32,}")
_TOKEN_SPLIT = re.compile(r"[\s,;|]+")
# a label run written as a quoted list / tuple / set literal (`["c", "a", ...]`, `('c', 'a')`): quote and bracket
# characters are also separators in a second tokenisation (auditor-to-kstats-01); the first one is kept, so a label
# vocabulary that itself contains such characters is matched as before
_LITERAL_SPLIT = re.compile(r"""[\s,;|"'`\[\](){}]+""")
MIN_VERBATIM_LEN = 8
MIN_ENCODED_LEN = 16
FRAGMENT_LEN = 16
MIN_FRAGMENT_SOURCE_LEN = 24
MIN_CONTENT_LEN = 64


def _b64_cores(data: bytes) -> list[str]:
    """For each byte alignment (0, 1, 2) of `data` inside a longer byte string, the base64 characters that depend
    only on `data` (so they appear verbatim in the base64 of ANY text containing `data` at that alignment)."""
    out = []
    n = len(data)
    for k in range(3):
        enc = base64.b64encode(b"\0" * k + data).decode()
        lo, hi = -(-8 * k // 6), (8 * (k + n)) // 6  # chars j with 6j >= 8k and 6j + 6 <= 8(k + n)
        out.append(enc[lo:hi])
    return out


def secret_patterns(value: str, *, min_verbatim: int = MIN_VERBATIM_LEN, fragments: bool = True) -> list[str]:
    """Every literal string whose presence in agent-visible text means `value` leaked (see the module docstring),
    longest first. Values shorter than `min_verbatim` give no pattern; shorter than MIN_ENCODED_LEN only themselves."""
    if len(value) < min_verbatim:
        return []
    pats = {value}
    if len(value) >= MIN_ENCODED_LEN:
        raw = value.encode("utf-8")
        pats |= {value[::-1], value.upper(), value.lower(), raw.hex(), raw.hex().upper(),
                 "".join(f"%{b:02X}" for b in raw), "".join(f"%{b:02x}" for b in raw),
                 urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value, safe="")}
        for core in _b64_cores(raw):
            pats |= {core, core.replace("+", "-").replace("/", "_")}
        core_s = value.removeprefix(CANARY_PREFIX)  # the public prefix carries no secret
        if fragments and len(core_s) >= MIN_FRAGMENT_SOURCE_LEN:
            for s in (core_s, core_s[::-1]):
                pats |= {s[i:i + FRAGMENT_LEN] for i in range(len(s) - FRAGMENT_LEN + 1)}
    return sorted((p for p in pats if len(p) >= min(len(value), FRAGMENT_LEN)), key=lambda p: (-len(p), p))


@functools.lru_cache(maxsize=4096)
def _patterns(value: str, fragments: bool) -> tuple[str, ...]:
    return tuple(secret_patterns(value, min_verbatim=1, fragments=fragments))


def _found(text: str, value: str, *, fragments: bool) -> bool:
    return value in text or any(p in text for p in _patterns(value, fragments))


def new_canary() -> str:
    return CANARY_PREFIX + secrets.token_hex(16)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def private_fingerprints(private_dir: Path, labels: list[str] | None = None) -> dict[str, Any]:
    """Hashes of every private file, canary tokens found in them, and label windows."""
    hashes: dict[str, str] = {}
    canaries: list[str] = []
    for root, _, files in os.walk(private_dir):
        for f in files:
            p = Path(root) / f
            data = p.read_bytes()
            hashes[_sha(data)] = p.relative_to(private_dir).as_posix()
            text = data.decode("utf-8", errors="ignore")
            canaries += [tok for tok in text.split() if tok.startswith(CANARY_PREFIX)]
    return {"hashes": hashes, "canaries": canaries, "label_windows": label_windows(labels or [])}


def label_windows(labels: list[str]) -> list[str]:
    """The first 40 labels joined by each common separator (empty when there are fewer than 40)."""
    if len(labels) < _WINDOW:
        return []
    head = [str(x) for x in labels[:_WINDOW]]
    return [sep.join(head) for sep in _SEPARATORS]


class LabelRuns:
    """Index of every run of consecutive hidden labels that is long and informative enough to count as a leak:
    for each start i, the shortest length L >= RUN_MIN (L <= MAX_RUN) whose labels carry >= MIN_RUN_BITS bits."""

    def __init__(self, *label_lists: Sequence[str]):
        self.lists = [tuple(str(x) for x in labels) for labels in label_lists]
        self.index: dict[tuple[str, ...], list[tuple[int, int, int]]] = {}  # first RUN_MIN labels -> (list, i, L)
        for k, labels in enumerate(self.lists):
            n = len(labels)
            freq = Counter(labels)
            prefix = [0.0, *accumulate(-math.log2(freq[x] / n) for x in labels)]
            end = 0
            for i in range(n - RUN_MIN + 1):
                end = max(end, i + RUN_MIN)
                while end < n and end - i < MAX_RUN and prefix[end] - prefix[i] < MIN_RUN_BITS:
                    end += 1
                if prefix[end] - prefix[i] >= MIN_RUN_BITS:
                    self.index.setdefault(labels[i:i + RUN_MIN], []).append((k, i, end - i))
        self.vocab = frozenset(x for head in self.index for x in head)

    def found(self, tokens: Sequence[str]) -> bool:
        streak = 0
        for j, tok in enumerate(tokens):
            streak = streak + 1 if tok in self.vocab else 0
            if streak < RUN_MIN:
                continue
            for k, i, length in self.index.get(tuple(tokens[j - RUN_MIN + 1:j + 1]), ()):
                rest = self.lists[k][i + RUN_MIN:i + length]
                if tuple(tokens[j + 1:j + 1 + len(rest)]) == rest:
                    return True
        return False


def label_runs(*label_lists: Sequence[str]) -> LabelRuns:
    return LabelRuns(*label_lists)


def _views(text: str) -> list[tuple[str, str]]:
    """(encoding, decoded text) views in which a private value could hide; the plain text first."""
    views = [("", text), ("reversed", text[::-1])]
    if "%" in text:
        views.append(("url-encoded", unquote(text)))
    for m in _HEX_RUN.finditer(text):
        for off in (0, 1):
            run = m.group()[off:]
            views.append(("hex-encoded", bytes.fromhex(run[:len(run) - len(run) % 2]).decode("utf-8", "ignore")))
    for m in _B64_RUN.finditer(text):
        run = m.group().rstrip("=").replace("-", "+").replace("_", "/")
        for off in range(4):
            chunk = run[off:]
            chunk = chunk[:-1] if len(chunk) % 4 == 1 else chunk + "=" * (-len(chunk) % 4)  # keep the tail
            views.append(("base64-encoded", base64.b64decode(chunk).decode("utf-8", "ignore")))
    return views


def _canary_in(view: str, canaries: list[str]) -> bool:
    low = view.lower()
    for c in canaries:
        if c in view:
            return True
        part = c[len(CANARY_PREFIX):] if c.startswith(CANARY_PREFIX) else c
        if len(part) >= MIN_FRAGMENT and any(part[i:i + MIN_FRAGMENT].lower() in low
                                             for i in range(len(part) - MIN_FRAGMENT + 1)):
            return True
    return False


def _runs_in(view: str, runs: list[LabelRuns]) -> bool:
    for split in (_TOKEN_SPLIT, _LITERAL_SPLIT):
        tokens = [t for t in split.split(view) if t]
        if any(r.found(tokens) or r.found(tokens[::-1]) for r in runs):
            return True
    return False


def scan_text(text: str, where: str, canaries: list[str], windows: list[str],
              runs: list[LabelRuns] | None = None) -> list[str]:
    """Public form of the per-text check (canaries, label windows and runs, escape marker; plain and encoded)."""
    return _check_text(text, where, [c for c in canaries if c], windows, runs or [])


def reader_texts(data: bytes, *, source: bool = False, depth: int = 0) -> list[str]:
    """What a reader of the bytes sees (UTF-8, NULs dropped, parsed JSON, and for Python `source` the decoded source,
    its literals in source order joined by newlines and directly, and every member of a zip, which `python` runs as a
    zipapp whatever its name: RT-S4-5): docs/kernel_s4.md, item 1 (K9's view of a blob)."""
    texts = [data.decode("utf-8", errors="ignore"), data.replace(b"\0", b"").decode("utf-8", errors="ignore")]
    with contextlib.suppress(ValueError, RecursionError):  # not JSON in any encoding
        texts.append(json.dumps(json.loads(data, object_pairs_hook=lambda kv: [list(p) for p in kv]),
                                sort_keys=True, separators=(",", ":"), allow_nan=False))
    with contextlib.suppress(ValueError, SyntaxError, RecursionError, LookupError, MemoryError):  # not Python
        nodes = ast.walk(ast.parse(data)) if source else iter(())
        consts = sorted((n.lineno, n.col_offset, n.value if isinstance(n.value, str) else n.value.decode(
            "utf-8", "ignore")) for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str | bytes))
        lits = [v.replace("\0", "") for *_, v in consts]
        texts += [importlib.util.decode_source(data), "\n".join(lits), "".join(lits)] if source else []
    with contextlib.suppress(zipfile.BadZipFile, ValueError, OSError, EOFError, NotImplementedError, zlib.error):
        z = zipfile.ZipFile(io.BytesIO(data)) if source and depth < 2 else None  # all members capped at 64 MiB
        members = z.infolist() if z and sum(i.file_size for i in z.infolist()) <= 2**26 else []
        texts += [t for i in members for t in reader_texts(z.read(i), source=True, depth=depth + 1)] if z else []
    return list(dict.fromkeys(texts))


def _strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _check_text(text: str, where: str, canaries: list[str], windows: list[str],
                runs: list[LabelRuns] | None = None) -> list[str]:
    """At most one canary finding and one label finding per text (the first view that matches names it)."""
    problems = []
    canary = labels = False
    for enc, view in _views(text) if canaries or windows or runs else [("", text)]:
        how = f" ({enc})" if enc else ""
        if not canary and canaries and _canary_in(view, canaries):
            canary = True
            problems.append(f"canary token{how} found in {where}")
        if not labels and any(w in view for w in windows):
            labels = True
            problems.append(f"hidden test labels (verbatim window{', ' + enc if enc else ''}) found in {where}")
        if not labels and runs and _runs_in(view, runs):
            labels = True
            problems.append(f"hidden test labels (a run of >= {RUN_MIN} consecutive labels{', ' + enc if enc else ''}"
                            f") found in {where}")
    if not canary and any(_found(text, c, fragments=True) for c in canaries):
        problems.append(f"canary token (encoded or partial) found in {where}")
    if not labels and any(_found(text, w, fragments=False) for w in windows):
        problems.append(f"hidden test labels (encoded window) found in {where}")
    if ESCAPE_MARKER in text:
        problems.append(f"agent code planted a link to escape the workspace ({where})")
    return problems


def leak_audit(trace: list, workspace: Path | None, fp: dict[str, Any], canaries: list[str],
               extra_texts: dict[str, str] | None = None) -> dict[str, Any]:
    """Scan everything agent-visible. Reports locations only, never the secret values themselves."""
    canaries = [c for c in canaries if c]
    windows = fp.get("label_windows", [])
    runs: list[LabelRuns] = list(fp.get("label_runs") or [])
    problems: list[str] = []
    for step in trace:
        where = f"trace step {step.index} ({step.node_id})"
        if isinstance(step.output, dict) and step.output.get("_redacted_private_values"):
            problems.append(f"a private value reached agent-visible output at {where} (redacted from the trace)")
        for text in _strings(step.output):
            problems += _check_text(text, where, canaries, windows, runs)
        if step.error:
            problems += _check_text(step.error, where, canaries, windows, runs)
    for name, text in (extra_texts or {}).items():
        problems += _check_text(text or "", name, canaries, windows, runs)
    if workspace is not None and Path(workspace).exists():
        for root, dirnames, files in os.walk(workspace, followlinks=False):
            dirnames[:] = [d for d in dirnames if not is_link_like(Path(root) / d)]
            for f in files:
                p = Path(root) / f
                if is_link_like(p):
                    continue
                rel = p.relative_to(workspace).as_posix()
                try:
                    if p.stat().st_size > _MAX_SCAN_BYTES:
                        problems.append(f"workspace file {rel} too large to scan (fail closed)")
                        continue
                    data = p.read_bytes()
                except OSError:
                    problems.append(f"workspace file {rel} unreadable by the audit (fail closed)")
                    continue
                if _sha(data) in fp.get("hashes", {}):
                    problems.append(f"workspace file {rel} is a copy of private file "
                                    f"{fp['hashes'][_sha(data)]}")
                elif any(len(c) >= MIN_CONTENT_LEN and c in data for c in fp.get("contents", [])):
                    problems.append(f"workspace file {rel} contains a copy of a private file")
                # no label-window check here: a correct predictions file legitimately contains the labels
                # in order (at 95% accuracy, the first 40 are all right ~13-16% of the time)
                problems += _check_text(data.decode("utf-8", errors="ignore"), f"workspace file {rel}",
                                        canaries, [])
    # de-duplicate, keep order
    problems = list(dict.fromkeys(problems))
    return {"passed": not problems, "problems": problems}
