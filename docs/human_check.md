# Human-held check scaffold (GOAL section 10 invariant 22, section 16.7)

**Invariant 22 stays NOT MET until a human supplies a check.** This scaffold is only the plumbing. It contains no
check, and no agent can write the real one: by definition it is a check no agent designed or can see
(docs/governance.md section 2). Writing it, and choosing what it tests, is a human action.

## What is built

- `src/custom_harness/assure/humancheck.py`, `scripts/human_check.py`, a step in `scripts/check.py`,
  tests in `tests/spec/test_spec_humancheck.py`.
- A human drops a script into `<data_dir>/human/checks/` (outside the repository; `<data_dir>/human` is already
  the human-only L9 directory in `sandbox/base.py`) and writes `registry.json` there:
  `{"checks": [{"id": "m3-a", "file": "m3-a.py", "sha256": "<hex>", "registered_by": "<name>", "registered": "YYYY-MM-DD"}]}`.
  The human also commits the same hash in `redteam/human_checks.lock` (`{"m3-a": "<hex>"}`), so the commitment is
  visible in git history.
- `--verify` hashes each file's raw bytes (never decoded or printed) and compares them to the registry AND the lock.
  A mismatch, a missing file, a registry/lock disagreement, a malformed registry or a `file` that is not a plain
  name inside the checks directory is a FAIL (fail closed).
- `--run` verifies, re-hashes each file immediately before executing it (a change fails the check and the script
  is not run), then runs `python <file> <repo_root>` with a fresh temp directory as cwd, stdin/stdout/stderr
  sent to DEVNULL, and a timeout (default 1800 s; timeout = FAIL). Exit 0 = PASS, anything else = FAIL.
- Only `{id, sha256, result, code, at, head, prev}` is appended to `redteam/human_check_runs.jsonl` (a hash chain:
  `prev` = sha256 of the previous line, `genesis` first). `--check-ledger` verifies the chain. No output or content
  is ever recorded or printed. The ledger is committed empty/absent; the human decides what to commit.
- `--status`: `NOT MET` with nothing registered (never "pass"); otherwise `REGISTERED-NOT-RUN`, `LAST-RUN-PASSED`
  or `FAIL`. `LAST-RUN-PASSED` records that a run passed, not that invariant 22 is met by an adequate check.
- `check.py` step (`--gate`): prints `human-held check: NOT MET (no check registered; invariant 22 needs the owner)`
  and does not fail while nothing is registered. It FAILS on a hash mismatch, a lock/registry disagreement, a
  ledgered failed run, or a broken ledger chain. The gate does not execute the checks (they may take 30 minutes);
  `--run` does, and the human runs it once per milestone.

## Honest limits

- Agents can still technically read files under the data dir. Only a filesystem permission or a separate account
  makes the check unreadable. The mechanism guarantees only that the content never passes through agent-run output
  or ledgers; it does not hide the file.
- A script registered by a human could itself print or exfiltrate; that is the human's responsibility. Output is
  discarded here, but the script runs with the agent user's permissions.
- Residual TOCTOU: the file is re-hashed immediately before launch, but a writer with access to the data dir
  could still swap it in the instant between hashing and interpreter start. Closing this needs the permission
  above.
- The gate step trusts the ledger it can read; an agent could delete a failed ledger tail (git history shows it).
  A lock entry cannot be forged without a visible commit.
- The scaffold does not check that the person who wrote `registered_by` is human.
