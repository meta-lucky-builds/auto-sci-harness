# The Lab: full-capability exploration, walled off from the Court (GOAL §17)

**Status: built 2026-10-23 on branch `lab-agents` (from `origin/int17` 916bc5d). Built, NOT test-verified.**
No test was run, no image was built or pulled, nothing was installed, no agent CLI was started. ruff and mypy pass
on the new code. Every statement about a vendor CLI's flags or switches comes from OpenResearch's code
(fe36f74, MIT) or vendor documentation, and is marked [U] where I could not check it.

## 1. What it is

GOAL §17: "generous when exploring, strict when claiming".

- **The Lab.** One coding agent per session (Claude Code, Codex, OpenCode with API or local models, ACP agents)
  runs with ALL its native tools inside a **lab box**. The box is a Docker container with:
  - network on;
  - a writable root filesystem (pip, npm, uv and conda installs work);
  - the session workspace bind-mounted at `/workspace` and kept across stops;
  - optional GPUs (`--gpus`);
  - no time limit.

  Inside the box the agent's own permission layer is set to full access. The box is the boundary.
- **The Court.** It stays exactly as it is: Kernel, kerneld, vault, release ledger, candidate sandbox. The only
  way from the Lab to a finding is `chh lab promote`, then the existing Court path.

```
owner host (never visible to a box beyond one directory)
 ├─ <data>/store, bundles, human, private (truth)   ← Court only; refused as mounts, checked by path
 ├─ <data>/lab/<session>/workspace  ──bind──▶  lab box /workspace  (agent + native tools + network)
 ├─ <data>/lab/<session>/transcripts                 (headless outputs; outside the mount)
 └─ <data>/lab_court/<promotion>/                    (frozen code + Court spec; outside the lab root)
```

### Commands

| Command | What it does |
|---|---|
| `chh lab agents` | lists the adapters, with the deferral reasons |
| `chh lab check` | static walls: the lab root, the image recipe and the truth layout |
| `chh lab start --agent claude-code --registry-key anthropic-api --credential-env CHH_LAB_CRED_ANTHROPIC --spend-cap-usd 20 --scope "..." [--gpus all]` | starts a session (Claude Code example) |
| `chh lab start --agent opencode --model qwen2.5-coder:7b` | starts a session with a local model: no credential |
| `chh lab attach SESSION` | the agent's own UI, interactive |
| `chh lab attach SESSION --prompt "..."` | one headless prompt; the transcript goes to `transcripts/` |
| `chh lab status [SESSION]` | the sessions and their state |
| `chh lab stop SESSION` | stops the box; the workspace stays |
| `chh lab promote SESSION --request promote.yaml` | freezes the claim's code and environment |
| `chh lab handoff PROMOTION --targets targets.yaml` | writes the Court spec |
| `chh audit show / approve-fidelity / prepare`, `chh approve-role`, `chh audit run --docker` | the existing Court, unchanged |
| `chh lab release-verdict PROMOTION --by NAME` | **HUMAN**: writes the verdict label only, as a ledgered release |
| `chh lab trace SESSION`, `chh lab trace-add`, `chh lab costs`, `chh lab story CLAIM_HASH` | the discovery trace (§9) |

### Code

| Path | What it holds |
|---|---|
| `src/custom_harness/lab/walls.py` | the static and run-time wall checks |
| `credentials.py` | the session credential policy |
| `agents.py` | the adapters |
| `acp_client.py` | the ACP v1 client |
| `box.py` | the Docker box manager |
| `session.py` | sessions and their records |
| `promote.py` | freeze, hand-off and verdict release |
| `trace.py` | the discovery trace (§9) |
| `cli.py` | `chh lab` |
| `docker/lab.Dockerfile` | the image recipe; **not built** |
| `THIRD_PARTY_NOTICES.md` | OpenResearch's MIT notice |
| `tests/test_lab_*.py`, `tests/_lab_fakes.py` | the tests; **not run** |

## 2. Threat model

**The adversary** is everything inside the box:
- the agent;
- the model behind it;
- any package, web page or repository it pulls in;
- any prompt injection it meets.

