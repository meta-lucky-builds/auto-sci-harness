"""Windows path-alias class outside the Kernel (s3-evolve red team, 2026-10-14; s4-integrity item 5).

Every containment check now goes through custom_harness.pathsafe. Fail-first reasoning (NOT run: owner rule, tests
deferred): at 82719e1 (origin/int17)
- sandbox.base.check_mount compared `Path.resolve()` text, which keeps a `\\\\?\\` / `\\\\.\\` prefix, so the
  prefixed spelling of a protected dir was ACCEPTED (verified by the red team's probe rt_probe_mount.py);
- salts.plan._under_agent_readable and commitment.secret_leak_scan(secret_file=) used resolve()+is_relative_to the
  same way, so a prefixed spelling of a path inside a root was reported as outside;
- custom_harness.pathsafe did not exist (ImportError).
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import pytest

from custom_harness import pathsafe as P

win = pytest.mark.skipif(sys.platform != "win32", reason="Windows path aliases")


def _aliases(p: Path) -> list[str]:
    s = str(p.resolve())
    out = ["\\\\?\\" + s, "\\\\.\\" + s, s.upper(), s.lower(), s + ".", s + " "]
    share = f"\\\\localhost\\{s[0]}${s[2:]}"
    if os.path.exists(share):  # admin shares may be disabled on this machine
        out += [share, "\\\\?\\UNC\\localhost\\" + s[0] + "$" + s[2:]]
    buf = ctypes.create_unicode_buffer(1024)
    if ctypes.windll.kernel32.GetShortPathNameW(s, buf, 1024) and buf.value.lower() != s.lower():  # type: ignore[attr-defined]
        out.append(buf.value)
    return out


@win
def test_every_alias_of_a_dir_is_within_it(tmp_path: Path) -> None:
    d = tmp_path / "Long Directory Name For Short" / "inner"
    d.mkdir(parents=True)
    for a in _aliases(d):
        assert P.is_within(a, d), a
        assert P.is_within(a + "\\child\\file.txt", d), a
        assert P.overlaps(d, a), a
    assert not P.is_within(tmp_path / "other", d)
    assert not P.is_within(str(d) + "x", d)  # a sibling with a longer name is not inside


@win
def test_check_mount_refuses_every_alias_of_a_protected_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_harness.sandbox import base

    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    store = tmp_path / "data" / "store"
    store.mkdir(parents=True)
    for a in _aliases(store):
        with pytest.raises(base.ProtectedMount):
            base.check_mount(a)
        with pytest.raises(base.ProtectedMount):
            base.check_mount(a + "\\ws")
    base.check_mount(tmp_path / "elsewhere")  # an unrelated workspace is fine


@win
def test_salt_seal_location_alias_is_inside_agent_readable_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                                ) -> None:
    from custom_harness.salts import plan

    root = tmp_path / "agent"
    root.mkdir()
    monkeypatch.setenv("CHH_AGENT_READABLE", str(root))
    for a in _aliases(root):
        assert plan._under_agent_readable(Path(a + "\\sealed.json")) is not None, a


@win
def test_secret_file_alias_inside_root_is_reported(tmp_path: Path) -> None:
    from custom_harness.commitment import secret_leak_scan

    root = tmp_path / "agent"
    root.mkdir()
    sf = root / "secret.bin"
    sf.write_bytes(b"s" * 32)
    for a in _aliases(root):
        problems = secret_leak_scan(b"z" * 32, [tmp_path / "empty"], secret_file=Path(a + "\\secret.bin"))
        assert problems == [], "a root that does not contain the file must not flag it"
        problems = secret_leak_scan(b"z" * 32, [root], secret_file=Path(a + "\\secret.bin"))
        assert any("inside agent-readable root" in p for p in problems), a


def test_export_tools_copy_agrees_with_pathsafe(tmp_path: Path) -> None:
    """export_tools.py is stdlib-only (copied into the proposer sandbox), so it carries a copy of the prefix rule;
    the copy must agree with pathsafe on every case it covers."""
    from custom_harness.evolve import export_tools as E

    d = tmp_path / "exp"
    (d / "a").mkdir(parents=True)
    cases = [d / "a", d, tmp_path, tmp_path / "expx"]
    if sys.platform == "win32":
        cases += [Path("\\\\?\\" + str(d.resolve()) + "\\a"), Path("\\\\.\\" + str(d.resolve()))]
    for c in cases:
        assert E._within(c, d) == P.is_within(c, d), c


def test_safe_path_still_refuses_escapes(tmp_path: Path) -> None:
    from custom_harness.sandbox.base import WorkspaceEscape, safe_path

    ws = tmp_path / "ws"
    ws.mkdir()
    assert safe_path(ws, "a/b.txt") == ws / "a" / "b.txt"
    for bad in ("../x", "/abs", *(("C:/x",) if sys.platform == "win32" else ())):
        with pytest.raises(WorkspaceEscape):
            safe_path(ws, bad)


def test_no_containment_check_bypasses_pathsafe() -> None:
    """Generalised: outside the Kernel (s4-kernel's files) and pathsafe itself, no source file decides containment
    with `is_relative_to` (the textual check the aliases defeat). Fails first at 82719e1: sandbox/base.py,
    salts/plan.py, commitment.py and evolve/export_tools.py did."""
    src = Path(__file__).resolve().parents[1] / "src" / "custom_harness"
    allowed = {"pathsafe.py"}
    offenders = []
    for f in src.rglob("*.py"):
        rel = f.relative_to(src).as_posix()
        if rel.startswith(("kernel/", "kerneld/")) or f.name in allowed:
            continue
        if ".is_relative_to(" in f.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert offenders == []
