"""GOAL §5 L0 "Model gateway": per-host pacing and cost tables WIRED into the real call paths and into every run's
cost record (invariant 9). No network: httpx.MockTransport and a faked subprocess stand in for the providers.

- pacing: every network backend takes a slot from a host pacer before EVERY call (fake clock, no real sleeping);
  a static check makes any new network-capable Backend subclass that does not pace fail;
- cost record: usd is a number only when every rate is a real (calibrated) one; a missing cost-table row is shown as
  unknown and placeholder rows as UNCALIBRATED, never as 0 (AI-Sci's "missing = 0" pitfall), in the run bundle, the
  CLI output and the scorecard's axis G;
- a --max-usd cap that no backend can price is refused instead of silently never triggering;
- every `chh run` / `chh rerun` gets a new directory of its own (GOAL L0), even within the same second.
"""

import ast
import json
import subprocess
import time
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
import yaml

from custom_harness.gateway import ChatRequest, Message, MockBackend, codex_cli
from custom_harness.gateway.costs import Rate
from custom_harness.gateway.openai_compat import OpenAICompatBackend
from custom_harness.gateway.pacing import HostPacer
from custom_harness.records import AccessMode, Cost, Provenance, StepKind, TraceStep

SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness"
REQ = ChatRequest(messages=[Message(role="user", content="hello there")])


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


# -------------------------------------------------------------------------------------------- pacing, wired
def _openai(pacer, base_url, starts, clk):
    def handler(request: httpx.Request) -> httpx.Response:
        starts.append((request.url.host, clk()))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    return OpenAICompatBackend(name="t", base_url=base_url, model="m", mode=AccessMode.API_KEY, pacer=pacer,
                               transport=httpx.MockTransport(handler))


def test_openai_compat_calls_are_paced_per_host_under_a_fake_clock():
    clk = FakeClock()
    pacer = HostPacer({"api.example.com": 2.0}, default_s=0.5, clock=clk, sleep=clk.sleep)
    starts: list[tuple[str, float]] = []
    a = _openai(pacer, "https://api.example.com/v1", starts, clk)
    b = _openai(pacer, "https://API.example.com/v1", starts, clk)  # a second backend, same host: shares slots
    other = _openai(pacer, "http://localhost:11434/v1", starts, clk)
    for be in (a, b, a):
        be.complete(REQ)
    other.complete(REQ)
    other.complete(REQ)
    ex = [t for h, t in starts if h == "api.example.com"]
    lo = [t for h, t in starts if h == "localhost"]
    assert len(ex) == 3 and len(lo) == 2
    assert [round(y - x, 9) for x, y in pairwise(ex)] == [2.0, 2.0]  # the host's interval, honoured
    assert lo[1] - lo[0] == pytest.approx(0.5)  # default interval for a host without an entry
    assert clk.slept == pytest.approx([2.0, 2.0, 0.5])  # the localhost calls never waited for api.example.com
    assert a.paced_wait_s == pytest.approx(2.0) and b.paced_wait_s == pytest.approx(2.0)


def test_codex_calls_are_paced_including_the_schema_fallback_recall(monkeypatch):
    clk = FakeClock()
    pacer = HostPacer({codex_cli.CODEX_PACING_HOST: 5.0}, clock=clk, sleep=clk.sleep)
    starts: list[float] = []

    def fake_run(cmd, **kw):
        starts.append(clk())
        if "--output-schema" in cmd:  # the provider rejects the schema: the backend re-calls without it
            err = json.dumps({"type": "error", "message": "invalid_json_schema"})
            return subprocess.CompletedProcess(cmd, 0, stdout=err + "\n", stderr="")
        Path(cmd[cmd.index("-o") + 1]).write_text("fine", encoding="utf-8")
        turn = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
        return subprocess.CompletedProcess(cmd, 0, stdout=turn + "\n", stderr="")
    monkeypatch.setattr(codex_cli.shutil, "which", lambda exe: "C:/fake/bin/codex.exe")
    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)
    b = codex_cli.CodexCLIBackend(pacer=pacer)
    b.complete(REQ)
    b.complete(REQ.model_copy(update={"json_schema": {"type": "object", "properties": {}}}))
    assert [round(y - x, 9) for x, y in pairwise(starts)] == [5.0, 5.0]  # 3 process starts
    assert clk.slept == pytest.approx([5.0, 5.0])


def test_backends_without_their_own_pacer_share_the_process_pacer(monkeypatch):
    monkeypatch.setattr(codex_cli.shutil, "which", lambda exe: "C:/fake/bin/codex.exe")
    a = OpenAICompatBackend(name="a", base_url="http://x", model="m", mode=AccessMode.API_KEY)
    b = OpenAICompatBackend(name="b", base_url="http://x", model="m", mode=AccessMode.API_KEY)
    c = codex_cli.CodexCLIBackend()
    from custom_harness.gateway.pacing import shared_pacer
    assert a._pacer is b._pacer is c._pacer is shared_pacer()


