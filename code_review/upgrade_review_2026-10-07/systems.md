# Systems-level upgrades for custom-harness: where the scarce resources go

Lens: the whole socio-technical loop (researcher + Codex + sandbox + Kernel). Labels: **verified** = read in the
named file this session; **inferred** = my reasoning from what I read; **guess** = an unmeasured estimate.
Citations from outside the repo are from memory. I did not re-check their bibliographic details this session.

## 0. First-principles bottleneck map

| Scarce resource | Evidence | Label |
|---|---|---|
| **Certifiable held-out signal** | K7 needs δ ≥ 0.67·SE (`checks.py` L559–571). K4 needs the CI lower bound ≥ δ. Run A's interval implies SE ≈ 0.058 at n = 54, so a claim certifies only if its observed effect is ≳ 0.135 at Bonferroni m = 1, and the bar rises with every claim on the target. Run B's held-out baseline was 0.815, so the maximum possible effect was 0.185. | SE and K7 rule verified; the 0.135 bar is inferred |
| **Model calls, not tokens** | 12–13 calls per run, with ≈15–19k input tokens of fixed CLI overhead per call (liverun1). M2 exit ≈ 300 calls ≈ 21 tasks × 13. | verified; the ×13 decomposition is inferred |
| **Sandbox wall time** | 3 dev evaluations took 5,299 of the 8,074 s in Run B. The losing ensemble used 2,410 s and failed. | verified |
| **Developer feedback** | Every integration bug found so far (K9-FP, DEV-1, DEV-TIME, CERT-INFRA, two sklearn API breaks) surfaced only through a live run or a rebuild. | verified (STATUS §A) |
| **Human attention** | Adapter approvals, budget decisions, the second model family and the Lean install all wait on the user. Loophole state lives in prose (STATUS §B–D). | verified |

The scarcest resource is **held-out signal**, not compute. Every look at a target consumes it: the Bonferroni family
grows, and test sets are tiny. The current loop spends ~13 calls and up to 8,000 s before learning that a claim
could never have certified. The system is linear today: nothing learned in run *k* makes run *k+1* cheaper or more
likely to certify. Proposals 1, 2 and 5 turn that into compounding loops.

---

## P1. Certifiability preflight + cheap-to-expensive cascade (held-out signal as a metered budget)