Assume it is capable and, at worst, adversarial. It may want to:
1. read held-out truth, sealed data, verdict details or the draw secret, to tune on them;
2. take or misuse owner secrets;
3. reach the host beyond its workspace, or persist into the Court (store, ledger, Kernel, adapters);
4. make Lab output look like a finding.

**Trusted:**
- the owner;
- the host OS and the Docker daemon;
- this manager code;
- the Kernel and kerneld;
- the human approval steps.

## 3. What is enforced, and where

| Wall | Static check | Run-time check |
|---|---|---|
| Only one host directory is visible: the workspace, strictly inside the lab root | `walls.lab_root_problems` (`chh lab check`) | `walls.require_workspace` at start and promote. It checks both directions against the store, bundles, human config, truth locations, draw secret, `~/.ssh` and the other credential stores, the repo, the Python env, the data dir, home and drive roots. It also refuses links or junctions on the workspace path. |
| No truth or store mount, no Docker socket, no host namespaces, no privileged mode, no added caps or devices | — | `walls.docker_argv_problems` on the final `docker run` argv (fail closed) |
| No truth copied into the workspace | — | `walls.scan_workspace` at start and promote. It looks for legacy `private/` truth files, exact copies of any PRIVATE vault blob (sha256; metadata only, no private read) and the draw secret in common encodings. |
| Truth layout vs the Lab | `walls.static_problems`. It calls s4-integrity's `truth_layout_problems` with the lab root added to the agent-readable roots once that module is merged; until then, the truth root vs lab root. | — |
| Image holds no host files, no secrets, pinned CLI versions, non-root | `walls.recipe_problems` on `docker/lab.Dockerfile` | — |
| No owner secret: one scoped, spend-capped credential | — | `credentials.resolve` (detail below) |
| Lab never writes evidence | — | Lab records are `store.append_record` rows labelled EXPLORATORY; no Kernel ledger entry. The test asserts the ledger length is unchanged. |
| Freeze before naming targets | — | `promote.freeze` must precede `handoff`. `handoff` refuses a second spec and runs a deny-only screen: frozen code that names a target's OpenML id or file hash is refused. |
| No host read while agent code runs | — | the box is `docker pause`d for the freeze and for the verdict write. Every component is checked with `sandbox.base.safe_path`. |
| The Lab sees only a verdict bit | — | `promote.release_verdict`, HUMAN-triggered. It emits one release per held-out target through `kerneld.releases.Releases`: channel `prompt`, alphabet `verdict_bit`, note `lab:promotion_verdict`. It writes only `{promotion_id, label}` to `.court/` in the workspace. |

**What `credentials.resolve` checks:**
- the source is a `CHH_LAB_CRED_*` variable or a file outside every agent-readable root and the lab root. The CLIs'
  default key variables are refused as sources;
- the value must not equal any other secret-like host variable;
- the cap is in (0, 200] USD and the TTL is at most 14 days;
- the registry gate passes (`gateway.registry.require_usable`);
- BYOS is refused in the Lab;
- the value reaches docker only through its environment, as `-e NAME`, and never through argv or a file.

## 4. What is NOT enforced: residual risks

1. **The agent CLI's own behaviour inside the box.**
   - The off-switches for usage reporting, error reports and auto-update are set from vendor docs and OR's code
     [U]. Whether each CLI honours them is untested.
   - The box has network, so anything inside it can talk to the internet: the agent, a package it installs, a
     vendor CLI ignoring a switch. That includes sending the workspace or the session key anywhere.
   - This is by design (§17: full network). The walls keep the truth and owner secrets out of the box, so there is
     nothing of the Court's to exfiltrate.
2. **Credential misuse within its cap.**
   - The key is visible to the agent (process environment; Codex also stores it in the box HOME, which is ephemeral).
   - The agent can spend up to the cap, or leak the key. The cap is enforced by the provider on the scoped key,
     never by this code. The recorded cap is the owner's declaration.
   - Revocation at session end is an owner step at the provider. The expiry recorded here only stops
     `attach`/`--prompt` from this CLI.
