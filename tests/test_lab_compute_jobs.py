"""Async compute lifecycle (compute/jobs.py): admission first, credentials never reach a run, the journal is
append-only and tamper-evident, reattach after a crash, infra-vs-policy mapping, collect into the content store, and
Lab discovery-trace events. WRITTEN, NOT RUN (lab-compute, build-only phase).

Fail-first reasoning: before lab-compute every import below fails (no compute.jobs, no discovery_trace). Each test
names the line whose removal makes it fail, so it keeps exercising its rule after the code exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_harness.compute.backend import (
    CodeOrigin,
    IsolationProperties,
    IsolationRefused,
    Revision,
    RunRequest,
    record_non_isolated_approval,
)
from custom_harness.compute.jobs import (
    AsyncComputeBackend,
    FailureClass,
    FakeClock,
    JobJournal,
    JobStage,
    JobStatus,
    JournalError,
    LogBatch,
    RemoteError,
    infra_error,
    policy_exit,
)
from custom_harness.discovery_trace import MemorySink
from custom_harness.errors import InfraError


class FakeAsync(AsyncComputeBackend):
    """Scripted provider: a list of statuses returned in order, a log, files."""

    name = "fake-async"
    isolation = IsolationProperties(container=True, no_secrets=True, network_policy="off", read_only_root=True,
                                    non_root=True)

    def __init__(self, store, root, statuses, *, log=b"hello\n", files=None, isolated=True, **kw):
        super().__init__(store, root, **kw)
        if not isolated:
            self.isolation = IsolationProperties(container=False, no_secrets=False, network_policy="open",
                                                 read_only_root=False, non_root=False)
        self.statuses, self.log, self.files = list(statuses), log, dict(files or {})
        self.submitted: list[str] = []
        self.cancelled: list[str] = []

    def _submit(self, job_key, request, run_dir):
        self.submitted.append(job_key)
        return {"id": job_key.split(":")[1]}

    def _status(self, handle):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def _logs(self, handle, cursor, max_bytes):
        data = self.log[cursor:cursor + max_bytes]
        return LogBatch(data, cursor + len(data), cursor + len(data) >= len(self.log))

    def _cancel(self, handle):
        self.cancelled.append(handle.job_key)

    def _fetch(self, handle, rel):
        return self.files[rel]


def _req(store, *, origin=CodeOrigin.AGENT, env=None, timeout=60.0):
    rev = Revision.record_file("main.py", b"print('hi')\n", store.blobs)
    return RunRequest(rev, ("python", "main.py"), origin=origin, env=env or {}, timeout_s=timeout)


def _backend(store, tmp_path, statuses, **kw):
    clock = FakeClock()
    sink = MemorySink()
    b = FakeAsync(store, tmp_path / "runs", statuses, sleep=clock.sleep, clock=clock.clock, trace=sink, **kw)
    return b, sink


def test_agent_code_on_a_trusted_only_async_backend_is_refused_before_anything_is_submitted(store, tmp_path):
    """Fails if AsyncComputeBackend.submit stops calling self.admit first."""
    b, sink = _backend(store, tmp_path, [JobStatus(JobStage.RUNNING)], isolated=False)
    with pytest.raises(IsolationRefused):
        b.submit(_req(store))
    assert b.submitted == [] and not (tmp_path / "runs").exists() and sink.events == []


def test_trusted_code_with_approval_still_never_receives_credentials(store, tmp_path):
    """admit() only checks secret names for AGENT code; refuse_secret_env covers every origin (§17). Fails if the
    refuse_secret_env call is removed from submit."""
    b, _ = _backend(store, tmp_path, [JobStatus(JobStage.RUNNING)], isolated=False)
    req = _req(store, origin=CodeOrigin.TRUSTED, env={"HF_TOKEN": "x"})
    record_non_isolated_approval(store, approver="owner", backend=b.name, request=req)
    with pytest.raises(IsolationRefused, match="credentials"):
        b.submit(req)
    assert b.submitted == []


def test_submit_journals_the_handle_and_reattach_reads_it_back_after_a_restart(store, tmp_path):
    b, _ = _backend(store, tmp_path, [JobStatus(JobStage.RUNNING)])
    h = b.submit(_req(store))
    fresh, _ = _backend(store, tmp_path, [JobStatus(JobStage.RUNNING)])  # a new process: nothing in memory
    assert fresh.reattach(h.job_key) == h
    with pytest.raises(KeyError):
        fresh.reattach("fake-async:" + "0" * 32)


def test_an_edited_journal_line_breaks_verification(store, tmp_path):
    b, _ = _backend(store, tmp_path, [JobStatus(JobStage.RUNNING)])
    h = b.submit(_req(store))
    b.status(h)
    path = JobJournal.for_store(store).path
    lines = path.read_text(encoding="utf-8").splitlines()
    e = json.loads(lines[0])
    e["body"]["handle"]["remote"]["id"] = "elsewhere"  # point the reattach handle at another job
    lines[0] = json.dumps(e)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalError):
        JobJournal.for_store(store).entries()


def test_run_maps_a_program_failure_to_its_exit_code_and_an_infra_failure_to_InfraError(store, tmp_path):
    b, _ = _backend(store, tmp_path, [JobStatus(JobStage.SCHEDULING), policy_exit(3)])
    res = b.run(_req(store))
    assert res.result.returncode == 3 and not res.result.timed_out and "hello" in res.result.stdout
    b2, _ = _backend(store, tmp_path, [JobStatus(JobStage.RUNNING), infra_error("node lost")])
    with pytest.raises(InfraError):
        b2.run(_req(store))


def test_unknown_monitoring_state_is_never_a_verdict(store, tmp_path):
    """OR: 'missing scheduler evidence never proves the job has terminated'. UNKNOWN is not terminal."""
    assert not JobStatus(JobStage.UNKNOWN).terminal
    with pytest.raises(ValueError):
        JobStatus(JobStage.ERROR)  # an ERROR must say policy or infra


def test_run_cancels_and_reports_a_timeout_when_the_job_outlives_its_deadline(store, tmp_path):
    b, _ = _backend(store, tmp_path, [JobStatus(JobStage.RUNNING)])
    b.submit_grace_s = 0
    res = b.run(_req(store, timeout=10))
    assert res.result.timed_out and res.result.returncode == -1 and len(b.cancelled) == 1


def test_oom_is_the_programs_failure_with_the_limit_code(store, tmp_path):
    from custom_harness.sandbox.base import LIMIT_RETURNCODE
    st = JobStatus(JobStage.ERROR, FailureClass.POLICY, "oom", exit_code=137, oom=True)
    b, _ = _backend(store, tmp_path, [st])
    assert b.run(_req(store)).result.returncode == LIMIT_RETURNCODE


def test_collect_only_after_the_job_finished_and_into_the_content_store(store, tmp_path):
    b, sink = _backend(store, tmp_path, [JobStatus(JobStage.RUNNING), policy_exit(0)], files={"out/m.json": b"{}"})
    h = b.submit(_req(store))
    with pytest.raises(RemoteError):
        b.collect(h, ["out/m.json"])
    got = b.collect(h, ["out/m.json"])
    assert store.blobs.get(got["out/m.json"]) == b"{}"
    with pytest.raises(ValueError):
        b.collect(h, ["../escape"])
    assert any(e.kind == "compute.collected" and e.fields["outputs"] == got for e in sink.events)


def test_trace_carries_the_lifecycle_with_cost_and_env_names_only(store, tmp_path):
    b, sink = _backend(store, tmp_path, [JobStatus(JobStage.SCHEDULING), JobStatus(JobStage.RUNNING), policy_exit(0)])
    b.run(_req(store, env={"SEED": "12345"}))
    kinds = [e.kind for e in sink.events]
    assert kinds[0] == "compute.submitted" and "compute.started" in kinds and "compute.finished" in kinds
    sub = sink.events[0].fields
    assert sub["env_names"] == ["SEED"] and "12345" not in json.dumps([e.to_json() for e in sink.events])
    fin = next(e for e in sink.events if e.kind == "compute.finished").fields
    assert fin["cost"]["usd"] is None and fin["isolation_class"] == "isolated"
    assert kinds.count("compute.finished") == 1  # emitted once, not on every poll


def test_a_job_handle_from_another_backend_is_refused(store, tmp_path):
    b, _ = _backend(store, tmp_path, [JobStatus(JobStage.RUNNING)])
    h = b.submit(_req(store))

    class Other(FakeAsync):
        name = "other"
    o = Other(store, Path(tmp_path / "r2"), [JobStatus(JobStage.RUNNING)])
    with pytest.raises(ValueError):
        o.status(h)
