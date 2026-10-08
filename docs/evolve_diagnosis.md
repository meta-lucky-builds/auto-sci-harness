# Structured diagnosis and claim checks (GOAL §5 L6.3) — what is and is not enforced

Branch `b-evolve` (2026-10-08). Code: `src/custom_harness/evolve/diagnosis.py`, `evolve/claims.py`, the hooks in
`evolve/loop.py`, `evolve/propose.py`, `evolve/proposer_agent.py`, `evolve/edits.py`. Tests:
`tests/test_evolve_diagnosis.py`.

## Enforced (fails closed, each has a test)

| Check | Where | On failure |
|---|---|---|
| The diagnosis reads only evolve-split runs | `propose.diagnose(evolve_ids=...)`, always passed by the loop | the run is dropped before anything is read; counted in `n_excluded_split` |
| Every causal signature carries verifier evidence (both runs' `#verdict`, the failing run's failed Kernel checks) and every reference it carries resolves (the step exists by its own index; the check failed in a certificate summarised exactly as the export summarises it) | `diagnosis.pair_divergences` / `link` | the signature is dropped, counted in `unlinked` |
| For the agentic proposer, every signature in its prompt resolves in the export it mounts | `proposer_agent.propose_from_export` → `link_digest(digest, export_refs(export))` | the signature is dropped from the prompt |
| Every citation of an edit (`evidence`, `failure_clusters_fixed`) resolves to something the proposer was shown: a cluster/divergence id of the diagnosis, or `<run>#<step>`, `<run>#verdict`, `<run>#<step>/<check>` present in the diagnosis or (agentic) the export | `claims.check_citations`, called by the loop | edit rejected unevaluated, lineage status `rejected_claims` |
| Cluster ids (C<n>/D<n>) the diagnosis does not contain | same | same (this was "recorded, not enforced" before; now enforced) |
| The claimed change (`claimed_change`, required in the edit schema) equals the real diff of the spec | `claims.check_claimed_diff`, called by the loop after the edit is applied | `rejected_claims`; a missing claim is a rejection too |

| A citation or claimed path that the prediction screen DROPPED (too long, too many, a non-id in `failure_clusters_fixed`) | `loop.UNVERIFIABLE_IF_DROPPED` | `rejected_claims` (it could not be checked; the reason quotes the screen note, never the text) |
| The digest in the proposer's prompt passes the export's leak audit (same fingerprints: private files, canaries, label windows, private run state, private paths) plus a canary-PREFIX rule (error normalisation turns digits into `<n>` and mangles a canary past the verbatim match) | `export.screen_digest`, called by the loop | cluster example excerpts dropped; if still failing, only counts remain (`withheld` = number of findings) |
| A run whose store record names a different task than its outcome (e.g. a gate run in an evolve cache slot) | `propose._evolve_run`, `export.export_evidence` | dropped from the diagnosis (`n_excluded_split`) / excluded from the export (`task_mismatch`) |

Ordering in the loop: prediction screen → novelty gate → dropped-claims rule → static checks (apply, one mechanism) → no-op/duplicate →
**claims (citations + claimed diff)** → critic → smoke → race. A claims rejection is not added to the novelty gate's
rejected signatures: the same ops, honestly cited and claimed, are a new proposal.

Real-diff granularity: dict keys are followed down to the changed value; a list is one value (its path is the list's
path, so a claimed `/edges/3/target` is read as `/edges`); an added or removed key is its own path. The claim must
equal the real diff as a set (no hidden change, no claimed change that did not happen).

## Content-level divergence

When node, route and error agree at every step, the first step whose OUTPUT CONTENT differs is the divergence
(mechanism `content`). A decision divergence also carries the first content difference at or before it (`content`).
Content is compared after the export's own reduction (`export._clean`, `export.embargo`: certificates reduced to their
label, report text withheld) and with run-specific ids normalised (hashes, `*_ref`, ledger sequence numbers, ids of the
form `prefix_<16 hex>`). Only the differing KEY NAMES reach the signature; the values stay in the audited export.

## Not enforced / limits (stated)

- A citation that resolves proves the evidence EXISTS, not that it supports the edit. Whether the cited step explains
  the failure is not checked by code.
- An edit that cites nothing passes the citation check (it claims nothing). Requiring at least one citation would be
  a further tightening; not done (owner decision).
- The claimed-diff check proves the proposer knows what its ops change, not that the change does what the rationale
  says.
- The leak audit is verbatim matching (verify/leak.py limits). Route values and output key NAMES are not
  number-normalised: a held-out value planted in a route or a key name would reach `cause` / `content.keys` (red-team,
  inferred, not reachable through an edit because nothing evolvable runs after the protected certify step). The
  canary-prefix rule is in `screen_digest` only, not yet in the export's own `_audit` (inferred gap: `scores.json`
  failure signatures and lineage smoke reasons carry normalised error text).
- Cluster ids are positional per generation: `C0` resolves to THIS generation's C0, whatever the proposer meant.
- A divergence is a correlate located in time, not a proven cause.
- Fixture changes made for the enforcement (honest fixtures, no assertion weakened): `tests/test_evolve.py` PROPOSALS
  gained `claimed_change` and the first edit no longer cites `C0` (in table-driven worlds there is no cluster C0, so the
  citation was false); `tests/test_evolve_evidence.py` `CostTable` gives a run that earned 0 a failure signature (so
  the fixtures that cite C0 cite a real cluster); `tests/test_evolve_mechanism_smoke.py` items gained `claimed_change`, and in the covert-channel test the edit
  whose covert cluster text the screen drops is now `rejected_claims` (asserted); a fifth, clean copy of that edit keeps
  every sanitisation assertion (delta rounding, at-risk ids, acceptance).

# Generation ledger (GOAL §5 L6.10) — `evolve/generations.py`, hooks in `evolve/driver.py`

Enforced (tests: `tests/test_evolve_generations.py`):
- Record kinds in the store's append-only `records` table (no Kernel ledger kind): `evolve_campaign` (one per
  campaign: the declared annealing schedule, its hash, the n_edits cap, the stall window) and `generation_ledger` (one
  per completed generation: budget used, refs to its `generation_stats` / `lineage` / `harness_edit` records, the
  accept/reject signal, the stall evidence).
- Stall: at least `STALL_WINDOW` (= 3, mirrors `config.ISLAND_STALL_WINDOW` on g-router, UNCALIBRATED) generations
  observed and no accepted edit in the last 3. Under the verdict embargo the only gate outcomes are the accept bit and
  the rejecting stage, so "no improvement in the gate bits" and "no accepted edit" coincide. Read only through the
  proposer-visible views (`stats_view`, `lineage_view`); malformed or inconsistent records fail closed.
- Annealed edit budget (RRSI): `AnnealSchedule` kinds constant / geometric / linear / step, monotone non-increasing,
  floor >= 1, start <= cfg.n_edits (the multiplicity bound is computed from cfg.n_edits). Persisted across resume via
  `campaign=`: numbering and budget continue from the ledger; a resume with a different schedule is refused.
- Generation report: `generation_report` builds every number as a cell with a ref to its ledger record (through the
  proposer-visible views only, so no gate mean/CI/LOFO value can be in it); `audit_report` recomputes every cell and
  the generation's signal/budget/stall from source, type-strict, and reports any mismatch or unresolvable ref.
  Proposer text is rendered as escaped, quoted data labelled unverified.

Not wired: no CLI calls `generation_report`/`audit_report`; without `campaign=` the driver writes no ledger; a
generation stopped by `BudgetExceeded` gets no record; `out_dir` files still use the local loop index on resume.
