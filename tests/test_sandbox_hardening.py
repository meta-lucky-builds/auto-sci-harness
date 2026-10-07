"""Sandbox hardening (STATUS §D known limitations): bounded output capture, a workspace size limit that is a POLICY
failure of the program (never infrastructure), LocalSandbox confined to test mode with a minimal environment, and no
sandbox workspace/mount overlapping the evidence store, the bundles or a task's private files."""

import json
import sys

import pytest
import yaml

import custom_harness.research  # noqa: F401
from custom_harness.cli import main
from custom_harness.config import data_dir
from custom_harness.errors import InfraError
from custom_harness.gateway import MockBackend
from custom_harness.kernel.execution import DEFAULT_ARGV, run_program
from custom_harness.records import RunStatus
from custom_harness.runtime import END, Condition, Edge, Engine, HarnessSpec, LLMNode, ToolNode
from custom_harness.sandbox import LocalSandbox
from custom_harness.sandbox.docker import DockerSandbox, docker_available
from custom_harness.store import EvidenceStore
from test_research import SPEC, rings_task, scripted_llm  # noqa: F401

MB = 1_000_000

FLOOD = ("import sys; w = sys.stdout.write; w('HEAD-MARK\\n'); chunk = 'x' * 65536 + '\\n'\n"
         "for _ in range(128): w(chunk)\n"
         "w('TAIL-MARK\\n'); sys.stderr.write('E' * 3_000_000 + 'ERR-TAIL')")

FILL = ("import pathlib, time\n"
        "for i in range({n}):\n"
        "    pathlib.Path(f'junk{{i}}.bin').write_bytes(b'x' * 1_000_000)\n"
        "time.sleep({sleep})\n")


def _sb(tmp_path, name="ws"):
    return LocalSandbox(tmp_path / name, i_understand_no_isolation=True)


# ---------------------------------------------------------------- 1. resource exhaustion
def test_output_capture_is_bounded_and_keeps_head_and_tail(tmp_path):
    """~8 MB of stdout and 3 MB of stderr: the host keeps a bounded head + tail of each and records the omission."""
    sb = _sb(tmp_path)
    res = sb.exec([sys.executable, "-c", FLOOD], timeout_s=120)
    assert res.returncode == 0
    assert len(res.stdout) < 2 * MB and len(res.stderr) < 2 * MB, (len(res.stdout), len(res.stderr))
    assert res.stdout.startswith("HEAD-MARK") and res.stdout.rstrip().endswith("TAIL-MARK")
    assert res.stderr.endswith("ERR-TAIL")
    assert res.stdout_omitted > 6 * MB and res.stderr_omitted > MB
    assert "[sandbox]" in res.stdout and "omitted" in res.stdout


def test_workspace_over_limit_after_the_run_is_a_policy_failure(tmp_path):
    sb = _sb(tmp_path)
    sb.max_workspace_bytes = 5 * MB
    res = sb.exec([sys.executable, "-c", FILL.format(n=8, sleep=0)], timeout_s=120)  # 8 MB, exits 0
    assert res.returncode != 0 and res.limit_exceeded and "workspace" in res.limit_exceeded
    assert "[sandbox] killed" in res.stderr and not res.timed_out


def test_workspace_over_limit_is_killed_during_the_run(tmp_path):
    sb = _sb(tmp_path)
    sb.max_workspace_bytes = 5 * MB
    res = sb.exec([sys.executable, "-c", FILL.format(n=10, sleep=60)], timeout_s=120)
    assert res.returncode != 0 and res.limit_exceeded and res.wall_s < 30, res


def test_too_many_workspace_files_is_a_policy_failure(tmp_path):
    sb = _sb(tmp_path)
    sb.max_workspace_files = 50
    res = sb.exec([sys.executable, "-c", "import pathlib\nfor i in range(80): pathlib.Path(f'f{i}').write_text('')"],
                  timeout_s=120)
    assert res.returncode != 0 and res.limit_exceeded and "files" in res.limit_exceeded


def test_over_limit_held_out_program_fails_as_the_program_never_as_infra(tmp_path):
    """Kernel execution path: an over-limit program yields no predictions (its failure), no exception."""
    sb = _sb(tmp_path)
    sb.max_workspace_bytes = 5 * MB
    code = FILL.format(n=8, sleep=0) + "open('out/preds.csv','w').write('prediction\\n1\\n')\n"
    preds, diag = run_program(sb, code=code.encode(), entrypoint="solution.py", argv=DEFAULT_ARGV,
                              inputs={"train": ("train.csv", b"a,label\n1,1\n"), "test": ("test.csv", b"a\n1\n")},
                              seed=0, timeout_s=120)
    assert preds is None and "rc=" in diag


def test_tool_node_over_limit_fails_the_step_as_policy_not_infra(store, tmp_path):
    sb = _sb(tmp_path)
    sb.max_workspace_bytes = 5 * MB
    spec = HarnessSpec(id="fill", entry="t",
                       nodes={"t": ToolNode(command=[sys.executable, "-c", FILL.format(n=8, sleep=0)], output_key="r"),
                              "recover": LLMNode(prompt="recover", output_key="x")},
                       edges=[Edge(source="t", target="recover", when=Condition(key="_last_error", op="truthy")),
                              Edge(source="t", target=END)])
    res = Engine(spec, backends={"executor": MockBackend(["ok"])}, store=store, sandbox=sb).run("t", {})
    assert res.status == RunStatus.COMPLETED and res.state.get("x") == "ok"  # routed through the error edge
    assert "workspace" in (store.trace(res.run_id)[0].error or "")