3. **Container escape.**
   - The box uses Docker's default runtime (no gVisor), so a kernel or runtime bug is a host compromise.
   - `--root` widens this (Docker's default capability set). On Windows, the Docker Desktop VM sits between the
     box and the host.
4. **Host services reachable over the network.**
   - With network on, the box can reach the LAN. On Docker Desktop it can also reach the host through
     `host.docker.internal`, including, I believe, services bound to the host's loopback [I, not verified].
   - kerneld speaks over a pipe only (verified by reading `kerneld/server.py`), so the Court has no socket.
   - Any other host service (a local model server, a database, a future Studio UI bound to an interface) is
     reachable.
   - Owner rule: no unauthenticated service holding private data on this host.
5. **Retrospective targets are downloadable from the Lab.**
   - The only Court route wired today is the Claim Audit: fresh, but public, OpenML datasets. A networked Lab can
     fetch them and tune on them before the freeze.
   - Mitigations, all partial:
     - targets are named after the freeze;
     - the deny-only screen;
     - the audit's listed assumptions.
   - The real fix is minted or future targets (§16.2): built on int18 as the minted Court route (`chh court`,
     `court/minted.py`, section 10). The audit route stays for public datasets, and its certificates are labelled
     RETROSPECTIVE and can never be VALIDATED or RE-VALIDATED (`court/levels.py`). Truth memorised in model weights remains (§16.12).
6. **Transformed copies of truth are not found by the content scans.** Only exact copies and common encodings of the
   secret are found. The guarantee rests on the truth never being reachable from the box (mount checks, no Court
   socket, separate OS user), not on the scan.
7. **Supply chain of the lab image.**
   - The npm CLIs are pinned by version, not by hash.
   - The base image digest is an owner step.
   - Agents install arbitrary packages at run time. That is fine for the Lab, and it is why the Court never runs in
     the lab image.
8. **Court environment mismatch.**
   - The frozen lock is recorded and checked for exact pins. The Court still runs the arms in the existing candidate
     image (fixed packages, no network).
   - A promotion needing other packages ends as `COULD NOT RUN` in the audit route.
   - Building a Court image from a frozen lock is an owner decision (§7). It must stay truth-scanned like the
     candidate image.
9. **The freeze stores a manifest, not the whole workspace.**
   - The Court holds the sha256 manifest and the three frozen files (method, baseline, lock).
   - The rest of the workspace (caches, data, notes) is pinned by hash only. Reproducing that context needs the
     owner to keep the workspace.
10. **ACP agents.**
    - Their internal tools, terms and switches are unknown per agent.
    - The client refuses client-side `fs/*` and `terminal/*`, so an agent that needs them does not work.
    - No preset is usable until the owner adds a registry entry for its provider.
11. **The verdict file persists in the workspace.** Later agents of that session can read the label. It is a ledgered
    `verdict_bit` release, which K4 counts.
12. **Lab records sit in the Court's store.**
    - They are labelled EXPLORATORY and are not Kernel ledger entries.
    - No check yet stops a future report or Studio view from rendering them as findings (§6 next).

## 5. Design choices made where the brief was silent (each consistent with §17)

| # | Choice | Reason |
|---|---|---|
| D1 | The workspace is a host **bind mount** under `<data>/lab/<session>/workspace`, not a Docker named volume | The existing mount checks (`check_mount`, path overlap) and the content scans apply to a host path. The owner can inspect it. Promotion can hash it without `docker cp`. |
| D2 | The box runs as uid 1000, `--cap-drop ALL`, `no-new-privileges`. `--root` is an opt-in that keeps Docker's default capabilities, and it is recorded. | None of this limits the agent's tools (user-space installs work). It limits what the box can do to the host. |
| D3 | Inside the box each agent runs in its full-access mode: Claude Code `bypassPermissions`, Codex `danger-full-access` plus `approval_policy never`, OpenCode all `allow` | §17: "ALL its native tools". The box, not the CLI, is the boundary. These modes are never used on the host. |
| D4 | The credential reaches the box only as `docker run -e NAME`, with the value in the docker CLI's environment. Codex additionally logs in with `--with-api-key` into the box HOME [U], which is ephemeral and removed at stop. | It is never in argv (process listings), never in a file we write, never in the workspace. |
| D5 | Subscription (BYOS) credentials are refused in the Lab, even `openai-codex-cli`, which the gateway allowlist admits | A subscription login is the owner's own account, neither scoped nor spend-capped (§17). `anthropic-subscription` is `prohibited` anyway, so **Claude Code in the Lab is API-key only (`anthropic-api`)**. |
| D6 | Credential sources must be `CHH_LAB_CRED_*` variables or a file outside agent-readable roots. A value equal to another host secret variable is refused. | It stops the owner's everyday key from being passed by accident. |
| D7 | The spend cap is declared (0 < cap ≤ 200 USD, UNCALIBRATED) and recorded as "enforced by the provider" | This code cannot enforce a provider-side cap. Saying otherwise would be false. |
| D8 | The ACP client advertises no `fs` or `terminal` capability and refuses those methods | OR serves them on the host for any absolute path (PORTMAP §10 S5). In the Lab the agent's own in-box tools suffice. |
| D9 | The Court route is the existing Claim Audit (`chh-audit/1`): method = frozen Lab code, baseline = frozen baseline | It is the existing path that takes code from outside a research run, pre-registers it with the Kernel before the data are read, runs both arms under one contract in the network-free candidate sandbox and certifies. Nothing in the Court changed. |
| D10 | Held-out targets are named in a separate step after the freeze, with a deny-only screen | It is the strongest ordering available without minted targets (§16.2 spirit). The screen can only deny (invariant 21). |
| D11 | The freeze is a whole-workspace sha256 manifest, plus the method, baseline and lock bytes in the store's blobs | Lab workspaces can be many GB. The Court needs only what it re-executes; the hash pins the rest. |
| D12 | The lock must pin every line exactly. Editable, URL, VCS, include and alternative-index lines are refused. `--hash` lines are recorded. | A frozen environment must be re-creatable by the Court without trusting the Lab's index choice. |
| D13 | The verdict release is HUMAN-triggered and label-only, one `verdict_bit` release per target, written to `.court/` in the workspace | §17: "the Lab never sees verdict details beyond what the existing release ledger allows". |
| D14 | `truth_layout` (s4-integrity, not on int17) is looked up at call time with importlib. Until it is merged, the same default is used (`$CHH_TRUTH_ROOT` or `<data>/private`). | There is no merge of s4-integrity's 2.8k lines into this branch. After the integrator merges, the check uses the real layout with no code change. |
| D15 | The lab root defaults to `<data>/lab` | It is outside the repo, the truth root and the store. `"lab"` is in `commitment.AGENT_READABLE_DATA_SUBDIRS` (int18, stage-a), so every secret and truth scan also walks Lab workspaces (a `$CHH_LAB_ROOT` elsewhere is not covered by that default list; `walls.static_problems` adds `lab_root()` itself). |
| D16 | `tests/test_telemetry.py`: exactly the one literal `"DISABLE_TELEMETRY": "1"` in `lab/agents.py` is exempt from its "no TELEMETRY anywhere" scan; any other mention still fails | The Lab must WRITE that vendor off-switch. Prose was reworded instead of exempted. |
| D17 | The box's own `pip freeze` is recorded (hash) next to the lock, as box-reported and untrusted | It is useful for diagnosis and never trusted. |
| D18 | Headless transcripts go to `<lab root>/<session>/transcripts/`, outside the mount | Agents cannot rewrite their own history. The store keeps the sha256. |
| D19 | A stopped session can still be promoted | No agent runs then, which is the safest moment to freeze. |
| D20 | No gVisor by default in the Lab | GPUs and many dev tools do not work under runsc. A later `--runtime` option can add it per session. |
| D21 | The discovery trace lives in the Court's store as `lab_trace` records (EXPLORATORY), not in the Kernel ledger | Append-only, owner-side, never mounted. The per-session hash chain gives tamper evidence without touching the Kernel. |
| D22 | Agent self-reports go through a workspace file (`.lab/trace.jsonl`), ingested with the box paused | It is agent-agnostic (any CLI can write JSON lines) and independent of each agent's own logs. It is untrusted and validated. |
| D23 | Certificate-to-Lab link through the Claim Audit spec's `paper_doi = lab:<session>:<promotion>` | It needs no Kernel or audit change. The link is inside the certificate's claim, so it survives in the Reproducibility Bundle. |

## 6. Next (not built)

- **Minted-target Court route for promotions:** built on int18 (section 10), NOT test-verified.
- **A Court image built from a frozen lock.** It needs an owner decision. The build must be offline at run time and
  get the same bundled-data truth scan as the candidate image.
- **Renderers never show EXPLORATORY records as findings:** a static check over report and studio code.
- **`chh lab` egress audit:** a `--network` mode with an allowlisting proxy, if the owner ever wants a tighter Lab
  (not required by §17).

## 7. Owner steps

1. **Build the image.**
   - Pin the base image by digest and pick the reviewed CLI versions.
   - Run `docker build -f docker/lab.Dockerfile -t custom-harness-lab:py312 --build-arg CLAUDE_CODE_VERSION=… --build-arg CODEX_VERSION=… --build-arg OPENCODE_VERSION=… docker`.
   - For ACP agents, extend the recipe with their pinned CLIs.
2. **API keys.** Create one key per Lab use, scoped and spend-limited at the provider:
   - Anthropic: a dedicated workspace with a spend limit;
   - OpenAI: a dedicated project with a budget.

   Then pass the key with `CHH_LAB_CRED_<NAME>=… chh lab start … --credential-env CHH_LAB_CRED_<NAME>`, and revoke it
   after the session.
3. **Separate OS user for private data.**
   - Run kerneld and keep `<data>/store`, `<data>/private` and the draw secret under a separate OS user. Use ACLs
     that deny the user who runs `chh lab` and Docker.
   - The Lab's walls then do not rest on path checks alone.
   - On Windows, also restrict Docker Desktop file sharing to the lab root.
4. **Local models.** Run the model server on the host and pass `--local-base-url` if it is not Ollama's default.
   Bind the server to loopback, and remember residual risk 4.
5. **GPUs.** Install the NVIDIA container toolkit and use `--gpus all`.
6. **Decide:** D5 (BYOS in the Lab), D7 cap ceiling, D11 (manifest-only freeze), D20 (gVisor), the Court-image-from-lock
   question, and which ACP agents get registry entries.

## 8. Tests owed (none run)

`tests/test_lab_walls.py`, `test_lab_credentials.py`, `test_lab_agents.py`, `test_lab_session.py`,
`test_lab_promote.py`, `test_lab_trace.py` and `test_lab_cli.py` use a fake docker client. `tests/test_telemetry.py` was amended (D16).

Real-box tests (owner, after the image build):
1. **Escape kit, per agent:**
   - read a planted host file outside the workspace;
   - write outside `/workspace`;
   - reach `/var/run/docker.sock`;
   - read `<data>/store` and `<data>/private`;
   - find a host env secret.

   Every attempt must fail.
2. **Egress capture,** to verify the off-switches. Run each CLI idle for 10 minutes with only a provider allowlist and
   log any other host it contacts.
3. **Host reachability:** a probe from a box to `host.docker.internal` ports of known host services.
4. **One end-to-end promotion on a toy:** start → promote → handoff → audit prepare/run → release-verdict. Check the
   release ledger entry.

## 9. The discovery trace: data contract `chh-lab-trace/1`

Added on the orchestrator's request (2026-10-23). It is a data contract, not UI. Consumers: a future UI, the memory
and archive layers, and harness self-evolution. Code: `src/custom_harness/lab/trace.py`.

### Where events live

**Storage.** One `lab_trace` record per event in the evidence store (`store.append_record`). Lab events are never
Kernel ledger entries.

**Common envelope** (every event):

```
{ "schema": "chh-lab-trace/1",
  "event_id": "<32 hex>",      # sha256 of the canonical event without event_id: stable, content-bound
  "session_id": "s-...", "seq": 1..n, "prev": "<event_id of seq-1>" | "genesis",   # per-session hash chain
  "kind": "session|idea|run|file|literature|action|human|cost|promote",
  "at": <unix seconds>,
  "actor": {"type": "human|agent|system|backend", "id": "...", "agent"?: "...", "model"?: "..."},
  "source": "manager|agent-reported|human|backend",
  "client_id"?: "...",          # agent / backend de-duplication key
  "label": "EXPLORATORY",
  "payload": { ...kind-specific, extra fields refused... } }
```

`verify_chain` recomputes every id and checks `seq` and `prev`. `chh lab trace` exits 1 on a broken chain.

### Payloads

| Kind | Payload | Written by |
|---|---|---|
| `session` | `op` start, stop, attach or start_failed; agent, model, image, image_id | manager |
| `idea` | `op` propose, branch, abandon, revisit or merge; `idea_id`, `parent_id`, title, summary. `reason` is required for abandon. | the agent (self-report), a human or a backend |
| `run` | run_id, command, `env_hash` (never the env), backend (`lab-box` or a lab-compute backend name), idea_id, duration_s, exit_code, cost_event, `outputs` [{path, sha256, size}], `metrics` [{name, value, split: **dev\|train**, dataset}] | agent self-report, or a backend through `trace.record` / `chh lab trace-add --source backend` |
| `file` | `op` add, modify, delete or summary; path, sha256_before, sha256_after, size. `summary` carries the new manifest blob ref and the count of unlisted changes (cap 2000). | manager (workspace diffs at start, after each prompt or attach, at stop and at promote) |
| `literature` | retrieval_session, entry_digests (RetrievalLog entry digests), ids | agent self-report or backend |
| (int18) backend fields | `run` gains optional op (submitted ... collected), job_key, isolation_class, origin, item_hash, revision, command_sha256, env_names, log cursors, stage, failure, oom, message, wall_s, usd; `literature` gains source, query, window, as_of, n_records, complete, envelopes, errors; `action` gains outputs [{path, sha256}] (private-blob checked) and problems | lab-compute's producers through `discovery_trace.LabTraceSink` (one schema, one chain per session; session `unattached` without a Lab session) |
| `action` | tool, target, outcome (ok, error, refused or unknown), step | manager: Claude stream-json `tool_use` blocks [U] and ACP `tool_call` titles; also agent self-report |
| `human` | `op` steer, edit, stop, redirect, prompt, attach, start, approve or note; prompt_sha256 and prompt_blob (**text by reference only**), note | manager (with the OS user) and `chh lab trace-add --by NAME` |
| `cost` | agent, model, `usd` (**None = unknown, never 0**), tokens_in, tokens_out, reported_by | manager (from stream-json [U]; an unknown-cost event when nothing is reported), backends, agents |
| `promote` | `op` frozen, handed_off or verdict_released; promotion_id, workspace_snapshot, idea_id, idea_path, claim_hash, spec_hash, audit_id, released_label | manager only (`promote.py`) |

### Agent-agnostic intake

Any agent may append JSON lines to `/workspace/.lab/trace.jsonl`:

```
{"kind": "idea|run|literature|action|cost", "client_id": "...", "payload": {...}}
```

The manager ingests the file with the box paused:
- after each headless prompt or interactive attach;
- at stop;
- at promote.

The events are validated against the same schema and guards, tagged `agent-reported` and de-duplicated by
`client_id`. Agents cannot write `session`, `file`, `human` or `promote` events.

### Guards (fail closed: `TraceRefused`)

- **Held-out truth.**
  - Metric splits are only `dev` or `train`.
  - A metric naming a vault dataset whose role is not DEV is refused.
  - A run output or file hash equal to a PRIVATE vault blob is refused (metadata only, no private read).
- **Verdict details.**
  - Keys such as `verdict`, `certificate`, `checks`, `test_score`, `labels` and `reasons` are refused anywhere in a
    payload.
  - A label appears only in `promote/verdict_released`, which only `promote.release_verdict` may write, after its
    ledgered `verdict_bit` release.
- **Secrets.**
  - Values that look like keys are refused (`sk-…`, `ghp_…`, `AKIA…`, private-key blocks, …).
  - So are long values under secret-like field names.
  - Environments are recorded by hash; files and prompts by hash or blob reference only.

### Read side

| Function | Command | What it returns |
|---|---|---|
| `idea_tree` / `idea_path` | — | the idea tree, and the root-to-leaf path of an idea |
| `costs` | `chh lab costs` | per session and per agent; `usd_total` is None when any part is unknown |
| `story(claim_hash)` | `chh lab story` | claim → promotion (via `promote/handed_off`) → session → idea path → every event in order, with the chain check |

**The link from a Court certificate or rejection back to the Lab.** It is two-way:
- the certificate's claim carries `scope.conditions.paper_doi = "lab:<session>:<promotion>"` (written by
  `promote.handoff` into the Claim Audit spec);
