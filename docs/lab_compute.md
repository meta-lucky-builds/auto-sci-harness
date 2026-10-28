# Lab compute, connectors and research outputs (lab-compute, 2026-10-23)

Status: **built, NOT test-verified.** Nothing here has been run: no test, no Docker build, no SSH host, no cluster, no
Modal/HF/Ray account, no literature request. ruff and mypy are clean on the new modules; `python -c` import checks
pass. Every module that ports OpenResearch (OR, MIT, commit fe36f74) names its source files in its docstring;
`THIRD_PARTY_NOTICES.md` carries the copyright and licence.

GOAL references: §17 (Lab and Court), §5 L0 (compute backends with isolation classes), §10 invariants 1, 8, 9, 11,
§16. Port map: `META/openresearch_portmap/PORTMAP.md` §4, §5, §8, §9.

## 1. What was built

| Area | Files |
|---|---|
| Async lifecycle (submit, status, logs with a byte cursor, cancel, reattach, collect, cleanup), job journal | `compute/jobs.py` |
| Credential custody | `compute/credentials.py` |
| SSH with a pinned host key | `compute/pinned_ssh.py` |
| Backends | `compute/remote_docker.py`, `slurm.py`, `k8s.py`, `modal_backend.py`, `ray_jobs.py`, `hf_jobs.py`, `rest.py` |
| Backend table and dropped list | `compute/registry.py` |
| `IsolationProperties.verified` (default True: old behaviour unchanged) | `compute/backend.py` |
| Lab literature connectors; alphaXiv gated OFF | `literature/lab.py`, `literature/alphaxiv.py`; `literature/retrieval.py` (optional `sources=`) |
| Research outputs | `outputs/latex.py`, `outputs/court_paper.py`, `outputs/drafts.py`, `outputs/figstyle.py`, `outputs/assets/openresearch/` |
| Lab discovery-trace events | `discovery_trace.py` |
| TeX image recipe (not built) | `docker/sandbox-tex.Dockerfile`, `docker/tex-warm.tex` |
| Tests (written, NOT run) | `tests/test_lab_compute_jobs.py`, `test_lab_compute_backends.py`, `test_lab_literature.py`, `test_lab_outputs.py`, `test_discovery_trace.py`, `test_no_phone_home.py`; `tests/test_telemetry.py` amended (one entry) |

`kernel/` is untouched. No Court check was changed. `cli.py` is untouched (no CLI was added in this phase).

## 2. Backend isolation table

The class is DERIVED in code (`ComputeBackend.isolation_class`), never asserted. ISOLATED = container, no secrets,
network off or allowlist, read-only root, non-root, hidden-data-free, and (new) verified.

| Backend | Class with no owner evidence | Can become ISOLATED? | Agent code | Notes |
|---|---|---|---|---|
| local-docker | ISOLATED | n/a | yes | existing `DockerBackend` |
| **remote-docker** (GPU host over SSH) | **ISOLATED** | n/a | yes | our docker flags; digest-pinned candidate image whose label is checked on the host; SSH user uid 0 refused; host key pinned |
| k8s | TRUSTED_ONLY ("isolation UNVERIFIED") | yes, with an owner-recorded planted-egress test on that cluster | only once verified | Job from a human-approved, SHA-256-pinned template; security fields forced, then validated; deny-all NetworkPolicy created first |
| modal | TRUSTED_ONLY ("isolation UNVERIFIED") | yes, with owner-recorded egress, rootfs and user tests (one hash each) | only once verified | `block_network=True`, no secrets; each property derived from its own evidence |
| ssh (plain) | TRUSTED_ONLY | never | never | existing `SSHBackend` |
| slurm | TRUSTED_ONLY | never (Apptainer variant not built) | never | sbatch over pinned SSH |
| ray | TRUSTED_ONLY | never | never | dashboards are often unauthenticated |
| hf-jobs | TRUSTED_ONLY | never (network policy unknown) | never | |
| local-process | TRUSTED_ONLY | never | never | tests only |

**Dropped** (refused by `registry.info`): tinker (training out of scope, §5 L8); OR managed and rental boxes
(third-party billing, and OR accepts ANY host key for them: `StrictHostKeyChecking=no`); ssh-container (`docker exec`
into a container whose flags we do not control); local host process (replaced by local Docker).

Trusted code on a TRUSTED_ONLY backend still needs the existing per-request human approval
(`record_non_isolated_approval`); agent code never runs there, approved or not (invariant 1).

