# Build status

Living checklist against `PLAN.md`. Updated 2026-10-06.

## Environment
- Code: this folder (git repo). Runtime data + venv: `C:\Users\mykids\custom-harness-data` (outside OneDrive).
- Python 3.12 venv: `C:\Users\mykids\custom-harness-data\venv\Scripts\python.exe`
- Tests: `python -m pytest -q` (32 passing).
- Model access available now: ChatGPT subscription via official Codex CLI (`codex exec`) only.
- **Docker: not installed** (user will install Docker Desktop). Until then, agent-written code cannot be executed live (LocalSandbox refuses without `CHH_ALLOW_UNISOLATED=1`, which is for trusted test code only).
- No GPU locally; cloud GPU to be rented when experiments need it.

## Done
| Layer | What | Where |
|---|---|---|
| L0 | Typed harness spec + graph interpreter; full traces; per-step checkpoints; exact fork/backtrack; contracts with bounded retries; failed attempts costed | `runtime/` |
| L0 | Sandboxes: Docker (no network, caps, non-root, read-only rootfs, gVisor if present) + dev-only local; state-exact snapshots | `sandbox/` |
| L0 | Gateway: mock, OpenAI-compatible (API key / Ollama / vLLM), Codex CLI (BYOS); strict-schema conversion; secret redaction | `gateway/` |
| §14 | Provider registry with BYOS gate (allowed / needs_partner_approval / prohibited / unknown), override file, training-on-outputs flags | `gateway/providers.yaml` |
| L1 | Budget ledger in provider-neutral units, 80% warning, hard caps | `budget.py` |
| L2 | Append-only evidence store (SQLite dev), content-addressed blobs, claims only from evidence, records table (lineage/ledger) | `store/` |
| L3 | Tiers T0–T6 + tier policy; T1 trusted metrics; T2 seed bootstrap + paired test-example bootstrap; Bradley-Terry ranking | `tiers.py`, `verify/` |
| L4 | Research loop v0: setup → data profiling → baseline → hypotheses → K-way tournament → implement/fix loop (deterministic routing) → hidden-test verify → claim-graph report → integrity audit (I1, I2, I4) | `research/`, `examples/research_v0.yaml` |
| L4 | Private-data guard: `_private*` state can never render into a prompt | `runtime/template.py` |
| L6 | Typed edits (JSON-pointer ops), static checks (protected nodes, leakage patterns, audit must be the only terminal), trace-grounded diagnosis, LLM proposer with predicted effects, racing (successive halving + paired CI), held-out gate, lineage incl. rejects, harness-science ledger, proposer calibration | `evolve/` |

## Verified live
- Codex BYOS path end to end (`examples/smoke.yaml`): contract validated, critic asked questions only, trace + cost recorded. Observed ~16k input tokens of Codex overhead per call, ~13–15 s per call.

## Known limitations (honest)
- No live research run yet (needs Docker).
- Evolution only tested with scripted LLMs; no evidence yet that a real proposer finds good edits.
- Objective/score function is an M1 placeholder (documented in `evolve/evaluate.py`).
- With few tasks per split, gate CIs are wide → gate is conservative (few acceptances). Needs more tasks.
- Subscription mode is slow and quota-bound; real evolution campaigns will need API or open-weights backends.
- I3 (citation check) is n/a until the literature module exists.
- SQLite only; Postgres before scorecard campaigns.

## Code review of existing systems (2026-10-06)
27 public repos read (28 reports) → `code_review/00_CODE_REVIEW_SYNTHESIS.md`. It contains the ranked adoption
backlog (P0 fixes to our own code, P1 self-improvement + research-loop gaps, P2 verifiers/memory/literature/branching,
P3 weights). That backlog supersedes the "Next" list below where they overlap.

## Live verification (2026-10-06)
- First live research run in Docker (rings-v1, Codex BYOS): 5 hypotheses, 5 working solutions, verified 0.956 vs
  0.351 baseline accuracy (test-example 95% CI [+0.57, +0.64]), audit passed; 9 Codex calls, ~158k input tokens, ~6 min.
  Easy synthetic task: proves the pipeline, not research ability.
- Docker Desktop's engine stopped once mid-session; restart it if `DockerUnavailable` appears.

## Next (in order)
1. Live research run in Docker once installed (build `docker/sandbox.Dockerfile`).
2. Multi-generation evolution driver + CLI (`chh evolve`), task-suite builder (more ML tasks, splits).
3. L5 memory (negative memory, dead-ends, cross-task idea memory) wired into hypothesize prompt.
4. Exploit quarantine (perturbed evaluator) + judge panel (multi-family, agreement stats).
5. Branching/routing (MoB-style), L7 islands/orchestrator.
6. Literature module (arXiv, dated snapshots) → I3 + time-split rediscovery set.
