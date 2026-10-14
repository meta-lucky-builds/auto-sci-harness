# L5 production skill writer (`memory/writer.py`)

GOAL §5 L5, invariant 4. Branch `s3-verifiers` (2026-10-09).

## Path
`research.certify` (protected) -> `memory_wiring.after_certify(kernel, state, cert_hash, secrets, run_id=..., spawn=...,
targets=...)` -> `SkillWriter`:
- `write_negatives()`: `harvest_negatives` under the writer identity `skill-writer` (verified failures only: REJECTED on
  K6 alone).
- `write_skill(...)` for a CERTIFIED certificate of this run: `SkillWriter.write_playbook` -> `store.propose`
  (QUARANTINED, unreadable) -> `store.admit` (Kernel re-derivation, tier floor, exploit check against the perturbed
  evaluator `research/perturbed.PerturbedTaskEvaluator`, built from the run's sandbox and the task's public development
  dataset + target column).

## Refusals before anything is proposed (`WriterRefused`)
- no exploit check configured; certificate missing, not CERTIFIED, retracted, or not re-deriving;
- any evidence record of the certificate from another run (a run cannot write a skill from another run's certificate);
- the method text describes another program (caller's code hash != claim subject hash).
In `after_certify` a missing sandbox / target column means no skill is written, and the record says why.

## Content
Deterministic from the Kernel certificate and claim (statement, label, tier, metric, minimum effect) plus the
model-written idea text of the certified hypothesis, quoted as DATA (screened by the store; rendered inside the
untrusted-data wrapper; never decides admission). Evidence = the certificate hash, so a Kernel retraction revokes the
skill and every entry deriving from it (`MemoryStore.sync`).

## Stated limits / owner decisions
- With the human-owned tier table (`tiers.DEFAULT_POLICY[ADMIT_SKILL]` = T1), research (T2) certificates are below the
  skill floor: in production the writer refuses every research skill before proposing it (no re-derivation, no
  perturbed evaluation is spent) until the owner sets a T2 skill floor.
- OPEN (review M1): the quoted method text is read from the run-state key `hypotheses`, which evolve/edits.py does not
  reserve, so an added node could rewrite the idea text after the claim. Admission is unaffected (text is data,
  screened, quoted); the fix is to reserve `hypotheses` and `memory` in `RESERVED_OUTPUT_KEYS` (b-evolve's file) or to
  hash the idea text into the claim record at claim time.
- A writer failure is recorded in `memory_after_certify.skill` and never stops negative harvesting, outcome counting or
  the certify node (review M2).
- The perturbed evaluator re-runs programs (k-fold, several seeds): it costs sandbox time on every certified outcome.
- Who may import `custom_harness.memory` is pinned by tests/test_memory.py; the writer lives inside the package.

Tests: tests/test_skill_writer.py.