## 3. Credential custody

- One human-owned file: `<CHH_DATA_DIR>/human/lab_compute.yaml` (format in `compute/credentials.py`). `<data>/human`
  is already a protected path (no sandbox workspace may overlap it). The path is never read from an environment
  variable.
- Each backend reads only its own section; unknown keys are refused (a typo cannot silently disable a pin).
- Secret values are `Secret` objects (redacted repr, not picklable); read at the moment of use.
- Where each credential goes:

| Credential | Goes to | Never goes to |
|---|---|---|
| SSH identity file (path) | `ssh -i` on the host | the remote run, the job, any event |
| SSH host key (pin) | our own known_hosts file under `<store>/compute/known_hosts` | `~/.ssh/known_hosts` |
| kubeconfig (path) + context | `kubectl --kubeconfig --context` on the host | the pod (`automountServiceAccountToken: false`, no Secret volumes or env) |
| Modal token id/secret (files) | the host-side launcher's environment | the sandbox (no `secrets=`), argv |
| HF token (file) | our REST Authorization header; the host-side staging launcher's stdin | the job's `environment`/`secrets` (always `{}`), argv |

- Host-side tools get `tool_env()`: PATH and OS basics plus that tool's own variables. Model API keys and other owner
  secrets in the harness's environment do not reach ssh, kubectl or launchers.
- Every async backend refuses secret-named variables for ANY code origin (`jobs.refuse_secret_env`), stricter than
  `admit`, which only checks agent code.
- Trace events carry environment variable NAMES only and refuse secret- or truth-named fields.

## 4. Design choices made where the brief was silent (each consistent with §17)

| # | Choice | Reason |
|---|---|---|
| C1 | `IsolationProperties.verified` (default True), and "isolation UNVERIFIED" listed in `unmet()` | Modal and K8s must not be ISOLATED by configuration alone; fail closed until evidence exists. Default True keeps every existing backend unchanged. |
| C2 | Job handles go to an append-only, hash-chained journal `<store>/compute/jobs.jsonl`, not the Kernel ledger | `kernel/` is off limits and its KINDS are fixed; the journal is tamper-evident the same way. |
| C3 | New `UNKNOWN` stage for "monitoring unavailable" (OR's GONE/UNAVAILABLE), never terminal; `run()` turns it into a transient InfraError | Missing scheduler evidence never proves termination (OR's own rule). |
| C4 | Failure classes per provider state: non-zero exit, OOM and the job's own time limit are POLICY; node failure, boot failure, preemption from accounting, eviction, vanished run dir/container, runtime errors are INFRA | Our infra-vs-policy split (errors.py): an agent cannot turn its own failure into an excluded unit. Ray FAILED and HF ERROR are POLICY (their APIs do not separate infrastructure). |
| C5 | Results come back only via `collect` (named files pulled into the blob store, resolved with `realpath` and refused if outside the run or a link), only after the job is terminal | Invariant 8; a remote status string is not a result; no TOCTOU while the program still runs. |
| C6 | `run()` (synchronous) is submit + poll + drain logs on every async backend | Existing callers keep working. |
| C7 | Remote Docker runs the container as the SSH user's uid:gid, refusing uid 0 | The 0700 run directory must be readable; root in the container is refused. |
| C8 | Remote Docker uses `--log-driver json-file` capped at 64 MiB | Async jobs are read back with `docker logs`. |
| C9 | Remote image must be pinned by digest and carry the candidate label, checked on the host before `docker create` | Hidden-data-free (invariant 2) and reproducible. |
| C10 | `ssh -F none` + `HostKeyAlias` + our own known_hosts | A user ssh config (ProxyCommand, StrictHostKeyChecking=no) cannot weaken the pin. Cost: ProxyJump is not supported. |
| C11 | K8s source delivered as a ConfigMap (<= 900 KiB tar) extracted by a fixed bootstrap into an emptyDir | The agent never writes manifests; `kubectl cp` would need a running pod. Larger workspaces: not built. |
| C12 | K8s and Modal: fetching files after the job ends is not built | The pod's emptyDir is gone; results come back in the log or an owner artifact store. |
| C13 | Modal logs only after the sandbox finished | Live streaming not built. |
| C14 | No auto-provisioned venvs (OR pip-installs `modal`/`huggingface_hub` itself); the owner points to a Python that has them | No runtime installs (§17). |
| C15 | Lab literature asks each source separately (`lab_search`); the Court citation path keeps its fallback order | Breadth for exploration without changing the Court. |
| C16 | alphaXiv requests carry only the query (OR also sends the date window) | The window is applied by our client to every record, as for every other source. |
| C17 | Court papers: the author supplies a body only; a fixed preamble; every number is `\chhnum{ref}` to a certificate; any other digit and any number-making command is refused; the audit re-renders byte-for-byte | Invariant 8 applied to papers; raw evidence refs are refused (the Court reports certified results). |
| C18 | Lab figures use OR's matplotlib style assets inside the sandbox; Court figures keep the trusted SVG renderer and `figures/audit.py` | OR only asks for real evidence; our audit checks it. |
| C19 | The no-phone-home scanner lives in `tests/`, not `src/` | `tests/test_telemetry.py` fails if any `src/*.py` even names the analytics clients. |
| C20 | Discovery-trace events behind an adapter (`TraceSink`); default sink `LabTraceSink` (int18, stage-a) writes them into lab-agents' ONE discovery trace (`lab/trace.py`, `chh-lab-trace/1`): compute -> `run`, literature -> `literature`, builds -> `action`; the old JSONL chain is removed | One schema, no duplicate chains (docs/lab.md section 9). Cost is `{wall_s, usd: null}`: provider billing is not read, and unknown is never 0. The Court paper's certificate list is not copied into the Lab trace. |
| C21 | `tests/test_telemetry.py`: `compute/rest.py` added to NETWORK_MODULES | Ray/HF REST calls, only on an owner-configured submit; httpx imported inside the call. |
| C22 | Merged origin/lab-agents into lab-compute (merge, not rebase) to append to its `THIRD_PARTY_NOTICES.md` | The brief asked to coordinate by merging. The notices sentence "nothing copied verbatim" was corrected: four figure assets are copied verbatim. |