- the trace's `promote/handed_off` event carries the claim hash.

A promotion request may name the `idea` it came from. It must exist in the trace, and its path is frozen into the
`promote/frozen` event.

### Versioning

- A new optional field is a minor change within `chh-lab-trace/1`, because readers must ignore unknown OPTIONAL
  fields of newer writers.
- A new kind, a removed field or a changed meaning is `chh-lab-trace/2`.
- Writers refuse fields they do not know.

### Stated limits

- Self-reported ideas, runs and metrics are the agent's claims. They are recorded as such and never trusted more for
  being in the trace.
- Action and cost extraction from vendor output is best effort [U].
- `seq` and `prev` assume one writer per session at a time (the manager).
- The trace is owner-side (the store); nothing of it is mounted into the box.

## 10. Court routes for a promotion and verification badges (int18, stage-a; built, NOT test-verified)

One Court route per freeze:

| Route | Commands | Held-out data | Badges possible |
|---|---|---|---|
| Minted | `chh court register PROMOTION --family F --beacon-round R` → `chh court mint CASE --beacon VALUE` → `chh approve-role TARGET` (HUMAN) → `chh court run CASE --docker` | minted by kerneld AFTER the claim is pre-registered and anchored (`kerneld/mint.py`, ingest kind `minted_heldout`, `Kernel.record_mint`); FUTURE role; sized by the certifiability preflight (default 4,500 rows, UNCALIBRATED) | all four |
| Retrospective | `chh lab handoff PROMOTION --targets FILE` → the Claim Audit commands | fixed public OpenML datasets (a networked Lab could have them) | REPRODUCIBLE, REPLICATED; labelled RETROSPECTIVE |

