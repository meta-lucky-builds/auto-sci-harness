# Proposal: computational biology on public data (data / statistical class)

> **STATUS: PROPOSAL, UNVERIFIED.** Written by an AI domain-scout agent from its own knowledge on 2026-10-08. No
> citation, version number, licence or URL in this file was checked against a source in this task. Nothing here is
> approved, and no code exists for it.

Legend: **[reported]** = from the scout's memory, unverified; **[inferred]** = the scout's reasoning from GOAL.md and
the worktree code. Nothing here is verified.

**Wet-lab and other physical/empirical claims stay BLOCKED** until the human-owned prohibited list and review
standard exist (GOAL §13.6). This proposal covers computational analyses of already-public data only, and that does
not change the block. This scout does not propose, write or decide any part of the prohibited list or the review
standard.

## 1. Summary

- Evidence class: **data / statistical**. Max tier: **T2**; **T4** only for a genuine time-split (held-out cohorts
  deposited after a pre-registered cutoff, and after the cutoff of every model involved, GOAL §12) [inferred from
  GOAL §4.7].
- All of K0-K9 apply (`CHECK_APPLICABILITY[DATA]` marks none n/a) [inferred from `kernel/checks.py`].
- v1 claim shape (fits today's Kernel, no Kernel change) [inferred]: **predictive** claims, kind `improvement`: "a
  predictor built on development cohorts (subject) beats a declared baseline predictor (comparator, e.g. clinical
  covariates only, or a published signature re-fitted under identical conditions) by at least δ in metric m on
  held-out cohorts never seen in development".
- v2 claim shape (needs a new claim kind and K4 method, owner decision): **replication** claims, "gene g is
  differentially expressed between conditions A and B with sign s and |log2 fold change| >= δ, and this replicates in
  held-out cohort(s) H".

## 2. What the certificate would state

"On the held-out cohort(s) H (accessions, sha256 of the registered matrices, platform, deposit dates), under the
pre-registered preprocessing P and analysis plan A, predictor S beat comparator C on metric m by `effect` with
interval [lo, hi] (alpha after correction)." Label CERTIFIED-INTERNAL if H is a sealed part of the same data source,
CERTIFIED-VALIDATED if H is an external cohort or a later time window (GOAL §4.4) [inferred].

Untested assumptions always listed: (1) the public data are what their metadata say (labels, tissue, disease status,
sample identity are depositor attestations); (2) no unmeasured confounder differs between conditions (GOAL §4.6:
the Kernel does not detect confounders absent from the data); (3) the held-out cohort population is the population
the claim names; (4) no causal or clinical meaning: an association/prediction claim is not a mechanism, not a
diagnostic validation, and not a physical claim [inferred].

## 3. Candidate public sources (downloads: owner must approve; none was downloaded)

| Source | Access / licence [reported, unverified] | Would download | Why |
|---|---|---|---|
| NCBI **GEO** | public; NCBI states it places no restrictions on use, but submitters may hold rights [reported, unsure of wording] | series matrices / count tables for selected GSE series, plus sample metadata | many independent cohorts per question, deposit dates usable for time-splits |
| EMBL-EBI **ArrayExpress / BioStudies**, Expression Atlas | public [reported] | processed matrices + SDRF metadata | cross-check of GEO cohorts, independent cohorts |
| **TCGA via the NCI GDC** | open tier (e.g. gene expression quantifications, clinical) vs controlled tier via dbGaP (germline, raw reads) [reported] | open-tier expression counts + clinical tables for 1-3 projects | large cohorts with uniform processing; external validation against GEO cohorts |
| **GTEx** | open tier (expression summaries/TPM) on the portal; individual-level genotypes are controlled via dbGaP [reported] | open-tier expression by tissue | normal-tissue reference cohorts |
| **recount3** | uniformly processed public RNA-seq [reported: Wilks et al. 2021] | selected projects | reduces pipeline heterogeneity across cohorts |
| GENCODE / Ensembl annotation | open [reported] | one pinned release (GTF) | stable gene IDs; the version becomes part of the scope |
| **UK Biobank** | **restricted**: application, fees, material/data transfer agreement, no redistribution [reported] | nothing | out of scope unless the owner applies; listed so it is not used by accident |
| dbGaP controlled-access data | requires approved data access request [reported] | nothing | out of scope |

Every downloaded file would be registered in the vault with sha256 and a role (`development`, `sealed`,
`external`, `future`) **before any agent sees it**; sealed and future cohorts must be fetched by a human or a trusted
non-agent process into a mount no agent can read (invariant 2, §16.1) [inferred]. Size estimate [inferred]: hundreds
of MB to a few GB for v1.

Data terms note [inferred]: even open-tier human data come with use terms (e.g. no re-identification attempts);
the owner should confirm each source's terms before download.

## 4. K6 held-out cohort design

- **Split at cohort/study level, never at sample level.** A study's samples share lab, protocol, platform and batch;
  a sample-level split leaks all of that [inferred; batch effects reported: Leek et al. 2010].
- **Participant level inside a study**: TCGA has several aliquots/samples per participant (barcode prefix identifies
  the participant [reported]); all of one participant's samples go to the same side.
- **Batch and platform confounding**: refuse a cohort where the label is (near-)perfectly confounded with batch,
  processing date or platform (a registered check on metadata; threshold UNCALIBRATED). Cross-platform claims
  (microarray to RNA-seq) must use rank-based or within-sample-normalised features pre-registered in P.
- **External and time-split**: preferred held-out = cohorts from other labs (CERTIFIED-VALIDATED); T4 = cohorts
  deposited after a registered cutoff date. Contamination caveat: a model trained after the deposit may have seen
  the cohort or its paper, so the cutoff must also precede model cutoffs (GOAL §12) [inferred].
- **Preprocessing is fit on development data only** (normalisation factors, batch correction such as ComBat
  [reported: Johnson et al. 2007], feature selection, gene filters). Fitting any of these on pooled data that
  includes the held-out cohort is leakage; the ML adapter's transductive check (F4) is the pattern to extend
  [inferred from `verify/transductive` in `ML_CHECKS`].
- **Feature selection inside cross-validation**, not before it [reported: Ambroise & McLachlan 2002].

## 5. Multiplicity

- The Kernel's K4 today uses **Bonferroni (FWER)** over max(declared n_comparisons, all pre-registered claims on the
  target) [inferred from `kernel/checks.py` and `kernel/checklist.py`].
