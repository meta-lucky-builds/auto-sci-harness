# Hypothesis front of research_v2 (GOAL §5 L4 stages 1, 3, 4, 5; §8 items 11, 17; §16.8)

Branch `s3-hypo`. Code: `src/custom_harness/research/front.py` (the four module nodes), `formulator.py` (tier per
evidence class, literature mining, unified ranking), `hypotheses.py` (mechanisms, admission), `tournament.py`
(`constrain_to_execution`). Spec: `examples/research_v2.yaml`. Tests: `tests/test_hypothesis_front.py`.

## Flow (research_v2)

```
setup -> profile -> baseline -> recall -> formulate -> front -> hypothesize -> rank (admit_rank) -> critic ...
   ... next/run --verify--> diversify -> ensemble (members: diversity) -> claim ...
rank --error (hypothesis set rejected)--> report
```

| Node | Module | Default | What it does |
|---|---|---|---|
| `formulate` | `research.formulate` | **off** (`enabled: false`) | L4.1 Problem Formulator. Ranks candidate problems by `tier_weight(best AVAILABLE verifier tier) x value x feasibility`. Sources: the declared-grammar anomaly scan of the public DEV training file (baseline out-of-fold error pockets; permutation-max corrected; grammar committed to the ledger first) and anomalies / open problems / failed replications mined from the stored abstracts of papers this run retrieved from a frozen, dated snapshot (run input `literature_snapshot`, param `literature_queries`; offline). |
| `front` | `research.hypothesis_front` | **off** (`enabled: false`) | Declares, before generation: the topic quota (topics, cap, per-topic caps), the forced distance-3 cross-domain inspiration (distance fixed at 3, not a parameter; at least 1 hypothesis must use it), and literature-mined mechanisms (verbatim sentences of retrieved papers with their retrieval-log ids; at least 1 hypothesis must cite one when any was mined). Rendered into the hypothesize prompt through `{{hypothesis_front.prompt}}` (empty when off). |
| `rank` | `research.admit_rank` | admission only when `front` is on; tournament **off** (`tournament: null`) | Admission (`hypotheses.admit`), then ranking. A hypothesis citing an id that is not in the run's retrieval log (re-derived from stored responses), with an undeclared topic or over its cap is rejected (removed from the queue, reason recorded in `hypothesis_admission`). A set failing a minimum (forced inspiration, mechanism citation, nothing admitted) is rejected as a whole: the node errors and the run goes honestly to `report` (no claim). Ranking: execution (proposer order; dev cross-validation decides). |
| `diversify` | `research.diversity_select` | **on** (0 calls) | §8.11: pairwise Pearson correlation of the executed candidates' out-of-fold dev error vectors (0/1 mismatch for classification, signed residual otherwise); a candidate with r > 0.8 (GOAL "SDE r>0.8") against a better kept one is collapsed. Not by vendor. The incumbent is first, so it is never dropped. |
| `ensemble` | `research.ensemble`, `members: diversity` | on | The one pre-declared vote ensemble draws its members only from the decorrelated set (then the existing signature dedup). Without a diversity record it refuses (fail closed). Without the `members` param it behaves exactly as before. |

## Rules enforced in code (tested)

- **Tier never from a model.** `ProblemProposal` has no tier field (extra fields refused). `rank_problems` computes
  every tier with `best_available_tier_for(ledger, class, metric)`, i.e. K1's `adapters.resolve` over the registry: a
  class with an approved adapter gets its class tier (formal T0, computational T1, data T2, simulated T2 (the weaker
  of "T1/T2 within the simulator"), physical T3), otherwise T6 with weight 0, priority 0. So an unverifiable problem
  ranks below every verifiable one with positive value and feasibility.
- **Deterministic ranking.** Sorted by (-priority, tier, id); mining is order-independent.
- **Literature provenance.** Problems and mechanisms are mined only from records with an id in the run's retrieval
  log; admission re-reads the log (`RetrievalLog.retrieved_ids`, which re-derives every entry's ids from the stored
  response) and fails closed to the empty set on any integrity error.
