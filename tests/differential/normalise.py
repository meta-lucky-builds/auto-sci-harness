"""Normalisation and diff of two Kernel recordings (kernel_recorder.py JSONL), for the differential test.

Rules are applied identically to both sides. Each rule names the field it touches and WHY it is nondeterministic; the
`explain` command proves each rule is needed by applying the rules one by one to two RAW recordings of the SAME
commit and counting the raw differences each removes (a rule that removes nothing is reported as UNPROVEN).

Identifier rules RENAME instead of blanking: inside one scope (one test, or one history) the k-th distinct evidence
id becomes `<ev#k>`, so "the same id is used twice" survives normalisation while the random value does not.

The adapter content hash is masked by a separate, named rule (`adapter_hash`): it is NOT nondeterministic, it is
EXPECTED to change when the refactor moves code behind the adapter interface. `--keep-adapter-hash` turns it off.

`--flaky FILE` masks individual (scope, record, path) cells listed by `explain --flaky-out`: cells that still differed
between two runs of the same commit after the rules (e.g. a test whose program is random on purpose). Each is printed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

Rec = dict[str, Any]
MASK = "<masked>"

EV_ID = re.compile(r"\b(ev|run|sn|st|cl|hy)_[0-9a-f]{16}\b")
HEX = re.compile(r"\b[0-9a-f]{8,64}\b")
# pytest's per-session base temp dir: ...\pytest-of-<user>\pytest-<N>\  (N increments per session)
PYTEST_TMP = re.compile(r"pytest-of-[^\\/]+[\\/]+pytest-\d+")
KDIFF_TMP = re.compile(r"kdiff-[a-z0-9_]{6,12}")


# ------------------------------------------------------------------ loading
def load(path: Path) -> tuple[Rec, list[Rec]]:
    header: Rec = {}
    recs: list[Rec] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("t") == "header":
                header = r
            else:
                recs.append(r)
    return header, recs


def by_scope(recs: list[Rec]) -> dict[str, list[Rec]]:
    out: dict[str, list[Rec]] = {}
    for r in recs:
        out.setdefault(r["scope"], []).append(r)
    return out


def _walk_strings(obj: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_walk_strings(v, fn) for v in obj]
    if isinstance(obj, dict):
        return {k: _walk_strings(v, fn) for k, v in obj.items()}
    return obj


def _strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)


# ------------------------------------------------------------------ rules (each: scope records -> scope records)
def r_evidence_ids(scope: list[Rec]) -> list[Rec]:
    """Evidence/run ids are `<prefix>_<uuid4 hex[:16]>` (records.new_id): random per process."""
    names: dict[str, str] = {}

    def sub(s: str) -> str:
        def one(m: re.Match[str]) -> str:
            if m.group(0) not in names:
                names[m.group(0)] = f"<{m.group(1)}#{len(names) + 1}>"
            return names[m.group(0)]
        return EV_ID.sub(one, s)
    return [_walk_strings(r, sub) for r in scope]


def _set(r: Rec, path: tuple[str, ...], value: Any) -> None:
    node: Any = r
    for k in path[:-1]:
        if not isinstance(node, dict) or k not in node:
            return
        node = node[k]
    if isinstance(node, dict) and path[-1] in node:
        node[path[-1]] = value


def r_issued_at(scope: list[Rec]) -> list[Rec]:
    """Certificate.issued_at = Kernel.clock() = time.time() (wall clock)."""
    for r in scope:
        _set(r, ("result", "issued_at"), MASK)
    return scope


def _claims(r: Rec) -> Iterator[Rec]:
    for path in (("result", "claim"), ("kwargs", "claim")):
        node: Any = r
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict):
            yield node
    body = (r.get("result") or {}).get("body") if isinstance(r.get("result"), dict) else None
    if isinstance(body, dict) and isinstance(body.get("claim"), dict):
        yield body["claim"]


def r_resolve_by(scope: list[Rec]) -> list[Rec]:
    """Claim.resolve_by = time.time() + 86400 in tests/kernel_world.py (and most test helpers): wall clock."""
    for r in scope:
        for c in _claims(r):
            if "resolve_by" in c:
                c["resolve_by"] = MASK
    return scope


def r_derived_hashes(scope: list[Rec]) -> list[Rec]:
    """Claim hashes cover resolve_by, certificate hashes cover issued_at (both wall clock), so both differ per run.
    Every hex string (>= 8 chars) that is a prefix of a claim or certificate hash recorded in this scope is renamed
    `<h#k:len>` by first appearance; all other hashes (data, artifact, adapter content) are left alone."""
    known: dict[str, list[str]] = {}  # 8-char prefix -> full hashes (recorded order)
    for r in scope:
        res: Rec = r["result"] if isinstance(r.get("result"), dict) else {}
        body: Rec = res["body"] if isinstance(res.get("body"), dict) else {}
        kwargs: Rec = r["kwargs"] if isinstance(r.get("kwargs"), dict) else {}
        for v in (res.get("claim_hash"), body.get("claim_hash"), kwargs.get("cert_hash")):
            if isinstance(v, str) and len(v) == 64 and v not in known.get(v[:8], []):
                known.setdefault(v[:8], []).append(v)
    if not known:
        return scope
    names: dict[str, str] = {}

    def sub(s: str) -> str:
        def one(m: re.Match[str]) -> str:
            t = m.group(0)
            for full in known.get(t[:8], []):
                if full.startswith(t):
                    if full not in names:
                        names[full] = f"h#{len(names) + 1}"
                    return f"<{names[full]}:{len(t)}>"
            return t
        return HEX.sub(one, s)
    return [_walk_strings(r, sub) for r in scope]


def r_tmp_paths(scope: list[Rec]) -> list[Rec]:
    """pytest's base temp dir is numbered per session (pytest-<N>); the generator's world dir is mkdtemp-random."""
    def sub(s: str) -> str:
        return KDIFF_TMP.sub("kdiff-<tmp>", PYTEST_TMP.sub("pytest-of-<user>/pytest-<N>", s))
    return [_walk_strings(r, sub) for r in scope]


def r_adapter_hash(scope: list[Rec]) -> list[Rec]:
    """EXPECTED change, not nondeterminism: the refactor moves code into the adapters, so their content hash changes."""
    for r in scope:
        res = r.get("result")
        if isinstance(res, dict) and isinstance(res.get("adapters"), list):
            for a in res["adapters"]:
                if isinstance(a, dict) and "hash" in a:
                    a["hash"] = MASK
    return scope


RULES: list[tuple[str, Callable[[list[Rec]], list[Rec]]]] = [
    ("evidence_ids", r_evidence_ids),
    ("issued_at", r_issued_at),
    ("resolve_by", r_resolve_by),
    ("derived_hashes", r_derived_hashes),
    ("tmp_paths", r_tmp_paths),
]


Flaky = tuple[set[tuple[str, int, str]], set[str]]  # (cells, whole scopes)


def rules_for(*headers: Rec, mask_adapter_hash: bool = True) -> list[str]:
    """All rules, except `evidence_ids` when every side recorded with deterministic ids (kernel_recorder): then the ids
    are a deterministic function of (scope, n), and renaming them would hide a change in how many are made."""
    names = [n for n, _ in RULES]
    if headers and all(h.get("deterministic_ids") is True for h in headers):
        names.remove("evidence_ids")
    return names + (["adapter_hash"] if mask_adapter_hash else [])


def normalise(recs: list[Rec], rules: list[str], flaky: Flaky | None = None) -> dict[str, list[Rec]]:
    table = dict([*RULES, ("adapter_hash", r_adapter_hash)])
    out: dict[str, list[Rec]] = {}
    for scope, rs in by_scope(json.loads(json.dumps(recs))).items():
        for name in rules:
            rs = table[name](rs)
        out[scope] = rs
    if flaky:
        cells, scopes = flaky
        for scope, i, path in cells:
            if scope in out and i < len(out[scope]):
                _mask_path(out[scope][i], path)
        for scope in scopes:  # structurally nondeterministic: only the test outcome is compared
            if scope in out:
                out[scope] = [r for r in out[scope] if r.get("t") != "call"]
    return out


def _mask_path(r: Any, path: str) -> None:
    keys = [k for k in path.split("/") if k]
    node = r
    for k in keys[:-1]:
        if isinstance(node, list) and k.isdigit() and int(k) < len(node):
            node = node[int(k)]
        elif isinstance(node, dict) and k in node:
            node = node[k]
        else:
            return
    last = keys[-1] if keys else None
    if isinstance(node, list) and last is not None and last.isdigit() and int(last) < len(node):
        node[int(last)] = MASK
    elif isinstance(node, dict) and last in node:
        node[last] = MASK


# ------------------------------------------------------------------ diff
def _short(v: Any, n: int = 300) -> str:
    s = json.dumps(v, sort_keys=True)
    return s if len(s) <= n else s[:n] + f"...(+{len(s) - n} chars)"


def diff_values(a: Any, b: Any, path: str = "") -> Iterator[tuple[str, Any, Any]]:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                yield f"{path}/{k}", "<absent>", b[k]
            elif k not in b:
                yield f"{path}/{k}", a[k], "<absent>"
            else:
                yield from diff_values(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            yield from diff_values(x, y, f"{path}/{i}")
    elif a != b or type(a) is not type(b):
        yield path, a, b


def diff_scopes(old: dict[str, list[Rec]], new: dict[str, list[Rec]],
                intersect: bool = False) -> Iterator[tuple[str, int, str, Any, Any]]:
    scopes = sorted(set(old) & set(new)) if intersect else sorted(set(old) | set(new))
    for s in scopes:
        a, b = old.get(s), new.get(s)
        if a is None or b is None:
            yield s, -1, "<scope>", "present" if a else "<absent>", "present" if b else "<absent>"
            continue
        for i in range(max(len(a), len(b))):
            if i >= len(a) or i >= len(b):
                x, y = (a[i] if i < len(a) else "<absent>"), (b[i] if i < len(b) else "<absent>")
                yield s, i, "<record>", x, y
                continue
            for path, x, y in diff_values(a[i], b[i]):
                yield s, i, path, x, y


def _label(rs: dict[str, list[Rec]], s: str, i: int) -> str:
    try:
        r = rs[s][i]
    except (KeyError, IndexError):
        return ""
    return f"{r.get('t')}:{r.get('api', r.get('when', ''))}"


def load_flaky(paths: list[Path]) -> Flaky:
    cells: set[tuple[str, int, str]] = set()
    scopes: set[str] = set()
    for p in paths:
        doc = json.loads(p.read_text(encoding="utf-8"))
        for c in doc["cells"]:
            cells.add((c["scope"], c["i"], c["path"]))
        scopes.update(doc.get("scopes", []))
    return cells, scopes


def diff_files(old_p: Path, new_p: Path, *, raw: bool = False, intersect: bool = False, limit: int = 0,
               mask_adapter_hash: bool = True, flaky_files: list[Path] | None = None) -> int:
    ho, ro = load(old_p)
    hn, rn = load(new_p)
    rules = [] if raw else rules_for(ho, hn, mask_adapter_hash=mask_adapter_hash)
    flaky = None if raw else load_flaky(flaky_files or [])
    print(f"OLD {old_p}: {len(ro)} records, header {_short({k: v for k, v in ho.items() if k != 't'}, 400)}")
    print(f"NEW {new_p}: {len(rn)} records, header {_short({k: v for k, v in hn.items() if k != 't'}, 400)}")
    cells, fscopes = flaky or (set(), set())
    print(f"rules applied to both sides: {rules or 'NONE (raw)'}; flaky cells masked: {len(cells)}; flaky scopes "
          f"(Kernel calls not compared, test outcome still compared): {len(fscopes)}")
    for s, i, path in sorted(cells):
        print(f"  flaky cell (masked): {s} #{i} {path}")
    for s in sorted(fscopes):
        print(f"  flaky scope (calls not compared): {s}")
    a, b = normalise(ro, rules, flaky), normalise(rn, rules, flaky)
    n = 0
    scopes: set[str] = set()
    for s, i, path, x, y in diff_scopes(a, b, intersect):
        n += 1
        scopes.add(s)
        if not limit or n <= limit:
            what = _label(a, s, i) or _label(b, s, i)
            print(f"DIFF {s} #{i} {what} {path}\n   OLD: {_short(x)}\n   NEW: {_short(y)}")
    calls = sum(1 for rs in a.values() for r in rs if r.get("t") == "call")
    print(f"compared {len(set(a) & set(b) if intersect else set(a) | set(b))} scopes, {calls} Kernel calls (OLD); "
          f"{n} difference(s) in {len(scopes)} scope(s)")
    return 1 if n else 0


def explain(run1: Path, run2: Path, flaky_out: Path | None = None, intersect: bool = False) -> int:
    """Prove each rule: apply rules cumulatively to two raw runs of the same commit; report what each removes."""
    h1, r1 = load(run1)
    h2, r2 = load(run2)
    names = rules_for(h1, h2, mask_adapter_hash=False)
    print(f"deterministic ids: run1 {h1.get('deterministic_ids')}, run2 {h2.get('deterministic_ids')}; rules: {names}")
    applied: list[str] = []
    prev = list(diff_scopes(normalise(r1, []), normalise(r2, []), intersect))
    print(f"raw differences between the two runs: {len(prev)} (in {len({d[0] for d in prev})} scopes)")
    by_path: dict[str, int] = {}
    for d in prev:
        key = re.sub(r"/\d+", "/*", d[2])
        by_path[key] = by_path.get(key, 0) + 1
    for k, v in sorted(by_path.items(), key=lambda kv: -kv[1])[:40]:
        print(f"   raw diff path {k}: {v}")
    unproven = []
    for name in names:
        applied.append(name)
        cur = list(diff_scopes(normalise(r1, applied), normalise(r2, applied), intersect))
        removed = len(prev) - len(cur)
        cur_keys = {(d[0], d[1], d[2]) for d in cur}
        example = next((d for d in prev if (d[0], d[1], d[2]) not in cur_keys), None)
        print(f"rule {name}: removes {removed} difference(s); {len(cur)} left")
        if example:
            print(f"   e.g. {example[0]} #{example[1]} {example[2]}: {_short(example[3], 160)} vs "
                  f"{_short(example[4], 160)}")
        if removed <= 0:
            unproven.append(name)
        prev = cur
    print(f"UNPROVEN rules (removed nothing in this pair of runs): {unproven or 'none'}")
    print(f"residual differences after all rules: {len(prev)}")
    for s, i, path, x, y in prev:
        print(f"RESIDUAL {s} #{i} {path}\n   run1: {_short(x)}\n   run2: {_short(y)}")
    if flaky_out is not None:
        # a scope whose call SEQUENCE differs (a call missing, or another API at the same index) cannot be masked
        # cell by cell: the whole scope's calls are excluded (its test outcome is still compared)
        structural = sorted({s for s, i, p, x, y in prev if i < 0 or p.startswith("<") or p == "/api"
                             or "<absent>" in (x, y)})
        cells = [{"scope": s, "i": i, "path": p} for s, i, p, _, _ in prev if s not in structural]
        flaky_out.write_text(json.dumps({"from": [str(run1), str(run2)], "cells": cells, "scopes": structural},
                                        indent=1), encoding="utf-8")
        print(f"wrote {len(cells)} flaky cell(s) and {len(structural)} flaky scope(s) to {flaky_out}")
    return 0
