"""kerneld: the Kernel process boundary (GOAL §16.5.2). Real subprocess; every attack test checks that the attempt
was made and refused with its exact closed code, and that no private read was logged for it."""

from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from custom_harness.kerneld import client as client_mod
from custom_harness.kerneld.client import KernelD, KernelDError, KernelDUnavailable
from custom_harness.kerneld.releases import released
from custom_harness.verify import metrics as M
from custom_harness.verify.leak import label_windows
from kernel_world import World


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    w = World(tmp_path_factory.mktemp("kerneld"), seed=11, n_train=60, n_test=60)
    claim = w.claim(w.artifact("good"), w.artifact("baseline"))
    w.kernel.preregister(claim)
    w.claim_hash = claim.claim_hash()  # type: ignore[attr-defined]
    w.unregistered = w.claim(w.artifact("good"), w.artifact("baseline"), delta=0.3).claim_hash()  # type: ignore
    yield w
    w.close()


@pytest.fixture(scope="module")
def kd(world):
    with KernelD(world.root / "store") as k:
        yield k


_RUNS = iter(range(10**6))


@pytest.fixture
def run(kd):
    r = kd.open_run(f"run-test-{next(_RUNS)}")  # one run id per test: the leak-scan budget is per run id
    yield r
    try:
        r.close()
    except KernelDError:
        pass


def _accesses(world) -> int:
    return len(world.vault.accesses())


def _refused(world, code: str, fn, *args, **kw) -> None:
    """The attempt is made, refused with exactly `code`, and leaves no data_access entry."""
    before = _accesses(world)
    with pytest.raises(KernelDError) as ei:
        fn(*args, **kw)
    assert ei.value.code == code
    assert str(ei.value) == code  # the code is the only detail that crosses the boundary
    assert _accesses(world) == before


def _preds(world) -> list[str]:
    labels = world.labels["test"]
    return [("1" if y == "0" else "0") if i % 4 == 0 else y for i, y in enumerate(labels)]


# ------------------------------------------------------------------------------------------- 1. honest path
def test_held_out_score_is_scored_and_logged_in_another_process(world, kd, run):
    assert kd.pid != os.getpid() and kd.alive()
    preds = _preds(world)
    before = _accesses(world)
    got = run.held_out_score("test", "test_y.csv", "accuracy", preds, claim_hash=world.claim_hash, arm="subject",
                             seed=0)
    assert got == M.score("accuracy", world.labels["test"], preds)
    new = world.vault.accesses()[before:]
    assert len(new) == 1
    b = new[0].body
    assert (b["purpose"], b["claim_hash"], b["arm"], b["seed"], b["run_id"], b["private"]) == (
        "evaluate", world.claim_hash, "subject", 0, run.run_id, True)


def test_rescore_with_kernel_token(world, run):
    tok = run.token("kernel", "test", "test_y.csv", claim_hash=world.claim_hash)
    preds = _preds(world)
    assert run.rescore(tok, "accuracy", preds) == run.rescore(tok, "accuracy", preds)  # reusable
    assert world.vault.accesses()[-1].body["purpose"] == "kernel"


# ------------------------------------------------------------------------------------------- 2. attacks
def test_forged_token_refused(world, run):
    _refused(world, "bad_token", run.score, "ab" * 32, "accuracy", _preds(world))


def test_evaluate_token_single_use(world, run):
    tok = run.token("evaluate", "test", "test_y.csv", claim_hash=world.claim_hash, arm="subject", seed=1)
    run.score(tok, "accuracy", _preds(world))  # first use is honest and logged
    _refused(world, "token_spent", run.score, tok, "accuracy", _preds(world))


def test_evaluate_token_cannot_rescore(world, run):
    tok = run.token("evaluate", "test", "test_y.csv", claim_hash=world.claim_hash, arm="subject", seed=2)
    _refused(world, "token_scope", run.rescore, tok, "accuracy", _preds(world))
    lk = run.token("leakscan", "test", "test_y.csv")
    _refused(world, "token_scope", run.score, lk, "accuracy", _preds(world))


def test_token_bound_to_its_run(world, kd, run):
    tok = run.token("kernel", "test", "test_y.csv", claim_hash=world.claim_hash)
    other = kd.open_run("run-other")
    try:
        _refused(world, "bad_token", other.rescore, tok, "accuracy", _preds(world))
    finally:
        other.close()


def test_token_dead_after_close_run(world, kd):
    r = kd.open_run("run-closing")
    tok = r.token("kernel", "test", "test_y.csv", claim_hash=world.claim_hash)
    r.close()
    _refused(world, "bad_token", r.rescore, tok, "accuracy", _preds(world))
    _refused(world, "bad_token", r.token, "kernel", "test", "test_y.csv", claim_hash=world.claim_hash)