- **Execution outranks the judge.** With the tournament opted in, `constrain_to_execution` refills the positions the
  LLM gave to executed candidates in execution order: for any two executed candidates, the one with the strictly
  better dev score comes first; the judge only orders equal scores and places unexecuted candidates (which may push
  an executed one later, a downgrade). The incumbent is still chosen by dev evidence in `run_candidate`. The opt-in
  may only REORDER: Improve-weakest / Combine (new, never-admitted hypotheses), extra generations and `max_pool`
  pruning (the judge deleting admitted hypotheses) are refused, and the output set must equal the admitted set.
  **Limit (stated):** in research_v2 `rank` runs before any execution, so no execution evidence exists there yet and
  the judge decides the TRY order (which, under `max_attempts` or a budget stop, can decide what is executed at all);
  it cannot decide the incumbent. The execution constraint bites only where `rank` sees executed attempts (a fork or
  resume, or a spec that re-ranks).
- **Reserved state.** `hypothesis_front`, `hypothesis_admission`, `problems`, `literature_pool` and `diversity` are
  reserved output keys (an evolved node cannot fake an empty front or a diversity record); the four modules are in
  `RESERVED_WRITING_MODULES`.
- **Relays are not evidence.** `research.execution_rank` (a pre-existing gap: it relays model-written hypotheses but
  was citable by critic answers), `research.admit_rank`, `research.hypothesis_front` and `research.formulate` are in
  `RELAY_MODULES`.

## Yield (GOAL §16.8; invariant 21). Model calls per run

| Stage | Model calls added | Default | Other cost | Why this default |
|---|---|---|---|---|
| `formulate` (Formulator) | 0 | off | CPU: B=199 permutations x cells x rows (pure Python; about 5-10 s at 1k rows x 40 cells, inferred); prompt tokens for the top 5 problems | 0 calls, but it changes the hypothesize prompt; off until the owner measures it on the suite |
| `front` (topics, inspiration, mechanisms) | 0 | off | prompt tokens; offline snapshot reads; a rejected set ends the run (loses the run's later calls, saves the implement calls) | 0 calls, but a rejected set ends a run: off until measured |
| admission in `rank` | 0 | on whenever `front` is on | none | enforcement is the point of the front |
| `diversify` | 0 | on | O(n_candidates^2 x n_dev) arithmetic | 0 calls; GOAL §8.11 is the selection rule |
| tournament in `rank` (opt-in) | K-way: `rounds` x blocks (whole pool: 1 call per round, default 3); pairwise both-order: `repeats` x n(n-1) (20 for 5 hypotheses); +1 per Improve-weakest and +1 per Combine per extra generation | **off** | none | §16.8: execution is the ranker; a judge ranking can only reorder what to try first |

Research_v2 with defaults therefore makes exactly the same number of model calls as before this change (verified by
`test_v2_defaults_are_off_and_cost_no_call` structurally and by the unchanged `test_research_yield` call counts).

## Limits (stated, not solved)

- Literature mining is lexical (a declared cue lexicon): it finds what papers SAY is open, anomalous or
  unreplicated, not whether it is; value and feasibility weights are UNCALIBRATED (GOAL §13.8). The "literature
  graph" is the record set one run retrieved, not a citation graph.
- Hypothesis `mechanism_cites` are checked for retrieval provenance only. Title / year / quote checks (K0/I3) apply to
  `citations` in the audit; a hypothesis is not a claim, and the mechanism text it relies on is a verbatim mined quote.
- Diversity acts after execution (error vectors need dev predictions); it cannot save the implement calls of a
  duplicate. It changes which candidates the ensemble may use; it does not change the incumbent.
- `formulate`, `front` and `rank` are PINNED (evolve/edits.py): their definitions (enabled flags, caps, the
  tournament opt-in) are human-only. Their WIRING stays evolvable like the critic chain's: an edge edit could route
  around `front`; the hypothesize prompt then fails to render `{{hypothesis_front.prompt}}` unless the prompt is
  edited too. The front's STATE cannot be faked (reserved keys). Quotas are a yield/diversity control, not an
  integrity check.
- When admission rejects a set, the record is in the `rank` step's error text, not in state (a failing module
  writes no state).
- The auto-grammar excludes id-like, constant and high-cardinality (> 12 levels) columns; with fewer than
  2 x min_cell dev rows the scan is not run (recorded in `problems.notes`).
