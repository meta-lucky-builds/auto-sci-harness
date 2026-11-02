# Evidence Kernel: trust boundaries and integrity mechanisms

What the Kernel enforces in code, what it only makes accountable, and what it does not cover. Written for the
2026-10-07 hardening round (branch `m3-fix-kernel`). Tests: `tests/test_kernel_integrity.py`,
`tests/test_review_kernel.py`, `tests/test_research_sequential_runs.py`.

## 1. Label reads (vault purposes) and K2

Every read of a private file goes through `Vault.read`, which writes a `data_access` ledger entry first.

- `evaluate` and `kernel` reads must name a pre-registered claim, AND the dataset must be one of that claim's
  generalization targets (R1-F1). A registered claim on another target is no longer a licence to read labels.
- K2 counts every private read of a target before the claim's registration as peeking, except
  - `evaluate`/`kernel` reads for ANOTHER claim registered before the read whose targets include this target or a
    related one (`related_targets`: shared example rows, or a byte-identical private file under another id). That
    is test-set reuse, charged by K4 as Bonferroni multiplicity over the same related set (R1-F4);
  - `leakscan` reads (see below).
- `reproduce` reads are counted as peeking. Nothing on a live store reads with that purpose (bundles are re-checked
  on a fresh store with purpose `kernel`), so excusing it would only widen the trusted surface.

### Integrity reads: a documented trust boundary

The research audit reads held-out labels with purpose `leakscan` to build leak fingerprints. Before the fix, that
read made every later claim on the same task fail K2 (two honest runs gave `[CERTIFIED-INTERNAL, REJECTED(K2)]`).
K2 now LISTS such reads as observations on the certificate ("N integrity read(s) (leakscan) before
pre-registration") and does not count them as peeking.

Boundary: purposes are declared by trusted host code. The Kernel cannot tell a real leak scan from a peek that was
mislabelled `leakscan` by host code. Models and harness edits have no vault access, so this trusts the host
modules, not any model output. Pinned by the strict xfail
`test_r1_label_read_under_a_registered_claim_on_another_target_is_peeking[leakscan]`: it turns into a failure on
the day this boundary is closed.

## 2. Re-execution (K3)

- The sampled replay seed hashes only plan-matched records (claim targets, planned seeds); K4 rejects any record
  outside the plan (R1-F2).
- Once a recorded certificate of a claim (issued while the claim was registered) failed K3, K3 fails for that claim
  for good (R1-F3). Certificates rejected for other reasons (e.g. K1 before an adapter approval) block nothing. An
  unreadable or mis-filed earlier certificate counts as a failure. A claim whose code did not reproduce once must
  be re-registered as a new claim, which K4 charges as multiplicity.
- Residual: K3 re-executes one sampled seed once. A program that is irreproducible half the time still passes its
  FIRST certification half the time.

## 3. Domain adapters (K1)

An adapter's identity hashes its listed modules AND the transitive closure of the project modules they import
(`adapters.covered_modules`, resolved with `ast`, relative imports included, lazy imports included). Stdlib and
installed packages (site-packages) are excluded: they are pinned by the environment, not by the approval.

Consequences and limits:
- The ML adapter now covers about 13 modules (kernel claim/execution/ledger/vault, store, records, tiers, sandbox
  base, verify metrics/stats/leak). Every edit to any of them needs a new human approval (`chh approve-adapter ml`).
  Existing approvals do not resolve after this change: the human must approve again.
- Package `__init__` modules executed implicitly on import are hashed only when imported explicitly.
- If the project itself were installed into site-packages (not editable), its modules would be excluded.

## 4. Dataset roles: external/future need a human

`register_dataset` accepts any role, so a registrant could type `external`. CERTIFIED-VALIDATED now needs, for every
target, role `external`/`future` AND a `role_approval` ledger entry for that dataset's current content hash and
role, written by the human CLI `chh approve-role DATASET --approver NAME [--note ...]`.

Without the approval the claim earns at most CERTIFIED-INTERNAL (fail closed: the weaker label; its K6 still ran
on held-out data never used in development), and K6 records an observation saying the role is not approved.
`chh return-data` (physical path) prints the approve-role command as a next step.

Boundary (as for adapter approvals): trusted host code could call `Vault.approve_role`; the entry names the
approver and is hash-chained (accountability, not enforcement). Models and harness edits cannot reach it.

## 5. Ledger: tamper-evident, externally anchored

The ledger is a hash chain: editing, deleting or reordering an entry breaks every later hash. Whoever controls the
database can still rewrite the whole chain consistently. External anchoring narrows this:

- `chh ledger-anchor FILE` appends `{format, length, head, at}` to FILE, which must be outside the store directory;
  a broken chain is never anchored.
- `chh ledger-verify --anchor FILE` (repeatable) fails unless the current chain still has each anchored head at its
  anchored position (appending later entries is fine; deleting or rewriting anything at or before an anchor is
  detected).

What it does NOT do: an attacker who controls both the store and every copy of the anchor file can rewrite both.
Entries written after the latest anchor are only tamper-evident. Keep anchor copies somewhere the store's owner
cannot silently edit (another machine, a git remote, a message to a third party). Entries are not signed.

## 6. Reproducibility bundles

- Format `chh-bundle/2` carries the whole ledger prefix up to and including the certificate entry; reproduction
  checks it as one unbroken chain from genesis, so leaving out an entry (e.g. an early label read) breaks the
  chain. The report gives `ledger_check` and `ledger_head` (compare the head with a published anchor of the same
  length). A forger who controls the bundle can still rebuild a whole consistent chain; only an external anchor
  pins it.
- The bundle also carries the public files and canaries of every registered dataset (the Kernel's shared-example
  and canary checks read all of them), and earlier certificates of the same claim (K3 history). Entry bodies hold
  ids, hashes, purposes and approver names, never data. Bundles still contain hidden labels: never agent-visible.
- Old `chh-bundle/1` bundles are re-checked as before, but `ledger_check` says "EXCERPT ONLY": per-entry hashes and
  order only, omitted entries cannot be detected.
- Reads are bounded: per-member (512 MiB), total (2 GiB) and member-count (100,000) caps on DECLARED sizes, and a
  compression-ratio cap (200:1 for members over 8 MiB). zipfile never returns more than a member's declared size (a
  longer stream fails its size/CRC check), so declared-size checks bound memory. Over a cap the bundle is refused
  with a `BundleError` naming the cap; `chh reproduce` prints it and exits 1.
