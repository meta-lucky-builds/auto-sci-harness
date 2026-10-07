# Product capabilities: jobs only a claim-certifier can do

Lens: direct capability upgrades that researchers would use, built on what exists in `wt-int` (branch m3-int,
head 41d8ef7). Labels: **verified** = I read the code or saw a source; **inferred** = reasoned from code, not run;
**guess** = judgement. Ideas already proposed (preflight, cassettes, `explain`, copilot, loophole museum, α-slots,
FunSearch-on-LP, generated task families) are reused as parts, not re-proposed.

## 0. First principles

What the code can already do that nothing else can (verified by reading `kernel/`, `literature/cite.py`,
`research/physical.py`, `kernel/sim.py`):
1. **Bind a typed claim to a hash-chained commitment before its labels are read** (vault access log + K2).
2. **Re-run a comparator under the identical run contract** (K5 compares inputs, target, argv, env, limits,
   metric, entrypoint).
3. **Refuse to say more than the evidence class allows** (CERTIFIED-EXACT / -SIMULATED / -FORMAL / -VALIDATED,
   physical never "done").
4. **Ship a bundle that a stranger re-checks with one command** (`chh reproduce`, full ledger chain).
5. **Check a citation mechanically**, including verbatim quote support against a dated snapshot.

The scarce thing in science that these produce is *trust that does not depend on trusting the author*. So the
highest-value jobs are those where the author is somebody else (audit), or where the author needs to prove
trustworthiness to somebody else (notary, manuscript binding). Generating new claims autonomously (M2) is the
job the field is crowded with; certifying claims is not.

Two hard limits shape every proposal (verified, `checks.py`):
- **K7 fails every negative_result and equivalence claim** (fail closed, §13.5). The system cannot *certify* a
  refutation today. It can only report "the positive claim was not confirmed" plus the observed interval.
- **K4 has one design**: paired t over seeds + BCa over examples. Replicate-level unpaired lab designs, time-course
  designs and survival analyses are not expressible (inferred from `AnalysisPlan.test` being a single literal).

---

## Rank 1. Claim Audit: pre-registered confirmation of someone else's published claim (`chh audit`)

**User / job.** A reviewer, a group deciding whether to adopt "method X", or a meta-researcher: "Does the
paper's headline claim hold on data the authors never touched, against a baseline run under identical conditions,
and do its citations say what it says they say?"

**Why valuable; why nothing else does it.** Existing tools reproduce *the same numbers on the same data*:
PaperBench (20 ICML papers, rubric-graded, best agent 21%; verified), CORE-Bench (270 tasks from CodeOcean, agents
answer questions about outputs; verified), ML Reproducibility Challenge and ReScience (human). Re-running the
authors' test set cannot detect test-set tuning, a strawman baseline or a claim that holds only on the chosen
datasets. A 2025 study found tabular papers use on average fewer than 10 datasets, half older than 20 years
(arXiv 2503.09159; verified via search summary). What none of them do (inferred from their descriptions, not
exhaustively verified): commit the claim *before* touching fresh external data, re-run the baseline under an
identical contract, and output a mechanically re-checkable certificate or a typed non-confirmation.

