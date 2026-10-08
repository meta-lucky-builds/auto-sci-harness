"""kerneld boundary ratchet (GOAL §16.5.2): which host code still reads private vault data or opens the store.

Covered host modules (evolve/*, runtime/engine.py, kerneld/client.py) make NO private vault read and construct no
EvidenceStore. Every remaining old-path site is listed here by file and count; a NEW site anywhere fails this test,
and moving a site behind kerneld means shrinking the list (the honest record of what is not yet behind the boundary).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness"

# private vault reads still in-process (old path). research/claims.py belongs to workstream yield: its three reads
# (confirm: evaluate; audit: leakscan; I4 re-check: kernel) have kerneld equivalents (score / leak_scan / rescore).
OLD_PATH_PRIVATE_READS = {"research/claims.py": 3}
# EvidenceStore constructions outside the Kernel process (old path: the CLI opens the store for everything)
OLD_PATH_STORE_OPENS = {"cli.py": 1}
COVERED = ["evolve", "runtime/engine.py", "kerneld/client.py"]


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
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read"
            and any(k.arg == "purpose" for k in node.keywords))


def _store_open(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "EvidenceStore"


def test_private_vault_reads_outside_kerneld_are_only_the_listed_old_path_sites():
    assert _sites(_private_read) == OLD_PATH_PRIVATE_READS


def test_store_opens_outside_the_kernel_are_only_the_listed_old_path_sites():
    assert _sites(_store_open) == OLD_PATH_STORE_OPENS


def test_covered_host_modules_have_no_private_read_or_store_open():
    sites = {**_sites(_private_read), **_sites(_store_open)}
    bad = [f for f in sites if any(f == c or f.startswith(c + "/") for c in COVERED)]
    assert not bad, bad
