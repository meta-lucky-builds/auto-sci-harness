"""Lab compute backends: isolation classes, host-key pinning, command construction and status mapping, with fake
transports only (no network, no cluster, no Docker). WRITTEN, NOT RUN (lab-compute, build-only phase).

Fail-first reasoning: none of these modules exists on origin/int17, so every test fails on import there. The
attack-shaped tests (accept-any host key, privileged pod, hostPath, root container, unverified Modal) each fail if
the one check they name is removed.
"""

from __future__ import annotations

import hashlib
import shlex

import pytest

from custom_harness.compute.backend import CodeOrigin, IsolationClass, IsolationRefused, Revision, RunRequest
from custom_harness.compute.credentials import CredentialError, OwnerConfig, Secret, check_keys, tool_env
from custom_harness.compute.jobs import CommandResult, FailureClass, JobStage, RemoteError
from custom_harness.compute.pinned_ssh import PinnedSSH, fetch_file_script
from custom_harness.discovery_trace import MemorySink

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
IMAGE = "registry.example/chh-candidate@sha256:" + "a" * 64


class FakeRunner:
    def __init__(self, replies=None):
        self.calls: list[dict] = []
        self.replies = list(replies or [])

    def __call__(self, argv, *, stdin=None, timeout_s=0, env=None, **kw):
        self.calls.append({"argv": list(argv), "stdin": stdin, "env": dict(env or {})})
        return self.replies.pop(0) if self.replies else CommandResult(0, "", "")


def _ssh(tmp_path, runner=None):
    return PinnedSSH("user@203.0.113.7", host_key=KEY, known_hosts_dir=tmp_path / "kh", runner=runner or FakeRunner())


# ------------------------------------------------------------------ pinned SSH
def test_pinned_ssh_never_accepts_an_unknown_host_key(tmp_path):
    """OR accepts any key on managed boxes (StrictHostKeyChecking=no). Fails if the pin options are dropped."""
    a = " ".join(_ssh(tmp_path).argv("true"))
    assert "StrictHostKeyChecking=yes" in a and "accept-new" not in a and "StrictHostKeyChecking=no" not in a
    assert "-F none" in a and "GlobalKnownHostsFile=none" in a and "UserKnownHostsFile=" in a
    assert "HostKeyAlias=chh-pin-" in a and "ForwardAgent=no" in a


def test_the_pin_file_holds_exactly_the_owner_key(tmp_path):
    s = _ssh(tmp_path)
    assert s.known_hosts.read_text(encoding="ascii") == f"{s.alias} {KEY}\n"


@pytest.mark.parametrize("bad", ["", "*", "ssh-ed25519", KEY + "\nssh-rsa AAAA", "none"])
def test_a_missing_or_malformed_pin_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        PinnedSSH("h", host_key=bad, known_hosts_dir=tmp_path)


def test_a_host_that_looks_like_an_option_is_refused(tmp_path):
    with pytest.raises(ValueError):
        PinnedSSH("-oProxyCommand=evil", host_key=KEY, known_hosts_dir=tmp_path)


def test_ssh_exit_255_is_an_infrastructure_failure(tmp_path):
    s = _ssh(tmp_path, FakeRunner([CommandResult(255, "", "Host key verification failed.")]))
    with pytest.raises(RemoteError, match="host key"):
        s.run("true")


def test_host_tools_get_a_minimal_environment():
    env = tool_env(base={"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-x", "AWS_SECRET_ACCESS_KEY": "y"})
    assert env == {"PATH": "/bin"}


def test_fetch_refuses_paths_outside_the_run_and_links():
    script = fetch_file_script("chh-runs/x/work", "out/m.json", 1024)
    assert "realpath -e" in script and "[ ! -L" in script and "exit 4" in script
    with pytest.raises(ValueError):
        fetch_file_script("chh-runs/x/work", "../../.ssh/id_rsa", 1024)


# ------------------------------------------------------------------ credentials
def test_secrets_never_print():
    s = Secret("hf_abc")
    assert "hf_abc" not in repr(s) and "hf_abc" not in str(s) and s.reveal() == "hf_abc"