- Not covered: `store/run_bundle.py` (per-run bundles, `chh rerun`) still reads its manifest without a cap.

## 7. Leak detection (K9)

K9 scans subject/comparator code, program inputs and run traces for canaries and hidden labels:
- verbatim canaries and the first-40-labels windows (as before);
- the same in common encodings: reversed text, URL-decoding, every hex run (>= 32 chars) and base64 run (>= 24
  chars, standard or URL-safe) decoded;
- canary fragments: any 16-character piece of a canary's random part, case-insensitive (only when the random part
  is at least 16 characters);
- label runs: any run of consecutive hidden labels at any offset, any common separator, forward or reversed, of at
  least 40 labels AND at least 64 bits of information under the labels' own frequencies.

False positives are controlled by those minimums: short label sequences, constant or low-information runs (e.g. a
90/10 binary target needs far more than 40 labels; a single-class target can never match), and random hex/base64
do not trigger. Label runs are not applied to workspace files (a perfect predictions file legitimately matches);
`test_perfect_dev_predictions_are_not_a_leak` still passes.

Not detected: other transformations (re-mapping labels, nested or compressed encodings, partial use below the
thresholds), and information leaking through the metric itself (probing submissions). The engine's trace scrubber
and the research audit (I5) are outside the Kernel; I5 gains the encoded-view checks through `verify/leak.py` but
not label runs unless its caller passes `label_runs` in the fingerprint.

## 8. Kernel 1.2 (GOAL §16, 2026-10-07)

- **Fixed at registration (invariant 17).** `preregister` stores `fixed` in the prereg entry: `p0` (alpha /
  n_comparisons, or the alpha-account slot level), `p_ref` (Laplace base rate of certificates resolved so far, the
  pinned reference forecaster) and the K3 reliability requirement (`eps_max`, `beta`, `replays`). A forecast q < p0 is
  refused (F2). VDS = sum of log2 LR with LR = q/p0 or (1-q)/(1-p0); `ville_threshold(alpha)` = log2(1/alpha). Skill
  is reported separately against `p_ref`.
- **Alpha-accounts.** `open_alpha_account(target, alpha, slots, approver)` (human; refused once a claim exists on the
  target family). Each later claim reserves a slot at alpha/slots; the last slot retires the target;
  `retire_alpha_account` retires early. Targets without an account keep alpha / max(n_comparisons, family).
- **Release ledger.** `record_release(target, channel, alphabet, note)`; K4 uses the transcript bound
  (`releases.transcript_bound`); a `continuous` release makes later claims on the family fail K6 (adaptively
  exposed). Assumption on every data certificate: the release log is complete.
- **verify.** `verify_status(cert)` re-derives as of the certificate's own ledger prefix (forged if different) and
  today (stale if different). `verify()` stays strict (problems prefixed `forged:` / `stale:`).
- **K3** passes only after `replays` consecutive clean re-executions (zero-failure demonstration; presets
  eps 0.2, beta 0.1 -> 11 replays, UNCALIBRATED).
- **K4** compares the executed test (interval methods, level, sample size) with the registered plan
  (`K4_PLAN_CODE_DIVERGENCE`).
- **Reason codes.** Every reason carries a closed `reasons.Code`; exception messages never enter reasons (type name
  from a fixed set only). Agent-visible surfaces should carry codes, never reason text.
- **Self-reported fields** (an external/future role no human approved) cap the tier at T4 (UNCALIBRATED).
- **Replayed evidence** (record or trace marked `replayed`) is refused at K0 (invariant 21).
- **Minted targets.** `record_mint` (one mint per frozen hash); K1 enforces `delta_floor` on minted targets.
