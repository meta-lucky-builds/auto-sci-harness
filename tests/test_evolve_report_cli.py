"""`chh evolve report CAMPAIGN` and the generation-ledger gaps b-evolve listed (GOAL §5 L6.10; evolve/generations.py,
evolve/driver.py, cli.py).

- the CLI renders every generation's report from the ledger, runs the recompute audit and exits nonzero when a
  ledger record or a cell does not reproduce;
- the CLI evolve command writes a generation ledger (it passes its campaign id);
- on resume, `out_dir` files and logged generation numbers use the campaign-global index, not the loop index;
- a generation stopped by BudgetExceeded gets a `generation_stopped` record with its stop reason.

Table-driven worlds from tests/test_evolve_generations.py (no harness, no model).
"""

from __future__ import annotations

import json

import pytest
import yaml

from custom_harness.budget import BudgetExceeded
from test_evolve import seed_spec
from test_evolve_generations import DECLINE, TableEvaluator, _ledger, drive  # noqa: F401  (fixture re-export)


def _cli(store, tmp_path, monkeypatch, *argv):
    """Run `chh` on the fixture's store (conftest: tmp_path/store = data_dir()/store)."""
    from custom_harness.cli import main
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path))
    assert (tmp_path / "store").resolve() == store.root.resolve()
    return main(list(argv))


# ============================================================================== the report CLI
def test_cli_renders_every_generation_and_its_audit_passes(drive, store, tmp_path, monkeypatch, capsys):  # noqa: F811
    drive(2, campaign="c1")
    assert _cli(store, tmp_path, monkeypatch, "evolve", "report", "c1") == 0
    out = capsys.readouterr().out
    assert "# Generation report: campaign c1, generation 0" in out
    assert "# Generation report: campaign c1, generation 1" in out
    assert "audit: OK (2 generation report(s), 0 stopped record(s)" in out
    assert _cli(store, tmp_path, monkeypatch, "evolve", "report", "c1", "--json") == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["audit"] == {"passed": True, "problems": {}} and len(doc["reports"]) == 2
    from custom_harness.evolve import generations as G
    assert all(G.audit_report(r, store) == [] for r in doc["reports"])  # the printed reports re-audit clean
    assert _cli(store, tmp_path, monkeypatch, "evolve", "report", "c1", "--generation", "1", "--json") == 0
    assert [r["facts"]["generation"]["value"] for r in json.loads(capsys.readouterr().out)["reports"]] == [1]


def test_cli_exits_nonzero_when_the_ledger_does_not_reproduce(drive, store, tmp_path, monkeypatch, capsys):  # noqa: F811
    drive(1, proposal=DECLINE, campaign="c1")
    assert _cli(store, tmp_path, monkeypatch, "evolve", "report", "nope") == 2  # unknown campaign
    # a forged next-generation record: copied refs, a budget the declared schedule never gives, a stall it never had
    forged = json.loads(json.dumps(_ledger(store)[0]))
    forged.update(generation=1, budget=1)
    forged["stall"]["stalled"] = True
    store.append_record("generation_ledger", forged)
    capsys.readouterr()
    assert _cli(store, tmp_path, monkeypatch, "evolve", "report", "c1") == 1
    assert "does not reproduce" in capsys.readouterr().err


def test_cli_exits_nonzero_on_a_forged_stopped_record(drive, store, tmp_path, monkeypatch, capsys):  # noqa: F811
    drive(1, proposal=DECLINE, campaign="c1")
    from custom_harness.evolve import generations as G
    led = _ledger(store)[0]
    store.append_record(G.STOPPED_KIND, {
        "format": G.FORMAT, "campaign": "c1", "generation": 7, "parent": led["parent"],
        "schedule_hash": led["schedule_hash"], "budget": 3, "reason": "budget_exceeded", "detail": "x",
        "refs": {"generation_stats": [0, 0], "lineage": [0, 0], "harness_edit": [0, 0]}})
    capsys.readouterr()
    assert _cli(store, tmp_path, monkeypatch, "evolve", "report", "c1") == 1
    assert "never the next one to run" in capsys.readouterr().err


