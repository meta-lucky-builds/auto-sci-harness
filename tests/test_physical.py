"""The physical / empirical claim path up to the Kernel boundary (GOAL §4.7, invariants 14-15).

draft -> prohibited-category screen -> pre-registration -> HUMAN approval of the exact protocol -> human returns data
(`chh return-data`, attested) -> HUMAN confirms receipt -> returned data checked -> Kernel. Today no physical adapter
exists, so the Kernel refuses at K1 (fail closed). Every human step is a CLI action; the trust boundary is the one
stated in runtime/engine.py (trusted modules are not sandboxed from the store)."""

from pathlib import Path

import pytest
import yaml

from custom_harness.cli import main
from custom_harness.config import data_dir
from custom_harness.kernel import DatasetRole, Kernel, Ledger
from custom_harness.records import RunStatus
from custom_harness.store import EvidenceStore

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "examples" / "physical_v1.yaml"
PROTO = ROOT / "examples" / "physical" / "bracket_protocol.yaml"
GOOD = "specimen_id,arm,breaking_load_n\n" + "".join(
    f"t{i},treatment,{900 + 10 * i}\n" for i in range(8)) + "".join(f"c{i},control,{700 + 10 * i}\n" for i in range(8))


def _store() -> EvidenceStore:
    return EvidenceStore(data_dir() / "store")


def _run(protocol: Path) -> str:
    main(["run", str(SPEC), "--input", f"protocol_file={protocol}", "--task", "bracket"])
    s = _store()
    try:
        return s.list_runs(limit=1)[0].id
    finally:
        s.close()


def _status(run: str) -> tuple[RunStatus, str]:
    s = _store()
    try:
        return s.run_status(run)
    finally:
        s.close()


def _state(run: str) -> dict:
    s = _store()
    try:
        last = s.trace(run)[-1]
        return s.blobs.get_json(last.checkpoint_ref)["state"]
    finally:
        s.close()


def _approve(run: str, node: str, decision: str = "approve") -> None:
    assert main(["approve", run, node, "--decision", decision, "--approver", "alice"]) == 0


def _resume(run: str) -> None:
    main(["resume", run])


def _return(run: str, tmp_path: Path, body: str, name: str = "m.csv") -> int:
    f = tmp_path / name
    f.write_text(body, newline="")
    return main(["return-data", run, "--file", str(f), "--attest", "Bob, materials lab B, 2026-10-07"])


def test_full_human_path_ends_at_the_kernel_which_refuses_today(tmp_path):
    run = _run(PROTO)
    assert _status(run)[0] == RunStatus.PAUSED and "protocol_review" in _status(run)[1]
    with pytest.raises(SystemExit, match="not paused at the data gate"):
        _return(run, tmp_path, GOOD)  # no data before the protocol is approved
    _approve(run, "protocol_review")
    _resume(run)
    assert _status(run)[0] == RunStatus.PAUSED and "data_received" in _status(run)[1]
    with pytest.raises(SystemExit, match="columns"):
        _return(run, tmp_path, "id,arm,load\n1,treatment,5\n", "bad.csv")
    with pytest.raises(SystemExit, match="replicates"):
        _return(run, tmp_path, "specimen_id,arm,breaking_load_n\nt0,treatment,900\nc0,control,700\n", "few.csv")
    assert _return(run, tmp_path, GOOD) == 0
    with pytest.raises(SystemExit, match="already registered"):
        _return(run, tmp_path, GOOD, "again.csv")
    _approve(run, "data_received")
    _resume(run)
    assert _status(run)[0] == RunStatus.COMPLETED, _status(run)
    st = _state(run)
    cert = st["physical_certificate"]
    assert cert["label"] == "REJECTED" and "K1" in cert["failed"]
    assert any("no registered adapter serves evidence class 'physical'" in r for r in cert["k1_reasons"])
    assert any("Bob, materials lab B" in a for a in cert["untested_assumptions"])  # the attestation is untested
    s = _store()
    try:
        ds = Kernel(s).vault.dataset(st["physical_item"]["dataset_id"])
        assert ds.role == DatasetRole.FUTURE and any(f.private for f in ds.files.values())
        assert Ledger(s).entries("certificate") == []  # not recorded: the prediction stays pending, not a "miss"
    finally:
        s.close()


@pytest.mark.parametrize("phrase", ["We recruit human volunteers.", "A Gain-Of-Function step follows.",
                                    "Tests are done in vivo."])
def test_a_prohibited_protocol_never_reaches_approval(tmp_path, phrase):
    doc = yaml.safe_load(PROTO.read_text(encoding="utf-8"))
    doc["procedure"] += "\n" + phrase
    p = tmp_path / "proto.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    run = _run(p)
    assert _status(run)[0] == RunStatus.COMPLETED
    st = _state(run)
    assert st["physical_screen"]["refused"] is True and st["physical_screen"]["categories"]
    assert st["physical_outcome"]["refused"] is True
    s = _store()
    try:
        assert [e for e in Ledger(s).entries("approval_request") if e.body["run_id"] == run] == []
        assert [e for e in Ledger(s).entries("prereg")] == []  # refused before anything is pre-registered
    finally:
        s.close()


def test_a_rejected_protocol_can_never_receive_data(tmp_path):
    run = _run(PROTO)
    _approve(run, "protocol_review", "reject")
    _resume(run)
    assert _status(run)[0] == RunStatus.COMPLETED and _state(run)["physical_outcome"]["refused"] is True
    with pytest.raises(SystemExit, match="not paused at the data gate"):
        _return(run, tmp_path, GOOD)


def test_confirming_receipt_without_returned_data_fails_closed(tmp_path):
    run = _run(PROTO)
    _approve(run, "protocol_review")
    _resume(run)
    _approve(run, "data_received")  # a human confirms, but nothing was ever returned through `chh return-data`
    _resume(run)
    status, detail = _status(run)
    assert status == RunStatus.FAILED and "no returned data" in detail


def test_evolution_cannot_touch_the_physical_path():
    from custom_harness.evolve.edits import Edit, EditOp, EditRejected, EditType, apply_edit
    from custom_harness.runtime import HarnessSpec
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text(encoding="utf-8")))
    edges = spec.model_dump(mode="json")["edges"]
    skip = next(i for i, e in enumerate(edges) if e["source"] == "draft")
    for ops in ([EditOp(op="replace", path="/nodes/screen/params", value={"x": 1})],
                [EditOp(op="replace", path=f"/edges/{skip}/target", value="prereg")],  # route around the screen
                [EditOp(op="remove", path="/nodes/screen")]):
        with pytest.raises(EditRejected):
            apply_edit(spec, Edit(type=EditType.HOP, rationale="x", ops=ops))