# Every Backend subclass in src, classified. A new subclass fails here until it is classified; a class that can reach
# the network (its module imports httpx / subprocess / requests / socket / urllib.request) must pace every call.
PACED = {"CodexCLIBackend", "OpenAICompatBackend"}
NO_NETWORK = {  # wrappers (their inner backend paces) and backends that make no external call
    "MockBackend", "PacedBackend", "CostedBackend", "_JournalingBackend",
    "gateway/cassette.py:RecordingBackend", "gateway/cassette.py:ReplayBackend",
    "kerneld/campaign.py:RecordingBackend", "kerneld/campaign.py:ReplayBackend",
}
_NET = ("httpx", "subprocess", "requests", "socket", "urllib.request", "aiohttp")


def _backend_classes() -> list[tuple[str, ast.ClassDef, bool]]:
    trees = {p: ast.parse(p.read_text(encoding="utf-8")) for p in SRC.rglob("*.py")}
    names = {"Backend"}
    found: dict[tuple[Path, str], ast.ClassDef] = {}
    changed = True
    while changed:  # transitive: subclasses of subclasses (by name)
        changed = False
        for p, tree in trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and (p, node.name) not in found and any(
                        (isinstance(b, ast.Name) and b.id in names)
                        or (isinstance(b, ast.Attribute) and b.attr in names) for b in node.bases):
                    if p.relative_to(SRC).as_posix() == "gateway/base.py" and node.name == "Backend":
                        continue
                    found[(p, node.name)] = node
                    names.add(node.name)
                    changed = True
    out = []
    for (p, name), node in found.items():
        mods = {a.name for n in ast.walk(trees[p]) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module or "" for n in ast.walk(trees[p]) if isinstance(n, ast.ImportFrom)}
        out.append((f"{p.relative_to(SRC).as_posix()}:{name}", node, any(m in _NET for m in mods)))
    return out


def test_every_backend_subclass_is_classified_and_network_backends_pace():
    classes = _backend_classes()
    assert len(classes) >= 10, "the scan found too few Backend subclasses: it is not scanning"
    for key, node, net in classes:
        name = key.split(":")[1]
        if name in PACED:
            src = ast.unparse(node)
            assert "_pacer.acquire(" in src, f"{key} reaches the network but takes no pacing slot"
        elif name in NO_NETWORK or key in NO_NETWORK:
            # a wrapper defined next to subprocess use (the engine) is allowed only if it never spawns itself
            assert "subprocess" not in ast.unparse(node) and "httpx" not in ast.unparse(node), key
        else:
            pytest.fail(f"unclassified Backend subclass {key} (network-capable module: {net}): classify it in "
                        f"this test; a network backend must pace every call")


# -------------------------------------------------------------------------------------------- cost records
# new names are imported inside each test, so on the parent commit each test fails on its own (fail-first evidence)
def _c():
    from custom_harness.gateway import costs
    return costs


def _step(i, cost, provider=None, model="m"):
    prov = None if provider is None else Provenance(backend="b", provider=provider, model=model,
                                                    mode=AccessMode.API_KEY, training_on_outputs="forbidden")
    return TraceStep(run_id="r", index=i, node_id=f"n{i}", kind=StepKind.LLM, input={}, output={}, provenance=prov,
                     cost=cost, started_at=0.0, ended_at=0.0)


REAL = Rate(quota_per_call=0.0, usd_per_mtok_in=2.0, usd_per_mtok_out=8.0, placeholder=False,
            source="owner, test fixture")
TABLE = {("calib", "*"): REAL, ("ph", "*"): Rate(quota_per_call=1.0)}


def test_missing_cost_table_row_is_unknown_never_zero():
    rec = _c().cost_record([_step(0, Cost(prompt_tokens=10, completion_tokens=5), "calib"),
                       _step(1, Cost(prompt_tokens=10, completion_tokens=5), "nobody")], TABLE)
    assert rec["usd"] is None and rec["usd_status"] == _c().USD_UNKNOWN
    assert any("'nobody'" in m for m in rec["missing_rates"])
    text = _c().usd_text(rec)
    assert "unknown" in text and "$0" not in text and "0.0" not in text


def test_model_spend_without_provenance_is_unknown():
    rec = _c().cost_record([_step(0, Cost(prompt_tokens=100, usd=0.5))], TABLE)
    assert rec["usd"] is None and rec["usd_status"] == _c().USD_UNKNOWN
    assert rec["backend_reported_usd"] == 0.5  # kept apart, never presented as the cost


def test_placeholder_rows_are_uncalibrated_never_a_price():
    rec = _c().cost_record([_step(0, Cost(prompt_tokens=10, completion_tokens=5, quota_units=1.0), "ph")], TABLE)
    assert rec["usd"] is None and rec["usd_status"] == _c().USD_UNCALIBRATED
    assert rec["calibration"] == _c().USD_UNCALIBRATED
    assert "UNCALIBRATED" in _c().usd_text(rec)
    # the bundled table: every row is a placeholder today, so a mock / openai run is UNCALIBRATED
    rec2 = _c().cost_record([_step(0, Cost(prompt_tokens=10), "openai"), _step(1, Cost(prompt_tokens=1), "mock")])
    assert rec2["usd"] is None and rec2["usd_status"] == _c().USD_UNCALIBRATED