def test_issue_refusals(world, run):
    _refused(world, "not_registered", run.token, "evaluate", "test", "test_y.csv", claim_hash=world.unregistered,
             arm="subject", seed=0)
    _refused(world, "not_registered", run.token, "kernel", "test", "test_y.csv")
    _refused(world, "not_target", run.token, "evaluate", "test2", "test_y.csv", claim_hash=world.claim_hash,
             arm="subject", seed=0)
    _refused(world, "not_private", run.token, "leakscan", "test", "test_X.csv")
    _refused(world, "vault_error", run.token, "leakscan", "nope", "test_y.csv")
    _refused(world, "bad_request", run.token, "evaluate", "test", "test_y.csv", claim_hash=world.claim_hash)


def test_wrong_launch_secret(world, kd):
    _refused(world, "bad_secret", kd.open_run, "run-x", secret="0" * 64)
    _refused(world, "bad_request", kd.open_run, "run-x", secret=5)  # type: ignore[arg-type]


@pytest.mark.parametrize("purpose", ["develop", "audit", "reproduce"])
def test_unissuable_purposes(world, run, purpose):
    _refused(world, "bad_request", run.token, purpose, "test", "test_y.csv", claim_hash=world.claim_hash)


# ------------------------------------------------------------------------------------------- 3. leak scan
def test_leak_scan_reports_locations_never_values(world, kd, run):
    labels = world.labels["test"]
    window = ",".join(labels[:40])
    text = f"my notes: {window} (copied)"
    assert window in text and window in label_windows(labels)  # the plant is real
    tok = run.token("leakscan", "test", "test_y.csv")
    leaky = [{"index": 0, "node_id": "agent", "output": {"text": text}, "error": None}]
    problems = run.leak_scan(tok, leaky, None, {"report": "clean"})
    assert problems and any("trace step 0 (agent)" in p for p in problems)
    raw = kd.last_reply
    assert raw is not None and json.loads(raw)["result"]["problems"] == problems
    for w in label_windows(labels):
        assert all(w not in p for p in problems)
        assert w not in raw
    clean = [{"index": 0, "node_id": "agent", "output": {"text": "nothing to see"}, "error": None}]
    assert run.leak_scan(tok, clean, None, {"report": "clean"}) == []
    canary = world.vault.canary("test")
    assert canary
    problems = run.leak_scan(tok, clean, None, {"report": f"x {canary} y"})
    assert problems == ["canary token found in report"]
    assert canary not in (kd.last_reply or "")


def test_leak_scan_of_a_trace_with_non_finite_numbers(world, run):
    """A real trace step can hold NaN/inf (a failed dev score); the wire is strict JSON (allow_nan=False). The scan
    must still run and still find a leak next to the NaN (only strings are scanned, so numbers carry nothing)."""
    labels = world.labels["test"]
    window = ",".join(labels[:40])
    tok = run.token("leakscan", "test", "test_y.csv")
    step = {"index": 3, "node_id": "dev", "error": None,
            "output": {"dev_score": float("nan"), "ci": [float("-inf"), float("inf")], "notes": f"x {window} y",
                       "_redacted_private_values": 1}}
    problems = run.leak_scan(tok, [step], None, {})
    assert any("trace step 3 (dev)" in p and "redacted" in p for p in problems), problems
    assert any("trace step 3 (dev)" in p and "redacted" not in p for p in problems), problems


def test_leak_scan_workspace_copy(world, run, tmp_path):
    raw_labels = world.store.blobs.get(world.vault.dataset("test").files["test_y.csv"].blob)
    (tmp_path / "stolen.csv").write_bytes(raw_labels)
    tok = run.token("leakscan", "test", "test_y.csv")
    problems = run.leak_scan(tok, [], tmp_path, {})
    assert problems == ["workspace file stolen.csv is a copy of private file test/test_y.csv"]


def test_leak_scan_is_not_an_unlimited_membership_oracle(world, kd, run):
    """STATUS §6.2: leak_scan answers 'is this text a copy of the hidden labels?', so unlimited calls make it a
    membership oracle. Each run id gets LEAK_SCAN_BUDGET scans (a re-opened capability for the same run id does not
    reset it), and one call carries at most MAX_EXTRA_TEXTS extra texts. Refusals are made before the private read."""
    from custom_harness.kerneld.server import MAX_EXTRA_TEXTS
    from custom_harness.kerneld.tokens import LEAK_SCAN_BUDGET
    clean = [{"index": 0, "node_id": "agent", "output": {"text": "nothing"}, "error": None}]
    tok = run.token("leakscan", "test", "test_y.csv")
    _refused(world, "rate_limited", run.leak_scan, tok, clean, None,
             {f"probe{i}": "x" for i in range(MAX_EXTRA_TEXTS + 1)})
    for _ in range(LEAK_SCAN_BUDGET):
        assert run.leak_scan(tok, clean, None, {"report": "clean"}) == []
    _refused(world, "rate_limited", run.leak_scan, tok, clean, None, {"report": "clean"})
    again = kd.open_run(run.run_id)  # a new capability for the same run id: same budget
    try:
        _refused(world, "rate_limited", again.leak_scan, again.token("leakscan", "test", "test_y.csv"), clean, None,
                 {})
    finally:
        again.close()
    other = kd.open_run(f"{run.run_id}-other")  # control: another run id still has its own budget
    try:
        assert other.leak_scan(other.token("leakscan", "test", "test_y.csv"), clean, None, {}) == []
    finally:
        other.close()


