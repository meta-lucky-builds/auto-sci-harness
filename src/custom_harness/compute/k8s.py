"""Kubernetes Jobs from a HUMAN-APPROVED, HASHED template (GOAL §5 L0, §17; PORTMAP §4 k8s row, §10).

OpenResearch applies a manifest committed on the experiment branch, i.e. written by the agent, and does not check
it: an agent can ask for a privileged pod, a hostPath mount or the node's network (PORTMAP §4, "[V no check]"). Here
the agent never writes a manifest:
- the Job template is a file the owner approved; its SHA-256 is pinned in the owner config and checked on every
  submit (an edited template is refused, not used);
- per run we fill in ONLY: the name, labels, the image (from the owner's digest-pinned allowlist), the argv (as the
  container's `args`, never a shell string), our fixed bootstrap `command`, the source ConfigMap and two emptyDirs;
- we force the security fields (pod and every container: runAsNonRoot, readOnlyRootFilesystem,
  allowPrivilegeEscalation false, capabilities drop ALL, seccomp RuntimeDefault, automountServiceAccountToken false)
  and then VALIDATE the final manifest, refusing privileged containers, hostPath/secret/PVC/projected volumes,
  hostNetwork/hostPID/hostIPC, hostPort, envFrom, env valueFrom, added capabilities, a service account, ephemeral
  containers and shareProcessNamespace (defence in depth: a template that asks for any of them is refused, not
  silently repaired);
- a deny-all NetworkPolicy (ingress and egress) selecting exactly this Job's pods is created BEFORE the Job.
ISOLATION IS UNVERIFIED until the owner records per-cluster evidence: a NetworkPolicy object only works where the
cluster's network plugin enforces it. Without `isolation_evidence` (the hash of a planted-egress test result) the
backend is TRUSTED_ONLY ("isolation UNVERIFIED") and agent code is refused (invariant 1, fail closed).

Ported from OpenResearch (MIT, Copyright (c) 2026 alphaXiv; src/jobs/kubernetes.rs at fe36f74):
- kubectl as the only client (inherits kubeconfig auth incl. exec plugins), manifests piped on stdin;
- `backoffLimit: 0` (a replacement pod would not see the staged source) and an `activeDeadlineSeconds` default;
- the status reading: Complete/succeeded -> COMPLETED, Failed condition -> ERROR with reason, otherwise RUNNING once
  the pod runs and SCHEDULING before (surfacing waiting reasons like ImagePullBackOff), NotFound -> DELETED;
- cancel deletes exactly the recorded resources, newest first, NotFound = already gone.
Changed: the template/validation regime above; source delivery through a size-capped ConfigMap (OR uses `kubectl cp`
into the running pod); failure classes (DeadlineExceeded/OOMKilled/non-zero exit = POLICY, Evicted/node loss = INFRA).
Not built: fetching files after the pod ends (the emptyDir is gone; results must come back in the log or an owner
artifact store). Built, NOT test-verified; never run against a real cluster.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from ..store import EvidenceStore
from .backend import IsolationProperties, RunRequest
from .credentials import OwnerConfig, check_keys, tool_env
from .jobs import (
    AsyncComputeBackend,
    FailureClass,
    JobHandle,
    JobStage,
    JobStatus,
    LaunchUncertain,
    LogBatch,
    RemoteError,
    Runner,
    infra_error,
    policy_exit,
    subprocess_runner,
)
from .remote_docker import DIGEST_IMAGE, tar_entries
from .ssh import collect_upload

CONFIGMAP_MAX_BYTES = 900 * 1024  # a ConfigMap is capped at 1 MiB including metadata
BOOTSTRAP = 'set -e; mkdir -p /work; tar -x -f /chh-src/src.tar -C /work; cd /work; exec "$@"'
_DNS = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_HEX64 = re.compile(r"[0-9a-f]{64}")
SECTION_KEYS = frozenset({"kubeconfig", "context", "namespace", "template_file", "template_sha256", "images",
                          "isolation_evidence", "kubectl", "tool_env_passthrough"})
_ALLOWED_VOLUME_TYPES = frozenset({"emptyDir", "configMap"})
_FORBIDDEN_POD_KEYS = ("hostNetwork", "hostPID", "hostIPC", "shareProcessNamespace", "ephemeralContainers",
                       "serviceAccountName", "serviceAccount", "hostUsers")


class TemplateRefused(PermissionError):
    """The Job template or the rendered manifest asks for something the Lab never grants."""


def load_template(path: Path, sha256: str) -> dict[str, Any]:
    data = Path(path).read_bytes()
    if not _HEX64.fullmatch(sha256 or "") or hashlib.sha256(data).hexdigest() != sha256:
        raise TemplateRefused("the Job template does not match its owner-approved SHA-256 (edited or not approved)")
    doc = yaml.safe_load(data)
    if not isinstance(doc, dict) or doc.get("kind") != "Job" or doc.get("apiVersion") != "batch/v1":
        raise TemplateRefused("the template must be exactly one batch/v1 Job")
    return doc


def _containers(pod: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in ("initContainers", "containers"):
        v = pod.get(key) or []
        if not isinstance(v, list) or not all(isinstance(c, dict) for c in v):
            raise TemplateRefused(f"{key} must be a list of containers")
        out += v
    return out


def validate_manifest(job: Mapping[str, Any]) -> None:
    """Refuse anything that would break the isolation contract. Called on the FINAL rendered Job."""
    if job.get("kind") != "Job" or job.get("apiVersion") != "batch/v1":
        raise TemplateRefused("not a batch/v1 Job")
    pod = ((job.get("spec") or {}).get("template") or {}).get("spec")
    if not isinstance(pod, dict):
        raise TemplateRefused("the Job has no pod template")
    for k in _FORBIDDEN_POD_KEYS:
        if pod.get(k) not in (None, False, []):
            raise TemplateRefused(f"pod field {k} is not allowed")
    if pod.get("automountServiceAccountToken") is not False:
        raise TemplateRefused("automountServiceAccountToken must be false")
    psc = pod.get("securityContext") or {}
    if psc.get("runAsNonRoot") is not True or psc.get("runAsUser") == 0 or psc.get("runAsGroup") == 0:
        raise TemplateRefused("the pod must run as non-root")
    for vol in pod.get("volumes") or []:
        types = set(vol) - {"name"}
        if not types <= _ALLOWED_VOLUME_TYPES:
            raise TemplateRefused(f"volume {vol.get('name')!r} of type {sorted(types)} is not allowed "
                                  "(emptyDir and configMap only: no hostPath, secret, PVC or projected token)")
    for c in _containers(pod):
        sc = c.get("securityContext") or {}
        caps = sc.get("capabilities") or {}
        if sc.get("privileged") is True or sc.get("allowPrivilegeEscalation") is not False:
            raise TemplateRefused(f"container {c.get('name')!r} may escalate privileges")
        if sc.get("runAsNonRoot") is not True or sc.get("runAsUser") == 0:
            raise TemplateRefused(f"container {c.get('name')!r} must run as non-root")
        if sc.get("readOnlyRootFilesystem") is not True:
            raise TemplateRefused(f"container {c.get('name')!r} needs a read-only root filesystem")
        if "ALL" not in (caps.get("drop") or []) or caps.get("add"):
            raise TemplateRefused(f"container {c.get('name')!r} must drop ALL capabilities and add none")
        if c.get("envFrom") or any(isinstance(e, dict) and "valueFrom" in e for e in c.get("env") or []):
            raise TemplateRefused(f"container {c.get('name')!r} reads env from a Secret/ConfigMap/field")
        if any(isinstance(p, dict) and p.get("hostPort") for p in c.get("ports") or []):
            raise TemplateRefused(f"container {c.get('name')!r} asks for a hostPort")
        if not DIGEST_IMAGE.fullmatch(str(c.get("image", ""))):
            raise TemplateRefused(f"container {c.get('name')!r} image must be pinned by digest")


def _harden(container: dict[str, Any]) -> None:
    sc = container.setdefault("securityContext", {})
    if sc.get("privileged") or (sc.get("capabilities") or {}).get("add"):
        raise TemplateRefused("the template asks for privileges")  # refused, not silently repaired
    sc.update({"runAsNonRoot": True, "readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
               "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}})


def render(template: Mapping[str, Any], *, name: str, job_key: str, image: str, argv: Sequence[str],
           env: Mapping[str, str], deadline_s: int, src_tar: bytes) -> list[dict[str, Any]]:
    """[NetworkPolicy, ConfigMap, Job] for one run, from the approved template. Pure (unit-testable)."""
    if not _DNS.fullmatch(name) or len(name) > 52:
        raise ValueError(f"bad job name {name!r}")
    if len(src_tar) > CONFIGMAP_MAX_BYTES:
        raise RemoteError(f"the revision is {len(src_tar)} bytes; the k8s source ConfigMap holds at most "
                          f"{CONFIGMAP_MAX_BYTES} (larger workspaces: not built)", transient=False)
    if not argv:
        raise ValueError("empty command")
    labels = {"chh.job": name, "app.kubernetes.io/managed-by": "custom-harness"}
    netpol = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
              "metadata": {"name": f"{name}-deny", "labels": labels},
              "spec": {"podSelector": {"matchLabels": {"chh.job": name}}, "policyTypes": ["Ingress", "Egress"]}}
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"{name}-src", "labels": labels},
          "binaryData": {"src.tar": base64.b64encode(src_tar).decode()}}
    job = copy.deepcopy(dict(template))
    job["metadata"] = {"name": name, "labels": {**labels, "chh.job-key": job_key.split(":", 1)[1][:32]}}
    spec = job.setdefault("spec", {})
    spec["backoffLimit"] = 0
    spec.setdefault("activeDeadlineSeconds", int(deadline_s))
    spec.pop("ttlSecondsAfterFinished", None)  # we clean up ourselves, after the status is read
    tmpl = spec.setdefault("template", {})
    tmpl["metadata"] = {"labels": labels}
    pod = tmpl.setdefault("spec", {})
    pod["restartPolicy"] = "Never"
    pod["automountServiceAccountToken"] = False
    pod["enableServiceLinks"] = False
    psc = pod.setdefault("securityContext", {})
    psc["runAsNonRoot"] = True
    psc.setdefault("runAsUser", 1000)
    psc.setdefault("runAsGroup", 1000)
    psc.setdefault("fsGroup", 1000)
    containers = pod.get("containers") or []
    if len(containers) != 1 or pod.get("initContainers"):
        raise TemplateRefused("the template must have exactly one container and no init containers")
    main = containers[0]
    _harden(main)
    main["name"] = "main"
    main["image"] = image
    main["command"] = ["/bin/sh", "-c", BOOTSTRAP, "chh"]
    main["args"] = list(argv)
    main["env"] = [{"name": k, "value": v} for k, v in sorted(env.items())]
    main["volumeMounts"] = [{"name": "chh-src", "mountPath": "/chh-src", "readOnly": True},
                            {"name": "chh-work", "mountPath": "/work"}, {"name": "chh-tmp", "mountPath": "/tmp"}]
    pod["volumes"] = [{"name": "chh-src", "configMap": {"name": f"{name}-src"}},
                      {"name": "chh-work", "emptyDir": {"sizeLimit": "10Gi"}},
                      {"name": "chh-tmp", "emptyDir": {"sizeLimit": "1Gi"}}]
    validate_manifest(job)
    return [netpol, cm, job]


def job_status(job: Mapping[str, Any] | None, pods: Sequence[Mapping[str, Any]]) -> JobStatus:
    """Pure mapping of `kubectl get job/pods -o json` onto our stages (OR `inspect_job` + our failure classes)."""
    if job is None:
        return JobStatus(JobStage.DELETED, FailureClass.INFRA, "the Job no longer exists")
    status = job.get("status") or {}
    completions = max(int((job.get("spec") or {}).get("completions") or 1), 1)
    conds = [c for c in status.get("conditions") or [] if c.get("status") == "True"]
    term = None
    for p in pods:
        for cs in (p.get("status") or {}).get("containerStatuses") or []:
            if cs.get("name") == "main" and (cs.get("state") or {}).get("terminated"):
                term = cs["state"]["terminated"]
    if any(c.get("type") == "Complete" for c in conds) or int(status.get("succeeded") or 0) >= completions:
        return JobStatus(JobStage.COMPLETED, exit_code=0)
    failed = next((c for c in conds if c.get("type") == "Failed"), None)
    if failed is not None:
        reason = str(failed.get("reason") or "")
        if reason == "DeadlineExceeded":
            return JobStatus(JobStage.ERROR, FailureClass.POLICY, "job hit its time limit (activeDeadlineSeconds)")
        if term is not None:
            if term.get("reason") == "OOMKilled":
                return JobStatus(JobStage.ERROR, FailureClass.POLICY, "killed: out of memory",
                                 exit_code=term.get("exitCode"), oom=True)
            code = term.get("exitCode")
            if isinstance(code, int) and code != 0:
                return policy_exit(code)
        pod_reasons = {str((p.get("status") or {}).get("reason") or "") for p in pods}
        return infra_error(f"job failed without a program exit ({reason or 'no reason'}; pod "
                           f"{sorted(r for r in pod_reasons if r) or 'gone'})")
    live, waiting = 0, ""
    for p in pods:
        if (p.get("status") or {}).get("phase") in ("Running", "Succeeded"):
            live += 1
        for cs in (p.get("status") or {}).get("containerStatuses") or []:
            waiting = str(((cs.get("state") or {}).get("waiting") or {}).get("reason") or waiting)
    if live >= completions:
        return JobStatus(JobStage.RUNNING)
    return JobStatus(JobStage.SCHEDULING, message=waiting)


class KubernetesBackend(AsyncComputeBackend):
    name = "k8s"

    def __init__(self, store: EvidenceStore, runs_root: Path, *, kubeconfig: str, context: str, namespace: str,
                 template: Mapping[str, Any], images: Sequence[str], isolation_evidence: Mapping[str, Any] | None,
                 kubectl: str = "kubectl", runner: Runner | None = None,
                 tool_env_passthrough: Sequence[str] = (), **kw: Any):
        super().__init__(store, runs_root, **kw)
        if not _DNS.fullmatch(namespace) or not context or context.startswith("-") or kubeconfig.startswith("-"):
            raise ValueError("bad namespace, context or kubeconfig")
        bad = [i for i in images if not DIGEST_IMAGE.fullmatch(i)]
        if not images or bad:
            raise ValueError(f"the image allowlist must be non-empty and digest-pinned (bad: {bad})")
        self.kubeconfig, self.context, self.namespace = kubeconfig, context, namespace
        self.template, self.images, self.kubectl = dict(template), tuple(images), kubectl
        self._runner = runner or subprocess_runner
        self._passthrough = tuple(tool_env_passthrough)
        ev = isolation_evidence or {}
        verified = bool(_HEX64.fullmatch(str(ev.get("egress_test_sha256", ""))) and str(ev.get("recorded_by", "")))
        self.isolation = IsolationProperties(container=True, no_secrets=True, network_policy="off",
                                             read_only_root=True, non_root=True, verified=verified)

    @classmethod
    def from_config(cls, store: EvidenceStore, runs_root: Path, config: OwnerConfig, name: str,
                    **kw: Any) -> KubernetesBackend:
        sec = config.section("k8s", name)
        check_keys(sec, SECTION_KEYS, frozenset({"kubeconfig", "context", "namespace", "template_file",
                                                 "template_sha256", "images"}), f"k8s/{name}")
        template = load_template(Path(str(sec["template_file"])), str(sec["template_sha256"]))
        return cls(store, runs_root, kubeconfig=str(sec["kubeconfig"]), context=str(sec["context"]),
                   namespace=str(sec["namespace"]), template=template, images=list(sec["images"]),
                   isolation_evidence=sec.get("isolation_evidence"), kubectl=str(sec.get("kubectl", "kubectl")),
                   tool_env_passthrough=list(sec.get("tool_env_passthrough") or []), **kw)

    def argv(self, *args: str) -> list[str]:
        return [self.kubectl, "--kubeconfig", self.kubeconfig, "--context", self.context, "-n", self.namespace,
                *args]

    def _kubectl(self, *args: str, stdin: bytes | None = None, timeout_s: float = 120.0,
                 max_output_bytes: int | None = None) -> Any:
        import os
        env = tool_env({k: os.environ[k] for k in self._passthrough if k in os.environ})
        kw = {} if max_output_bytes is None else {"max_output_bytes": max_output_bytes}
        return self._runner(self.argv(*args), stdin=stdin, timeout_s=timeout_s, env=env, **kw)

    def job_name(self, job_key: str) -> str:
        return "chh-" + job_key.split(":", 1)[1][:24]

    def _submit(self, job_key: str, request: RunRequest, run_dir: Path) -> dict[str, str]:
        image = str(self.template_image())
        name = self.job_name(job_key)
        docs = render(self.template, name=name, job_key=job_key, image=image, argv=request.command,
                      env=request.env, deadline_s=int(request.timeout_s), src_tar=tar_entries(collect_upload(run_dir)))
        created: list[str] = []
        for doc in docs:
            handle = f"{doc['kind'].lower()}/{doc['metadata']['name']}"
            r = self._kubectl("create", "-f", "-", stdin=json.dumps(doc).encode())
            if r.returncode != 0:
                if doc["kind"] == "Job":
                    raise LaunchUncertain(f"kubectl create job not acknowledged: {r.stderr.strip()[-500:]}",
                                          transient=False)
                self._delete(created)
                raise RemoteError(f"kubectl create {handle} failed: {r.stderr.strip()[-500:]}", transient=True)
            created.append(handle)
        return {"context": self.context, "namespace": self.namespace, "job": name, "resources": ",".join(created)}

    def template_image(self) -> str:
        """The image the run uses: the template's own image if it is on the allowlist, else the first allowed."""
        containers = (((self.template.get("spec") or {}).get("template") or {}).get("spec") or {}).get(
            "containers") or [{}]
        img = str(containers[0].get("image", ""))
        return img if img in self.images else self.images[0]

    def _get_json(self, *args: str) -> Any:
        r = self._kubectl("get", *args, "-o", "json")
        if r.returncode != 0:
            if "NotFound" in r.stderr or "not found" in r.stderr:
                return None
            raise RemoteError(f"kubectl get {' '.join(args)} failed: {r.stderr.strip()[-300:]}", transient=True)
        return json.loads(r.stdout)

    def _canceled(self, job_key: str) -> bool:
        return any(e["kind"] == "cancel" and e["job_key"] == job_key for e in self.journal.entries())

    def _status(self, handle: JobHandle) -> JobStatus:
        try:
            job = self._get_json("job", handle.remote["job"])
            pods = (self._get_json("pods", "-l", f"job-name={handle.remote['job']}") or {}).get("items") or []
        except (RemoteError, ValueError) as e:
            return JobStatus(JobStage.UNKNOWN, message=str(e)[:300])
        if job is None and self._canceled(handle.job_key):
            return JobStatus(JobStage.CANCELED)
        return job_status(job, pods)

    def _logs(self, handle: JobHandle, cursor: int, max_bytes: int) -> LogBatch:
        r = self._kubectl("logs", f"job/{handle.remote['job']}", "-c", "main", f"--limit-bytes={cursor + max_bytes}",
                          max_output_bytes=cursor + max_bytes + 4096)
        if r.returncode != 0:
            return LogBatch(b"", cursor, False)
        data = r.stdout.encode("utf-8", errors="replace")[cursor:cursor + max_bytes]
        return LogBatch(data, cursor + len(data), len(data) < max_bytes and self._status(handle).terminal)

    def _delete(self, resources: Sequence[str]) -> None:
        for res in reversed(list(resources)):
            r = self._kubectl("delete", res, "--ignore-not-found", "--wait=false")
            if r.returncode != 0:
                raise RemoteError(f"kubectl delete {res} failed: {r.stderr.strip()[-300:]}", transient=True)

    def _cancel(self, handle: JobHandle) -> None:
        # the Job first (stops the pod), then everything else; the NetworkPolicy goes last
        res = [r for r in handle.remote["resources"].split(",") if r]
        self._delete(res)

    def _cleanup(self, handle: JobHandle) -> None:
        self._cancel(handle)
