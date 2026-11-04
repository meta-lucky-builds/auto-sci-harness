# STATUS: what is built, against GOAL.md

Rewritten 2026-11-02 at the `court-v1` merge. GOAL.md says what to build; this file records the state. The old file, last
true on 2026-10-08, is kept at docs/archive/status_2026-10-08.md: its item-by-item tables and its list of accepted
limitations have not been re-checked since. The worklogs are outside the repository, in `META/agent_worklogs/<stage>/`.

Every statement carries one of these:
- **verified**: someone saw the output of a gate log, a test run or a git command.
- **reported**: a stage's agent ran it and wrote it down; nobody re-ran it.
- **inferred**: reasoned from code, never run.

## 1. Where the code is

| Ref | What it holds |
|---|---|
| `main` | The merge of `int18` into the old `main` (c117dcd, tagged `court-v1`). |
| `int18` | Fast-forwarded to `stage-b` b1ec458: everything up to the Experience API. It stops there. |
| `launch` | Merged into `main` once the governance record existed. |
| `product` | `main` plus the stage-product work: the feedback-table fix, the UI, product basics, version 1 features. Fast-forwarded into `main`. |
| Other branches | Kept as they were. The repository has no archive tags or refs, so none were added, and nothing was deleted. |

## 2. How far the code has been tested

| Code | Test | Result |
|---|---|---|
| b1ec458 (`court-v1` code) | Full gate, two passes, 16 file chunks each, chunk boundaries shifted between passes | Clean (verified): pass 1 3,825 passed, 0 failed, 5 skipped, 20 expected failures, 0 unexpected passes; pass 2 the same totals, chunk p2_04 re-run after a PC sleep killed it (254 passed, 1 expected failure, 0 failed) |
| After `court-v1` (`launch`) | No full gate. The changed code's test files, one file per process, the source-scanning ratchets, and `scripts/check.py --fast` | The changed and directly affected test files passed, each in its own process. `--fast` passed every step except governance (section 4). The list is in the stage-launch worklog |

| Stage-product (`product`) | No full gate, and no complete `--fast` pass: the owner ruled out a second run. Fast run at 0ec0bba: conflict markers, strict Kernel types, red-team registry, governance, sealed-look ledger, human-check step passed; Kernel tests 277 passed, 100% coverage, one failure (the ruff check); lint, types and trusted coverage (96.65%) failed. Afterwards `ruff`, `mypy` and the quality test passed alone, and the uncovered lines got tests, but **the trusted coverage step was not re-measured as a whole** | Stage-product worklog |

Stated plainly: **the code after `court-v1` has had no full gate.** The owner chose this for the launch stage (no long test
runs). Any later change to the Court starts a new Court version, which needs a new `chh court health --docker`.

## 3. What is built

| Area | State | Where |
|---|---|---|
| Evidence Kernel, kerneld, vault, release ledger | Built and frozen at about 4,000 lines, 100% line and branch coverage (verified by fast checks) | docs/kernel.md, docs/kerneld_boundary.md |
| The Lab: agent boxes, walls, session records, discovery trace, freeze | Built; the box is tested with a fake docker | docs/lab.md |
| Lab network rule: internal network per session, recording gateway | Checked on real Docker Desktop, with test programs and no agent (reported, stage-p0) | docs/lab.md section 11 |
| Court routes: minted, sealed, retrospective Claim Audit | Built; run with scripted programs on a local sandbox | docs/lab.md section 10, docs/sealed_projects.md |
| Badges | Built, computed by code | `court/levels.py`, GOAL 18.2 |
| P0 follow-ups: health run, failures never spend truth, program contract, seal at project start, rehearsal, feedback split, one display path | Built | docs/court_failures.md, docs/court_rehearsal.md |
| Experience API and `chh exp` | Built: one full session runs through it with real kerneld, a fake docker and scripted programs | docs/experience_api.md |
| Older layers: research loop, memory, evolution, adapters, verifiers, studio, scorecard | Built to varying depth; see the old file and each doc. Not re-assessed here | docs/ |

Stage-launch adds, after `court-v1`: `chh exp doctor`, `chh exp toy`, the
explicit opt-in for a local model server, a wider check for copies of sealed rows, and the Lab image
(`custom-harness-lab:py312`, sha256:b0a4b697318a; each agent tool in it reports its version, and none has run a task).

Stage-product adds, tested with scripted stand-ins and no agent (reported, stage-product worklog):

| Area | State | Where |
|---|---|---|
| Feedback table | K8 and K2 moved to the withheld side; K0, K1, K5 stay shown. Still the owner's decision | docs/court_rehearsal.md |
| UI | All GOAL 18.7 screens, on `chh exp ui`. Plain HTTP and server-sent events, not HTTPS and WebSocket. Agents cannot be started from it. No user testing | docs/ui.md |
| Product basics | Install script (dry-run only; Windows), setup, ML starter kit and tutorial, backup, restore, upgrades, spending limit (not a hard cap), default Lab model `claude-haiku-5-5` | docs/user_guide.md, docs/tutorial_ml.md |
| Version 1 built | Future-data route through kerneld, time window on real-data routes, INCONCLUSIVE, two parallel-world families (synthetic noise), Lab defaults pack | docs/user_guide.md |
| Version 1 built with a stand-in only | Measured no-data prior (no real model is set up), second family setting (never run with a real model) | `court/prior.py`, docs/user_guide.md |
| Version 1 part-built | Same-budget baseline: a `gate` check exists, **the evolution loop does not pass it yet**. Hardened benchmark versions: not built. LLM judging: kept out of the Court by a test, but the older research loop still calls it. Literature: Court limited by a test; other modules stay in place | `evolve/selection.py`, `literature/__init__.py` |

