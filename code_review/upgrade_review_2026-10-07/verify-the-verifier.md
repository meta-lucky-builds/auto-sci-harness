# Verify the verifier: proposals

Labels: **verified** = I saw it (file read or command output). **inferred** = reasoned from code, not run. **guess** = estimate.
Code refs are at `wt-int` HEAD `41d8ef7` (branch m3-int). The tree changed while I read it (a Lean merge landed), so line numbers may drift.

## 0. First principles: what the TCB is today

- **Size (verified).** `src/custom_harness/kernel/` is 3,487 lines at 41d8ef7, against 1,852 on main 9be3b84. GOAL §4.1 sets a target of < 3k, and §5 still says "1.3k LoC".
- **What the Kernel imports (verified, checks.py:23-35).** It also trusts `store/evidence.py`, `verify/stats.py`, `verify/metrics.py`, `verify/leak.py` and `sandbox/base.py`, about 1.2k more lines. Beyond that it trusts pydantic, sqlite3, CPython, Docker and every host module that holds an `EvidenceStore`.
- **Write access is wide (verified).** `store/evidence.py:180` `ledger_import` is a raw INSERT. Vault purposes are declared by the caller (vault.py:37-46).
- **The real TCB** = Kernel + the adapter import closure + `scripts/check.py` + the tests that guard them (inferred).
- **The gate is part of the TCB (verified).** `KERNEL_TESTS` in `scripts/check.py` is a hand-maintained list. During the merge I watched, it held unresolved conflict markers between the `test_kernel_sim` and `test_kernel_formal` entries. A Kernel test file that is not on the list silently leaves the 100% coverage gate.

**What the ~30 findings were (inferred, my own classification of STATUS §A–C).** At least two-thirds were **specification gaps**, not code that diverged from an intended rule:
- K-1 laundering, K-2 throwaway-claim peek, K-6 predictable seed;
- M1-a twin id;
- R1-F1, F2, F3 and F4;
- K2-TWO-RUN;
- R2-2 order dependence.

Only a few were implementation slips (M1-b crashes, R1-F6 NaN, CERT-INFRA).

**Consequence.** A refinement proof ("Python = spec", the seL4/CompCert style) would have caught few of these. What catches spec gaps is **properties stated against the goal**: non-interference ("the verdict does not depend on X") and monotonicity ("adding suspicious evidence never raises the label"). The ranking below follows from that.

**A live candidate defect found while reading (inferred, not run).** `Kernel.verify` (kernel.py:205-210) re-runs `certify` against the **current** ledger. Two later events can change that re-derivation:
- K4's Bonferroni family counts every prereg on a related target, including ones registered **after** the certificate (checks.py:416);
- K6 counts `develop` reads at any time (checks.py:517).

So an honest CERTIFIED certificate can re-derive as REJECTED after a later honest claim on the same test set. `verify()` then reports it exactly as it would report a forgery. That merges "forged" with "superseded". Proposal 2 turns this into a test.

---

## P1. Loophole museum + mutation-kill gate (rank 1: highest assurance per effort)

**Idea.** The "attack test silently stopped attacking" incident happened 3 times. That is a test-adequacy failure, and mutation analysis measures exactly that.

Two layers:
- **(a) Loophole museum.** Every patch-shaped historical finding gets a minimal *re-introduction patch* under `redteam/museum/<id>.patch`. For example, R1-F1 = delete the `dataset_id not in targets` guard at vault.py:194. The gate applies each patch in a scratch copy and requires ≥ 1 test to fail. This is a regression test of the *test suite*, not of the code.
- **(b) Mutation gate.** Run mutmut over the decision-bearing files (vault.py, ledger.py, the K2/K4/K6 functions in checks.py, `adapters.resolve`). Every surviving mutant is either killed by a new test or marked equivalent with a written reason.

Also fix the gate itself: derive `KERNEL_TESTS` from `tests/test_kernel_*.py` and assert the derived set equals the declared one.

**GOAL changes.** New invariant 17 (diff in §7). It strengthens §5 Evidence Kernel "done when (b)": 100% coverage is necessary but not sufficient.

