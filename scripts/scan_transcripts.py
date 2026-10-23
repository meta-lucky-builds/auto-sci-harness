"""List every agent transcript and scratchpad file that holds suite held-out content (s4-integrity item 8).

    python scripts/scan_transcripts.py SUITE_DIR --out REPORT.json [--root DIR ...] [--features]

w2-suite's second red team found 13 held-out FEATURE rows inside 3 agent transcripts (`...\\tasks\\<agent>.output`
under %TEMP%\\claude) and could not say whether a label sat next to them. This is the scanner that settles it, to be
RUN LATER (a long run: tens of thousands of files; it is not run by the agent that wrote it, and nobody needs to open a
transcript by hand). TRUSTED audit code: it reads the suite's held-out labels (truth_layout.truth_file) only to build
needles; it never prints or writes a label, a feature row or file contents, only paths and counts.

Roots (default): %TEMP%\\claude (every session's transcripts `tasks\\*.output` and scratchpads) and
~/.claude/projects (session transcripts, `*.jsonl`), when they exist; `--root` replaces them.

Per file it reports, per task:
- `labelled`: held-out rows found WITH their label, format-agnostically (scripts/suite_checks.TokenIndex: numbers
  normalised to 6 significant digits, any delimiter, JSON-escaped newlines; label right after the features, or right
  before them, reported apart);
- `canary`: the task's canary string;
- with `--features`: `features_only`: held-out FEATURE rows (public in test_X.csv, so informational: such a file is
  only a problem if it also has the labels; listing it lets the owner decide whether to archive it).
The same limits as suite_checks' scan: copies with fewer significant digits, binary files (NUL in the first 8 KiB),
and split or transformed copies are not found.

The report must be written outside every agent-readable root (it names files that hold labelled rows): refused
otherwise. Follow-up for any listed file: archive it (scripts/migrate_truth.py style: move + hash manifest, never
delete) after the owner's go-ahead.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from custom_harness.commitment import agent_readable_root_of
from custom_harness.truth_layout import truth_file

MAX_READ = 256 * 1024 * 1024  # transcripts can be large; bigger files are counted, not read


def _suite_checks() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_suite_checks", Path(__file__).with_name("suite_checks.py"))
    if spec is None or spec.loader is None:
        raise ImportError("scripts/suite_checks.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


def default_roots() -> list[Path]:
    return [r for r in (Path(tempfile.gettempdir()) / "claude", Path.home() / ".claude" / "projects") if r.is_dir()]


def _tasks(suite: Path) -> list[str]:
    doc = yaml.safe_load((suite / "suite.yaml").read_text(encoding="utf-8"))
    return [str(t["id"]) for t in doc["tasks"]]


def build_index(suite: Path, sc: ModuleType, features: bool) -> tuple[Any, dict[bytes, list[str]]]:
    index = sc.TokenIndex()
    canaries: dict[bytes, list[str]] = {}
    for tid in _tasks(suite):
        d = suite / tid
        for form, tuples in sc.heldout_tuples(d).items():
            for tup in tuples:
                index.add(tup, tid, form)
                if features and form == "label_last" and len(tup) > 2:  # the feature row alone (>= 2 numbers)
                    index.add(tup[:-1], tid, "features_only")
        canaries.setdefault(truth_file(d, "canary.txt").read_text(encoding="utf-8").strip().encode(), []).append(tid)
    return index, canaries


def _walk(roots: Iterable[Path]) -> Iterable[Path]:
    for r in roots:
        for dirpath, dirs, names in os.walk(r):
            dirs[:] = [x for x in dirs if x != ".git"]
            for n in names:
                yield Path(dirpath) / n


def scan(suite: Path, roots: list[Path], *, features: bool = False) -> dict[str, Any]:
    sc = _suite_checks()
    index, canaries = build_index(suite, sc, features)
    files: dict[str, dict[str, dict[str, int]]] = {}
    n = big = unreadable = binary = 0
    for f in _walk(roots):
        n += 1
        if n % 5000 == 0:
            print(time.strftime("%H:%M:%S"), f"{n} files, now in {f.parent}", flush=True)
        try:
            if f.stat().st_size > MAX_READ:
                big += 1
                continue
            raw = f.read_bytes()
        except OSError:
            unreadable += 1
            continue
        hit: dict[str, dict[str, int]] = {}
        for c, tids in canaries.items():
            if c in raw:
                for tid in tids:
                    hit.setdefault(tid, {})["canary"] = hit.get(tid, {}).get("canary", 0) + 1
        if b"\x00" in raw[:8192]:
            binary += 1
        else:
            seen: set[tuple[tuple[str, ...], str, str]] = set()
            for tup, tid, form in index.scan(raw):
                if (tup, tid, form) in seen:
                    continue  # distinct rows per file
                seen.add((tup, tid, form))
                key = "labelled" if form in ("label_last", "label_first") else form
                hit.setdefault(tid, {})[key] = hit.get(tid, {}).get(key, 0) + 1
        if hit:
            files[str(f)] = hit
    labelled = sorted(p for p, h in files.items() if any("labelled" in v or "canary" in v for v in h.values()))
    return {"suite": str(suite), "roots": [str(r) for r in roots], "files_scanned": n, "skipped_big": big,
            "unreadable": unreadable, "binary_not_token_scanned": binary,
            "files_with_labelled_rows_or_canary": labelled,
            "files_with_features_only": sorted(set(files) - set(labelled)) if features else None,
            "per_file": files}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("suite", help="the suite directory (suite.yaml + task dirs; truth via truth_layout)")
    ap.add_argument("--out", required=True, help="JSON report, OUTSIDE every agent-readable root")
    ap.add_argument("--root", action="append", default=[], help="a root to scan (replaces the defaults)")
    ap.add_argument("--features", action="store_true", help="also list files holding held-out FEATURE rows")
    a = ap.parse_args(argv)
    inside = agent_readable_root_of(Path(a.out))
    if inside is not None:
        print(f"refused: the report {a.out} is inside agent-readable root {inside}", file=sys.stderr)
        return 2
    roots = [Path(r) for r in a.root] or default_roots()
    rep = scan(Path(a.suite), roots, features=a.features)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("files_scanned", "unreadable", "binary_not_token_scanned")}
                     | {"files_with_labelled_rows_or_canary": len(rep["files_with_labelled_rows_or_canary"])}))
    return 1 if rep["files_with_labelled_rows_or_canary"] else 0


if __name__ == "__main__":
    sys.exit(main())
