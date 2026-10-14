"""scripts/scan_transcripts.py (s4-integrity item 8) on a toy suite and toy transcripts. Fail-first reasoning (NOT
run: owner rule): at 82719e1 the script did not exist (every test fails on import). No real transcript is read:
roots and suite live in tmp_path.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location("scan_transcripts", ROOT / "scripts" / "scan_transcripts.py")
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CHH_AGENT_READABLE", str(tmp_path / "agent"))
    suite = tmp_path / "suite"  # outside every agent-readable root: legacy in-task private/ is allowed here
    t = suite / "rings-s1"
    (t / "public").mkdir(parents=True)
    (t / "private").mkdir()
    (t / "public" / "train.csv").write_text("x1,x2,label\n0.5,0.25,1\n", encoding="utf-8")
    (t / "public" / "test_X.csv").write_text("x1,x2\n1.23457,-2.5\n3.75,4.125\n", encoding="utf-8")
    (t / "private" / "test_y.csv").write_text("label\n2\n0\n", encoding="utf-8")
    (t / "private" / "canary.txt").write_text("CHH-CANARY-abc\n", encoding="utf-8")
    (t / "task.yaml").write_text("id: rings-s1\ntarget_column: label\n", encoding="utf-8")
    (suite / "suite.yaml").write_text("tasks:\n- id: rings-s1\n", encoding="utf-8")
    agent = tmp_path / "agent"
    (agent / "s1" / "tasks").mkdir(parents=True)
    # a JSON transcript line whose text holds a labelled held-out row in another format (escaped newlines, tabs)
    (agent / "s1" / "tasks" / "a.output").write_text(json.dumps({"text": "x\n1.234570\t-2.50\t2\nend"}),
                                                     encoding="utf-8")
    (agent / "s1" / "tasks" / "b.output").write_text("features 3.75,4.125 seen", encoding="utf-8")
    (agent / "s1" / "notes.txt").write_text("nothing here 1,2,3", encoding="utf-8")
    (agent / "s1" / "c.txt").write_text("CHH-CANARY-abc", encoding="utf-8")
    return {"suite": suite, "agent": agent, "out": tmp_path / "private-reports" / "scan.json"}


def test_lists_labelled_and_canary_files_only_with_paths(world) -> None:
    m = _mod()
    rc = m.main([str(world["suite"]), "--out", str(world["out"]), "--root", str(world["agent"]), "--features"])
    assert rc == 1  # something labelled was found
    rep = json.loads(world["out"].read_text(encoding="utf-8"))
    labelled = {Path(p).name for p in rep["files_with_labelled_rows_or_canary"]}
    assert labelled == {"a.output", "c.txt"}
    assert {Path(p).name for p in rep["files_with_features_only"]} == {"b.output"}
    text = world["out"].read_text(encoding="utf-8")
    assert "4.125" not in text and "1.23457" not in text  # never a feature value or label in the report


def test_report_inside_an_agent_readable_root_is_refused(world) -> None:
    m = _mod()
    assert m.main([str(world["suite"]), "--out", str(world["agent"] / "r.json"), "--root", str(world["agent"])]) == 2