@pytest.mark.skipif(not docker_available(), reason="Docker daemon not running")
def test_docker_limits_are_policy_failures(tmp_path):
    """Real Docker: bounded output, per-file size cap (RLIMIT_FSIZE) and the workspace size limit; none of them
    raises InfraError (the agent cannot turn its own failure into an excluded unit)."""
    sb = DockerSandbox(tmp_path / "ws", max_workspace_bytes=20 * MB, max_file_bytes=10 * MB)
    try:
        flood = sb.exec(["python", "-c", FLOOD], timeout_s=180)
        assert flood.returncode == 0 and len(flood.stdout) < 2 * MB and flood.stdout_omitted > 6 * MB
        assert flood.stdout.startswith("HEAD-MARK") and flood.stderr.endswith("ERR-TAIL")
        big = sb.exec(["python", "-c", "open('big.bin','wb').write(b'x' * 30_000_000)"], timeout_s=180)
        assert big.returncode != 0 and not (tmp_path / "ws" / "big.bin").stat().st_size > 10 * MB
        fill = sb.exec(["python", "-c", FILL.format(n=40, sleep=60)], timeout_s=180)
        assert fill.returncode != 0 and fill.limit_exceeded and fill.wall_s < 60, fill
    except InfraError as e:  # pragma: no cover - this is exactly the failure the test exists to catch
        pytest.fail(f"a resource limit surfaced as infrastructure: {e}")


# ---------------------------------------------------------------- 2. LocalSandbox is test-only and minimal
def test_local_sandbox_refuses_to_run_outside_test_mode(tmp_path, monkeypatch):
    sb = _sb(tmp_path)
    monkeypatch.delenv("CHH_ALLOW_UNISOLATED", raising=False)
    with pytest.raises(PermissionError, match="CHH_ALLOW_UNISOLATED"):
        sb.exec([sys.executable, "-c", "print('ran on the host')"])
    with pytest.raises(PermissionError, match="CHH_ALLOW_UNISOLATED"):
        sb.spawn(tmp_path / "other").exec([sys.executable, "-c", "pass"])


def test_local_sandbox_environment_is_minimal(tmp_path, monkeypatch):
    """No credentials and no pointer to the real home (where e.g. the Codex CLI keeps its login) or temp dir."""
    import os
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secretsecretsecretsecret")
    monkeypatch.setenv("CHH_SOME_TOKEN", "tok-secretsecretsecret")
    res = _sb(tmp_path).exec([sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"])
    env = json.loads(res.stdout)
    assert not any("secret" in v for v in env.values())
    real = {os.path.normcase(v) for k, v in os.environ.items() if k in ("HOME", "USERPROFILE", "TEMP", "TMP") and v}
    seen = {os.path.normcase(env[k]) for k in ("HOME", "USERPROFILE", "TEMP", "TMP") if k in env}
    assert not (real & seen), seen
    assert not set(env) - {"PATH", "SYSTEMROOT", "PATHEXT", "COMSPEC", "LANG", "HOME", "USERPROFILE", "TEMP", "TMP",
                           "TMPDIR", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE"}, sorted(env)


@pytest.mark.parametrize("argv", [
    ["run", str(SPEC), "--dev-sandbox"],
    ["resume", "run_doesnotexist", "--dev-sandbox"],
    ["evolve", str(SPEC), "--suite", "nosuite.yaml", "--proposer", "mock", "--dev-sandbox"],
    ["rerun", "nobundle.zip", "--dev-sandbox"],
    ["reproduce", "nobundle.zip", "--dev-sandbox"],
])
def test_cli_cannot_select_the_unisolated_sandbox_outside_test_mode(argv, monkeypatch):
    monkeypatch.delenv("CHH_ALLOW_UNISOLATED", raising=False)
    with pytest.raises(SystemExit, match="CHH_ALLOW_UNISOLATED"):
        main(argv)


# ---------------------------------------------------------------- 8. no protected path inside a sandbox mount
def test_sandbox_refuses_a_workspace_overlapping_the_store_or_bundles(tmp_path):
    store = EvidenceStore(tmp_path / "evidence")
    try:
        for ws in (tmp_path / "evidence" / "ws", tmp_path / "evidence" / "blobs", tmp_path,
                   data_dir() / "bundles" / "x", data_dir(), data_dir() / "store"):
            with pytest.raises(PermissionError, match="protected"):
                LocalSandbox(ws, i_understand_no_isolation=True)
            with pytest.raises(PermissionError, match="protected"):  # checked before Docker is even contacted
                DockerSandbox(ws)
        ok = _sb(tmp_path / "elsewhere")
        with pytest.raises(PermissionError, match="protected"):
            ok.spawn(tmp_path / "evidence" / "fold0")
    finally:
        store.close()


def test_run_refuses_a_workspace_holding_the_tasks_private_files(store, rings_task, monkeypatch):  # noqa: F811
    """The run workspace (the agent's mount) must not contain the task's private/ directory (hidden labels)."""
    llm = MockBackend(scripted_llm)
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    res = Engine(spec, backends=dict.fromkeys(("proposer", "executor", "judge", "replicator"), llm), store=store,
                 sandbox=LocalSandbox(rings_task, i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.FAILED and "protected" in res.detail, res.detail
    assert llm.requests == []