# ------------------------------------------------------------------------------------------- 4. no free text
def test_internal_errors_carry_no_text(world, kd, run):
    tok = run.token("kernel", "test", "test_y.csv", claim_hash=world.claim_hash)
    with pytest.raises(KernelDError) as ei:
        run.rescore(tok, "zzz_marker_metric", _preds(world))
    assert ei.value.code == "internal"
    assert "zzz_marker" not in (kd.last_reply or "") and "unregistered" not in (kd.last_reply or "")
    assert json.loads(kd.last_reply or "{}") == {"id": json.loads(kd.last_reply or "{}")["id"], "ok": False,
                                                 "code": "internal"}
    with pytest.raises(KernelDError) as ei:
        run.rescore(tok, "accuracy", ["0"])  # count mismatch inside M.score
    assert ei.value.code == "internal" and "count" not in (kd.last_reply or "")


def test_protocol_errors_keep_serving(kd):
    with pytest.raises(KernelDError) as ei:
        kd._call("frobnicate", {})
    assert ei.value.code == "unknown_op"
    with kd._lock:
        kd._write("this is not json\n")
        assert kd._proc.stdout is not None
        reply = json.loads(kd._proc.stdout.readline())
    assert reply == {"id": None, "ok": False, "code": "bad_request"}
    assert kd._call("hello", {})["pid"] == kd.pid


# ------------------------------------------------------------------------------------------- releases
def test_record_release(world, run):
    run.record_release("test", "evolution_score", "verdict_bit", "kerneld:unit")
    assert any(b.get("note") == "kerneld:unit" for b in released(world.store, "test"))
    with pytest.raises(KernelDError) as ei:
        run.record_release("test", "carrier_pigeon", "verdict_bit", "kerneld:unit")
    assert ei.value.code == "release_error"
    with pytest.raises(KernelDError) as ei:
        run.record_release("nope", "prompt", "verdict_bit", "kerneld:unit")
    assert ei.value.code == "release_error"


# ------------------------------------------------------------------------------------------- 5. client purity
def test_client_imports_no_store_kernel_or_server():
    tree = ast.parse(Path(client_mod.__file__).read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            pkg = ["custom_harness", "kerneld"][: 3 - node.level] if node.level else []
            base = ".".join([*pkg, *([node.module] if node.module else [])])
            names += [base] + [f"{base}.{a.name}" for a in node.names]
    assert "custom_harness.kerneld.protocol" in names  # the walk resolves relative imports (not vacuous)
    banned = ("custom_harness.store", "custom_harness.kernel", "custom_harness.kerneld.server")
    bad = [n for n in names if any(n == b or n.startswith(b + ".") for b in banned)]
    assert bad == [], bad
    assert not hasattr(client_mod.RunHandle, "store")


# ------------------------------------------------------------------------------------------- 6. death
def test_kerneld_death_is_unavailable(world):
    k = KernelD(world.root / "store")
    try:
        r = k.open_run("run-death")
        os.kill(k.pid, signal.SIGTERM)
        k._proc.wait(timeout=10)
        assert not k.alive()
        with pytest.raises(KernelDUnavailable) as ei:
            r.token("leakscan", "test", "test_y.csv")
        assert ei.value.transient is False
    finally:
        k.kill()


def _pid_gone(pid: int, timeout_s: float = 10.0) -> bool:
    from custom_harness.kerneld.client import process_started_at
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if process_started_at(pid) is None:
            return True
        time.sleep(0.05)
    return False


def test_kill_never_terminates_a_process_that_reused_the_server_pid(tmp_path):
    """The server pid is captured at hello. If that pid is later owned by another process, kill() must leave it alone
    (on Windows os.kill with SIGTERM is TerminateProcess: nothing asks the process)."""
    (tmp_path / "s").mkdir()
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    k = KernelD(tmp_path / "s")
    real = k.pid
    try:
        k.hello = {**k.hello, "pid": sentinel.pid}  # the pid k holds now names an unrelated process
        k.kill()
        assert sentinel.poll() is None, "kill() terminated a process that is not the server"
        time.sleep(0.5)
        assert sentinel.poll() is None, "kill() terminated a process that is not the server"
    finally:
        sentinel.kill()
        sentinel.wait(timeout=10)
    assert _pid_gone(real), "the launcher's death must still take the real server down"


def test_kill_still_terminates_the_real_server(tmp_path):
    (tmp_path / "s").mkdir()
    k = KernelD(tmp_path / "s")
    pid = k.pid
    k.kill()
    assert _pid_gone(pid)


def test_kill_refuses_a_pid_whose_creation_time_changed(tmp_path):
    """Same pid, another process: only the creation time tells them apart."""
    from custom_harness.kerneld.client import process_started_at
    (tmp_path / "s").mkdir()
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    k = KernelD(tmp_path / "s")
    try:
        k.hello = {**k.hello, "pid": sentinel.pid}
        k._server = (sentinel.pid, process_started_at(sentinel.pid) + 1)
        k.kill()
        time.sleep(0.5)
        assert sentinel.poll() is None
    finally:
        sentinel.kill()
        sentinel.wait(timeout=10)
