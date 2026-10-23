"""Move held-out truth out of every agent-readable root (truth_layout; s4-integrity item 1). A DATA CHANGE: run it
only in the run phase, by the owner or with the owner's go-ahead. Archives, never deletes.

    python scripts/migrate_truth.py plan    [--extra-root DIR ...]                      # default: lists, writes nothing
    python scripts/migrate_truth.py apply   --archive DIR [--extra-root DIR ...] [--copy-manifest FILE]
                                            [--only-mapped]
    python scripts/migrate_truth.py verify  --archive DIR [--extra-root DIR ...]

What it finds (truth_layout.legacy_truth_paths): every directory named `private` that holds a test_y.csv, canary.txt or
mint.json, under every agent-readable root (commitment.default_agent_readable_roots: the repo checkout, <data>/
{sandboxes,repos,suites,tasks,lab}, %TEMP%\\claude) plus each --extra-root. That includes the live suite's
`<data>/suites/m2/<task>/private/`, the old `<data>/tasks/rings-v1/private/test_y.csv`, and old copies in agent
scratchpads such as `%TEMP%\\claude\\...\\d7fa1cf8...\\scratchpad\\salts-tmp\\e\\<task>\\private\\test_y.csv`.

What `apply` does, per legacy dir P with task dir T = P's parent (each step fails closed, nothing is ever deleted):
1. inventory P (relative path, bytes, SHA-256 of every file);
2. if T is a MAPPED task (under <data>/suites or <data>/tasks): copy every file to truth_layout.truth_dir(T) (outside
   every agent-readable root) and re-hash the copies. Files already there must be byte-identical (an earlier,
   interrupted run): anything else is a conflict and P is left in place;
3. MOVE P to ARCHIVE/<root label>/<path of P relative to its root> and re-hash it there;
4. record the entry in ARCHIVE/TRUTH-MIGRATION-<timestamp>.manifest.json (paths, sizes, hashes; never contents).
A legacy dir that is not a mapped task (a scratchpad copy, an old sandbox) is only archived (step 3): no live task
reads it. `--only-mapped` restricts `apply` to mapped tasks (leave other sessions' scratchpads for the owner).

Idempotent: a second `apply` finds nothing to do (moved dirs are gone; a half-done dir resumes at step 3). `verify`
re-hashes every recorded archive and truth copy against every manifest in ARCHIVE, and runs the static check
truth_layout.truth_layout_problems(scan=True) over the roots (no legacy truth left in any of them). It prints
paths and counts only, never file contents (the files are labels).

Refuses (exit 2): an ARCHIVE inside an agent-readable root or inside the truth root; an archive target that exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from custom_harness.commitment import agent_readable_root_of, default_agent_readable_roots
from custom_harness.pathsafe import is_within, overlaps
from custom_harness.truth_layout import (
    TruthLayoutError,
    legacy_truth_paths,
    mapped_truth_dir,
    truth_layout_problems,
    truth_root,
)

FORMAT = "chh-truth-migration/1"


def _inventory(d: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for p in sorted(d.rglob("*")):
        if p.is_file():
            h = hashlib.sha256()
            with open(p, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
            out[p.relative_to(d).as_posix()] = {"bytes": p.stat().st_size, "sha256": h.hexdigest()}
    return out


def _roots(extra: list[str]) -> list[Path]:
    return [*default_agent_readable_roots(), *(Path(r) for r in extra)]


def _legacy_dirs(roots: list[Path]) -> list[tuple[Path, Path, int]]:
    """(legacy private dir, the root it was found under, root index), each dir once, in a stable order."""
    seen: dict[str, tuple[Path, Path, int]] = {}
    for i, r in enumerate(roots):
        for f in legacy_truth_paths([r]):
            key = os.path.normcase(str(f.parent.resolve()))
            seen.setdefault(key, (f.parent, r, i))
    return sorted(seen.values(), key=lambda t: os.path.normcase(str(t[0])))


def _label(root: Path, i: int) -> str:
    name = "".join(c if c.isalnum() or c in "-_." else "_" for c in (root.name or "root"))
    return f"{i:02d}-{name}"


def _plan(roots: list[Path], archive: Path | None) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for p, root, i in _legacy_dirs(roots):
        task = p.parent
        try:
            dest = mapped_truth_dir(task)
        except TruthLayoutError as e:
            actions.append({"legacy": str(p), "error": str(e)})
            continue
        rel = p.resolve().relative_to(root.resolve()).as_posix() if is_within(p, root) else p.name
        actions.append({"legacy": str(p), "root": str(root), "mapped_truth_dir": str(dest) if dest else None,
                        "archive_to": str(archive / _label(root, i) / rel) if archive else None})
    return actions


def _check_archive(archive: Path) -> str | None:
    r = agent_readable_root_of(archive)
    if r is not None:
        return f"the archive {archive} is inside agent-readable root {r}"
    if overlaps(archive, truth_root()):
        return f"the archive {archive} overlaps the truth root {truth_root()} (keep archives apart from live truth)"
    return None


def cmd_plan(a: argparse.Namespace) -> int:
    roots = _roots(a.extra_root)
    acts = _plan(roots, Path(a.archive) if a.archive else None)
    print(json.dumps({"roots": [str(r) for r in roots], "legacy_dirs": len(acts), "actions": acts}, indent=1))
    return 0


def cmd_apply(a: argparse.Namespace) -> int:
    archive = Path(a.archive).resolve()
    why = _check_archive(archive)
    if why:
        print(f"refused: {why}", file=sys.stderr)
        return 2
    roots = _roots(a.extra_root)
    acts = _plan(roots, archive)
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    for act in acts:
        if "error" in act:
            problems.append(f"{act['legacy']}: {act['error']}")
            continue
        if a.only_mapped and act["mapped_truth_dir"] is None:
            continue
        src, dst = Path(act["legacy"]), Path(act["archive_to"])
        before = _inventory(src)
        entry: dict[str, Any] = {"legacy": str(src), "archived_to": str(dst), "inventory": before,
                                 "truth_dir": act["mapped_truth_dir"]}
        if act["mapped_truth_dir"]:
            dest = Path(act["mapped_truth_dir"])
            inside = agent_readable_root_of(dest)
            if inside is not None:  # e.g. $CHH_TRUTH_ROOT set inside a root: never copy labels there
                problems.append(f"{src}: truth dir {dest} is inside agent-readable root {inside}; left in place")
                continue
            conflict = [rel for rel in before if (dest / rel).exists() and _inventory_one(dest / rel) !=
                        before[rel]["sha256"]]
            if conflict:
                problems.append(f"{src}: {dest} already holds DIFFERENT {conflict[:3]}; left in place")
                continue
            for rel in before:
                target = dest / rel
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src / rel, target)
            copied = {rel: v for rel, v in _inventory(dest).items() if rel in before}
            if copied != before:
                problems.append(f"{src}: the copy in {dest} does not match after copying; left in place")
                continue
        if dst.exists():
            problems.append(f"{src}: archive target {dst} exists (never overwrite an archive); left in place")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        after = _inventory(dst)
        if after != before:
            problems.append(f"ERROR {dst} differs from {src} after the move (nothing deleted; inspect both)")
            entry["moved_mismatch"] = True
        entries.append(entry)
    manifest = {"format": FORMAT, "migrated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "archive": str(archive),
                "truth_root": str(truth_root()), "roots": [str(r) for r in roots], "entries": entries,
                "problems": problems}
    text = json.dumps(manifest, indent=1, sort_keys=True) + "\n"
    if entries or problems:
        archive.mkdir(parents=True, exist_ok=True)
        out = archive / f"TRUTH-MIGRATION-{time.strftime('%Y%m%d-%H%M%S')}.manifest.json"
        out.write_text(text, encoding="utf-8")
        if a.copy_manifest:
            Path(a.copy_manifest).write_text(text, encoding="utf-8")
        print(f"manifest: {out}")
    print(json.dumps({"moved": len(entries), "problems": problems}, indent=1))
    return 1 if problems else 0


def _inventory_one(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def cmd_verify(a: argparse.Namespace) -> int:
    archive = Path(a.archive)
    problems: list[str] = []
    manifests = sorted(archive.glob("TRUTH-MIGRATION-*.manifest.json")) if archive.is_dir() else []
    n = 0
    for m in manifests:
        doc = json.loads(m.read_text(encoding="utf-8"))
        if doc.get("format") != FORMAT:
            problems.append(f"{m.name}: unknown format")
            continue
        for e in doc.get("entries", []):
            n += 1
            inv = e["inventory"]
            have = _inventory(Path(e["archived_to"])) if Path(e["archived_to"]).is_dir() else {}
            if have != inv:
                problems.append(f"{m.name}: archive {e['archived_to']} does not match its recorded hashes")
            if e.get("truth_dir"):
                t = Path(e["truth_dir"])
                got = {rel: v for rel, v in (_inventory(t) if t.is_dir() else {}).items() if rel in inv}
                if got != inv:
                    problems.append(f"{m.name}: truth copy {t} does not match the recorded hashes")
    problems += truth_layout_problems(_roots(a.extra_root), scan=True)
    print(json.dumps({"manifests": len(manifests), "entries": n, "problems": problems}, indent=1))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--archive", required=name != "plan")
        p.add_argument("--extra-root", action="append", default=[])
        if name == "apply":
            p.add_argument("--copy-manifest")
            p.add_argument("--only-mapped", action="store_true")
    a = ap.parse_args(argv)
    return {"plan": cmd_plan, "apply": cmd_apply, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
