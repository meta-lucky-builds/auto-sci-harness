"""scripts/migrate_truth.py on a toy data dir (s4-integrity item 1). Fail-first reasoning (NOT run: owner rule): at
82719e1 the script and custom_harness.truth_layout did not exist, so every test here fails on import.

Nothing here touches the real data dir: CHH_DATA_DIR and CHH_AGENT_READABLE point into tmp_path.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from custom_harness import truth_layout as TL

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("migrate_truth", ROOT / "scripts" / "migrate_truth.py")
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    data = tmp_path / "data"
    scratch = tmp_path / "claude-scratch"
    for d in (data / "suites", data / "tasks", scratch):
        d.mkdir(parents=True)
    monkeypatch.setenv("CHH_DATA_DIR", str(data))
    monkeypatch.delenv("CHH_TRUTH_ROOT", raising=False)
    roots = (data / "suites", data / "tasks", scratch)
    monkeypatch.setenv("CHH_AGENT_READABLE", os.pathsep.join(str(p) for p in roots))
    for task in (data / "suites" / "m2" / "rings-s1", data / "suites" / "m2" / "xor-s1",
                 data / "tasks" / "rings-v1", scratch / "salts-tmp" / "e" / "x1"):
        (task / "public").mkdir(parents=True)
        (task / "public" / "train.csv").write_text("a,label\n1,0\n", encoding="utf-8")
        (task / "private").mkdir()
        (task / "private" / "test_y.csv").write_text(f"label\n{len(str(task)) % 2}\n", encoding="utf-8")
    (data / "suites" / "m2" / "rings-s1" / "private" / "canary.txt").write_text("CHH-CANARY-a\n", encoding="utf-8")
    return {"data": data, "scratch": scratch, "archive": tmp_path / "archive"}


def test_plan_lists_every_legacy_dir_and_writes_nothing(world, capsys) -> None:
    m = _mod()
    assert m.main(["plan"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["legacy_dirs"] == 4
    mapped = {a["legacy"]: a["mapped_truth_dir"] for a in out["actions"]}
    assert sum(v is not None for v in mapped.values()) == 3  # 2 suite tasks + tasks/rings-v1; the scratch copy is not
    assert not (world["data"] / "private").exists() and not world["archive"].exists()


def test_apply_moves_truth_out_archives_and_is_idempotent(world, capsys) -> None:
    m = _mod()
    rings = world["data"] / "suites" / "m2" / "rings-s1"
    want = (rings / "private" / "test_y.csv").read_bytes()
    assert m.main(["apply", "--archive", str(world["archive"])]) == 0
    capsys.readouterr()
    assert not (rings / "private").exists()
    assert TL.truth_file(rings, "test_y.csv").read_bytes() == want
    assert TL.truth_file(rings, "canary.txt").exists()
    assert (world["data"] / "private" / "tasks" / "rings-v1" / "test_y.csv").exists()
    assert not (world["scratch"] / "salts-tmp" / "e" / "x1" / "private").exists()  # archived, not copied anywhere
    archived = list(world["archive"].rglob("test_y.csv"))
    assert len(archived) == 4
    manifests = list(world["archive"].glob("TRUTH-MIGRATION-*.manifest.json"))
    assert len(manifests) == 1
    assert b"label\n" not in manifests[0].read_bytes()  # hashes and paths only
    assert m.main(["apply", "--archive", str(world["archive"])]) == 0  # nothing left to do
    assert len(list(world["archive"].glob("TRUTH-MIGRATION-*.manifest.json"))) == 1
    capsys.readouterr()
    assert m.main(["verify", "--archive", str(world["archive"])]) == 0
    assert json.loads(capsys.readouterr().out)["problems"] == []


def test_only_mapped_leaves_scratch_copies_for_the_owner(world, capsys) -> None:
    m = _mod()
    assert m.main(["apply", "--archive", str(world["archive"]), "--only-mapped"]) == 0
    assert (world["scratch"] / "salts-tmp" / "e" / "x1" / "private" / "test_y.csv").exists()
    capsys.readouterr()
    assert m.main(["verify", "--archive", str(world["archive"])]) == 1  # the static scan still sees the copy
    assert any("salts-tmp" in p for p in json.loads(capsys.readouterr().out)["problems"])


def test_archive_inside_an_agent_readable_root_is_refused(world) -> None:
    m = _mod()
    assert m.main(["apply", "--archive", str(world["scratch"] / "arch")]) == 2
    assert m.main(["apply", "--archive", str(world["data"] / "private" / "arch")]) == 2  # overlaps the truth root
    assert (world["data"] / "suites" / "m2" / "rings-s1" / "private").exists()


def test_conflicting_truth_copy_is_left_in_place(world, capsys) -> None:
    m = _mod()
    xor = world["data"] / "suites" / "m2" / "xor-s1"
    dest = world["data"] / "private" / "suites" / "m2" / "xor-s1"
    dest.mkdir(parents=True)
    (dest / "test_y.csv").write_text("label\nOTHER\n", encoding="utf-8")
    assert m.main(["apply", "--archive", str(world["archive"])]) == 1
    assert (xor / "private" / "test_y.csv").exists()  # never archived when its copy conflicts
    assert "DIFFERENT" in capsys.readouterr().out