def test_cli_evolve_writes_a_ledger_under_its_campaign_id(store, tmp_path, monkeypatch, capsys):
    """`chh evolve` passes its campaign id to the driver (ledger, durable units and proposals), so that
    `chh evolve report` has something to report and a re-run with --campaign-id resumes."""
    from custom_harness.evolve import driver
    seen: dict = {}
    monkeypatch.setattr(driver, "load_suite", lambda p: [])
    monkeypatch.setattr(driver, "evolve", lambda *a, **kw: seen.update(kw))
    spec = tmp_path / "spec.yaml"
    spec.write_text(yaml.safe_dump(seed_spec().model_dump(mode="json")), encoding="utf-8")
    from custom_harness.sandbox.local import TEST_MODE_ENV
    monkeypatch.setenv(TEST_MODE_ENV, "1")  # --dev-sandbox is refused outside trusted test code
    assert _cli(store, tmp_path, monkeypatch, "evolve", str(spec), "--suite", "s.yaml", "--proposer", "mock",
                "--dev-sandbox", "--campaign-id", "camp-7") == 0
    assert seen["campaign"] == "camp-7" and seen["out_dir"] == tmp_path / "evolve" / "camp-7"
    assert "chh evolve report camp-7" in capsys.readouterr().out


# ============================================================================== resume: global generation index
def test_resume_writes_out_dir_files_under_the_campaign_global_index(drive, store, tmp_path):  # noqa: F811
    out = tmp_path / "out"
    drive(1, campaign="c1", out_dir=out)
    drive(2, campaign="c1", out_dir=out)  # resumed: generations 1 and 2 of the campaign
    assert [r["generation"] for r in _ledger(store)] == [0, 1, 2]
    assert sorted(p.name for p in out.glob("gen*.json")) == ["gen000.json", "gen001.json", "gen002.json"]
    for g in (0, 1, 2):
        summary = json.loads((out / f"gen{g:03d}.json").read_text(encoding="utf-8"))
        assert summary["generation"] == g
        assert summary["new_parent"] == _ledger(store)[g]["new_parent"][:12]
        assert (out / f"spec_gen{g:03d}.yaml").exists()


def test_a_resumed_campaign_continues_from_its_last_new_parent_and_refuses_another_seed(drive, store):  # noqa: F811
    from custom_harness.evolve import generations as G
    drive(1, campaign="c1")  # the table world accepts max_fixes 0 -> 2
    led = _ledger(store)
    assert led[0]["signal"]["accepted"] is True and led[0]["new_parent"] != led[0]["parent"]
    drive(1, campaign="c1")
    assert _ledger(store)[1]["parent"] == led[0]["new_parent"]  # not the seed spec again
    other = seed_spec().model_copy(update={"description": "another seed"})
    assert other.spec_hash() != seed_spec().spec_hash()
    from custom_harness.evolve import EvolveConfig, driver
    with pytest.raises(G.LedgerError, match="seed spec"):
        driver.evolve(other, [], generations=1, backends={}, proposer=None, store=store,  # type: ignore[arg-type]
                      sandbox_factory=lambda k: None, campaign="c1", log=lambda s: None,
                      cfg=EvolveConfig(seeds=(0, 1), n_edits=3, race_rounds=1))


# ============================================================================== BudgetExceeded gets a record
class _BudgetTable(TableEvaluator):
    """The campaign budget runs out on the `limit`-th harness run (mid-generation)."""

    limit = 10**9

    def outcome(self, spec, unit):
        key = (spec.spec_hash(), unit[0], unit[1])
        if key not in self.cache and self.calls >= self.limit:
            raise BudgetExceeded("campaign budget exhausted: tokens 1000.0 >= cap 1000.0")
        return super().outcome(spec, unit)


def test_a_generation_stopped_by_the_budget_gets_a_ledger_record_with_its_reason(drive, store, monkeypatch):  # noqa: F811
    from custom_harness.evolve import driver
    from custom_harness.evolve import generations as G
    drive(1, campaign="c1")
    _BudgetTable.limit = 0  # the next generation stops at its first new unit (a fresh evaluator: empty cache)
    monkeypatch.setattr(driver, "Evaluator", _BudgetTable)
    drive(2, campaign="c1")
    assert [r["generation"] for r in _ledger(store)] == [0]  # no completed generation was added
    stopped = G.stopped_records(store, "c1")
    assert len(stopped) == 1
    n, rec = stopped[0]
    assert rec["generation"] == 1 and rec["reason"] == "budget_exceeded" and "exhausted" in rec["detail"]
    assert rec["parent"] == _ledger(store)[0]["new_parent"] and rec["budget"] == _ledger(store)[0]["budget"]
    assert G.verify_stopped(store, n) == []
    # the stop does not consume the index: the resumed campaign completes generation 1
    monkeypatch.setattr(driver, "Evaluator", TableEvaluator)
    drive(1, campaign="c1")
    assert [r["generation"] for r in _ledger(store)] == [0, 1]
    assert G.verify_stopped(store, n) == []  # still consistent: same index, same parent as the completed one