**(1) Idea.** Before pre-registration, run the **real K4/K6/K7 functions on simulated outcomes**. The simulation
uses only public quantities: target size n, the dev-estimated baseline and effect, the current Bonferroni family
size m and the planned seeds. From it, estimate P(certify), the minimum detectable effect and a runtime risk
(dev wall per fold vs the per-run limit). If P(certify) < θ, the claim is not registered. The loop instead shrinks
the claim, picks a cheaper candidate or reports "uncertifiable at this n". Before that gate, use a cascade (the
Station's float-screen → exact-check, generalised):
1. static check: import and API calls against the sandbox's pinned library versions;
2. 20-row smoke run;
3. **fold-racing**: stop a candidate's 5-fold CV once its upper bound cannot beat the incumbent, with a time term;
4. full CV;
5. preflight;
6. held-out.

This changes the loop "spend → reject → learn nothing" into "predict → spend only where the evidence can pay off".
- Run A's K7 failure was computable before `confirm`: δ = 0.02 against SE ≈ 0.058 (inferred).
- Run B's h4 took 2,259 s of dev time against a 600 s per-run limit. A runtime screen flags that (inferred).

**(2) GOAL impact.** Touches L1 (allocator), L3 (the float-screen bullet, generalised), L4.6 and L4.7.
- Ordering: the preflight runs before K2 commits. An abandoned **post**-registration claim must still be scored by
  VDS (§4.5).
- Can break: the preflight must not read held-out labels, or it will trip K2 and the vault log. It may only deny
  spending, never grant a certificate (§1.1 "only downgrade"). Selecting by dev data adds dev winner's curse,
  which P5 measures.

**(3) Build** (`research/preflight.py`, outside the Kernel; it imports Kernel pure functions).
- M-a (S): `chh preflight-suite` runs a zero-call audit of all 21 tasks and prints P(certify) for a perfect-on-dev
  solution at each task's n.
- M-b (S): a `preflight` node before `claim` in `research_v1.yaml`, protected like the Kernel nodes.
- M-c (M): fold-racing inside the dev evaluator.
- M-d (S): library versions in the implement prompt plus the static screen (fixes liverun1 item 4).

**(4) Acceptance** (falsifiable):
- (a) The suite audit costs 0 model calls and flags every task where even a perfect solution has P(certify) < 0.5.
- (b) Run on Run A's registered claim, the preflight predicts P(K7 pass) < 0.05.
- (c) Fold-racing replayed on the stored attempt programs from both runs plus cassette runs (P2) cuts median dev
  wall time by ≥ 50%. It picks the same candidate as full CV in ≥ 90% of cases where the time cap does not bind.
- (d) In the M2 exit run, ≤ 10% of model calls go to runs whose claim ends REJECTED only for K4 sample size or K7.
  Planted 51-claim suite: still 0 false certifications.
- (e) Preflight calibration: among claims predicted P ≥ 0.7, the observed certify rate is ≥ 0.5. Reported with its
  CI, because n is small.

**(5) Costs and failure modes.**
- The normal-approximation SE is optimistic for tiny n.
- Fold-racing stops on 2 folds, which are noisy, and can kill a late winner.
- The tool could become a "why bother" generator: it may truthfully say most real tasks are uncertifiable. That is
  a finding, and it pushes toward bigger or pooled targets.
- It prevents wasted spend. It does not create signal.

**(6) Prior art.**
- Registered Reports require a power analysis before Stage-1 acceptance (Chambers 2013, *Cortex*).
- Racing and successive halving: Maron & Moore 1994 (Hoeffding races), Jamieson & Talwalkar 2016, Hyperband (Li et
  al. 2018).
- Test-set reuse as a budget: Dwork et al. 2015 (*Science*, reusable holdout); Blum & Hardt 2015 (the Ladder).
- Value of information: Howard 1966.
- What is new (inferred): running the **certifier's own code** as the power model, and gating pre-registration on it.

---

## P2. Cassettes: distil live runs into replayable fixtures

**(1) Idea.** The gateway records every `ChatRequest → ChatResponse` of a live run into a content-addressed
cassette, keyed by (node, role, prompt hash). A `REPLAY` access mode serves the recorded responses, while the sandbox
code, the Kernel, the audit and the bundle all run for real.

This attacks the developer-feedback bottleneck. Most of the system's code is deterministic and wraps the model
(Kernel, vault, racing, audit). Today it gets end-to-end tests only from scripted mocks or 13-call live runs.
Cassettes turn each paid run into a permanent regression asset, so live spend compounds.

There are two extra uses:
- **Counterfactual replay**: swap one recorded response (for example, inject Run B's label-copying-style code) and
  re-run downstream.
- **Offline policy evaluation** for P1: fold-racing, θ and caps.

**(2) GOAL impact.** Touches the L0 model gateway, invariant 3 and §7.
- Can break: a replayed run must never mint a fresh certificate that counts as evidence, enter the prediction
  ledger or reach the scorecard. It also must not touch real held-out targets, or test-set wear grows and the
  K2-TWO-RUN class of bug is triggered.
- Fix: replay runs use a scratch vault/ledger copy, and certificates are labelled REPLAY. Cassettes of gate/sealed
  tasks must never enter the evolver export (L6.1).

**(3) Build.**
- C-a (S): a `RecordingBackend` wrapper and a `ReplayBackend`, with strict mode (a miss fails and prints the prompt
  diff). This mirrors the existing `MockBackend` (verified: `gateway/mock.py`).
- C-b (S): a `chh replay <run_id> [--from-step k]`. On a miss, fall back to live with a recorded fork, which reuses
  the existing `fork`.
- C-c (S): scratch-store isolation, plus a REPLAY label that both the Kernel and the scorecard refuse.
- C-d (M): a fixture pool built from evolve-split and retired tasks only.

**(4) Acceptance.**
- (a) The Run A replay makes 0 gateway calls, with identical node sequence, verdict and reasons.
- (b) Reverting the K9-FP fix makes the Run A replay test fail. Reverting the DEV-TIME cap makes a Run B replay
  assertion fail.
- (c) A replayed certificate fed to `scorecard`, the ledger or the memory store is refused (3 tests).
- (d) Over the next milestone, ≤ 1 live run per merged branch (guess target), against "a live run to find each bug"
  today.
- (e) Run B's timeouts may not reproduce on an idle machine. Report whether they do. That makes sandbox
  nondeterminism visible; it does not count as a failure of the test.

**(5) Costs and failure modes.**
- Cassettes test the code around the model, not the model. Any prompt or harness edit misses the cassette, so L6
  candidates still need live calls.
- **Overfitting by developers**: tuning the harness on replayed exit-suite trajectories is a human form of test
  leakage, so exit-suite tasks are never fixtures.
- Cassettes go stale as templates change; the miss rate is the metric to watch.
- Codex ToS on storing outputs needs a check (invariant 11; unverified).

**(6) Prior art.** VCR/VCR.py HTTP cassettes; record/replay debugging (rr); LLM response caches (LangChain, inferred).
What is new: replay bound to an evidence store, with a Kernel that refuses replay as evidence.

---

## P3. The research graph + `chh explain` + a machine-readable loophole register

**(1) Idea.** Build one derived, read-only projection that rebuilds from the append-only store. Its nodes are
hypotheses, tree nodes, attempts, preregs, claims, certificates, predictions, memory entries and approvals. Its edges
are typed: derived-from, tested-by, retracts, approved-by.

Two views:
- `chh explain <cert>`: for each check, the evidence ids it touched, plus every attempt and repair that preceded the
  claim (the garden-of-forking-paths count), cost per node and the human-in-the-loop points.
- `limitations.yaml`: STATUS §C/§D as records with id, severity, applies-to predicate (check, evidence class,
  backend) and status (open, or fixed by a named test). Every certificate's untested-assumptions list then includes
  each open limitation that applies to it. For example, while R1-F3 is open, a certificate carries "flaky-replay
  shopping possible".

Bottleneck: human attention and trust calibration. The reader of a certificate today cannot see which known holes it
sits on. This closes the loop "find loophole → it shows up on every affected result → fixing it removes the warning".

**(2) GOAL impact.** Touches L9 (explain view, dashboard), §4.1 assumption audit, §4.4 and L2.
- Can break: certificate content changes, so the bundle format moves to a new version. The register becomes a
  human-owned trust input to the Kernel and should be protected like adapters.
- The projection must not become a second source of truth; it has no write API.

**(3) Build.**
- G-a (S): SQLite views over the existing store tables, plus `chh graph export` (JSON/DOT).
- G-b (S): `chh explain` as text, with every number resolved via invariant 8.
- G-c (S): `limitations.yaml` plus a `check.py` rule: a "fixed" entry must name a test that exists and passes.
- G-d (M): a static HTML dashboard: open loopholes by severity, the claim graph, calibration.

**(4) Acceptance.**
- (a) `chh explain` on Run B lists all 8 attempts, the 3 fixes, the failed checks K3/K4/K6/K7 with evidence ids, and
  5,299 s of dev time. Each number resolves (audit test).
- (b) 100% of STATUS §C/§D entries are machine-readable.
- (c) A certificate issued with R1-F3 open lists it. After the fix merges, the same claim's re-certificate does not.
- (d) Marking an entry fixed without a passing test fails the gate.

**(5) Costs and failure modes.**
- Applies-to predicates will be coarse, so warnings will be noisy and get ignored. Keep the severity filter.
- This is bookkeeping work with no research gain on its own.

**(6) Prior art.**
- W3C PROV.
- Assurance cases with defeaters (GSN; Goodenough et al. 2013, eliminative argumentation).
- Model cards and datasheets (Mitchell 2019; Gebru 2018).
- Lean's `#print axioms`, which is the closest analogue.
- OR's experiment tree (verified in `openresearch.md`).
- What is new (inferred): system-level known defects propagated into per-result certificates.

---

## P4. Copilot mode with ledgered human interventions

**(1) Idea.** The human and the agent share the experiment tree through **typed interventions**: prune node, add
hypothesis, reprioritise, set budget, annotate. Each goes through the same edit surface and protections as the agent.
Each intervention is a ledger entry (who, when, hash, phase), and the VG checklist's human-in-the-loop item is filled
from these entries.

Bottleneck: autonomous runs burn budget on branches a human would kill in seconds (h4: 2,410 s). The non-obvious
point is **credit assignment**: VDS and the L6 objective must exclude, or down-weight, runs that a human steered.
Otherwise the evolver gets credit for the human's ideas and the loop stops being VALIDATED in the §6.1 sense.

**(2) GOAL impact.** Touches L9, L4.7, §6.1 and §6.3, and invariant 2. The copilot CLI must never display
vault-private data. Interventions after any held-out read on the claim's target are refused.

**(3) Build.**
- H-a (S): an `Intervention` record, plus `chh tree show|prune|add` built on the approval-node machinery.
- H-b (S): a `human_influenced` flag on claims, propagated through the derivation graph.
- H-c (S): the L6 objective and VDS filter on that flag.

**(4) Acceptance.**
- (a) An intervention after a held-out read is refused (test).
- (b) A run with one human-added hypothesis contributes 0 to the harness objective (test).
- (c) The checklist lists every intervention hash.
- (d) Usage (guess): on 5 copilot sessions, sandbox time per finished claim falls ≥ 30% against autonomous runs on
  the same tasks.

**(5) Costs and failure modes.**
- Humans are the strongest p-hacking channel. Refusing interventions after a held-out read only blocks that window;
  a human can still steer using dev results, which is legal but shapes the claim.
- (d) is hard to measure with one user.

**(6) Prior art.**
- Mixed-initiative interfaces (Horvitz 1999).
- OR copilot mode and "bring your coding agent" (verified in `openresearch.md`).
- VG Table 10 human-in-the-loop points (GOAL §1.3).
- What is new: human influence as a provenance flag that removes VALIDATED credit.

---

## P5. Ledger-calibrated forecasts (the compounding loop)

**(1) Idea.** STATUS states that the CV forecast is optimistic because of dev winner's curse and missing
between-fold variance, and that "the prediction ledger measures calibration". So far nothing feeds that measurement
back. Proposal: once there are ≥ 30 resolved predictions, fit an empirical-Bayes shrinkage of dev effects toward
realised held-out effects. Use it in two places:
- choosing δ and the declared probability;
- as a candidate K7 empirical prior, which resolves the GOAL §13.1 question of empirical vs baseline prior.

This makes run *k+1* better because of run *k*.

**(2) GOAL impact.** Touches §4.5, §13.1 and L4.6.
- Can break: a prior learned by the system feeding a Kernel check is a coupled-Goodhart route. Keep it outside the
  Kernel (it is used only for forecasts) until a human approves it as an adapter parameter.
- Run B's 0.95 is a clamp, which already shows overconfidence (verified).

**(3) Build** (S–M): `research/calibrate.py`. Fit on the ledger, use leave-one-out evaluation, version the fit and
record it in claims.

**(4) Acceptance.** On ≥ 30 resolved predictions, comparing leave-one-out to raw:
- Brier score improves by ≥ 0.03;
- the confident-miss rate (p ≥ 0.9, outcome false) halves;
- the K7 pass rate does not drop.

If the M2 exit run yields < 30 resolutions, this is deferred, not declared successful.

**(5) Costs and failure modes.** The data are small, non-stationary and drawn from one task family, so shrinkage
fitted on ML tasks may not transfer to other domains.

**(6) Prior art.** Efron & Morris 1975 (shrinkage); winner's-curse correction (Zöllner & Pritchard 2007); Tetlock
and Good Judgment calibration feedback. All known; the application here is standard.

---

## What NOT to do

1. **Do not make T2 claims composable premises.** A statistical claim is scoped to its population. Chaining such
   claims multiplies untested assumptions and hides reuse of the test set.
   - Only T0/T1 artefacts (Lean lemmas, re-checkable solver certificates) compose cleanly.
   - T2 claims transfer only as priors (P5) or as comparators that K5 still re-runs.
   - A "claim marketplace" is hype at this stage.
2. **Do not let any cheap proxy grant.** The cascade only prunes. A float screen or a preflight never replaces K3–K7.
3. **Do not build a GUI before the projection and `explain` exist** (P3). A dashboard over unstable schemas rots.
4. **Do not count replayed outputs as evidence, as tests of the model, or as scorecard input.**
5. **Do not run a VOI scheduler across claims on held-out data.** Choosing which claim's held-out look to buy next by
   looking at earlier held-out results is adaptive data analysis. VOI belongs on dev and sandbox spend only.
6. **Do not spend the ~300-call M2 exit budget before P1-a.** Zero calls tell you which tasks can certify at all.
7. **Do not treat a second Codex variant as a second family.** Invariant 5 needs a different vendor or open weights
   (§12).

---

## GOAL.md diff for the top two (P1, P2)

```diff
@@ §5 L0 Model gateway (after "...and earns no replication credit).") @@
 recorded as invariant 5 NOT MET in every run, report and certificate checklist, and earns no replication credit).
+**Replay mode** (❌, development only): live runs are recorded as content-addressed cassettes (node, role, prompt
+hash → response). `REPLAY` serves them, while the sandbox, the Kernel and the audit run for real, in a scratch
+store. A replayed run never writes the real ledger, prediction ledger, memory or scorecard, never reads a real
+held-out target, and its certificates are labelled REPLAY. Cassettes of gate/sealed/exit-suite tasks are never
+fixtures and never enter the evolver export.
+- Done when: a recorded live run replays with 0 model calls to the same node sequence and verdict; reverting a
+  live-run-found fix makes its replay test fail; a REPLAY certificate is refused by the ledger, memory and scorecard.

@@ §5 L1 Control plane @@
   downgrade (deny, shrink, kill-with-code-checked-evidence).
+- **Certifiability preflight and cascade** (❌): before pre-registration, the Kernel's own K4/K6/K7 functions run on
+  outcomes simulated from public quantities only (n, dev baseline and effect, family size m, seeds, dev wall
+  time). A claim with P(certify) < θ is not registered. Dev spend follows a static/API screen → smoke → fold-racing →
+  full CV cascade. The preflight never reads private data and can only deny spending, never grant. Held-out looks
+  are a metered budget per target, shown in every report.
 - Done when: a Docker outage mid-campaign changes no accept/reject decision. ✅
+  Preflight done when: a zero-call suite audit reports P(certify) per task; Run A's claim is predicted to fail K7;
+  in the M2 exit run ≤ 10% of model calls go to claims rejected only for K4 sample size or K7, with 0 false
+  certifications on the planted suite.

@@ §10 Hard invariants @@
 16. No harness edit is accepted without passing the leave-one-family-out gate (and leave-one-domain-out once ≥ 2
     domains exist).
+17. Replayed or cached model output is never evidence: no certificate, ledger entry, memory write or scorecard
+    number may come from a REPLAY run; cheap proxies (screens, preflight) may only prune, never certify.

@@ §11 M2 row, exit-test column @@
-| ≥ X% CERTIFIED-or-NEGATIVE, 0 integrity failures |
+| preflight suite audit run first (0 calls; uncertifiable tasks repaired or dropped before spending); then ≥ X%
+CERTIFIED-or-NEGATIVE, 0 integrity failures; every live call recorded as a cassette |
```
(The M2 row hunk replaces only the trailing exit-test cell of line 623; the other cells are unchanged.)
