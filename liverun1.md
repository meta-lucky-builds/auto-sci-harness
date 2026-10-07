# Live runs of research_v1 — what happened, with all recorded metrics

Every number below was read back from the evidence store (trace, checkpoint state, evidence records, ledger,
certificate) with a read-only script on 2026-10-07; nothing is from memory. Two live runs exist, both on the repair
task `wine_knn_unscaled` (13 chemical features, 3 wine cultivars; the task baseline is a deliberately defective
1-nearest-neighbour classifier on UNSCALED features, so `proline` dominates distances). Both used the official Codex
CLI (ChatGPT subscription, BYOS) for every model role and the Docker sandbox for all code execution.

| | Run A (first live run) | Run B (second live run) |
|---|---|---|
| Run id | `run_cd99288e65ba4a4f` | `run_f1bff8ab659f48e6` |
| Date / code | 2026-10-06, commit 48d8bdd era (single 20 % dev split) | 2026-10-07, main a499a6f (5-fold CV dev estimate, before the dev-time cap and the adapter requirement) |
| Store | archived as `custom-harness-data/store-v1` | `custom-harness-data/store` |
| Suite data | old suite (`suites/m2-v1`; repair task had its own split, seed 11) | rebuilt suite (`suites/m2`; repair task shares wine-real's split, seed 7) |
| Status | completed, 33 steps | completed, 33 steps |
| Model calls | 12 | 13 |
| Tokens (input / output) | 224,159 / 29,939 | 207,195 / 27,333 |
| Wall time | 845 s | 8,074 s (machine shared with 5 agents + test suites) |
| Certificate | **REJECTED** (K7, K9) | **REJECTED** (K3, K4, K6, K7) |
| Audit | FAILED (I5) | PASSED (I1, I2, I4, I5; I3 n/a) |
| Invariant 5 | not recorded (before the policy existed) | recorded: NOT MET (explicit override, Codex in every role) |

---

## Run A — `run_cd99288e65ba4a4f`

### Steps (seconds, input/output tokens)
setup 0.2 · profile 0.0 · baseline 6.6 · hypothesize 46.2 (18,891/1,235) · tournament 53.5 (3 calls, 54,704/1,039) ·
critic 30.5 (18,743/876) · socratic 0.0 · then 5 × [implement → run]: implement 88.7/66.2/142.8/103.9/78.4 s
(≈18.4k in each; 3,912/2,850/6,766/4,828/3,281 out), run 4.9/9.3/4.5/33.3/7.5 s · improve ×2 (48.5 s, 61.0 s;
19,820 in each) with runs 4.9 s each · claim 0.0 · confirm 35.6 · certify 13.1 · report 0.0 · audit 0.1.

### Development
- Dev estimate: ONE random 20 % split of the public training data = 24 rows (dev/train 100 rows, 14 columns).
- Baseline dev accuracy 0.9167 (22/24).
- Hypotheses (tournament order h3, h1, h4, h2, h5; 3 valid rounds): h1 scaled RBF-SVC with tuned C/gamma; h2
  scaled logistic regression with log/ratio features; h3 scaled shrinkage LDA; h4 soft-voting SVC+LDA+GaussianNB;
  h5 RBF-SVC with robust scaling / clipping.
- Critic: 5 questions kept, 0 dropped (e.g. "How will you separate model selection from the folds used to report
  accuracy?").
- Attempts: all 7 candidates (h3, h1, h4, h2, h5, improve-1, improve-2) scored dev 1.0 (24/24); both improvements
  were refused by dev racing (no paired improvement over a perfect incumbent). Best = h3 (first perfect one).

### Claim and held-out confirmation
- Claim: "the solution for h3 beats the task baseline on accuracy by at least 0.02 on held-out data"; min effect
  0.02 (= the task floor, because the dev lower bound was ≈0), predicted [0.00, 0.25], probability 0.798,
  α = 0.05 one-sided, seeds 0/1/2, Bonferroni family 1.
- Held-out (54 test rows): subject 0.9630 / 0.9630 / 0.9444; baseline 0.7037 ×3. Effect +0.2531; two-sided 90 %
  BCa over examples [+0.1667, +0.3580]; paired Student-t over seeds [+0.2351, +0.2711].

### Kernel verdict
K0–K6 and K8 pass (K3 re-executed 2 records). **K7 fail**: P(success | no effect) ≤ 0.365 → 1.45 bits < 2.0
(the 0.02 claim was trivially safe). **K9 fail**: "a private value reached agent-visible output" at 7 run steps.

### What it revealed and what was done
1. K9/I5 FALSE POSITIVE: perfect dev predictions serialised to the same JSON as the private dev labels, so their
   content hash equalled the private labels' ref and the trace scrubber redacted it. Fixed (private dev labels stored
   wrapped), test `test_perfect_dev_predictions_are_not_a_leak`.
2. K7 rejection was correct; the 24-row dev split carried almost no information. Decision (user): k-fold CV dev
   estimate — built, with each fold in a fresh sandbox (which also closed a demonstrated dev-cheating loophole).
3. Bundle reproduces in Docker: REJECTED → REJECTED, 0 problems.

---

## Run B — `run_f1bff8ab659f48e6`

### Steps (seconds, input/output tokens)
| # | node | s | in / out | | # | node | s | in / out |
|---|---|---|---|---|---|---|---|---|
| 0 | setup | 0.2 | | | 17 | run | 603.2 | |
| 1 | profile | 0.0 | | | 18 | fix | 61.8 | 15,852 / 3,915 |
| 2 | baseline | 32.9 | | | 19 | run | 2,272.4 | |
| 3 | hypothesize | 22.5 | 15,584 / 681 | | 20 | next | 0.0 | |
| 4 | tournament (3 calls) | 26.8 | 47,008 / 1,073 | | 21 | implement | 61.5 | 15,857 / 3,461 |
| 5 | critic | 8.8 | 16,164 / 267 | | 22 | run | 7.7 | |
| 8 | implement | 27.1 | 15,993 / 1,562 | | 23 | fix | 14.6 | 14,776 / 1,340 |
| 9 | run | 74.1 | | | 24 | run | 117.3 | |
| 11 | implement | 51.5 | 16,679 / 3,740 | | 26 | implement | 22.6 | 16,714 / 2,990 |
| 12 | run | 32.2 | | | 27 | run | 8.9 | |
| 14 | implement | 70.9 | 15,998 / 5,558 | | 28 | claim | 0.2 | |
| 15 | run | 2,423.0 | | | 29 | confirm | 1,471.2 | |
| 16 | fix | 49.8 | 16,570 / 2,746 | | 30–32 | certify / report / audit | 611.3 / 0.0 / 0.1 | |
(`socratic` and `next` steps took ≈0 s.) 3 dev evaluations (steps 15, 17, 19) took 5,299 s of the 8,074 s.

### Development
- Dev estimate: stratified 5-fold cross-validation over all 124 public training rows (each fold in a fresh Docker
  sandbox). Baseline dev accuracy **0.6452** (80/124): CV exposed the defective baseline that the 24-row split had
  hidden (0.917).
- Hypotheses (tournament h2, h1, h4, h3, h5): h1 scaled LDA (± shrinkage); h2 RBF-SVM tuned by stratified CV; h3
  regularised logistic regression with selected pairwise interactions; h4 soft-voting ensemble of tuned LDA + RBF
  SVM + calibrated random forest / extra-trees; h5 robust scaling, winsorising, log transforms.
- Critic: 5 questions kept, 0 dropped (e.g. on nested CV and optimistic hyperparameter selection with 124 rows).
- Attempts (dev accuracy, dev wall time):
  | n | hypothesis | dev | wall | note |
  |---|---|---|---|---|
  | 1 | h2 | 0.9758 | 62 s | |
  | 2 | h1 | 0.9758 | 21 s | |
  | 3 | h4 | failed | 2,410 s | warnings, then no usable predictions → fix |
  | 4 | h4 (fix 1) | failed | 600 s | "no valid predictions produced" (a fold hit the 600 s per-run limit) → fix |
  | 5 | h4 (fix 2) | **0.9839** | 2,259 s | best |
  | 6 | h3 | failed | 4 s | `LogisticRegression(multi_class=...)`: removed in the sandbox's scikit-learn 1.9 → fix |
  | 7 | h3 (fix) | 0.9758 | 101 s | |
  | 8 | h5 | failed | 6 s | `liblinear` solver on a 3-class problem; attempt budget (8) exhausted, no fix |
  The improve operator never ran: the 8-attempt budget was spent on implement/fix.

### Claim and held-out confirmation
- Claim: "the solution for h4 beats the task baseline on accuracy by at least **0.1331** on held-out data";
  min effect 0.1331 = ½ × the dev lower bound (no longer the 0.02 floor), predicted [0.266, 0.411], probability
  0.95 (clamped), α = 0.05 one-sided, seeds 0/1/2, Bonferroni family 1. Development dataset
  `data@9672562e0774923b/train`, target `data@9672562e0774923b/test` (content-based ids).
- Held-out (54 rows): subject seed 0 **1.000**, seed 1 **1.000**, seed 2 **timed out** (`rc=-1 timed_out=True`,
  600 s limit, no predictions); baseline 0.8148 on all 3 seeds. (On this split the defective baseline scores 0.815;
  Run A's old split gave 0.704.)

### Kernel verdict
K0 (5/5 records resolved), K1, K2, K5, K8, K9 (code, 1 trace, 1 canary scanned) pass. **K3 fail**: re-execution of
subject seed 0 produced no predictions (timeout). **K4 fail**: subject seeds [0, 1] ≠ planned [0, 1, 2]. **K6 / K7
fail**: no statistics (consequence of K4). Resolution: outcome false, declared probability 0.95 → scored as a
confident miss in the prediction ledger. Bundle `bundles/1a25c648584424281a5b.zip` reproduces in Docker:
REJECTED → REJECTED, 0 problems. Report rendered from the ledger; checklist complete; not publishable (REJECTED).

### What it revealed and what was done
1. CV fixed the dev-information problem (baseline 0.645, claimed effect 0.133 instead of the floor).
2. Cost blow-up: a slow candidate's k-fold dev evaluation took 2,423 s (each fold may use the full 600 s limit).
   Fixed afterwards: a dev evaluation's total wall time is capped at 2× the per-run limit (test
   `test_dev_evaluation_wall_time_is_capped_across_folds`). Inferred, not run: under the cap h4 would have failed dev
   and h2 (0.976, 62 s) would have been claimed.
3. Selecting the slowest, most complex candidate by a 1-row dev margin (0.9839 vs 0.9758 = 1 of 124 examples) made
   the claim fragile: the Kernel correctly refused a program it could not reproduce within the limits.
4. Codex wrote code for older scikit-learn APIs twice (`multi_class`, `liblinear` multiclass): the sandbox's library
   versions are not in the implement prompt (possible improvement: state library versions in the prompt).
5. Machine load (5 agents + test suites + Docker) inflated every timing and plausibly caused the timeouts; future
   live runs should run alone.

---

## Cost summary
| | calls | input tokens | output tokens | wall |
|---|---|---|---|---|
| Run A | 12 | 224,159 | 29,939 | 845 s |
| Run B | 13 | 207,195 | 27,333 | 8,074 s (load-inflated) |
Codex per-call overhead ≈ 15–19k input tokens (each call re-sends the CLI's own context).