**What can be minted:** the synthetic generator families of `research/suite.FAMILIES` (rings, xor, spirals,
imbalanced, friedman, interactions, and the null family noise), each with its own metric (`chh court families`).
**What cannot:** public datasets (retrospective route), and simulated, formal, computational or physical claims (no
generator; physical claims get FUTURE data only through `chh return-data`). A minted claim is generator-scoped: its
population is the family (§16.2).

**Badges** (GOAL §17.1 as decided 2026-10-23; `court/levels.py`, `chh court level CERT|CASE`, a column of
`chh certificates`; code only, never a model; SEPARATE, not a ladder):
- REPRODUCIBLE: K0 + K3 pass and a bundle is recorded; "across machines" is not checked yet (the badge says so).
- REPLICATED: a certified, reproducible replica case by a DIFFERENT AGENT (`chh court replicate`: a separate
  replicator call given the claim text, never the code). Any model family; the badge names them ("openai -> openai,
  same family"; a different family is the stronger form); invariant 5 is a note.
- VALIDATED: the fresh-data test alone (CERTIFIED-VALIDATED, K0-K9 pass, every target minted after the freeze,
  future, or sealed external data on a human role approval).
- RE-VALIDATED: a second, independent fresh-data test of the same claim and frozen code, declared before the first
  verdict (`chh court register ... --revalidation-round R2`, or `chh court revalidate CASE --beacon-round R2` while no
  label of the first test was read; one per case). A failed one is shown ("validated once, failed re-validation").
The badges reach the Lab only through `chh lab release-verdict` (`LevelResult.release_value`: at most 16 values, no
families or reasons; a ledgered `count` release; field `released_level` of `promote/verdict_released`).