def test_unknown_keys_in_a_backend_section_are_refused():
    with pytest.raises(CredentialError):
        check_keys({"host": "h", "host_kye": "typo"}, frozenset({"host", "host_key"}), frozenset({"host"}), "x")


def test_no_config_means_no_remote_backend():
    with pytest.raises(CredentialError):
        OwnerConfig.empty().section("remote-docker", "gpu1")


# ------------------------------------------------------------------ remote Docker (ISOLATED)
def _rd(store, tmp_path, runner=None):
    from custom_harness.compute.remote_docker import RemoteDockerBackend
    return RemoteDockerBackend(store, tmp_path / "runs", ssh=_ssh(tmp_path, runner), image=IMAGE, gpus="all",
                               trace=MemorySink())


def test_remote_docker_is_isolated_and_uses_the_local_sandbox_flags(store, tmp_path):
    b = _rd(store, tmp_path)
    assert b.isolation_class is IsolationClass.ISOLATED
    a = b.create_args("remote-docker:" + "b" * 32, "chh-runs/x", ["python", "main.py"], {"SEED": "1"})
    for flag in (["--network", "none"], ["--read-only"], ["--cap-drop", "ALL"],
                 ["--security-opt", "no-new-privileges"], ["--gpus", "all"]):
        assert any(a[i:i + len(flag)] == flag for i in range(len(a))), flag
    assert a[-3:] == [IMAGE, "python", "main.py"]


def test_remote_docker_refuses_a_mutable_image_tag(store, tmp_path):
    from custom_harness.compute.remote_docker import RemoteDockerBackend
    with pytest.raises(ValueError):
        RemoteDockerBackend(store, tmp_path, ssh=_ssh(tmp_path), image="registry.example/chh:latest")


def test_remote_docker_submit_script_refuses_root_and_a_non_candidate_image(store, tmp_path):
    s = _rd(store, tmp_path).submit_script("remote-docker:" + "c" * 32, "chh-runs/x", ["python", "m.py"], {})
    assert '"$CHH_UID" = 0' in s and "exit 3" in s
    assert "org.custom-harness.bundled-data" in s and "exit 4" in s
    assert 'docker create --user "$CHH_UID:$CHH_GID"' in s


@pytest.mark.parametrize(("out", "stage", "failure"), [
    ("ST created|0|false|", JobStage.SCHEDULING, FailureClass.NONE),
    ("ST running|0|false|", JobStage.RUNNING, FailureClass.NONE),
    ("ST exited|0|false|", JobStage.COMPLETED, FailureClass.NONE),
    ("ST exited|2|false|", JobStage.ERROR, FailureClass.POLICY),
    ("ST exited|137|true|", JobStage.ERROR, FailureClass.POLICY),
    ("ST exited|0|false|oci runtime error", JobStage.ERROR, FailureClass.INFRA),
    ("MISSING", JobStage.DELETED, FailureClass.INFRA),
    ("CANCELED", JobStage.CANCELED, FailureClass.NONE),
    ("garbage", JobStage.UNKNOWN, FailureClass.NONE),
])
def test_remote_docker_status_mapping(out, stage, failure):
    from custom_harness.compute.remote_docker import RemoteDockerBackend
    st = RemoteDockerBackend.parse_status(out)
    assert (st.stage, st.failure) == (stage, failure)


def test_remote_docker_admission_runs_before_any_ssh_call(store, tmp_path):
    runner = FakeRunner()
    b = _rd(store, tmp_path, runner)
    rev = Revision.record_file("m.py", b"x", store.blobs)
    with pytest.raises(IsolationRefused):
        b.submit(RunRequest(rev, ("python", "m.py"), env={"GITHUB_TOKEN": "x"}))
    assert runner.calls == []


def test_an_unacknowledged_start_is_launch_uncertain_and_journaled(store, tmp_path):
    from custom_harness.compute.jobs import JobJournal, LaunchUncertain
    runner = FakeRunner([CommandResult(0, "", ""), CommandResult(0, "", ""), CommandResult(1, "CHH_CREATED\n", "x")])
    b = _rd(store, tmp_path, runner)
    rev = Revision.record_file("m.py", b"x", store.blobs)
    with pytest.raises(LaunchUncertain):
        b.submit(RunRequest(rev, ("python", "m.py")))
    assert [e["kind"] for e in JobJournal.for_store(store).entries()] == ["submit_uncertain"]