**Plan.**
- **M1 (1 day).** Write museum patches for STATUS §A/§B (guess: ~20 of the ~30 are patch-shaped; SPLIT, DEV-TIME and the data rebuilds are not). Add `scripts/museum.py`, which uses `git worktree` + `git apply` and runs only the Kernel and red-team tests.
- **M2 (1–2 days).** Run mutmut 3 in a `python:3.12` Docker container with the repo mounted. mutmut 3 needs fork(), so it does not run natively on Windows (inferred from its docs; unverified this session). Record the baseline score.
- **M3.** Add a nightly or pre-merge job. Mutation runs only on files changed by the PR, to keep it cheap.
- **Gate self-test.** Plant a failing `tests/test_kernel_zz_canary.py` and an untested branch. Assert that `check.py` goes red.

**Acceptance (falsifiable).**
- 100% of museum patches are killed. The count of patch-shaped findings is stated, and the non-patch ones are listed with a reason.
- Mutation score ≥ 90% on the named files, with equivalent-marked mutants ≤ 10% of all mutants and each one justified in `redteam/equivalent.yaml`.
- The gate goes red on both planted canaries.
- Prediction (guess): the first baseline will be 70–85%, even at 100% branch coverage. If it comes in ≥ 95%, this proposal was worth less than I claim.

**Costs and failure modes.**
- Runtime. The Kernel tests' runtime is unknown to me. At ~1–2k mutants with coverage-based test selection: hours, not minutes (guess).
- Equivalent mutants cost triage time.
- Museum patches rot as the code moves. Rewriting a patch when its anchor moves is a feature: it forces someone to re-read the old attack.
- Mutants model local slips, not spec gaps (P2 does that), so this is a floor, not a ceiling.

**Prior art.**
- DeMillo, Lipton & Sayward 1978 (mutation adequacy).
- Just et al., FSE 2014: mutant detection correlates with real-fault detection.
- Petrović & Ivanković, ICSE-SEIP 2018: mutation testing at scale at Google, diff-based.

What is new: using *historical exploit re-introductions* as the mutant set for a verifier's attack tests. Close to "proof-of-vulnerability regression", but I know no standard tool for it. Citations are from memory, not re-checked.

---

## P2. Executable reference model + metamorphic and stateful property testing of the decision core (rank 2)

**Idea.** Write a ≤ 400-line **pure** reference model:

`decide(ledger_prefix, claim, evidence) -> {K2, K4-procedure, K6-hygiene, family size, label ceiling}`

- **Clean room.** Write it from GOAL §4.3 and docs/kernel.md *without reading checks.py*: a different author, or another model family when one exists.
- **Check the spec against the goal.** Small-scope exhaustive checking of the model against metamorphic relations (MRs) catches spec gaps.
- **Check the Python against the spec.** A Hypothesis `RuleBasedStateMachine` drives the real Kernel (SQLite in a tmp dir) and the model with the same event sequences, then diffs the results. This catches implementation slips.

This is lightweight formal methods in the S3 ShardStore style, and it is buildable today without Lean.

**Metamorphic relations, each derived from a goal-level sentence.**
1. **Irrelevance.** Appending events on a dataset that shares no example rows and no private blob with any target never changes any check. Covers the R1-F2 class.
2. **Permutation.** Reordering `evidence_ids` never changes the verdict. Neither does the order in which *independent* claims are certified. Covers R2-2.
3. **Renaming.** A consistent renaming of dataset and artifact ids never changes the verdict. Covers M2-c and the twin-id class.
4. **Fail-closed monotonicity.** Adding an unexcused private read before the prereg, or a `develop` read on a target, or a twin registration, can only lower the label in the lattice REJECTED < INTERNAL < VALIDATED. Covers K-1, K-2, M1-a and R1-F1.
5. **Honest composition.** Two honest runs on one task, in either order, give two certificates with identical K2 status. Covers K2-TWO-RUN.
6. **Twin equivalence.** Byte-identical private labels under a new id count in the multiplicity family exactly like the original id. Covers R1-F4.
7. **As-of stability.** `verify(cert)` evaluated on the ledger prefix up to the cert's own entry equals the issued certificate. Later events act only through explicit retraction, or through a separate "stale" status. Covers the candidate defect in §0.
8. **Subject/comparator swap.** Swapping the two arms of an improvement claim never certifies both versions.

**GOAL changes.** New invariant 18. §4.3 gets an "as-of" sentence. `verify` semantics change: this is a decision for the user (a design change, not a bug fix). Diff in §7.