**Reuses.** Claim language (IMPROVEMENT, scope, generalization_targets), vault roles `external` + `chh
approve-role`, K2/K4/K5/K6/K9, sandbox replayer, bundles + `chh reproduce`, `literature/cite.py` (I3 with
quotes), the approval node (for statement fidelity, same pattern as Lean's `approve-statement`), preflight (16.6)
to refuse underpowered audits before spend.

**Missing.**
- `audit/` front end: a paper record (DOI via literature module) + a claim YAML + a repo ref.
- **Contract shim**: a small wrapper that makes the paper's code read the run contract's inputs and write
  predictions. Both arms run through the same shim. Written by a human or by Codex (sandboxed), hashed as part of
  the subject/comparator artifacts.
- **OpenML / public-dataset ingester** that registers fetched datasets as `external` with their licence recorded.
- **Statement-fidelity approval**: a human approves "this typed Claim is a faithful reading of the paper's
  sentence S" (hash-bound). Listed on the certificate as an UNTESTED assumption, exactly like formal claims.
- **Verdict taxonomy** in the report, computed from the certificate, never by a model:
  CONFIRMED (CERTIFIED-VALIDATED) · NOT CONFIRMED: criterion missed, interval [lo, hi] (and "upper bound below the
  paper's reported effect" when true; an observation, not a certificate) · NOT CONFIRMED: other check failed
  (with the K reason) · INCONCLUSIVE (preflight: underpowered at this n) · UNRUNNABLE (infra; excluded).

**GOAL touched.** §4.2–4.4 (no change to checks), §4.7 data class, §5 L4.3 citation check, L2 bundles, L9
approvals. Invariants 1, 2, 3, 7, 8, 9, 11 (licences), 15. None weakened; one new listed assumption.

**Plan (laptop + Docker + Codex CLI, no GPU).**
- M-A1 (3 days): claim YAML → Claim; OpenML ingester with licence field; shim template for sklearn-API estimators;
  audit report with the verdict taxonomy.
- M-A2 (2 days): literature pass: extract the paper's reference list (Crossref/OpenAlex), run `check_citations`
  on the audited sentence's citations; quote checks only where the paper quotes.
- M-A3 (1 week): Codex writes shims for repos with non-sklearn layouts; shim is shown to the human at the
  statement-fidelity approval.

**Acceptance test (falsifiable).** 5 published sklearn-scale tabular claims chosen *before* any run (frozen
list committed to the ledger), each tested on ≥ 3 OpenML datasets not in the paper, 5 seeds, plus 3 planted
inflated claims (baseline at bad defaults with a tuned subject; subject tuned on the target; label leak through a
shared ID column). Pass iff: all 3 planted claims end NOT CONFIRMED with the correct K reason (K5/K2-or-K6/K9);
0 crashes reported as policy failures; every audit's bundle reproduces on a second machine; the citation pass
flags ≥ 1 of 2 planted wrong quotes and 0 of 10 correct ones. Expected outcome (guess): 1–3 of 5 real claims
CONFIRMED; the number is not the test, the correct reasons are.

**Costs.** Kernel: 0 model calls. Claim drafting: 0 (human YAML) in MVP, ~5 calls/paper later. Shims: 0–30 Codex
calls per repo. Compute: minutes per dataset on a laptop for sklearn-scale.

**Failure modes (honest).**
- Statement fidelity is the weak link: a sloppy Claim audits a strawman of the paper. Mitigation is only the
  human approval and showing the original sentence on the report.
- External ≠ same population: NOT CONFIRMED on new datasets may mean "scope narrower than claimed", not "wrong".
  The report must say so.
- K5 equalises contract limits, not hyperparameter-search effort inside them (inferred); list as assumption.
- **Legal/licence**: code without an OSI licence is "all rights reserved"; running it privately is usually
  tolerated, but **bundles redistribute code and data** (and contain hidden labels). Rule: refuse to bundle-publish
  unlicensed code or data whose licence forbids redistribution; publish a hash-only bundle instead. Publishing a
  NOT CONFIRMED verdict about a named paper carries reputational risk: wording fixed to "not confirmed under
  protocol P", authors notified with a right of reply before publication, human approval to publish. Not legal
  advice; a lawyer should look before any public service.
- Deep-learning papers are out of reach without GPUs.

---

## Rank 2. Lab-analysis notary: executable pre-registration for small wet-lab studies

**User / job.** A PI or a core facility: "Prove to reviewers that the analysis we ran is the one we committed to
before the plates were read, and that the returned data support the claim." OSF Registries timestamps prose plans
with a DOI (verified); nothing I found checks the *returned data against the committed analysis mechanically*
(unverified novelty claim).

**Reuses.** The physical path already end to end up to the Kernel: draft → prohibited-list screen → prereg →
approval node → `chh return-data` (columns, types, replicates checked) → data_received approval → certify
(verified, `research/physical.py`). Ledger anchoring (`chh ledger-anchor`) gives a public timestamp.

**Missing.**
- A **physical adapter** (none approved, so K1 refuses today; verified).
- **A replicate-level K4 design**: unpaired two-arm (Welch / permutation) with biological replicates as the unit.
  Today's paired-seed + example bootstrap does not fit (inferred). This is a Kernel change: needs owner approval,
  fault-injection validation and the 100% coverage gate.
- The **prohibited list and review standard** (§13.6, owner decision) before any life-science protocol is accepted.
- A human-readable protocol PDF from the registered protocol artifact.

**GOAL touched.** §4.7 physical row, §13.6, §13.7, L9, invariants 3, 7, 14, 15. Strengthens §4.7 (returned data
checked) without weakening anything; attestation stays UNTESTED.

**Plan.** P1 (owner): prohibited list + adapter validation standard. P2 (1 week): Welch/permutation design as a
new `AnalysisPlan.test` value + fault-injection cases (swapped arms, dropped replicates, post-hoc outlier removal,
unit change). P3: `physical` adapter registration; dry run with synthetic qPCR-style ΔΔCt data.

**Acceptance test.** On 40 synthetic studies (20 true effects, 20 null; n = 3–6 replicates/arm), plus 5 planted
deviations (replicate dropped after unblinding, arm labels swapped, analysis changed after return, data returned
before approval, missing replicate): false-certification rate on nulls ≤ α + 2 SE; all 5 planted deviations
refused with the correct reason; 0 certificates without both human approvals.

**Costs.** 0 model calls. **Failure modes.** Attestation is the whole trust gap (a lab can fabricate CSVs); the
system cannot fix that and must say so. With n = 3, K7 will reject most real claims (p_null rarely ≤ 25% for
modest δ; guess) — the product may mostly produce honest "uncertifiable at this n" outcomes, which is valuable to
the user only if the preflight says it *before* the experiment. Biosafety/dual-use liability if the screen is weak.

---

## Rank 3. Kinetic-model reproduction pack (systems biology, simulated class)

**User / job.** A BioModels-style curator or a modeller publishing an ODE model: "Certify that the published
trajectory values are what the model as deposited actually produces, at a converged step size."
Tiwari et al. 2021 (Mol Syst Biol): ~49% of 455 published kinetic models could not be reproduced from the
manuscript (verified). BioSimulators / COMBINE archives already *execute* models (verified common knowledge;
not re-checked here); the differentiator is a **pre-registered claimed value with convergence evidence and a
bit-reproducible, pinned simulator in a certificate** (inferred; partial novelty only).

**Reuses.** `sim` adapter: pure-Python RK4, h/h2/h4 refinement, Richardson error bound, exact-fraction decisions,
CERTIFIED-SIMULATED with the reality gap untested (verified, `kernel/sim.py`). Bundles. Literature module to resolve
the source paper.

**Missing.** The registered model family is only `linear`, `damped_oscillator`, `sir` (verified). Needed: a
**mass-action / Michaelis–Menten reaction-network family** from a small SBML subset (no events, no rules beyond
assignment). This is a new adapter version: human approval + fresh fault-injection validation (invariant 13).
**Stiffness**: fixed-step explicit RK4 with MAX_STEPS 20,000 will fail convergence on stiff networks — most
signalling models are stiff (guess). A pinned implicit method (e.g. exact-arithmetic-checked Rosenbrock) is a
second adapter, later.

**Acceptance test.** 20 non-stiff BioModels entries chosen before running; claims = component values at 3 time
points read from the paper. Pass iff: ≥ 12 certify or fail with the right reason (non-convergence vs value
mismatch distinguished in 100% of cases); 5 planted wrong parameters and 3 planted unconverged step sizes all
rejected; every certificate's scope names simulator, version and config hash.

**Costs.** 0 model calls (or ~3/model to draft the claim from a figure, human-approved). **Failure modes.**
Stiffness limits coverage; values read off figures carry digitisation error that must be in the tolerance;
SBML is large and a subset parser invites scope creep into the trusted base.

---

## Rank 4. Bound manuscripts: every number and "outperforms" in a paper resolves to a certificate

**User / job.** An author before submission (and a journal that asks for bundles): "Prove that every number in
my paper comes from a re-checkable result, and that no sentence claims more than its certificate."
statcheck recomputes APA-format p-values and GRIM checks mean granularity (verified); Quarto/knitr bind numbers to
code at render time (common knowledge). Neither knows whether the *assertion* ("significantly outperforms on held-out
data") matches what was certified (inferred novelty).

**Reuses.** Invariant 8 machinery (reports generated from certificates, numbers recomputed), certificate labels,
bundles, `cite.py` for the bibliography.

**Missing.** A LaTeX/Markdown macro (`\chh{<cert>:effect.lo}`) resolver with declared rounding; an
**unbound-numeral scan** of the rendered text (whitelist years, equation numbers, sizes); a **controlled
assertion vocabulary**: a sentence linked to a cert may use "outperforms/significantly" only if the label is
CERTIFIED-*, "on external data" only if CERTIFIED-VALIDATED, "in simulation" mandatory for CERTIFIED-SIMULATED.
Deterministic phrase rules; any LLM sentence classifier may only *add* flags (downgrade-only, §1.1).

**Acceptance test.** On 3 manuscripts generated from existing bundles with 15 planted edits (changed digits,
rounding beyond declared precision, an "outperforms" bound to a REJECTED cert, a simulated result phrased as
physical, an unbound number, a fabricated citation quote): ≥ 14/15 flagged, ≤ 1 false flag per manuscript.

**Costs.** 0 model calls. **Failure modes.** Only works for papers whose results came through the harness; reviewer
mode on arbitrary PDFs is not possible (see tempting list). Free prose escapes the phrase rules.

---

## Rank 5. Known-denominator registry of non-confirmations

**User / job.** Anyone choosing a research direction: "Which claims of this kind have been tried and did not
hold, out of how many tried?" Negative-results journals and OSF exist but cannot show the denominator. The
prediction ledger records every pre-registration, resolved or not (verified, `predictions.py`, §4.5), so the
file drawer is closed *within* the registry by construction.

**Reuses.** Ledger, predictions, memory store negative entries (`memory/`), audit outputs (rank 1), ledger
anchors. **Missing.** A public export filtered by release policy (16.5: no continuous held-out values to agents —
a public page is agent-readable in principle); owner decision §13.4; a query CLI. K7 means entries are
"not confirmed", never "refuted"; F2 (VDS gaming) must be fixed before any score is shown.

**Acceptance test.** Export of a seeded ledger with 30 claims: counts per kind/label equal a recomputation from the
raw ledger chain; 0 private label values or continuous held-out scores in the export (leak audit); removing one
entry breaks the anchored head hash.

**Costs.** 0 model calls. **Failure modes.** Value scales with volume; with one user it is a demo, not a product
(guess: needs ≥ 100 resolved claims to be useful).

---

## Tempting but wrong

- **Reviewer mode on arbitrary PDFs** ("check this paper's numbers"): without bundles there is nothing to resolve
  numbers against; it degrades into LLM opinion (T5) or statcheck, which already exists.
- **Repository reproducibility badge**: a badge on a repo certifies nothing; only a specific certificate with its
  scope does. A badge gets copied and outlives retractions. At most: a badge that links to one bundle hash and
  shows STALE/RETRACTED live.
- **Claim-first pre-registration UX as a new product**: this is preregister + preflight (16.6) + `explain` (16.15),
  already proposed; build those, do not wrap them again.
- **LLM benchmark-contamination checker**: crowded, needs model weights or logprobs, and K9 only detects canaries
  in the system's own artifacts.
- **Composable T2 certificates / claim marketplace**: rejected in systems.md and §16; statistical claims do not
  compose.
- **Exact-certificate library for OR problems**: VIPR-style exact MIP proof logs and MIPLIB checkers already exist
  (from memory, unverified); our LP checker adds a ledger, not a capability. Revisit after FunSearch-on-LP.
- **Auditing deep-learning papers**: no GPU; partial runs would produce INCONCLUSIVE at best.
- **"Refuted" verdicts**: K7 cannot certify negatives; calling a non-confirmation a refutation would be a lie.

## Recommended first build (~1 week): Claim Audit MVP on tabular ML

Scope: rank 1, M-A1 only, human-written claim YAMLs, sklearn-API shims, OpenML external datasets.
1. Day 1: `audit` record type + YAML → Claim; ledger-commit the frozen list of 3 real + 2 planted claims first.
2. Day 2: OpenML ingester (licence recorded; refuse redistribution-incompatible data in bundles).
3. Day 3: shim template; both arms through one shim; K5 contract identical by construction.
4. Day 4: verdict taxonomy report from certificate fields only; preflight hook marks INCONCLUSIVE.
5. Day 5: run, reproduce bundles on a second machine (or clean container), write results with labels.
Done when: both planted claims are NOT CONFIRMED with the correct K reason, all 3 real audits produce a verdict
with a reproducing bundle, and the Kernel diff is zero lines.
