"""Static checks of the human-only write paths (GOAL invariants 13, 15; custom_harness/human/catalog.py).

Like the approval-node checks (tests/test_approval.py, evolve/edits.py), but over the source:

1. Every `<x>.append(KIND, ...)` whose KIND is a human-only ledger kind (a string literal, or a module-level string
   constant of the same file) appears only in the files allowed to write that kind.
2. Only the human-operated CLI layers (and the package itself) import the write side of `custom_harness.human`.
   Research, evolve, runtime, memory, control, orchestration, gateway, compute and kerneld code never does, so no
   registered module (and so no evolved node, which can only call registered modules) has a path to it.
3. `custom_harness.human` registers no module.

Written to FAIL FIRST on the parent (origin/int17 4567e6c): custom_harness/human does not exist (rule 2 and 3 have no
package to scan and the catalogue import fails), and on the parent `cli.py` itself appended `approval` and
`statement_approval` (rule 1 now allows only custom_harness/human/nodes.py for those writes). NOT RUN (owner: no tests
this phase).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness"

# kind -> files (relative to src/custom_harness) allowed to append it
ALLOWED_WRITERS: dict[str, frozenset[str]] = {
    "approval": frozenset({"human/nodes.py", "human/providers.py", "human/checks.py", "audit/gate.py",
                           "compute/backend.py", "evolve/finalize.py", "evolve/router.py"}),
    "statement_approval": frozenset({"human/nodes.py"}),
    "adapter_approval": frozenset({"kernel/adapters.py"}),
    "role_approval": frozenset({"kernel/vault.py"}),
    "alpha_open": frozenset({"kernel/kernel.py"}),
    "feature_ruling": frozenset({"verify/provenance.py"}),
    "verifier_review": frozenset({"verify/synthesis.py"}),
    "run_kill": frozenset({"studio/kill.py"}),
    "tier_policy": frozenset({"studio/tierpolicy.py"}),
    "salt_reveal": frozenset({"salts/plan.py"}),
}
# who may import the WRITE side of custom_harness.human (catalog and guard are read-only and import-light)
HUMAN_WRITE_MODULES = frozenset({"nodes", "actions", "review", "features", "providers", "checks", "cli", "queue"})
HUMAN_IMPORTERS = frozenset({"cli.py", "studio/cli.py", "studio/queue.py", "verify/provenance_cli.py"})


def _files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(p: Path) -> str:
    return p.relative_to(SRC).as_posix()


def _constants(tree: ast.Module) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def _appended_kinds(tree: ast.Module) -> list[tuple[str, int]]:
    consts = _constants(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append" \
                and node.args:
            a = node.args[0]
            kind = a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else \
                consts.get(a.id) if isinstance(a, ast.Name) else None
            if isinstance(kind, str):
                found.append((kind, node.lineno))
    return found


def test_the_catalogue_kinds_are_all_policed_here():
    from custom_harness.human.catalog import MODULE_FORBIDDEN_KINDS
    assert set(MODULE_FORBIDDEN_KINDS) <= set(ALLOWED_WRITERS)


def test_human_only_kinds_are_appended_only_by_their_writers():
    bad = []
    for p in _files():
        for kind, line in _appended_kinds(ast.parse(p.read_text(encoding="utf-8"))):
            if kind in ALLOWED_WRITERS and _rel(p) not in ALLOWED_WRITERS[kind]:
                bad.append(f"{_rel(p)}:{line} appends {kind!r}")
    assert bad == []


def test_only_the_cli_layers_import_the_human_write_api():
    bad = []
    for p in _files():
        rel = _rel(p)
        if rel.startswith("human/") or rel in HUMAN_IMPORTERS:
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(a.name == "human" for a in node.names):  # `from . import human`: the whole package
                    bad.append(f"{rel}:{node.lineno} imports the human package")
                if mod.endswith("human") or ".human." in f".{mod}." or mod == "human":
                    names = [f"{mod}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names if ".human" in a.name]
            for n in names:
                tail = n.split("human.", 1)[-1].split(".")[0] if "human." in n else ""
                if tail in HUMAN_WRITE_MODULES:
                    bad.append(f"{rel}:{node.lineno} imports {n}")
    assert bad == []


def test_the_human_package_registers_no_module():
    for p in (SRC / "human").glob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "@register(" not in text and "runtime import register" not in text, p.name


def test_kill_module_ledger_uses_the_catalogue():
    text = (SRC / "studio" / "kill.py").read_text(encoding="utf-8")
    assert "MODULE_FORBIDDEN_KINDS" in text