## 5. Lab discovery trace (events emitted)

| Producer | Events |
|---|---|
| every async backend | `compute.submitted` (backend, job key, isolation class, origin, approval item hash, revision hash, command hash, env names), `compute.started`, `compute.log` (cursor pointers, not content), `compute.finished` / `compute.failed` / `compute.cancelled` (stage, failure class, exit code, OOM, duration, cost), `compute.collected` (outputs as blob hashes) |
| `literature.lab.lab_search` | `literature.retrieval` (source, query, window, as-of, stored response digests, record ids) |
| `outputs.court_paper.generate` | `output.paper_built` (tex hash, certificates, manifest hash) |
| `outputs.latex.compile_in_sandbox` | `output.compiled` (driver, ok, PDF hash, problems) |
| `discovery_trace.figure_built` | `output.figure_built` (script/svg/renderer hashes from figure.json) |

Guard: field names that look like secrets or held-out truth are refused (`TraceRefused`); a sink I/O failure never
changes the producer's result. Since int18 every event is stored as a `chh-lab-trace/1` event (source `backend`;
session `unattached` when the producer has no Lab session) and also passes the lab trace's guards.

## 6. TeX image (owner decision)

Estimates [I], not measured:

| Option | Approx. size | Notes |
|---|---|---|
| tectonic + pre-warmed bundle cache (recommended; `docker/sandbox-tex.Dockerfile`) | ~0.3–0.6 GB | one binary; `--only-cached` so compiles need no network; XeTeX engine (pdfLaTeX documents compile with XeTeX, noted in the log) |
| Debian `texlive-latex-recommended` + `latexmk` | ~0.5–0.8 GB | pdfLaTeX parity for common papers; venue classes may need extra packages |
| TeX Live scheme-full | ~5–7 GB | Overleaf parity; slow to build and pull |

The recipe pins the tectonic version but leaves its SHA-256 for the owner to fill in from the published checksum.

## 7. Overleaf sync (design only, deferred)

- Needs Overleaf premium (git bridge). Owner decision about paying.
- Design: a human-run publication action `chh publish overleaf <paper>` pushes ONLY a Court paper whose
  `audit_court_paper` passes (or an EXPLORATORY-labelled Lab draft to a separate project). The git token lives in the
  owner config, is used host-side only, and is never in a run or event.
- OR's "Overleaf live" (undocumented socket.io protocol and decrypted browser cookies) is DROPPED.

## 8. No telemetry, no feedback upload, no auto-update

