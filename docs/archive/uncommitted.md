# Work not yet on `main` — what each piece is and what it needs

State on 2026-10-07 (verified with `git status` / `git rev-parse` this session):
- There is **no uncommitted code** anywhere. Every piece of work below is committed and pushed to its own branch on
  origin, with local and origin at the same commit. What is missing is **merging to `main`**, and each merge needs
  the full gate (`python check.py`) to pass on the merged result first.
- `main` = 0cf8177 (m3-adapters merged after its full gate).
- `.claude/` is untracked on purpose. It holds local worktrees and settings and must never be committed.

Merge order (each step is gated, then pushed):
1. m3-adapters
2. m3-fix-gateway
3. m3-b1-compute
4. m3-fix-r1 (finished)
5. m3-b3-evolve
6. R2 fixes

The reason for this order: m3-b1-compute and m3-b3-evolve touch `checks.py` and the loop, so they should land on
top of the adapter split and the K2 fix. The full design for each fix is in HANDOFF2.md.

---

## 1. `m3-adapters` @ 20cb09e — MERGED to main (0cf8177)

**Contents:**
- `kernel/adapters.py`:
  - content-hash adapter identity;
  - fault-injection validation (9 ML checks);
  - `adapter_approval` ledger entries naming a human;
  - K1 adapter resolve;
  - `chh approve-adapter`.
- The approval node: `RunStatus.PAUSED`, `chh approve`, and the approved item bound by its hash.
- LOFO/LODO gates: `SuiteTask.family/domain`, `held_out`, `check_held_out`, `non_regression`.
- Dev-time cap (`DEV_TIME_FACTOR` = 2).
- Physical evidence path: protocol_review and data_received approval nodes, `chh return-data`,
  `policy_prohibited.yaml`.
- STATUS records for live run 2 and the open R1/R2 findings.

**Verified:**
- 0f174d3 was fully gated: Kernel 101 passed at 100% line+branch coverage; whole suite 274 passed, 1 skipped.
- On 20cb09e (gate10), these passed: lint, types, Kernel strict types, and Kernel tests (101 passed, 100% coverage).

gate10 finished: all tests 281 passed, 1 skipped, `== all tests: OK`. Merged as 0cf8177; the merged tree differs from the gated tree only in HANDOFF2.md, liverun1.md and uncommitted.md (no code).

**Needs:**
- gate10 must finish with `== all tests: OK`. Then fast-forward `main` to it and push.
- **Behaviour change once merged:** the `research_v1` loop refuses to run until a human approves the `ml` adapter.
  The user runs `chh approve-adapter ml`. Claude never runs it.

## 2. `m3-fix-gateway` @ 653d757 — needs full gate

**Contents:**
- `gateway/base.request_fault`.
- `codex_cli` and `openai_compat` no longer label request-caused errors as infrastructure errors (`infra=False`).
  Before this fix, a bad request could pause a run as "infra" forever.

**Verified:** `tests/test_gateway_faults.py`: 11 passed, and the tests were shown to fail on the old code.

**Needs:**
- the full gate after rebasing on main;
- then merge.

## 3. `m3-b1-compute` @ 346f1f6 — needs full gate + follow-ups

**Contents:**
- `kernel/lp.py`: exact-rational checking of LP certificates.
- `CHECK_APPLICABILITY`.
- `ClaimKind.COMPUTED_VALUE`.
- `Label.CERTIFIED_EXACT`.
- The `lp` adapter.

**Verified, from the builder agent's run:** Kernel 109 passed, 100% coverage.

**Not verified:**
- I have not re-run the Kernel tests myself.
- The full suite has not been run on this branch.

**Needs:**
1. Rebase on main after steps 1–2, then run the full gate.
2. Something that produces CERTIFIED-EXACT claims. Today nothing in the loop does, so the label is reachable only in
   tests. This needs either a computational task in the suite or an explicit note in STATUS that the label is
   unused.
3. A human approval of the `lp` adapter (`chh approve-adapter lp`) before any real use.

## 4. `m3-fix-r1` @ 3ff66ab — WORK IN PROGRESS, tests not passing

These are the integrity fixes for the Kernel red-team findings (R1). Branch `m3-r1-kernel`, file
`tests/test_review_kernel.py`: 10 tests fail, 8 hold, 1 is xfail.

**Done on the branch:**
- In `checks.py`: `INTEGRITY_PURPOSES`.
- `related_targets`, with a guard against unregistered targets.
- **New K2 rule.** Integrity reads (`leakscan`, `reproduce`) are logged but not counted as peeking.
  This fixes a real bug, **proven** by `tests/test_probe_two_runs.py`: a second run on the same task in the same
  store was rejected at K2 because of run 1's audit leakscan read.
- The K4 Bonferroni family now includes related targets.
- `replay_seed` is restricted to records that match the plan.
- K4 now gives a reason for stray records.
- `selection.py`: the NaN fix for non_regression.

**Remaining:**
1. R1-F3 and R1-F5. HANDOFF2.md has the designs.
2. Bind `vault.read` to its target.
3. Coverage tests that bring the Kernel back to 100% line+branch coverage. The new branches are not yet covered, so
   the gate will fail until they are.
4. Turn the temporary probe test into a permanent regression test.
5. Every R1 test in `test_review_kernel.py` either passes or is justified as an xfail in writing.
6. Kernel version bump and docs (`docs/kernel.md`, STATUS).
7. The full gate.

## 5. `m3-b3-evolve` @ 4769fd3 — built by an agent, not reviewed

**Contents:**
- `export.py` and `diagnosis.py`.
- A cost term.
- Ledger upgrades.
- A novelty gate.
- Decline.
- A fix for a proposer-prompt leak.

**Verified, from the agent's run:** its tests: 62 passed.

**Not verified:**
- I have not reviewed the diff line by line.
- The full gate has not been run.

**Needs:**
1. A review of the diff, especially that no hidden-label path reaches the proposer prompt.
2. Rebase after step 4, then the full gate.
3. The cost-term default is uncalibrated. Set it from live-run costs: run B was 13 calls, ~207k input tokens, and
   took 8,074 s.

## 6. R2 loop fixes — NOT STARTED

These are the red-team loop findings. Branch `m3-r2-loop`, file `tests/test_review_loop.py`: 9 tests fail, 19 hold.
- R2-1, R2-2, R2-4 and R2-5 are open. R2-3 was handled in the decisions batch.
- HANDOFF2.md has the designs.

**Needs:**
- a branch from main after step 5;
- fixes with failing-first tests;
- the full gate.

## 7. Red-team test branches `m3-r1-kernel` @ f26a4f4 and `m3-r2-loop` @ bc69910

These branches hold only the failing review tests; they are not to be merged as they are.
- Each test is copied onto its fix branch together with its fix, as `m3-fix-r1` already does for
  `test_review_kernel.py`.
- When the fixes have landed, delete both branches.

---

## Human actions pending (only the user can do these)
1. `chh approve-adapter ml`, after m3-adapters is on main. This is required before the next live run.
2. `chh approve-adapter lp`, after m3-b1-compute lands.
3. A second model family (an approved provider) so that invariant 5 is MET. Today it is NOT MET under an explicit
   override.
4. Lean toolchain install for the formal/T0 evidence class. It is not started.
5. The budget for the M2 exit test.
6. The next live run should run alone on the machine. Run B's timeouts happened while 5 agents and test suites were
   sharing it (see liverun1.md).