- Genome-wide differential expression conventionally uses **FDR** (Benjamini-Hochberg [reported: 1995]). FDR
  controls a different error rate and certifies a set, not each member; replacing Bonferroni by BH inside K4 would be
  a weaker per-claim guarantee [inferred].
- Proposal that weakens nothing [inferred]: genome-wide FDR is used only in development (on development cohorts, in
  the sandbox) to choose a SMALL number of candidates; the certified claims are the pre-registered candidates tested
  on held-out cohorts with the existing Bonferroni K4. Any FDR-based certificate is a new K4 method and claim kind:
  owner decision.

## 6. Registered metrics (proposed; direction, range)

| Metric id | Meaning | Direction | Range |
|---|---|---|---|
| `auroc` | area under ROC for a binary phenotype | higher | [0, 1] |
| `brier` | Brier score of predicted probabilities | lower | [0, 1] |
| `c_index` | concordance index for survival | higher | [0, 1] |
| `log_loss` | binary cross-entropy | lower | [0, inf) |
| v2: `lfc_sign_concordance` | fraction of pre-registered genes with the same fold-change sign in H | higher | [0, 1] |
| v2: `lfc_correlation` | correlation of log fold changes, dev vs H | higher | [-1, 1] |

Existing ML metrics (`verify/metrics`) may already cover some (accuracy, F1, RMSE); adding `auroc` etc. changes
the ML adapter or needs a new adapter: human approval either way [inferred].

## 7. K8 invariants

1. Raw count matrices: non-negative integers; no NaN [inferred].
2. Library sizes (column sums) > 0 and within a registered range; flag outliers.
3. TPM columns sum to 1e6 within rounding; CPM likewise [reported: definition].
4. p-values in [0, 1]; adjusted p >= raw p; log fold changes finite.
5. Metric values within their ranges (section 6).
6. Sample IDs unique; gene IDs unique and resolvable in the pinned annotation version; the same ID never maps to two
   rows after symbol mapping.
7. Sample counts per arm in the result equal the registered cohort manifest.
8. Optional metadata sanity (owner decision): recorded sex vs sex-chromosome expression markers; disagreements are
   reported, not silently dropped [inferred].

## 8. Golden cases

- Planted signal: a synthetic count matrix (negative-binomial draws, seeded) with known differentially expressed
  genes and a known predictor effect; the adapter must recover the planted effect and its interval covers it.
- Planted null: permuted labels must NOT yield a certificate (K4/K6 fail) [inferred].
- Known metric values: AUROC of a perfect, an inverted and a constant scorer = 1, 0, 0.5.
- Known count-model case: a two-group comparison with a hand-computable fold change.
(Synthetic fixtures need no download; real-data golden cases would need one: owner decision.)

## 9. Fault-injection list

1. Leakage across cohorts: one study's samples on both sides -> refused by cohort-level split isolation
   (`check_split_isolation`, GOAL §15) [inferred].
2. The same sample deposited in two series (GEO SuperSeries/SubSeries, re-deposits) on both sides -> refused by
   sample accession AND expression-fingerprint duplicate detection (e.g. near-identical profiles) [inferred].
3. Same participant, different aliquots, across splits (TCGA) -> refused by participant-level grouping.
4. Batch-confounded labels (all cases in batch 1, all controls in batch 2) -> refused by the confounding check.
5. Normalisation or batch correction fit on data including the held-out cohort -> transductive check fails.
6. Feature selection on all data before the split -> refused (selection must be inside the development pipeline;
   detection by re-running the pipeline without the held-out data and comparing selected features) [inferred].