def test_calibrated_rows_price_tokens_and_no_spend_is_zero():
    big = Cost(prompt_tokens=1_000_000, completion_tokens=500_000, usd=99.0)
    rec = _c().cost_record([_step(0, big, "calib")], TABLE)
    assert rec["usd_status"] == _c().USD_CALIBRATED and rec["usd"] == pytest.approx(2.0 + 4.0)
    assert _c().usd_text(rec) == "$6.0000"
    none = _c().cost_record([_step(0, Cost(wall_s=3.0))], TABLE)  # a module step with no model spend
    assert none["usd"] == 0.0 and none["usd_status"] == _c().USD_NONE
    assert _c().combine_usd([rec, none]) == (pytest.approx(6.0), _c().USD_CALIBRATED)
    assert _c().combine_usd([rec, {"usd_status": _c().USD_UNCALIBRATED}]) == (None, _c().USD_UNCALIBRATED)
    assert _c().combine_usd([rec, {}]) == (None, _c().USD_UNKNOWN)  # a run without a record: unknown, not 0


def _spec_file(tmp_path: Path) -> Path:
    from custom_harness.runtime import HarnessSpec, LLMNode
    spec = HarnessSpec(id="w", entry="x", nodes={"x": LLMNode(prompt="say something", output_key="x")}, edges=[])
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.safe_dump(spec.model_dump(mode="json")), encoding="utf-8")
    return p


def test_every_run_bundle_and_cli_run_report_carry_the_priced_cost_record(tmp_path, capsys, monkeypatch):
    from custom_harness import cli
    from custom_harness.config import data_dir
    from custom_harness.store import EvidenceStore
    from custom_harness.store.run_bundle import build_run_bundle, read_run_bundle
    assert cli.main(["run", str(_spec_file(tmp_path)), "--role", "executor=mock", "--task", "t",
                     "--allow-same-family"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cost"]["usd"] is None and out["cost"]["usd_status"] == _c().USD_UNCALIBRATED  # not 0.0
    assert out["cost_record"]["usd_status"] == _c().USD_UNCALIBRATED and out["cost_record"]["tokens"] > 0
    store = EvidenceStore(data_dir() / "store")
    try:
        build_run_bundle(store, out["run_id"], tmp_path / "b.zip", environment=None, workspace_ref=None)
    finally:
        store.close()
    m = read_run_bundle(tmp_path / "b.zip")
    assert m["cost_record"]["usd"] is None and m["cost_record"]["usd_status"] == _c().USD_UNCALIBRATED
    assert m["cost_record"]["by_model"][0]["rate"] == _c().USD_UNCALIBRATED


def test_scorecard_axis_g_never_shows_an_unpriced_usd_as_a_number():
    from custom_harness.scorecard.axes import axis_g

    def run(ref, rec):
        return {"ref": ref, "cost": {"usd": 0.0, "gpu_hours": 0.0, "quota_units": 1.0, "tokens": 15,
                                     "estimated": True}, "cost_record": rec}
    sub = _c().cost_record([_step(0, Cost(prompt_tokens=10, completion_tokens=5, quota_units=1.0), "openai")])
    g = axis_g({"runs": [run("run:a", sub)], "certificates": []})
    assert g["total_cost"]["value"]["usd"] is None and g["total_cost"]["usd_status"] == _c().USD_UNCALIBRATED
    assert g["total_cost"]["value"]["tokens"] == 15
    g2 = axis_g({"runs": [run("run:a", sub), {**run("run:b", {}), "cost_record": None}], "certificates": []})
    assert g2["total_cost"]["value"]["usd"] is None and g2["total_cost"]["usd_status"] == _c().USD_UNKNOWN


def test_a_usd_cap_no_backend_can_price_is_refused(monkeypatch):
    from custom_harness import cli
    from custom_harness.gateway import costs
    with pytest.raises(SystemExit, match="UNCALIBRATED placeholder"):
        cli._refuse_unpriced_usd_cap([MockBackend(["x"])])
    monkeypatch.setitem(costs.COST_TABLE, ("mock", "*"), REAL)
    cli._refuse_unpriced_usd_cap([MockBackend(["x"])])  # a calibrated row: the cap can be enforced


# -------------------------------------------------------------------------------------------- own directory per run
def test_two_cli_runs_in_the_same_second_never_share_a_workspace(tmp_path, capsys, monkeypatch):
    from custom_harness import cli
    from custom_harness.config import data_dir
    from custom_harness.store import EvidenceStore
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)  # both runs start in the same second
    spec = str(_spec_file(tmp_path))
    ids = []
    for _ in range(2):
        assert cli.main(["run", spec, "--role", "executor=mock", "--task", "t", "--dev-sandbox",
                         "--allow-same-family"]) == 0
        ids.append(json.loads(capsys.readouterr().out)["run_id"])
    store = EvidenceStore(data_dir() / "store")
    try:
        ws = [store.get_run(i).workspace for i in ids]
    finally:
        store.close()
    assert ws[0] and ws[1] and ws[0] != ws[1], ws
