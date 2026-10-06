# HANDOFF — read this first in a new chat

Written 2026-10-06 at the user-requested stopping point. Remote `main` = `ad83c28` (+ the commit adding this file).
This file holds what the other documents do not: operating knowledge, lessons from failures, traps, and the exact
next steps. `GOAL.md` says WHAT to build; `STATUS.md` says what is built and verified; this says HOW to keep going.

---

## 0. First 10 minutes in the new chat (do these in order)

1. Read, in this order:
   1. this file;
   2. `STATUS.md`, the "STOPPING POINT" section first, then the M0/M1/M2 tables and "Known limitations";
   3. `GOAL.md` §0, §4 (Evidence Kernel), §5 (component specs, with ✅/🟡/❌ markers), §10 (invariants), §11 (milestones);
   4. only when needed: `code_review/00_CODE_REVIEW_SYNTHESIS.md` (what other systems' code does, the P1/P2 backlog for M3+),
      `../autoresearch_survey/00_SYNTHESIS.md` (the survey; the user calls it the master reference), `PLAN.md` (history only).
2. Start Docker Desktop if it is not running (`docker info`). AutoStart is OFF, and the user has not changed it.
   Do not change that setting yourself.
3. Run the full quality gate and **do not edit code while it runs** (see §4, trap 1):
   ```
   cd C:\Users\mykids\OneDrive\Desktop\META\custom-harness
   C:\Users\mykids\custom-harness-data\venv\Scripts\python.exe -u scripts\check.py
   ```
   ~20 min: lint → mypy → `mypy --strict` Kernel → Kernel tests with a 100% branch-coverage gate (~6 min) → all tests (~12+ min).
   The last full run on the CURRENT code never completed (stopped by request). Expect possible breakage in tests not
   re-run after the M2 changes: `test_infra.py`, `test_resume.py`, `test_snapshot.py`, `test_stats.py`,
   `test_winners_curse.py`, the Hypothesis fuzz tests in `test_fail_closed.py`. Fix, then commit and push.
4. Tell the user the gate result honestly before doing anything else.

---

## 1. Who the user is and how they want work done (non-negotiable)

- Demands **extreme honesty, precision and rigour**. State what was not verified. Never present a stopped or partial
  run as a pass. When a test passes on the first try, check that it can fail (they asked for "radical honesty").
- **Commit and push every change** to `github.com/meta-lucky-builds/auto-sci-harness` (private).
  **No attribution lines** (no Co-Authored-By; `settings.local.json` sets attribution to empty). Plain commit messages.
- Wants continuous bug and loophole hunting while building: red-team your own work and write the attack as a failing test first.
- **Ask before spending model quota at scale.** One validation run is OK; the M2 exit test (~300 Codex calls, ~5M
  tokens) is the user's decision.
- Do not change the user's app settings (Docker AutoStart, etc.). Do not delete `custom-harness/github repo/` (the
  user's GitHub Desktop clone, gitignored). Never run third-party code from the cloned repos under `../` or `repos/`.
- Provider ToS: BYOS only through official auth flows and only for allowlisted providers (Codex CLI = allowed;
  Anthropic subscription = prohibited). The user is not responsible for bans; the product must not circumvent ToS.
- Security rules the product enforces and you must keep: agent-written code runs ONLY in the Docker sandbox; hidden
  labels, graders and sealed data never reach any agent-visible place (prompts, traces, workspaces, the repo).

---

## 2. Environment facts (not written anywhere else)

| Thing | Fact |
|---|---|
| Repo | `C:\Users\mykids\OneDrive\Desktop\META\custom-harness` (inside OneDrive) |
| Runtime data | `C:\Users\mykids\custom-harness-data` (outside OneDrive): `venv/`, `store/` (SQLite + blobs), `bundles/`, `suites/m2/` (21-task suite), `tasks/rings-v1/`, `sandboxes/` |
| Python | ALWAYS use `C:\Users\mykids\custom-harness-data\venv\Scripts\python.exe`. Plain `python` in Bash is a bare system Python 3.12 without the project's deps (fine for small text-patch scripts only). |
| Dev tools in venv | pytest, hypothesis, ruff, mypy, pytest-cov. The host venv has NO scikit-learn/numpy (by design). |
| Sandbox image | `custom-harness-sandbox:py312` (id `sha256:187a5f90…dbaa7`), has scikit-learn 1.9.1, pandas, numpy. Real-data tasks are built INSIDE it. |
| Docker | Desktop 29.8.1, AutoStart off; it has stopped mid-session before. `DockerUnavailable` → ask the user to start it. |
| Model access | ChatGPT subscription via official `codex` CLI 0.160.0 only (`codex exec --json --ephemeral -s read-only`). ~16k input tokens of overhead per call, ~13–15 s per call. No API key, no GPU. |
| GitHub | `gh` 2.102.0 logged in as meta-lucky-builds (keyring; scopes repo, read:org, workflow, gist). Git Credential Manager also works. The GitHub MCP connector is broken ("Authorization header is badly formatted"); use `gh`/git. In PowerShell, refresh PATH first: `$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")` |
| OS quirks | Windows 11. This account CANNOT create symlinks (junctions work). A symlink created inside a Linux container on the bind mount appears on the host as a WSL reparse point that fails `lstat` (WinError 1920) and `readlink` (ValueError); the code handles both. |
| Env vars | `CHH_DATA_DIR` (data root); `CHH_ALLOW_UNISOLATED=1` lets research modules run on `LocalSandbox` (trusted TEST code only). |

---

## 3. Commands you will need

```
# quality gate (full / fast)
...\python.exe -u scripts\check.py
...\python.exe -u scripts\check.py --fast

# one live research run (Docker + Codex) on a suite task, e.g. a repair task with big headroom
set CHH_DATA_DIR=C:\Users\mykids\custom-harness-data
...\python.exe -m custom_harness.cli run examples\research_v1.yaml --role proposer=codex --role executor=codex --role judge=codex --input task_dir=C:/Users/mykids/custom-harness-data/suites/m2/wine_knn_unscaled --task wine_knn_unscaled --docker
# then re-check its bundle on a clean store (path is printed in the run's report / state["certificate"]["bundle_path"])
...\python.exe -m custom_harness.cli reproduce <bundle.zip> --docker

# suite: build (synthetic on host; real + repair inside the sandbox) and validate in Docker
...\python.exe -m custom_harness.cli build-suite <OUT> --docker
...\python.exe scripts\validate_suite.py <OUT>\suite.yaml

# inspection
chh runs | chh trace RUN_ID [--full] | chh certificates | chh ledger-verify | chh predictions | chh bundle CERT_HASH

# state bundle at a stopping point (refuses a dirty tree); commit only the manifest, never the zip
...\python.exe scripts\snapshot_state.py C:\Users\mykids\custom-harness-data\bundles C:\Users\mykids\custom-harness-data\suites\m2 --note "..."
```
The CLI module is `custom_harness.cli` (also installed as `chh` in the venv). A live run of research_v1 is now about
12–16 Codex calls (hypothesize, 3 tournament rounds, critic, ~5 implement, fixes, 2 improve, replicate).

---

## 4. Traps that cost time in this build (avoid them)

1. **Editing code while a gate/test run is in progress** produced stale or mixed results four times. Start the
   gate only after the last edit, and stop/restart it if you must edit.
2. **Backslashes in Bash heredocs get mangled** in this tool environment: `\n` became a real newline, `\b` a
   backspace character, `\\` collapsed. Any text with backslashes → use the Write/Edit tools, or put the patch in a
   `.py` file written with Write. (Corrupted a regex, an f-string and several tests before this was learned.)
3. **Patch-by-replace scripts**: a small `rw(path, [(old, new), ...])` helper (assert old in text, replace, write
   with `newline=''`) was used from the session scratchpad; it is gone with the session, so recreate it if wanted.
   Anchors go stale because `ruff --fix` reorders imports and rewrites code; re-read before patching.
4. **CRLF**: `open(p, 'w')` on Windows writes CRLF. Use `newline=''` (files are LF; `.gitattributes` normalises).
5. **mypy** on `src/` is normal mode; the Kernel is checked with `mypy --strict` separately. A per-module
   `strict = true` override in pyproject made mypy strict EVERYWHERE; it was removed, so don't re-add it.
6. **Kernel coverage gate is 100% line+branch.** Any Kernel change needs tests for every new branch. Remove dead
   branches instead of writing contrived tests (done once in K9).
7. **Probabilistic tests**: one test failed ~10% of the time by design (E6, Binomial threshold). Write tests that are
   deterministic in outcome (loop until the condition occurs, with a negligible cap probability).
8. **Long tool runs**: foreground commands time out at 10 min; run gates with `run_in_background` and read the log.

---

## 5. Architecture you must understand before changing anything

- **Kernel** (`src/custom_harness/kernel/`, ~1.3k LoC, no model call reachable — `test_kernel_quality.py` imports it
  in a clean interpreter and fails if gateway/runtime/research/evolve/httpx get loaded):
  - `claim.py` typed Claim (NaN/inf impossible); `ledger.py` hash chain (tamper-EVIDENT only); `vault.py` datasets
    with public/private files, every private read logged FIRST, laundering refused, evaluate/kernel reads only for
    registered claims; `checks.py` K0–K9; `kernel.py` certify/verify/retract; `predictions.py` VDS; `bundle.py`;
    `checklist.py`; `execution.py` (`run_program`, `ExecRecord`, `SandboxReplayer`).
  - **K4 = valid procedure** (exact planned seeds, every held-out evaluation reported, Bonferroni over
    max(declared n_comparisons, all claims registered on the target)). **K6 = criterion met on never-used held-out
    targets.** So "honest test, no effect" fails K6 only.
  - **K2 peeking** = a `develop`/`audit` read, an evaluate for THIS claim, or any read for a claim not registered at
    that time, before registration. Reads made for OTHER registered claims = test-set reuse → multiplicity, not K2.
  - **K7** = P(effect ≥ δ | no effect) ≤ 2⁻² from the target's resampling noise (and the declared prior).
  - Vault purposes are declared by trusted host code: the log is accountability, not enforcement.
  - Certificates: lookup checks the content hash; a self-consistent forgery written straight into SQLite passes
    lookup and is caught only by `Kernel.verify()` (re-derivation). No signing yet.
- **Statistics** (`verify/stats.py`): seeds are PAIRED → Student-t (stdlib t-distribution, matches tables);
  zero spread → `degenerate` (never evidence); examples → BCa bootstrap averaged over seeds (fast path for
  decomposable metrics); gate/race CIs → Student-t. Tolerances: relative 1e-9 for degeneracy.
- **Research loop v1** (`examples/research_v1.yaml`, `research/modules.py`, `research/claims.py`):
  setup (registers task in vault; ids `<task>@<digest12>/train|test|baseline`) → profile → baseline → hypothesize →
  tournament → critic → socratic filter → next/implement/run/fix → improve ×2 (dev racing) → claim (pre-register) →
  confirm (fresh sandboxes, vault reads, idempotent) → certify (bundle, checklist, route) → replicate (claim+idea, no
  code) → replica_dev (near-copy refusal) → replica_claim/confirm/certify (same δ) → report (from the ledger only) →
  audit (I1 state vs ledger for both certificates, I2 K5, I4 byte-identical re-render + recomputed scores, I5 leaks).
- **Claim rule**: δ = max(task floor, ½ × dev 90% lower bound); q = P(held-out effect ≥ δ) from dev, clamped to
  [0.05, 0.95]. Learned the hard way: claiming the weakest bound (floor 0.02) gets correctly rejected by K7.
- **Evolution score** (`evolve/evaluate.py`): 0.5·certified + 0.5·replicated + 0.5·clip(relative held-out
  improvement, −1, 1); None if infra failed; 0 if the run failed or the audit failed. Range [−0.5, 1.5].
- **Evolution protections** (`evolve/edits.py`): protected nodes setup, claim, confirm, certify, replicate,
  replica_dev, replica_claim, replica_confirm, replica_certify, report, audit; edits may not write
  `RESERVED_OUTPUT_KEYS` or reuse protected modules; audit must be the only terminal; sealed split never used.

---

## 6. Knowledge from building and failing (not in the other files)

- **Live runs so far** (rings-v1, Codex, Docker, BEFORE the Kernel-integrated loop): certified-equivalent results
  0.958 vs 0.351; Codex-written solutions ignored `--seed` (all seeds identical → degenerate seed CI). Expect this
  again; the Kernel handles it (evidence then rests on example resampling, stated in the certificate).
- **There has been NO live run of research_v1 yet.** Every M2 component is tested only with scripted models. The
  first live run is the real test of: Codex obeying the JSON contracts of the new nodes (critic, improve,
  replicate), the replicator producing independent and working code, and the claim forecast being sane.
- **Test-set wear is real and by design**: every registered claim (incl. replications) on a task's test set grows
  its Bonferroni family. Repeated evolution runs on the same task make certification stricter over time → bias
  against later candidates. Mitigation available: set `n_comparisons` in the claim node params of a campaign's
  seed spec to the planned number of claims per task (protected node → human-authored). Not yet used anywhere.
- **Small real test sets** (iris 45 rows, wine 54): K7 will reject most claims on iris (headroom = 1 example).
  Wine has large headroom (0.74 → 0.98) so it can certify. Repair tasks have the largest, known headroom.
- **Suite validation numbers** (baseline → random-forest reference): rings ~0.26/0.36 → 0.84/0.86; xor ~0.5 → 0.89;
  spirals ~0.64 → 0.99; imbalanced F1 ~0.66 → 0.77; friedman RMSE ~5.0 → ~2.0; interactions MAE ~2.18 → ~1.08;
  wine 0.74 → 0.98; cancer F1 0.87 → 0.98; digits 0.91 → 0.97; diabetes RMSE 81.6 → 61.4; repair tasks: wine 1-NN
  0.70 → 0.94, unconverged logreg 0.65 → 0.98, depth-2 tree 0.37 → 0.98, over-regularised ridge 78.7 → 53.7.
- **Bugs this build found in its OWN code** (useful as a checklist of failure modes): unpaired seed statistics; a
  percentile bootstrap under-covering at small n; infra retries repeating held-out evaluations (→ false K4
  rejection); label laundering; throwaway-claim peeking; certify as a free look at labels; unauthenticated
  certificates; unbounded reproduce tolerance; predictable replay seed; NaN claims unhashable; dataset-id collisions
  across same-named tasks; replicator able to see code; profiler calling features "IDs"; reproduce crashing on a
  missing blob; `config.data_dir()` needing a home dir; agent-faked infra failures via Docker exit codes; symlink host
  escapes; private values written into traces; prompts not recorded in traces.
- **Weak spots still open** (also in STATUS): replication shares the author's model family (Codex only); near-copy
  check is line overlap only; ledger not signed; dataset roles self-declared (external/future should become a
  human-approved action); K9 catches verbatim leaks only; a backend raising an unexpected exception type aborts a
  campaign; evolution has never run with a real proposer; adaptive test-set reuse is only Bonferroni-defended.

---

## 7. What to do next (priority order)

1. **Gate**: run `scripts/check.py`, fix failures, commit + push. Report the result.
2. **One live validation run** of research_v1 on `wine_knn_unscaled` (or `digits_tree_depth2`) in Docker with Codex.
   Check: the run completes; the certificate label and its K0–K9 reasons; the replication outcome; audit passed;
   `chh reproduce <bundle> --docker` reproduces. Record the cost (calls, tokens, wall time) in STATUS "Verified
   live". Fix anything it exposes (likely: contract failures in new LLM nodes, the seed issue, replicator quality).
3. **Ask the user** for the M2 exit-test budget (≈300 Codex calls / ≈5M tokens for the 21 tasks; or a subset).
   The exit metric: share of runs ending CERTIFIED or honestly NEGATIVE, integrity failures (target 0), forecast
   calibration (`chh predictions`), cost per certified claim. GOAL leaves "X%" to be set after the M1 baseline:
   propose a value from the first runs, do not invent one.
4. **M3 Evolution v1** (GOAL §5 L6, §11; CR backlog P1 #7–13): read-only evidence export (exclude bundles and
   private blobs!), agentic proposer in a sandbox, structured diagnosis, critic from a DIFFERENT model family
   (needs an API key or open-weights model — ask the user; Codex-only cannot satisfy invariant 5), smoke run,
   ledger upgrades, cost term in the gate, VDS objective once enough predictions resolve. Real campaigns need an API
   budget or rented GPU (user said they will rent cloud GPU when experiments need it).
5. Update `STATUS.md` (and GOAL markers) at every milestone step; keep the stopping-point style when stopping.

---

## 8. File map (where things are)

```
GOAL.md, STATUS.md, PLAN.md, HANDOFF.md      what / state / history / how-to-continue
examples/research_v1.yaml                    the seed research harness (the object the evolver edits)
examples/smoke.yaml, examples/tasks/make_rings_task.py
scripts/check.py | validate_suite.py | snapshot_state.py
snapshots/state-f15c3d4fd53f.manifest.json   manifest of the state bundle (zip lives in custom-harness-data/bundles)
src/custom_harness/
  kernel/      Evidence Kernel (see §5)
  research/    modules.py (loop steps), claims.py (Kernel steps, report, audit), data.py (profiling),
               suite.py (task suite), task.py (task format)
  runtime/     engine.py (graph interpreter, retries, resume, cost journal, trace scrubbing), template.py
               (rendering, private guard, archive, |log), spec.py, modules.py
  evolve/      edits.py (typed edits + protections), evaluate.py (score), selection.py (race, gate),
               loop.py (generation, Evaluator), propose.py (diagnosis + weak proposer), driver.py
  sandbox/     docker.py (create/start/inspect, spawn, environment), local.py (dev only), base.py (safe_path),
               snapshot.py (safe restore)
  verify/      stats.py, metrics.py, leak.py, ranking.py
  gateway/     codex_cli.py (BYOS), openai_compat.py (API/OW), mock.py, registry + providers.yaml
  store/       evidence.py (SQLite: runs, steps, evidence, records, ledger), blobs.py (CAS)
  errors.py (infra vs policy), budget.py (persistent budgets), records.py, tiers.py, cli.py, config.py
tests/         142 test functions (more cases with parametrization); kernel_world.py = the Kernel test world; test_kernel_suite.py = the 50(+1)-claim exit suite
code_review/   28 per-repo reviews + 00_CODE_REVIEW_SYNTHESIS.md
```