**Plan.**
- **M1 (2 days).** Write `tests/spec/kernel_model.py` (pure functions over a list of event dicts) and an event grammar of about 10 kinds: register dataset (fresh, twin, shared-rows), prereg, read × 6 purposes × {own, other, unregistered claim}, record, certify, verify, approve-role.
- **M2 (1 day).** Exhaustive small-scope run on the *model*: all sequences of ≤ 4 events over 2 datasets × 2 claims. With an alphabet of ~40, that is ≈ 2.6M sequences, minutes for a pure model (guess).
- **M3 (2 days).** Hypothesis stateful differential test of model vs Kernel, and MR tests on the real Kernel.
- **M4 (½ day).** Stats N-version check: `verify/stats.py` (stdlib t-quantiles, BCa) against `scipy.stats` as a **test-only oracle**. scipy enters no TCB.

**Acceptance.**
- 0 model/Kernel disagreements over 10⁵ generated sequences of ≤ 8 events, after triage. Each triaged disagreement is a bug in one of the two, written up.
- All 8 MRs hold on the model exhaustively at scope 4, and on the Kernel for ≥ 2,000 Hypothesis examples each.
- **Historical recall.** With the museum patches from P1 applied, the MRs plus the differential test fail for ≥ 70% of the *Kernel-level* findings.
- Honesty control: the MRs above were written knowing those findings, so recall on them is optimistic. The real measure is recall on the **next** red-team round's findings, recorded when it happens.
- MR7 is predicted to **fail today** (inferred). If it passes, my reading of kernel.py:210 / checks.py:416 is wrong.
- Stats: t-quantiles within 1e-9 relative error of scipy, and BCa endpoints within Monte-Carlo error (|Δ| ≤ 3 SE over 200 replicates), on 10⁴ random inputs.

**Costs and failure modes.**
- The model and the Python can share a misreading of GOAL. N-version failures are correlated (Knight & Leveson 1986); that is why the clean-room author matters, and one model family limits it.
- Generators bias toward what the author imagines.
- Stateful tests on SQLite are slow (~10–50 ms per step, guess).

**Prior art.**
- QuickCheck (Claessen & Hughes, ICFP 2000); Hypothesis (MacIver et al., JOSS 2019).
- Metamorphic testing (Chen et al. 1998; survey: Chen et al., ACM CSUR 2018).
- The small-scope hypothesis (Jackson, *Software Abstractions*; Andoni et al. 2002).
- Bornholt et al., SOSP 2021: executable reference models plus property-based conformance testing in S3.
- Differential testing (McKeeman 1998).

What is new: MRs stated as *statistical-procedure non-interference* for a science verifier. Citations are from memory.

---

## P3. Threat model and findings registry as code ("invariant compiler") (rank 3)

**Idea.** Replace the prose loophole ledger with `redteam/findings.yaml`. Each entry carries:
- `id`;
- `invariant` (an MR or a predicate);
- `capability`, from **A0** model text, **A1** evolved harness edit, **A2** sandboxed program, **A3** trusted host module that is buggy or mis-declares a purpose, **A4** store writer, **A5** human operator;
- `tests` (node ids);
- `museum_patch`;
- `generator` (a Hypothesis strategy producing variants, not just the one instance);
- `status` (fixed / open / accepted).

**Accepted limitations become *pins*.** Each STATUS §D item gets a strict-xfail test showing that the exploit works today. The existing `test_r1_..._is_peeking[leakscan]` xfail (docs/kernel.md §1) is the template. The limitation list then gets checked instead of only asserted, and a fix shows up as an XPASS.

**GOAL changes.** §10 gets a capability matrix: each invariant × A0–A5 is covered, out of scope, or accepted. This makes "each has a test" (§10 header) checkable.

**Plan.** About 1–2 days. Write the schema and a gate step that verifies every referenced test id exists and is collected, and that every `open` finding has a failing or xfail test.

**Acceptance.**
- 100% of STATUS §A–D rows are present.
- 0 dangling test ids.
- Every §D limitation has a strict-xfail pin, or a written reason why no test can be written (e.g. "paraphrase copies": pin it with one hand-written paraphrase).
- Matrix: no blank cells.

