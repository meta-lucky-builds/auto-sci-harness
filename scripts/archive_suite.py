"""Archive (never delete) task-suite directories: MOVE each to an archive directory outside every agent-readable root,
and write a manifest of every moved file (path, size, SHA-256), checked after the move.

    python scripts/archive_suite.py ARCHIVE_DIR SRC_DIR [SRC_DIR ...] [--reason TEXT] [--copy-manifest FILE]

Refuses (exit 2, nothing moved) when ARCHIVE_DIR is inside an agent-readable root
(commitment.default_agent_readable_roots), when a target already exists, or when ARCHIVE_DIR is inside the repo.
The manifest holds hashes and paths only, never file contents; `--copy-manifest` also writes it into the repo
(e.g. snapshots/), which is safe for that reason.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

from custom_harness.commitment import default_agent_readable_roots


def inventory(d: Path) -> dict[str, dict[str, object]]:
    return {p.relative_to(d).as_posix(): {"bytes": p.stat().st_size,
                                         "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(d.rglob("*")) if p.is_file()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archive")
    ap.add_argument("src", nargs="+")
    ap.add_argument("--reason", default="")
    ap.add_argument("--copy-manifest")
    a = ap.parse_args()
    archive = Path(a.archive).resolve()
    roots = [r.resolve() for r in default_agent_readable_roots()]
    bad = [r for r in roots if archive == r or archive.is_relative_to(r)]
    if bad:
        print(f"refused: {archive} is inside agent-readable root(s) {bad}", file=sys.stderr)
        return 2
    srcs = [Path(s).resolve() for s in a.src]
    for s in srcs:
        if not s.is_dir():
            print(f"refused: {s} is not a directory", file=sys.stderr)
            return 2
        if (archive / s.name).exists():
            print(f"refused: {archive / s.name} already exists (never overwrite an archive)", file=sys.stderr)
            return 2
    archive.mkdir(parents=True, exist_ok=True)
    entries = []
    for s in srcs:
        before = inventory(s)
        dst = archive / s.name
        shutil.move(str(s), str(dst))
        after = inventory(dst)
        if after != before:
            print(f"ERROR: {dst} differs from {s} after the move; nothing deleted, inspect both", file=sys.stderr)
            return 1
        entries.append({"from": str(s), "to": str(dst), "files": len(after),
                        "bytes": sum(int(str(v["bytes"])) for v in after.values()),
                        "tree_sha256": hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest(),
                        "inventory": after})
    manifest = {"format": "chh-archive/1", "archived_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "reason": a.reason,
                "archive": str(archive), "moved": entries}
    text = json.dumps(manifest, indent=1, sort_keys=True) + "\n"
    out = archive / f"ARCHIVE-{time.strftime('%Y%m%d-%H%M%S')}.manifest.json"
    out.write_text(text, encoding="utf-8")
    if a.copy_manifest:
        Path(a.copy_manifest).write_text(text, encoding="utf-8")
    print(json.dumps({"manifest": str(out), "moved": [{k: e[k] for k in ("from", "to", "files", "bytes", "tree_sha256")}
                                                      for e in entries]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
