"""State Reproducibility Bundle: freeze code + task suites + environment at a stopping point.

    python scripts/snapshot_state.py OUT_DIR SUITE_DIR [SUITE_DIR ...] --note "..."

Writes OUT_DIR/state-<commit12>.zip containing:
  code.tar          `git archive HEAD` (exactly the committed tree)
  suites/<name>/... every file of each suite directory (INCLUDING private/ labels: this zip is a bundle,
                    never agent-visible, never committed to git)
  env/*.txt         pip freeze, python/platform, docker + sandbox image id, codex CLI version
  manifest.json     format, commit, created, note, sha256 of every member
and prints the manifest hash. Commit the small manifest (snapshots/), not the zip.
"""

from __future__ import annotations

import hashlib
import io
import json
import platform
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False, cwd=ROOT)
        return (p.stdout or p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"unavailable: {e}"


def main() -> int:
    args = sys.argv[1:]
    note = ""
    if "--note" in args:
        i = args.index("--note")
        note = args[i + 1]
        args = args[:i] + args[i + 2:]
    out_dir, suites = Path(args[0]), [Path(a) for a in args[1:]]
    if _run(["git", "status", "--porcelain"]):
        print("refusing: the working tree has uncommitted changes (the bundle must equal a commit)")
        return 1
    commit = _run(["git", "rev-parse", "HEAD"])
    members: dict[str, bytes] = {
        "code.tar": subprocess.run(["git", "archive", "--format=tar", "HEAD"], capture_output=True, cwd=ROOT,
                                   check=True).stdout,
        "env/pip_freeze.txt": _run([sys.executable, "-m", "pip", "freeze"]).encode(),
        "env/python_platform.txt": f"{sys.version}\n{platform.platform()}\n".encode(),
        "env/docker.txt": (_run(["docker", "version", "--format", "{{.Server.Version}}"]) + "\n" +
                           _run(["docker", "image", "inspect", "--format", "{{.Id}}",
                                 "custom-harness-sandbox:py312"]) + "\n").encode(),
        "env/codex.txt": _run(["codex", "--version"]).encode(),
    }
    for suite in suites:
        for f in sorted(p for p in suite.rglob("*") if p.is_file() and "_export" not in p.parts):
            members[f"suites/{suite.name}/{f.relative_to(suite).as_posix()}"] = f.read_bytes()
    manifest = {"format": "chh-state/1", "commit": commit, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "note": note, "members": {k: hashlib.sha256(v).hexdigest() for k, v in sorted(members.items())}}
    data = json.dumps(manifest, indent=1, sort_keys=True).encode()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"state-{commit[:12]}.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", data)
        for name, blob in members.items():
            z.writestr(name, blob)
    path.write_bytes(buf.getvalue())
    print(json.dumps({"bundle": str(path), "manifest_sha256": hashlib.sha256(data).hexdigest(),
                      "members": len(members), "bytes": path.stat().st_size}))
    (out_dir / f"state-{commit[:12]}.manifest.json").write_bytes(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