**Costs.** Bookkeeping. The risk is a stale YAML, which the gate prevents.

**Prior art.** STRIDE/attack trees (Schneier 1999); CWE-style taxonomies. New: a generator per finding, so each fix is tested against a family of variants.

---

## P4. Shrink the TCB: split a pure decision core (rank 4)

**Idea (de Bruijn criterion).** Move the order and bookkeeping logic out of checks.py into `kernel/core.py`. That is K2's peeking rule, K4's plan, selective-reporting and family logic, K6's hygiene rules, the label lattice and `verify_chain`. The core takes plain event tuples and no I/O, and is ≤ ~600 lines (guess).

Everything else (metrics, BCa, replay, leak scan) becomes an *evaluator* whose output is a recorded datum the core consumes. P2's model then has a small target. A future Lean or TLA+ model has a tractable scope: about 15 event kinds, no floats.

**Also: a TCB budget gate.** Compute TCB lines from `adapters.covered_modules` (it already resolves the import closure). Fail the gate if the TCB grows more than X% without a recorded human approval.

**Acceptance.**
- The core imports nothing from store, sandbox, verify or pydantic, enforced by an AST test.
- 100% mutation score on the core, which is feasible at that size.
- TCB report printed by `check.py`.
- Kernel behaviour unchanged: the 50-claim suite passes, and certificates are byte-identical on a replayed store.

**Costs.** A refactor of the most sensitive file, with a risk of new bugs. Mitigate by landing it after P1 and P2 exist. 3–5 days.

**Prior art.** seL4 (Klein et al., SOSP 2009); CompCert (Leroy, CACM 2009). Csmith (Yang et al., PLDI 2011) found bugs only in CompCert's *unverified* parts, which is the lesson for where effort goes. Certifying algorithms (McConnell et al. 2011).

---

## P5. Transparency anchoring and Merkle inclusion for bundles (rank 5)

**Idea.** External anchoring already exists (ledger.py:98-145) but is manual.

- **Automatic anchors.** Anchor automatically after every certificate and approval. Commit `{length, head}` to a separate git repo pushed to GitHub. GitHub's push timestamps act as a free third-party witness: weak, but external.
- **Merkle tree over entries.** Use the RFC 6962/9162 shape. A bundle then carries an *inclusion proof* against an anchored root. This fixes the stated deviation "bundle ledger excerpts are checked per entry, not as an unbroken chain" (STATUS).
- **Signatures last.** Add ed25519 signatures only after P6. A signing key in the same process as the attacker is security theatre.

**Acceptance.** A bundle whose excerpt omits, reorders or alters any entry fails reproduction in 100% of 1,000 random tamper trials. A rewrite of the store before the last anchor is detected by `ledger-verify`. Both tests assert that the tamper actually changed bytes: the TEST-BLIND lesson.

**Costs.** 2–3 days. Assurance is limited to the A4 attacker. It does nothing against A3 (a host module writing a true but misleading entry).

**Prior art.** Crosby & Wallach, USENIX Security 2009; Certificate Transparency RFC 6962/9162; Sigstore/Rekor (Newman et al., CCS 2022); Schneier & Kelsey 1999.

---

## P6. Kernel process boundary ("kerneld") (rank 6)

**Idea.** "Trusted modules have store access" (STATUS §D) is the root of the leakscan and purpose trust boundaries. Run the Kernel in its own container: it alone mounts the store and private blobs, and exposes a narrow RPC (`prereg`, `record`, `certify`, `read(token)`). Tokens are per run, per claim and per purpose; a leakscan token returns only fingerprints, never raw labels. `ledger_import` exists only in a fresh-store mode.

**Acceptance.** Every existing direct-store exploit (forged cert, test_kernel_exploits.py:127; ledger rewrite; mislabelled purpose) is refused at the boundary, with one test per exploit asserting the attempt happened. Host code imports no `EvidenceStore` (AST test). Overhead < 10% of research_v1 wall time (guess).

**Costs.** 1–2 weeks. A separate OS user needs admin rights, so a container is the practical boundary. More moving parts, and infra failures must still classify correctly (M0).

**Prior art.** Saltzer & Schroeder 1975; Capsicum (Watson et al., USENIX Security 2010); Miller, *Robust Composition*, 2006.

---