Not built, as GOAL 18.5 defers them: overnight mode, public challenge, integrations, visible self-improvement.

Branch `evolve-embargo` adds an opt-in evolution mode whose campaign should be able to finish (`--evidence embargo`,
docs/evolve_embargo.md, GOAL 18.9). No Kernel file changed. `tests/test_evolve_embargo.py` passed, 31 tests, end-to-end
acceptance included, with scripted programs and no model (verified). The legacy default is unchanged, and its strict
expected failures stay open. The time-split experiment (GOAL 6.4) is still not wired, so no self-improvement has been
shown.

The same branch adds DeepSeek as a Lab provider through OpenCode (`deepseek-api`, docs/first_session_checklist.md). Its
adapter tests pass (verified), but **no call has reached DeepSeek**, its terms are unread, and the entry has no human
sign-off.

The same branch closes three routes to a false VALIDATED, in the Court; no Kernel file changed. Each has tests and a
health-run plant (`tests/test_court_false_validated.py`, verified with a local sandbox, not Docker):

| Route | Now | Still open |
|---|---|---|
| A baseline honest on the public check and sabotaged on the real test | Its held-out score is held against its public score and the trivial answer. A collapse is NOT CONFIRMED, without VALIDATED (`court/comparator.py`) | Sabotage smaller than the allowed drop, 0.15 (UNCALIBRATED). Not applied against the public score on the future route |
| A family retried until something passes | Each try spends `alpha / attempt ** 2`, at most 0.05, so the total stays under 8.3% however many tries (`court/minted.py`) | Counted per store and per family: a new store or a new family starts again. The preflight sizes with the declared level, so a later try can look more certifiable than it is |
| Public data put through the sealed route | A seal must state its provenance, and only `private` escapes RETROSPECTIVE (`court/sealed.py`) | The statement is the researcher's: a false `private` is not caught by code |

These changes make a new Court version, so **the health run recorded in section 4 does not cover them**.

Tested on this branch with those changes, with a local sandbox and no full gate (verified): 22 test files touching the
Court, audit or experience, each in its own process, after two stale tests were updated; Kernel tests, 278 passed, 100%
coverage; trusted code 100%, from one full step (226 passed) plus two new tests added to its coverage data, not one
uninterrupted run. Ruff, governance and the museum registry pass. The `README.md` quick start ran on a clean copy on
Windows: fresh install, UI build, scripted UI, starter kit. `scripts/install.ps1` is still only dry-run tested.

## 4. What has not happened yet

- **No run with a real agent in a real Lab box.** No model has been called by this route.
- **Zero Court-certified discoveries**, and no simulator-scoped certified result yet.
- `chh court health --docker` passed for the Court version of commit 0ce18f3, on the owner's store and real Docker (verified,
  stage-product). The Court has changed since, on `evolve-embargo`, so **a new one is needed before any case can register**.
- Kaspersky on the owner's machine rewrites the UI page and widens its security policy (docs/ui.md).
- Invariant 22 stays NOT MET until a person registers a human-held check.
- `TRUTH_INDEPENDENT` in `court/display.py` is the orchestrator's proposal until the owner has reviewed it.
- The readiness settings and the strawman margin are UNCALIBRATED, as are all values marked so in the code.
- The deferred hardening list of GOAL 18.4 item 9 is deferred, not done. It resumes before any public release.

## 5. What the owner does next

docs/first_session_checklist.md, in order: every human step, its command, what success looks like and what to do on failure.
`chh exp doctor` says what is still missing at any time.

## 6. Next, in order (GOAL 18.8)

1. Owner setup.
2. The first certified result on the minted toy track, labelled simulator-scoped.
3. The headline on sealed real data or future data.
4. Real validation of the UI with 3 to 5 researchers, after a live session.
5. The rest of P1, then P2; the deferred hardening before any public release.

## Environment

- **Code:** this folder, with git pointing at github.com/meta-lucky-builds/auto-sci-harness (private).
- **Runtime data and venv:** `%USERPROFILE%\custom-harness-data`, outside OneDrive.
- **Python:** `%USERPROFILE%\custom-harness-data\venv\Scripts\python.exe`.
- **Gate:** `python scripts/check.py`, or `--fast` to skip the full suite. A full gate is run in chunks and alone on the
  machine, and takes hours. A PC sleep or logoff kills a run.
- **Docker:** Docker Desktop 29.8.1 with `custom-harness-sandbox:py312` and `:py312-candidate`; `custom-harness-lab:py312`
  from stage-launch. No GPU.