Nothing in lab-compute sends usage data, uploads feedback, checks for updates or installs packages at run time.
`tests/test_no_phone_home.py` scans `src/`, `scripts/` and `docker/` (all shipped text files, including the vendored
assets) for analytics SDK imports, analytics hosts, telemetry/track endpoints, feedback uploads, update checks and
runtime installs, and plants one example per rule. Two vendor OFF switches written by lab-agents into the Lab box
are exempt by exact literal. A static smoke run of the scanner over the tree (a read-only scan, not the test suite)
found only the OpenCode `"autoupdate": False` off switch, now exempt.

## 9. Residual risks (stated, not solved)

1. Remote Docker: the rented host's Docker daemon and kernel are trusted. Whoever controls that host controls the
   run. The SSH user is in the docker group (root-equivalent on that host). No gVisor is requested remotely.
2. GPU containers share the host's GPU driver: a driver bug is an escape path that `--cap-drop ALL` does not cover.
3. K8s isolation depends on the cluster enforcing NetworkPolicy and on admission controllers we do not see. The
   evidence is per cluster and goes stale when the cluster changes.
4. Modal's sandbox semantics are unknown until the owner's planted tests run, and may change between SDK versions.
5. Trusted-only backends (Slurm, Ray, HF, plain SSH) run approved code with the cluster user's full access. An
   approval binds the exact request, not what that code does.
6. Ray dashboards are often unauthenticated: anyone on that network can submit jobs there.
7. HF job logs are an SSE stream; reading them while the job runs may time out (logs are reliable after it finishes).
8. `ssh -F none` means no ProxyJump/bastion support.
9. The phone-home scan is a tripwire against ported or accidental code, not a sandbox: a host assembled at run time
   is not caught.
10. Court papers: numbers spelled in words are not detected; LaTeX counters and the bibliography are not data and are
    not checked.
11. The remote login shell is assumed POSIX `sh`, with GNU coreutils (`realpath -e`, `stat -c`, `base64`, `head -c`).

## 10. Owner steps

1. **Rented GPU host for remote-docker:** a Linux host with Docker and the NVIDIA container toolkit; a non-root SSH
   user in the docker group; push or build the candidate image there and record its digest; record the host's ed25519
   public key (from the provider console, NOT from a first connection) and the identity file path in
   `lab_compute.yaml`.
2. **Kubernetes:** a cluster whose CNI enforces NetworkPolicy; write and approve a Job template (one container, image
   by digest, resources); record its SHA-256 and the image allowlist; run a planted-egress test there and record its
   result hash as `isolation_evidence` (otherwise the backend stays TRUSTED_ONLY).
3. **Modal:** an account (money); install the `modal` SDK in a separate venv and give its python path; token files;
   run planted egress, rootfs-write and `id -u` tests and record one hash each to verify isolation.
4. **Slurm / Ray / HF Jobs:** accounts; pinned host key for the Slurm login node; HF token file (and a paid plan if
   Jobs needs one [U]); a Python with `huggingface_hub>=1.22`.
5. **alphaXiv:** read its terms or obtain written permission; only then set `literature.alphaxiv` to `enabled: true`,
   `tos_status: permitted`, `permission_note`, `recorded_by`, and the endpoints `api_base` and `web_base` (https base
   URLs, no path). Since 2026-10-28 no alphaXiv endpoint is written in the code: without both base URLs in the owner
   record the client stays OFF. OpenResearch at fe36f74 used the API host api.alphaxiv.org and the web host
   www.alphaxiv.org (src/config.rs).
6. **TeX image:** choose an option (§6), fill in the tectonic checksum, then build.
7. **Overleaf:** decide on premium; the design is in §7.

## 11. Tests owed (exact commands, to run after the owner's gate)

```
pytest tests/test_lab_compute_jobs.py tests/test_lab_compute_backends.py tests/test_lab_literature.py \
       tests/test_lab_outputs.py tests/test_discovery_trace.py tests/test_no_phone_home.py tests/test_telemetry.py \
       tests/test_compute_backends.py tests/test_compute_ssh.py tests/test_literature*.py tests/test_regex_anchors.py
ruff check src tests && mypy src/custom_harness/compute src/custom_harness/outputs src/custom_harness/literature \
       src/custom_harness/discovery_trace.py
```

Then the full gate. Real-host verification (owner): remote-docker escape tests (planted egress, write to `/`, `id -u`,
host-file read via a symlink in `/work` refused by `collect`), and the k8s/Modal planted tests of §10.