## P7. Calibrated adversarial search against frozen Kernel versions (rank 7)

**Idea.** A scripted attacker searches P2's event grammar with the goal "reach CERTIFIED for a claim false by construction". Later, an LLM attacker from **another model family** adds event kinds. **Calibrate first** on historical pre-fix Kernel versions: "no findings" means nothing until recall is known. Findings go into P3's registry (attacker, version, budget).

**Acceptance.** Recall ≥ 50% of Kernel-level historical findings on their pre-fix versions within 10⁶ scripted steps. Any run on the current version is reported together with that recall.

**Costs and failure modes.** A same-family attacker shares the builder's blind spots. Safety classifiers halted "red-team" agents (HANDOFF2 §2), so use the "counterexamples as unit tests" framing. The LLM part needs budget.

**Prior art.** AFL/OSS-Fuzz, Jepsen, bug bounties. New: recall calibration on a verifier's own exploit history.

---

## 6. What NOT to do

- **No Lean or TLA+ refinement proof of the current checks.py.** It is 3.5k lines with floats, SQLite and pydantic. The cost is months, and most historical bugs were spec gaps that such a proof would not catch. Revisit only for P4's core, and only after P2 shows the spec is stable.
- **No signatures before a process boundary** (P5 after P6): a key the attacker can read proves nothing.
- **No N-version voting in production certification** (both implementations must agree to certify) until P2 measures how often their failures coincide. Keep it as a test oracle only.
- **Do not chase a mutation score on stats and metrics code.** That surface is better served by the scipy oracle (P2 M4).
- **Do not count "the red-team found nothing"** as assurance without recall calibration (P7).
- **Do not let tests list their own gate membership by hand** (the `KERNEL_TESTS` list).
- **Do not make `verify()` silently "as-of"** without also adding a "stale" status. Otherwise a real later contamination (a `develop` read after certification) disappears.

---

## 7. Exact GOAL.md diff for the top 2

```diff
@@ §4.3 (after line 194)
 Plus **K0 Provenance (always)**: every number and citation resolves to an artifact (current audit I1/I3/I4).
+
+**Time semantics.** Every check is a function of the ledger PREFIX up to and including the reads the Kernel makes
+for that certification. `verify` re-derives a certificate as of that prefix (forgery detection). Later events (new
+claims on a related target, later `develop` reads, twins) never change a re-derivation silently: they make the
+certificate STALE (reported separately from "forged"), and a human or a rule decides whether to retract.
@@ §5 Evidence Kernel, done-when (lines 306-309)
   the Kernel accepts the true ones and rejects each failure type with the correct reason; (b) Kernel code has 100% branch
-  coverage and contains no LLM call. ✅ both (`tests/test_kernel_suite.py`, `scripts/check.py` coverage gate,
-  `tests/test_kernel_quality.py` import-closure test). v1 deviations are listed in STATUS.md.
+  coverage and contains no LLM call. ✅ both (`tests/test_kernel_suite.py`, `scripts/check.py` coverage gate,
+  `tests/test_kernel_quality.py` import-closure test). v1 deviations are listed in STATUS.md.
+  (c) (new) every historical loophole re-introduced as a patch (`redteam/museum/`) is caught by >= 1 test; mutation
+  score >= 90% on vault.py, ledger.py and the K2/K4/K6 code (equivalent mutants <= 10%, each justified); the gate's
+  Kernel test set is derived, not listed by hand. (d) (new) a clean-room reference model of the decision core agrees
+  with the Kernel on >= 1e5 generated ledger histories, and the metamorphic relations of invariant 18 hold.
@@ §10 Hard invariants (after invariant 16)
 16. No harness edit is accepted without passing the leave-one-family-out gate (and leave-one-domain-out once ≥ 2
     domains exist).
+17. Every attack test kills its attack: each red-team finding has a re-introduction patch that >= 1 test fails on,
+    and the Kernel's mutation score stays at or above the §5 floor. A test that no longer exercises its attack is a
+    gate failure, not a review finding.
+18. Kernel verdicts are non-interfering and fail-closed monotone: events on unrelated data, evidence order, claim
+    certification order and consistent id renaming never change a verdict; adding suspicious evidence never raises a
+    label; `verify` re-derives as of the certificate's ledger prefix. Each relation has a property-based test.
```