7. ID collisions: gene symbols auto-converted to dates by spreadsheets [reported: Ziemann et al. 2016]; alias symbols
   mapping two Ensembl IDs to one row -> refused by ID validation against the pinned annotation.
8. Wrong annotation version or genome build -> refused (version is part of the scope).
9. Negative or non-integer "counts" fed to a count model -> K8.
10. Unpaired test on a paired design (or the reverse) -> K4 "test matches design".
11. Time-split violation: a "future" cohort deposited before the cutoff -> K6 refuses T4.
12. Label permutation (planted null) certified -> must fail.
13. Canary in a sealed cohort's file appearing in an agent artifact -> K9.

## 10. K7 risk model notes [inferred]

- Prior predictive from the comparator: the baseline predictor's held-out performance distribution (bootstrap over
  development cohorts) gives p0 for "subject beats baseline by δ"; fixed at registration (invariant 17).
- Many expression findings are trivially safe (very large tumour-vs-normal differences, proliferation signatures);
  K7 must fail those. A literature-based prior (how often similar signatures replicated) needs the literature module
  (M7): open, GOAL §13.1.
- Cross-cohort heterogeneity inflates variance: the prior must be computed across cohorts, not within one.

## 11. Tolerances (UNCALIBRATED)

δ floors per metric (e.g. AUROC δ >= 0.02?); confounding threshold (e.g. Cramer's V between label and batch);
duplicate-fingerprint correlation threshold; library-size range; TPM sum tolerance; minimum held-out cohort size and
events per arm. All owner decisions.

## 12. Task suite (proposed families; LOFO)

- F1 binary phenotype prediction from bulk expression (case vs control) across GEO cohorts.
- F2 survival / outcome prediction (TCGA open tier) with external GEO cohorts.
- F3 tissue or cell-type classification against GTEx reference.
- F4 (v2) DE replication across cohorts.
- **Held out (LOFO): F2** [inferred: different outcome type and metric, strongest test of generality].
- Sources recorded per task (accession, sha256, deposit date, licence/terms).

## 13. Compute, download, licence needs

- Downloads as in section 3: **owner approval required**, performed by a human or trusted non-agent process.
- Compute: laptop CPU is enough for bulk-expression predictors on a few thousand samples [inferred]; no GPU.
- Tools [reported licences]: SciPy/scikit-learn/statsmodels (BSD), PyDESeq2 (MIT, unsure); R/Bioconductor DESeq2
  (LGPL), limma and edgeR (GPL). Proposal [inferred]: heavy pipelines run in the sandbox in development only; the
  certifying statistic is a small, pre-registered procedure inside the adapter (as `verify/stats` is today), so the
  trusted base does not import R or large packages.

## 14. What stays untested

Metadata truth (labels, identity, tissue); unmeasured confounders; transfer to other populations; any causal,
mechanistic or clinical meaning; any wet-lab follow-up (BLOCKED as stated above).

## 15. Needs owner decision

1. Which sources, which cohorts, and approving each download (and who performs it into the sealed mount).
2. Data-use terms review per source; UK Biobank / dbGaP stay out unless the owner applies.
3. v1 predictive-only claims vs adding a replication claim kind (Kernel change).
4. Multiplicity policy: keep Bonferroni on pre-registered candidates (proposed) vs an FDR-based K4 method.
5. New metrics (`auroc`, `c_index`, ...): extend the ML adapter or a new `compbio` adapter.
6. All thresholds in section 11; the time-split cutoff date.
7. LOFO held-out family.
8. The prohibited list and review standard (human-owned; this proposal does not touch them).

## 16. Citations [all reported, unverified; details may be wrong]

- Y. Benjamini, Y. Hochberg, "Controlling the false discovery rate", J. R. Stat. Soc. B, 1995.
- J. T. Leek et al., "Tackling the widespread and critical impact of batch effects in high-throughput data", Nat.
  Rev. Genet., 2010.
- W. E. Johnson, C. Li, A. Rabinovic, "Adjusting batch effects in microarray expression data using empirical Bayes
  methods" (ComBat), Biostatistics, 2007.
- C. Ambroise, G. J. McLachlan, "Selection bias in gene extraction on the basis of microarray gene-expression data",
  PNAS, 2002.
- M. Ziemann, Y. Eren, A. El-Osta, "Gene name errors are widespread in the scientific literature", Genome Biology,
  2016.
- M. I. Love, W. Huber, S. Anders, DESeq2, Genome Biology, 2014; M. E. Ritchie et al., limma, Nucleic Acids Res.,
  2015; M. D. Robinson et al., edgeR, Bioinformatics, 2010.
- C. Wilks et al., recount3, Genome Biology, 2021.
- GTEx Consortium, Science, 2020 (v8 release paper; year reported, unsure).
