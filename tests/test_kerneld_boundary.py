"""kerneld boundary ratchet (GOAL §16.5.2): which host code still reads private vault data or opens the store.

Covered host modules (evolve/*, runtime/engine.py, kerneld/client.py, research/claims.py) make NO private vault read
and construct no EvidenceStore. Every remaining old-path site is listed here by file and count; a NEW site anywhere
fails this test, and moving a site behind kerneld means shrinking the list (the honest record of what is not yet behind
the boundary). Besides logged reads and store opens, two more ways private bytes are in the host process are counted:
private INGEST (`register_dataset` with non-empty `private=`: the host holds the bytes it registers) and vault
CANARY reads; and a `.read(**kwargs)` call counts as a private read (its purpose cannot be seen statically).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness"

# private vault reads still in-process (old path): none. research/claims.py's three reads (confirm: evaluate;
# audit: leakscan; I4 re-check: kernel) moved behind kerneld (score / leak_scan / rescore) on 2026-10-08.
OLD_PATH_PRIVATE_READS: dict[str, int] = {}
# EvidenceStore constructions outside the Kernel process (old path: the CLI opens the store for everything)
OLD_PATH_STORE_OPENS = {"cli.py": 1}
COVERED = ["evolve", "runtime/engine.py", "kerneld/client.py", "research/claims.py"]
# NOT old-path sites, exempt with a stated reason (2026-10-08): research/preflight.py (GOAL §16.8 zero-call preflight)
# builds a SCRATCH store in a temporary directory holding only SIMULATED labels it generated itself, and makes one
# logged read of them so the real K4 sees logged = reported evaluations. It never opens the real store and never reads
# real held-out data; routing simulated labels through kerneld would add a process and protect nothing.
SCRATCH_STORE_SITES = {"research/preflight.py": 1}
# Private ingest outside the Kernel (old path, 2026-10-08; no kerneld ingest op exists): the host reads the private
# file itself and hands the bytes to the vault. cli.py: `chh return-data` (FUTURE measurements); research/claims.py:
# task registration (the task dir's private/test labels); audit/data.py: the claim-audit's EXTERNAL split.
# research/preflight.py ingests only its own SIMULATED labels into a scratch store (exempt, reason above).
OLD_PATH_PRIVATE_INGEST = {"cli.py": 1, "research/claims.py": 1, "audit/data.py": 1}
# Vault canary reads outside kerneld (old path): research/claims.py checks, at registration, that a task's canary
# equals the one registered for the same held-out data (it holds that canary already, from the task dir).
OLD_PATH_CANARY_READS = {"research/claims.py": 1}


def _files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "kernel" not in p.relative_to(SRC).parts[:1]
            and p.relative_to(SRC).as_posix() != "kerneld/server.py"]


def _sites(pred) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in _files():
        n = sum(1 for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))) if pred(node))
        if n:
            out[p.relative_to(SRC).as_posix()] = n
    return out


def _private_read(node: ast.AST) -> bool:
    """`x.read(..., purpose=...)` (Vault.read's purpose is keyword-only), or `x.read(**kw)` (purpose hidden)."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read"
            and any(k.arg in ("purpose", None) for k in node.keywords))


def _private_ingest(node: ast.AST) -> bool:
    """`x.register_dataset(..., private=<anything but an empty dict literal>)`, or with `**kw` (private hidden)."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_dataset"):
        return False
    if any(k.arg is None for k in node.keywords):
        return True
    priv = [k.value for k in node.keywords if k.arg == "private"]
    if len(node.args) > 3:  # register_dataset(dataset_id, role, public, private, ...) positionally
        priv.append(node.args[3])
    return any(not (isinstance(v, ast.Dict) and not v.keys) for v in priv)


def _canary_read(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "canary"


def _dynamic_access(node: ast.AST) -> bool:
    """`getattr(x, "read" | "canary" | "register_dataset")`: a call the checks above cannot see."""
    return (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr" and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant) and node.args[1].value in ("read", "canary", "register_dataset"))


def _store_open(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "EvidenceStore"


def test_private_vault_reads_outside_kerneld_are_only_the_listed_old_path_sites():
    assert _sites(_private_read) == OLD_PATH_PRIVATE_READS | SCRATCH_STORE_SITES


def test_store_opens_outside_the_kernel_are_only_the_listed_old_path_sites():
    assert _sites(_store_open) == OLD_PATH_STORE_OPENS | SCRATCH_STORE_SITES


def test_private_ingest_outside_the_kernel_is_only_the_listed_old_path_sites():
    assert _sites(_private_ingest) == OLD_PATH_PRIVATE_INGEST | SCRATCH_STORE_SITES


def test_canary_reads_outside_kerneld_are_only_the_listed_old_path_sites():
    assert _sites(_canary_read) == OLD_PATH_CANARY_READS


def test_no_dynamic_access_to_private_vault_methods():
    assert _sites(_dynamic_access) == {}


def test_covered_host_modules_have_no_private_read_or_store_open():
    # research/claims.py keeps its registration-time ingest and canary check (listed above); every READ moved
    sites = {**_sites(_private_read), **_sites(_store_open)}
    bad = [f for f in sites if any(f == c or f.startswith(c + "/") for c in COVERED)]
    assert not bad, bad
