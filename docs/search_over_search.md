# L7 search over search: what is built (GOAL §5 L7)

Status: built and tested on scripted islands (table evaluator, mock proposers and controller). No real campaign has
run it. Every parameter below is UNCALIBRATED (owner decision, GOAL §13.8).

## What was built

- `control/islands.py`: islands (different proposer and mutator mixes) and a deterministic `Orchestrator`.
  - **One stall rule.** `stalled` is decided only by `evolve.generations.stall_evidence` (at least `stall_window`
    generations observed and no accepted edit in the last `stall_window`). The orchestrator calls it with its
    signals; it has no copy of the rule. Two tests pin this: a monkeypatched rule flips every decision, and a
    static check finds no comparison on `accepted` / `observed` / `stall_window` in `step` or `evidence`.
  - Signals are re-validated on `observe` even when built directly, and an accept needs a gated candidate.
  - Levers: `harness_edit`, `more_evaluation` (one more seed, bounded by `max_seeds`), and `weights` (below).
  - Budget may be capped by the caller, but only lowered: `min(budget, floor(cap))`, and a cap that is not a
    finite number >= 0 is refused.
  - Every decision is an `orchestrator_decision` record: lever, evidence counts, policy version and hash.
- `evolve/loop.py`: one additive keyword, `edit_admission`. It runs right after the proposal is parsed. Refused
  edits are recorded `rejected_static`, the edit is shown through `withheld(edit)`, the reason names the edit-type
  quota, and there is no `signature`. So the duplicate gate is not fed, and the same edit can be admitted in a
  later generation. `withheld(...)` does not validate as an `Edit` (extra key `withheld`), so `rejected_signatures`
  cannot re-sign it either (checked). Default `None` = unchanged behaviour.
- `evolve/search.py`: `evolve_islands`, the L7 driver.
  - Each island has its own parent lineage and its own generation-ledger campaign `"{campaign}/{island}"`.
  - Islands run sequentially, in id order, every round.
  - Per round: the orchestrator decides. Then, per island: `evolve_generation` with
    `n_edits = min(decision, cfg.n_edits)` and the decided seeds. The proposer is picked by smooth weighted
    round robin over the island's mix. The edit-type quota is `islands.admit` under that round's policy.
  - The signal is built only by `signal_from_generation`, through the proposer-visible views.
  - Before any run:
    - the sealed split is filtered out;
    - a mix that names an unknown proposer is refused;
    - `check_campaign` gets the worst-case bound: `rounds x islands` generations, `cfg.n_edits`, `max_seeds`
      race seeds and as many fresh seeds, and `cfg.infra_retries`.
- **Controller (optional LLM Backend).** Each round, each island's budget is shown as
  `Budget(limits={"quota_units": budget})` to a `DowngradeGate`. The gate gets a fresh `AdmissionRegister` and a
  kill callback that refuses. The observation is built by code and holds only the round number and the caps (no
  accept bit, no score). The gate applies only shrinks; any other action is rejected and ledgered. The
  orchestrator then applies `min`.
- `control/policy_gate.py` + `Orchestrator.declare_trial / close_round / set_policy`: a policy changes only on
  measured gain (next section).

## The weights lever trains nothing

`weights` is chosen only when the caller passes a `weights_export(island, decision)` hook. The rule:

- the island is stalled;
- it is not underpowered (the `more_evaluation` condition fails);
- its last `weights_after` decisions (UNCALIBRATED, default 2) were already stalled `harness_edit`
  diversifications.

The hook is meant to be bound to `weights.export.export_trajectories` with its declared inputs. It writes SFT rows
and a manifest. That island runs no harness generation that round. A `weights_route` record stores
`trained: False` and the export's counts. **No training happens anywhere** (no GPU, and a weight update stays a
human decision). Without a hook the lever is never chosen.

## Policy gate: the exact statistical form

1. **Declaration (before the first trial round).** A `policy_trial` store record holds:
   - the trial id;
   - the incumbent and the challenger, each as version and parameter hash;
   - `n_pairs` W, `alpha`, `min_nonzero`;
   - the order seed (the coins are NOT part of the declaration: they are drawn per pair, see 2);
   - the declaring round and the test name.

   The declaration is refused when:
   - a trial is open;
   - the challenger version, OR its parameters under any version name (`OrchestratorPolicy.param_hash`), was adopted
     or trialled before (no retrying until a lucky pass; red-team finding: renaming the version re-trialled it);
   - fewer than `cadence` rounds have passed since the last change;
   - the window cannot reach alpha, i.e. 2^-W > alpha, or `min_nonzero` > W.
2. **Window.** W round pairs, starting the round after the declaration.
   - Pair k runs incumbent then challenger, or the reverse, by its coin (an AB/BA crossover). The coin is DRAWN when
     pair k starts: `seeded_coin(order_seed, trial_id, k) XOR (int(sha256(f"{nonce_k}|{trial_id}|{k}"), 16) & 1)`
     with `nonce_k = secrets.token_hex(16)` (injectable for tests only, never a `declare_trial` argument), and the
     nonce and coin are ledgered (`policy_trial_coin`) before the pair runs. Red-team finding (2026-10-09): with
     coins fixed by a declared seed, a seed found by search plus a pure period effect got a no-effect challenger
     accepted at p = 11/1024; with drawn coins that attack is accepted at the test's level (test_redteam_l7.py,
     300 repetitions).
   - Every island must report in every trial round (a signal, or an explicit `Orchestrator.skip` for an island that
     ran no generation: 0 edits or the weights lever). A round with a silent island VOIDS the trial (not accepted).
     Red-team finding: missing signals counted as non-accepts, so lost incumbent-round signals passed a null
     challenger.
   - Every island in a round runs under that round's arm, so the total budget of a round is the same in both arms.
