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
| `launch` | `main` plus the stage-launch work. It waits for the governance record (section 4), then fast-forwards into `main`. |
| Other branches | Kept as they were. The repository has no archive tags or refs, so none were added, and nothing was deleted. |

## 2. How far the code has been tested

| Code | Test | Result |
|---|---|---|
| b1ec458 (`court-v1` code) | Full gate, two passes, 16 file chunks each, chunk boundaries shifted between passes | Clean (verified): pass 1 3,825 passed, 0 failed, 5 skipped, 20 expected failures, 0 unexpected passes; pass 2 the same totals, chunk p2_04 re-run after a PC sleep killed it (254 passed, 1 expected failure, 0 failed) |
| After `court-v1` (`launch`) | No full gate. The changed code's test files, one file per process, the source-scanning ratchets, and `scripts/check.py --fast` | The changed and directly affected test files passed, each in its own process. `--fast` passed every step except governance (section 4). The list is in the stage-launch worklog |

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

Not built, as GOAL 18.5 defers them: overnight mode, public challenge, integrations, visible self-improvement. HTTPS and
WebSocket are left to the UI stage. The UI itself comes after the first real session (GOAL 18.7).

## 4. What has not happened yet

- **No run with a real agent in a real Lab box.** No model has been called by this route.
- **Zero Court-certified discoveries**, and no simulator-scoped certified result yet.
- The owner's real store has no passing health run for the current Court version. One passed on a scratch store at
  stage-b's version (verified on real Docker, stage-b). Stage-launch changed `court/rehearsal.py`, so that version is gone and a new
  `chh court health --docker` is owed.
- `python scripts/check.py --fast` fails its governance step on `main` since `court-v1`: the owner's rewording of invariant 5
  in GOAL section 10 is part of the core fingerprint and has no record yet. No Kernel source changed. Only a person may
  record it (checklist step 1).
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
4. The UI.
5. P1, then P2; the deferred hardening before any public release.

## Environment

- **Code:** this folder, with git pointing at github.com/meta-lucky-builds/auto-sci-harness (private).
- **Runtime data and venv:** `C:\Users\mykids\custom-harness-data`, outside OneDrive.
- **Python:** `C:\Users\mykids\custom-harness-data\venv\Scripts\python.exe`.
- **Gate:** `python scripts/check.py`, or `--fast` to skip the full suite. A full gate is run in chunks and alone on the
  machine, and takes hours. A PC sleep or logoff kills a run.
- **Docker:** Docker Desktop 29.8.1 with `custom-harness-sandbox:py312` and `:py312-candidate`; `custom-harness-lab:py312`
  from stage-launch. No GPU.
