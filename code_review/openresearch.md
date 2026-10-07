# alphaXiv/OpenResearch vs GOAL.md — what it is, what it does better, what we adopt

Read on 2026-10-07. Repo: github.com/alphaXiv/OpenResearch (MIT, Rust, created 2026-06-07, last push 2026-10-07,
~6.7k stars). Cited in GOAL.md as **OR**.

What I read: README, SKILL.md, SYSTEM_PROMPT.md, AGENTS.md, the skills `orx-experiment-tree`, `orx-evidence`,
`orx-lit-review` (first ~170 lines), `orx-agent-delegation`, `orx-reports`, the nanochat demo evidence pack
(README, run-manifest.json, evaluation-metrics.json, bottleneck-diagnosis report), and from the Rust code:
`src/store.rs` (grep only), `src/commands/exp.rs`, `create_experiment.rs`, `plan_gate.rs`, `invocation_gate.rs`,
`supervise.rs` (grep), `src/local/localrun.rs` (head), plus a GitHub code search over the whole repo for
`frozen`, `freeze`, `provisional`. I did NOT read the UI, the compute backends, the paper/Overleaf code, or most
of `up.rs` (330 KB). I ran none of their code.

Labels: **verified** = I read it in the file named; **inferred** = my reasoning from what I read; anything not
read is said to be not read.

---

## 1. What OpenResearch is

A local-first desktop app + CLI (`orx`) that turns existing coding agents (Claude Code, Codex, OpenCode, Cursor,
Antigravity, or local models via OpenCode) into research agents (verified, README). The agent gets a system
prompt ("playbook") plus a set of skills; `orx` owns a project = a git repo + an **experiment tree** (one git
branch per node, one fixed run command), a local SQLite store of runs and logs, many compute backends (local,
SSH, Slurm, Kubernetes, Modal, Ray, Hugging Face Jobs, Tinker, managed compute), literature connectors (alphaXiv
full-text keyword + embedding search, OpenAlex, bioRxiv via OpenAlex, PubMed via E-utilities), and artifact
outputs (reports, publication-style figures, LaTeX papers, Overleaf) (verified, SKILL.md / README).

It is a **workspace and orchestration product for a human + coding agent**. It is not a verifier. It has no
pre-registration, no held-out confirmation, no statistics, no certificates, no leak audit (verified by absence in
everything read; inferred for the code not read).

## 2. Where it is weaker than GOAL.md (do not copy)

| Topic | OpenResearch | Evidence | GOAL position |
|---|---|---|---|
| Experiment discipline is prompt-only | "Never edit a node once a run has answered it" and "the run command and environment are a fixed contract" are the "cardinal rules" in SKILL.md, but a repo-wide code search finds `frozen`/`provisional` only in skills, the UI and unrelated code. The store keeps a `run_command` column per experiment with a full-row update function and I found no check that forbids a child's command differing | verified (code search + `store.rs` ~L1928–1940); "no enforcement anywhere" is inferred, since code search can miss things | GOAL §1.2 already says prompt-only invariants are a failure mode; this is another instance |
| Isolation | local runs are detached host processes with a synced environment (`localrun.rs`, `list_synced_env`); the coding agent itself runs on the host with the user's tools | verified (localrun.rs head); scope of synced env not read | invariant 1 forbids this for agent code |
| Acceptance rule | "Promote" a node to parent of the next round when "result is a clear win"; stop after ~3 failed/regressed runs. No seeds, no CI, no re-measure | verified (orx-experiment-tree) | winner's curse; GOAL L6.8 / M0 re-measure |
| Repair loop | a node is "provisional" until a run "answers" it; implementation failures (OOM, bug, timeout) are repaired in place and re-run, cap 2 consecutive non-answers. Nothing records how many repairs preceded the answer | verified (skill text) | garden of forking paths unless repairs are counted (inferred) |
| Causal claims in reports | the demo's bottleneck report concludes "insufficient pretraining" is the primary bottleneck from one run's curve + literature; no ablation run tests it. Its CORE numbers (e.g. 0.5625 accuracy) look like ~16-example evals | verified (report text, metrics JSON); the sample size is inferred (0.5625 = 9/16) | would be SUPPORTED/T5 at best under the Kernel |
| Evidence contract | every chat claim must link a run or file; "status alone is not evidence" — enforced by prompt only | verified (SYSTEM_PROMPT.md) | GOAL invariant 8 enforces this in code for reports |
| Telemetry | opt-out usage analytics in release builds | verified (README) | GOAL L9: opt-in only. Keep ours |