3. **Outcome.** y = the number of islands whose generation in that round accepted an edit. Only accept bits are
   used: no gate mean, CI, per-task value or score. Pair difference: d_k = y_challenger - y_incumbent.
4. **Test.** An exact one-sided sign test.
   - Counts: n+ = #{d_k > 0}, n- = #{d_k < 0}, n = n+ + n-.
   - p = P(Bin(n, 1/2) >= n+) = sum_{i=n+}^{n} C(n, i) / 2^n, computed with integers and `Fraction`. No float is
     compared; a float alpha is read as its decimal, so 0.05 = 1/20.
   - **Accept iff n >= min_nonzero AND p <= alpha.**
5. **Records.** `policy_trial_round` (per round: arm, y); `policy_trial_decision` (n+, n-, n0, p as an exact
   fraction string, accepted bit, outcomes, diffs, complete). A window that does not finish is recorded as not
   accepted.
6. **Adoption.** `Orchestrator.set_policy` refuses a change unless all of these hold:
   - it carries a `policy_trial_decision` equal to one this orchestrator recorded;
   - that decision is for exactly this challenger (version AND hash) against the current incumbent;
   - it is accepted;
   - it recomputes from its declaration;
   - the cadence is met.

**Validity.**
- Under the sharp null (the policy has no effect on any round outcome), a pair's two outcomes do not depend on
  which arm ran first. So the sign of each non-zero d_k is the order coin's.
- Given the non-zero pairs, n+ ~ Binomial(n, 1/2) exactly. This is a randomization test, valid whatever the
  carry-over or period effects are. It is conservative because the distribution is discrete.
- Under the alternative, carry-over (the orchestrator state the previous round left) can bias the size of the
  effect. The test estimates no effect size.
- **alpha holds per trial, not across trials.** Report the number of trials, `len(store.records("policy_trial"))`.
- The coins are drawn per pair from local entropy (`secrets`), not a public beacon: an auditor must trust the host
  that drew them (the nonces are ledgered). A declarer who abandons a trial after seeing early coins can only declare a
  DIFFERENT challenger next (same parameters are refused); each such trial counts against the per-trial alpha
  (reported trial count), which is not controlled across trials.
- A seeded simulation test (4000 null trials, identical arms) checks that the false-acceptance rate is
  <= alpha + 3 binomial standard errors, using the same test function.

**Parameters (UNCALIBRATED, config.py).**
- `POLICY_TRIAL_ALPHA = 0.05`.
- `POLICY_TRIAL_MIN_NONZERO = 5`. 0.5^5 = 0.03125 is the smallest p that 5 non-zero pairs can reach, so 5 is the
  smallest value at which alpha 0.05 can be reached.
- `POLICY_TRIAL_PAIRS = 10` (20 rounds). This is twice `min_nonzero`, so a trial can still pass when half its pairs
  tie, and 9 positive pairs out of 10 give p = 11/1024.
- Also `ISLAND_WEIGHTS_AFTER = 2`, plus the earlier `ISLAND_*` values.

**Cadence for the first trial (a stated choice).** The cadence counts from the last change. The initial policy is
not a change, so the first trial may be declared at round 0. `set_policy` itself still refuses any change before
`cadence` rounds have passed.

## What is NOT built or guaranteed

- No real campaign has run this. The defaults are not calibrated, and nothing shows that islands beat a single
  lineage.
- The `generation_ledger` record's `budget` field is the campaign's declared cap (a constant schedule at
  `cfg.n_edits`). The `n_edits` an island's proposer actually got is in that round's `orchestrator_decision`
  (`generation_stats.proposed` counts what it proposed).
- The decision, trial, weights-route and controller records are EvidenceStore records (`append_record`), not Kernel
  ledger kinds. Adding a Kernel ledger kind would be a `kernel/` change.
- Proposer calibration does not yet steer island budgets (GOAL §8 item 9).
- One challenger per run of `evolve_islands`; there is no automatic search over policies.
- A trial that is accepted but finishes before the cadence allows the change is recorded, and the change is
  applied at the first later round where the cadence allows it. If the run ends first, it is not applied.

## Red-team round (s3-evolve__redteam, 2026-10-09; fixed 2026-10-14)

- Order coins chosen by the declarer: fixed (coins drawn per pair, above).
- Re-trial under a new version string: fixed (`param_hash` excludes the version).
- Missing island signals counted as non-accepts: fixed (a silent island voids the trial; `skip` for deliberate skips).
- Mislabelled prompt edits passed the edit-type quota: fixed in the loop's admission seam: the quota sees each edit's
  EFFECTIVE type (`loop._quota_view`: an edit whose every op targets a prompt-text field, `system` / `prompt` /
  `description`, counts as `prompt_text` whatever it declares). The edit itself is unchanged.
- Decision records carried the unreleased gate stage (`cost` vs `held_out_gate`): fixed at the root, the stage is now a
  ledgered `count` release of the generation (loop.py `GATE_STAGES`; docs/branch_route_reveal.md).
