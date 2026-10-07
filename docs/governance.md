# Governance of the verifier (GOAL §16.7, invariant 22)

AI agents wrote the Kernel, its tests, its reviews and this document. The examinee wrote the exam. This file says
what code enforces, and what only a human can do.

## 1. What code enforces (built)

| Rule | Mechanism | Where |
|---|---|---|
| Two-speed Kernel | `scripts/governance.py` fingerprints the core: GOAL §10 + §4.3 + `kernel/*.py` with the `KernelConfig` defaults blanked. A changed fingerprint fails `check.py --fast` until a record in `redteam/kernel_core_changes.yaml` names it: `window` (>= 7 days after its proposal date) or `tighten` (merges at once). Parameter changes do not touch the fingerprint. | `tests/spec/test_spec_gate.py` |
| Reputation only raises scrutiny | Metamorphic test: a track record of CERTIFIED-VALIDATED claims by the same artifacts never changes the verdict of a flawed claim (identical checks to a fresh store). The Kernel has no reputation input today; the test keeps it that way. | `test_mr_reputation_never_lowers_scrutiny` |
| Every finding keeps a test that attacks | Findings registry + loophole museum + mutation tool | `redteam/`, `scripts/museum.py`, `scripts/mutate.py` |
| Sealed halves are looked at only on the record | Hash-chained look ledger | `scripts/sealed.py` |

What the code CANNOT check:
- that a record's approver is a human (anyone with write access can type a name);
- that a `tighten` change only tightens (the museum and the mutation score are evidence, not proof);
- that an agent did not read the sealed halves (they are files in the repository).

The first record in `redteam/kernel_core_changes.yaml` was written by the assure agent as a bootstrap at f0f703f and
says so. **Needs owner:** confirm it, or replace it with your own record.

## 2. The human-held check (cannot be built by an agent)

By definition an agent cannot build a check that no agent designed or can see. What follows is the procedure; the
check itself must come from the owner.

1. **Who.** The owner, or a person the owner names, who is not running any agent session on this repository.
2. **What.** A small set of claims with known ground truth (for example 5-10), built by the human outside the
   repository: some honest, some broken in a way the human chooses (peeking, a strawman baseline, a leaked label,
   a seed-dependent program, a reused test set under a new id). The human writes down the expected label and the
   expected failing check for each, before running anything.
3. **Where it lives.** Off the agent machine (another computer, paper, an encrypted archive whose key the agents do
   not hold). Never in the repository, the store, a prompt, the scratchpad or a mailbox.
4. **When.** Once per milestone, after the milestone's code is frozen, and before the milestone is called done in
   STATUS.md.
5. **How.** The human runs the frozen Kernel on the set from a fresh store (`chh` CLI only), compares the labels and
   failing checks with the written expectations, and records only a summary in STATUS.md: date, commit, number of
   items, number of mismatches, and the human's name. Mismatches are described in words only after they are fixed,
   and the broken items are then retired (a used item is no longer secret).
6. **Rotation.** Retired items may join the museum or the claim suite. The next milestone needs new items.

## 3. The outside attack per milestone (cannot be run by the agent pipeline)

1. **Attacker.** Someone outside the agent pipeline: a human, or a model of another family run by a human in a
   separate environment that has never seen the agents' reviews or this repository's red-team notes.
2. **Brief.** GOAL.md §4 and §10, the CLI, and the threat model (capabilities A0-A5 in `redteam/findings.yaml`).
   Goal: get a CERTIFIED label for a claim that should be REJECTED, or make an honest claim fail, without editing
   Kernel source.
3. **Budget.** Fixed in advance by the owner (hours or model calls).
4. **Output.** Each successful attack becomes a finding in `redteam/findings.yaml` with a test that fails before the
   fix and a museum patch after it. An attack round with zero findings is recorded too (date, attacker, budget).
5. **Who records.** The owner. An agent may draft the test after the finding is disclosed, never the attack.

## 4. Status (2026-10-07)

- Built: §1 mechanisms (verified by `tests/spec/test_spec_gate.py` and `tests/spec/test_spec_kernel.py`).
- Not built, needs owner: the human-held check (§2) and the first outside attack (§3). No agent can satisfy
  invariant 22; it stays NOT MET until the owner runs §2 and §3 once.