# ------------------------------------------------------------------ Slurm (TRUSTED_ONLY)
def test_slurm_is_trusted_only_and_refuses_agent_code(store, tmp_path):
    from custom_harness.compute.slurm import SlurmBackend
    b = SlurmBackend(store, tmp_path, ssh=_ssh(tmp_path))
    assert b.isolation_class is IsolationClass.TRUSTED_ONLY
    rev = Revision.record_file("m.py", b"x", store.blobs)
    with pytest.raises(IsolationRefused):
        b.submit(RunRequest(rev, ("python", "m.py"), origin=CodeOrigin.AGENT))


def test_sbatch_runs_the_payload_in_a_subshell_and_quotes_everything():
    from custom_harness.compute.slurm import SlurmJobSpec, render_sbatch
    s = render_sbatch(SlurmJobSpec("slurm:" + "d" * 32, ("python", "a b.py", "$(rm -rf ~)"), {"X": "$(id)"}))
    assert "(\ncd repo || exit 97\n" in s and 'echo "$code" > exit_code' in s
    assert shlex.quote("$(rm -rf ~)") in s and "export X='$(id)'" in s


@pytest.mark.parametrize("bad", ["gpu\n#SBATCH --uid=0", "a b", "x;y"])
def test_sbatch_directive_values_are_validated(bad):
    from custom_harness.compute.slurm import SlurmJobSpec, render_sbatch
    with pytest.raises(ValueError):
        render_sbatch(SlurmJobSpec("slurm:" + "d" * 32, ("true",), {}, partition=bad))


@pytest.mark.parametrize(("token", "stage", "failure"), [
    ("EXIT 0", JobStage.COMPLETED, FailureClass.NONE), ("EXIT 1", JobStage.ERROR, FailureClass.POLICY),
    ("EXIT ", JobStage.RUNNING, FailureClass.NONE), ("SQ PENDING", JobStage.SCHEDULING, FailureClass.NONE),
    ("SQ COMPLETED", JobStage.RUNNING, FailureClass.NONE), ("SA COMPLETED", JobStage.COMPLETED, FailureClass.NONE),
    ("SQ NODE_FAIL", JobStage.RUNNING, FailureClass.NONE), ("SA NODE_FAIL", JobStage.ERROR, FailureClass.INFRA),
    ("SA PREEMPTED", JobStage.ERROR, FailureClass.INFRA), ("SA TIMEOUT", JobStage.ERROR, FailureClass.POLICY),
    ("SA OUT_OF_MEMORY", JobStage.ERROR, FailureClass.POLICY), ("SA CANCELLED by 12", JobStage.CANCELED,
                                                                 FailureClass.NONE),
    ("GONE", JobStage.UNKNOWN, FailureClass.NONE), ("SA WEIRD", JobStage.UNKNOWN, FailureClass.NONE),
])
def test_slurm_token_mapping(token, stage, failure):
    from custom_harness.compute.slurm import map_inspect_token
    st = map_inspect_token(token)
    assert (st.stage, st.failure) == (stage, failure)


def test_slurm_helpers_match_openresearch():
    from custom_harness.compute.slurm import parse_job_id, parse_mem, resolve_gres, slurm_time
    assert slurm_time(90061) == "1-01:01:01" and slurm_time(3600) == "01:00:00"
    assert parse_mem("64G") == "64G" and resolve_gres("h100:2") == "gpu:h100:2" and resolve_gres("gpu") == "gpu:1"
    assert parse_job_id("123;cluster") == "123"
    for bad in ("0", "64Q", "", "1G;x"):
        with pytest.raises(ValueError):
            parse_mem(bad)


# ------------------------------------------------------------------ Kubernetes (approved template)
TEMPLATE = {"apiVersion": "batch/v1", "kind": "Job", "spec": {"template": {"spec": {
    "containers": [{"name": "x", "image": IMAGE, "resources": {"limits": {"nvidia.com/gpu": 1}}}]}}}}


