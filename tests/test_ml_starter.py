"""The ML starter kit does what the tutorial says, through the real Lab and Court code. No model, no real Docker."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _experience_world import World
from custom_harness.experience import schemas as M
from custom_harness.lab import session as S
from test_sealed_project import BEACON

KIT = Path(__file__).resolve().parents[1] / "examples" / "ml_starter"


@pytest.fixture
def kit(tmp_path):
    copy = tmp_path / "kit"
    shutil.copytree(KIT, copy)
    subprocess.run([sys.executable, str(copy / "make_data.py")], check=True, capture_output=True)
    return copy


def test_the_quick_look_runs_with_nothing_but_python_and_the_method_wins(kit):
    out = subprocess.run([sys.executable, "-I", str(kit / "try_it.py")], check=True, capture_output=True,
                         text=True).stdout
    scores = {x.split()[0]: float(x.split()[-1]) for x in out.splitlines() if " accuracy " in x}
    assert scores["method"] > scores["baseline"] + 0.1 and "Only the Court" in out


def test_the_data_is_the_same_on_every_run(kit):
    first = (kit / "data.csv").read_bytes()
    subprocess.run([sys.executable, str(kit / "make_data.py")], check=True, capture_output=True)
    assert (kit / "data.csv").read_bytes() == first


def test_the_kit_freezes_rehearses_and_previews_through_the_real_code(kit, tmp_path):
    w = World(tmp_path / "w")
    try:
        w.data.write_bytes((kit / "data.csv").read_bytes())
        w.x.seal(M.SealIn(project="ml-starter", data_file=str(w.data), label_column="label", rows=400, parts=1,
                          beacon_round="r1", beacon_value=BEACON, provenance="private"))
        w.x.start(M.StartIn(agent="opencode", project="ml-starter", session_id="s-kit", model="m",
                            local_model_server=True))
        ws = S.workspace_of("s-kit")
        for name in ("method.py", "baseline.py", "requirements.lock"):
            shutil.copy2(kit / name, ws / name)
        (ws / ".lab").mkdir(exist_ok=True)
        (ws / ".lab" / "trace.jsonl").write_text(
            '{"kind": "idea", "client_id": "1", "payload": {"op": "propose", "idea_id": "radius", "title": "Radius"}}',
            encoding="utf-8")
        pid = w.x.freeze(M.FreezeIn(session_id="s-kit", request_file=str(kit / "promote.yaml"))).promotion_id
        report = w.x.rehearse(M.RehearseIn(promotion_id=pid, rows=300))
        assert report.ran and report.would_certify is True, report.reason
        preview = w.x.promote_preview(M.PromoteIn(promotion_id=pid, route="sealed", project="ml-starter"))
        assert preview.finding_possible and preview.looks_remaining.value == 1
    finally:
        w.close()
