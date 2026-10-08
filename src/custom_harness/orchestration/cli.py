"""`python -m custom_harness.orchestration <command>`: the delegation driver as a CLI (JSON on stdout).

Tokens are read from / written to files (--token-file / --token-out), never passed in argv (visible to every
process on the machine). Exit codes: 0 ok, 3 refused (ledgered), 2 integrity or usage error.

  register-lead --name N --token-out F [--spends spends.json]   owner/orchestrator: register a top-level session
  spawn   --token-file F --brief brief.json --token-out H        a lead spawns a helper (writes the helper's token)
  spend   --token-file F --kind K --resource R --unit U --amount A   authorise a spend BEFORE making it
  claim   --token-file F --branch B --worktree W                 a lead holds its own branch + worktree
  release --token-file F --branch B
  complete --token-file F --output TEXT                          a helper finishes (frees its slot and locks)
  abort   --token-file F --helper S --reason TEXT                the parent lead stops a helper
  force-release --branch B --by WHO --reason TEXT                HUMAN: clear a crashed session's lock (ledgered)
  status / verify
All commands take --root (default $CHH_DATA_DIR/orchestration) and --git-repo (probe `git worktree list`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from ..config import data_dir
from .driver import Driver, IntegrityError, Refused, git_checked_out


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m custom_harness.orchestration")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, token: bool = True) -> argparse.ArgumentParser:
        c = sub.add_parser(name)
        c.add_argument("--root", type=Path, default=None)
        c.add_argument("--git-repo", type=Path, default=None, help="refuse branches checked out elsewhere in it")
        if token:
            c.add_argument("--token-file", type=Path, required=True)
        return c

    r = cmd("register-lead", token=False)
    r.add_argument("--name", required=True)
    r.add_argument("--token-out", type=Path, required=True)
    r.add_argument("--spends", type=Path, help="JSON list of spend grants for the lead itself (default: none)")
    s = cmd("spawn")
    s.add_argument("--brief", type=Path, required=True)
    s.add_argument("--token-out", type=Path, required=True)
    sp = cmd("spend")
    sp.add_argument("--kind", required=True)
    sp.add_argument("--resource", required=True)
    sp.add_argument("--unit", required=True)
    sp.add_argument("--amount", type=float, required=True)
    c = cmd("claim")
    c.add_argument("--branch", required=True)
    c.add_argument("--worktree", required=True)
    rl = cmd("release")
    rl.add_argument("--branch", required=True)
    co = cmd("complete")
    co.add_argument("--output", required=True)
    ab = cmd("abort")
    ab.add_argument("--helper", required=True)
    ab.add_argument("--reason", required=True)
    fr = cmd("force-release", token=False)
    fr.add_argument("--branch", required=True)
    fr.add_argument("--by", required=True)
    fr.add_argument("--reason", required=True)
    cmd("status", token=False)
    cmd("verify", token=False)
    return p


def _write_token(path: Path, token: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)  # never overwrite another session's token
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(token + "\n")


def _out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    root = a.root or (data_dir() / "orchestration")
    repo = a.git_repo
    d = Driver(root, checked_out=(lambda: git_checked_out(repo)) if repo else None)
    tok = a.token_file.read_text(encoding="utf-8").strip() if getattr(a, "token_file", None) else ""
    try:
        if a.cmd == "register-lead":
            grants = json.loads(a.spends.read_text(encoding="utf-8")) if a.spends else []
            _write_token(a.token_out, d.register_lead(a.name, allowed_spends=grants))
            _out({"session": a.name, "token_file": str(a.token_out)})
        elif a.cmd == "spawn":
            raw = json.loads(a.brief.read_text(encoding="utf-8"))
            if a.token_out.exists():
                raise FileExistsError(f"{a.token_out} exists: refusing to overwrite a token file")
            h = d.spawn(tok, raw)
            _write_token(a.token_out, h.token)
            _out({"session": h.session, "token_file": str(a.token_out)})
        elif a.cmd == "spend":
            d.spend(tok, a.kind, a.resource, a.unit, a.amount)
            _out({"authorised": {"kind": a.kind, "resource": a.resource, "unit": a.unit, "amount": a.amount}})
        elif a.cmd == "claim":
            d.claim(tok, branch=a.branch, worktree=a.worktree)
            _out({"claimed": a.branch})
        elif a.cmd == "release":
            d.release(tok, branch=a.branch)
            _out({"released": a.branch})
        elif a.cmd == "complete":
            d.complete(tok, output=a.output)
            sessions = d.status()["sessions"]
            done = [n for n, s in sessions.items() if s["finished"] and s["role"] == "helper"]
            _out({"completed": True, "session": done[-1] if done else None})
        elif a.cmd == "abort":
            d.abort(tok, a.helper, reason=a.reason)
            _out({"aborted": a.helper})
        elif a.cmd == "force-release":
            d.force_release(a.branch, by=a.by, reason=a.reason)
            _out({"force_released": a.branch})
        elif a.cmd == "status":
            _out(d.status())
        else:  # verify
            n = len(d.ledger.entries())
            d.locks.all()
            _out({"ok": True, "entries": n})
    except Refused as e:
        _out({"refused": e.action, "reason": e.reason, "detail": e.detail})
        return 3
    except (IntegrityError, OSError, ValueError, TimeoutError) as e:
        _out({"error": f"{type(e).__name__}: {e}"})
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