def _render(template=TEMPLATE, **kw):
    from custom_harness.compute.k8s import render
    args = {"name": "chh-abc", "job_key": "k8s:" + "e" * 32, "image": IMAGE, "argv": ["python", "m.py"], "env": {},
            "deadline_s": 600, "src_tar": b"tar"}
    return render(template, **{**args, **kw})


def test_k8s_render_creates_a_deny_all_policy_first_and_a_hardened_job():
    netpol, _cm, job = _render()
    assert netpol["kind"] == "NetworkPolicy" and netpol["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in netpol["spec"] and "egress" not in netpol["spec"]  # deny all
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False and c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["args"] == ["python", "m.py"] and job["spec"]["backoffLimit"] == 0


@pytest.mark.parametrize("patch", [
    lambda t: t["spec"]["template"]["spec"]["containers"][0].update(securityContext={"privileged": True}),
    lambda t: t["spec"]["template"]["spec"].update(hostNetwork=True),
    lambda t: t["spec"]["template"]["spec"].update(serviceAccountName="admin"),
    lambda t: t["spec"]["template"]["spec"]["containers"][0].update(envFrom=[{"secretRef": {"name": "s"}}]),
    lambda t: t["spec"]["template"]["spec"]["containers"][0].update(
        securityContext={"capabilities": {"add": ["SYS_ADMIN"]}}),
    lambda t: t["spec"]["template"]["spec"].update(securityContext={"runAsUser": 0}),
])
def test_k8s_refuses_a_template_that_asks_for_privileges(patch):
    import copy

    from custom_harness.compute.k8s import TemplateRefused
    t = copy.deepcopy(TEMPLATE)
    patch(t)
    with pytest.raises(TemplateRefused):
        _render(t)


def test_k8s_validator_refuses_hostpath_volumes():
    from custom_harness.compute.k8s import TemplateRefused, validate_manifest
    job = _render()[2]
    job["spec"]["template"]["spec"]["volumes"].append({"name": "h", "hostPath": {"path": "/"}})
    with pytest.raises(TemplateRefused):
        validate_manifest(job)


def test_k8s_template_hash_must_match(tmp_path):
    import yaml

    from custom_harness.compute.k8s import TemplateRefused, load_template
    p = tmp_path / "job.yaml"
    p.write_text(yaml.safe_dump(TEMPLATE), encoding="utf-8")
    good = hashlib.sha256(p.read_bytes()).hexdigest()
    assert load_template(p, good)["kind"] == "Job"
    p.write_text(p.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    with pytest.raises(TemplateRefused):
        load_template(p, good)


def test_k8s_is_trusted_only_until_the_owner_records_egress_evidence(store, tmp_path):
    from custom_harness.compute.k8s import KubernetesBackend
    kw = {"kubeconfig": "kc", "context": "lab", "namespace": "chh", "template": TEMPLATE, "images": [IMAGE],
          "runner": FakeRunner()}
    assert KubernetesBackend(store, tmp_path, isolation_evidence=None, **kw).isolation_class is \
        IsolationClass.TRUSTED_ONLY
    ev = {"egress_test_sha256": "f" * 64, "recorded_by": "owner"}
    assert KubernetesBackend(store, tmp_path, isolation_evidence=ev, **kw).isolation_class is IsolationClass.ISOLATED


def test_k8s_status_mapping():
    from custom_harness.compute.k8s import job_status
    assert job_status(None, []).stage is JobStage.DELETED
    done = {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
    assert job_status(done, []).stage is JobStage.COMPLETED
    dl = {"status": {"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]}}
    assert job_status(dl, []).failure is FailureClass.POLICY
    oom_pod = {"status": {"containerStatuses": [{"name": "main", "state": {"terminated": {"reason": "OOMKilled",
                                                                                         "exitCode": 137}}}]}}
    failed = {"status": {"conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}}
    assert job_status(failed, [oom_pod]).oom
    evicted = {"status": {"reason": "Evicted"}}
    assert job_status(failed, [evicted]).failure is FailureClass.INFRA
    pull = {"status": {"phase": "Pending", "containerStatuses": [{"state": {"waiting": {"reason": "ErrImagePull"}}}]}}
    st = job_status({"status": {}}, [pull])
    assert st.stage is JobStage.SCHEDULING and st.message == "ErrImagePull"


# ------------------------------------------------------------------ Modal (isolation UNVERIFIED)
def test_modal_is_unverified_without_evidence_and_never_isolated_by_assertion():
    from custom_harness.compute.modal_backend import isolation_from_evidence
    p = isolation_from_evidence(None)
    assert not p.meets_invariant_1 and "isolation UNVERIFIED" in p.unmet()
    partial = isolation_from_evidence({"egress_test_sha256": "a" * 64, "recorded_by": "owner"})
    assert not partial.meets_invariant_1
    full = isolation_from_evidence({k: "a" * 64 for k in ("egress_test_sha256", "rootfs_test_sha256",
                                                          "user_test_sha256")} | {"recorded_by": "owner"})
    assert full.meets_invariant_1


def test_modal_token_goes_only_to_the_launcher_environment(store, tmp_path):
    from custom_harness.compute.modal_backend import ModalBackend
    (tmp_path / "id").write_text("ak-1\n")
    (tmp_path / "sec").write_text("as-2\n")
    runner = FakeRunner([CommandResult(0, '{"returncode": null}', "")])
    b = ModalBackend(store, tmp_path / "r", python="py", token_id_file=str(tmp_path / "id"),
                     token_secret_file=str(tmp_path / "sec"), image=IMAGE, runner=runner)
    from custom_harness.compute.jobs import JobHandle
    b._status(JobHandle("modal", "modal:" + "1" * 32, {"sandbox_id": "sb-abc"}, "h", "trusted"))
    call = runner.calls[0]
    assert call["env"]["MODAL_TOKEN_SECRET"] == "as-2" and "as-2" not in " ".join(call["argv"])


# ------------------------------------------------------------------ Ray / HF Jobs (TRUSTED_ONLY)
def test_ray_and_hf_status_maps():
    from custom_harness.compute.hf_jobs import map_hf_stage, parse_timeout, sse_lines
    from custom_harness.compute.ray_jobs import map_ray_status
    assert map_ray_status("SUCCEEDED").stage is JobStage.COMPLETED
    assert map_ray_status("FAILED").failure is FailureClass.POLICY
    assert map_ray_status("STOPPED").stage is JobStage.CANCELED and map_ray_status("??").stage is JobStage.UNKNOWN
    assert map_hf_stage("UPDATING").stage is JobStage.RUNNING and map_hf_stage("DELETED").stage is JobStage.DELETED
    assert parse_timeout("4h") == 14400 and parse_timeout("90") == 90
    text = 'data: {"data": "===== Job started"}\n: keepalive\ndata: {"data": "loss 1"}\n'
    assert sse_lines(text) == ["loss 1"]


def test_ray_hf_are_trusted_only(store, tmp_path):
    from custom_harness.compute.hf_jobs import HFJobsBackend
    from custom_harness.compute.ray_jobs import RayBackend
    assert RayBackend(store, tmp_path, address="http://10.0.0.5:8265").isolation_class is IsolationClass.TRUSTED_ONLY
    hf = HFJobsBackend(store, tmp_path, namespace="o", token_file="t", python="p", image=IMAGE)
    assert hf.isolation_class is IsolationClass.TRUSTED_ONLY


# ------------------------------------------------------------------ registry
def test_registry_table_matches_the_derived_classes_and_dropped_backends_are_refused(store, tmp_path):
    from custom_harness.compute import registry
    from custom_harness.compute.backends import DockerBackend, LocalProcessBackend, SSHBackend
    derived = {"local-docker": DockerBackend(store, tmp_path).isolation_class,
               "ssh": SSHBackend(store, tmp_path, host="h").isolation_class,
               "local-process": LocalProcessBackend(store, tmp_path).isolation_class}
    for b in registry.BACKENDS:
        if b.kind in derived:
            assert derived[b.kind].value == b.default_class, b.kind
    for kind in ("tinker", "openresearch-managed", "openresearch-rental", "ssh-container"):
        with pytest.raises(registry.BackendDropped):
            registry.info(kind)