## 3. What it does better, or that GOAL.md lacks (adopt)

1. **Experiment-tree shape and node lifecycle** (verified, orx-experiment-tree). A round is a small fan of co-equal
   options of ONE decision (siblings); the next round descends from that round's winner; flat fans off the root
   and single-child chains are named failure shapes. A node is provisional until a run answers its question,
   then permanently frozen; children differ from the parent only in committed code/config, never in command or
   environment. GOAL L4.7 has draft/debug/improve but no round structure, no node lifecycle, no run-contract rule.
   **Adopt, enforced in code** (OR enforces none of it): immutable answered nodes (content hash), identical run
   contract across nodes, every repair attempt counted in the ledger, tree-shape diagnostics logged.
2. **Run from a recorded revision, not the live worktree** (verified, localrun.rs comment: the run extracts the
   recorded revision into its own run dir). We already snapshot; make it an explicit L0 rule.
3. **Literature for life sciences** (verified, SKILL.md / orx-lit-review): PubMed (E-utilities) and bioRxiv are
   connectors; GOAL L4.3 lists only arXiv / Semantic Scholar / OpenAlex / Crossref, although GOAL's scope is now
   life sciences + engineering. Full-text retrieval with match snippets (alphaXiv) is also useful.
4. **Retrieval discipline that maps onto time-split** (verified, orx-lit-review): one publication window and
   priority fixed for the whole retrieval loop and inherited by every follow-up call ("never widen a window");
   difficulty-capped number of follow-up rounds; dedup by id, then DOI/arXiv id, then normalised title; and
   "never invent or recall an ID": only IDs that appeared in a successful retrieval result may be cited. OR
   states this in a prompt; we can enforce it: a citation counts only if its ID appears in this run's recorded
   retrieval log (retrieval provenance), in addition to resolving and matching.
5. **Per-completion scheduling** (verified, orx-experiment-tree step 6): wake on the FIRST completion, re-read the
   store as the source of truth, act on every newly terminal run, refill the slot; terminate when drained. A
   good pattern for the L1 evaluation allocator (racing) instead of batch barriers.
6. **Compute backends** (verified, README / SKILL.md): SSH, Slurm, K8s, Modal, Ray, HF Jobs, Tinker. GOAL L0 says
   only "per-campaign cloud-GPU backend". Adopt as a backend interface **with an isolation class**: only backends
   that meet invariant 1 (container, no secrets, network policy, read-only root) may run agent-written code.
7. **Large-artifact manifest** (verified, demo run-manifest.json): omitted outputs (weights, optimizer states,
   datasets) are listed with path, bytes, sha256, and whether they are bundled. GOAL's Reproducibility Bundle does
   not say how multi-GB artifacts are handled; M8/L8 will need it.
8. **Figures and papers as outputs** (verified, orx-reports / orx-paper / orx-figures names): reproducibility
   scripts kept beside each figure; LaTeX paper drafts. GOAL has reports from the claim graph only. Adopt for
   figures with invariant 8 extended: every plotted value resolves to an artifact and the audit re-renders it.
9. **Delegation hygiene** (verified, orx-agent-delegation): a helper's brief must state exactly which compute
   launches it may make (explicitly forbid them otherwise), helpers cannot spawn helpers, an in-flight cap, and a
   helper never gets a branch another session holds. Useful for GOAL §15 multi-agent expansion.
10. **Interactive (copilot) mode + many agent front-ends** (verified, README). GOAL is autonomous-first and CLI-only
    (L9 ❌). OR's popularity is evidence that a workspace UI and "bring your coding agent" matter for adoption
    (inferred). Use of coding-agent CLIs must stay within provider ToS (GOAL invariant 11).

## 4. Honest bottom line

OpenResearch solves usability, breadth of compute and literature access, and day-to-day experiment hygiene. GOAL
solves trust. The two overlap mainly in the experiment-tree discipline, where OR has the better *design* and
GOAL has the better *enforcement philosophy*: the useful move is OR's rules, enforced by our Kernel/ledger
machinery. Nothing in OR changes GOAL's core (Kernel, held-out confirmation, VALIDATED loops). Nothing here was
run; all OR behaviour statements come from its docs and a partial code read.
