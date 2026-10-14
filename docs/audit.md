# Claim Audit and label-dependence audit (GOAL §16.10)

Package: `src/custom_harness/audit/`. CLI: `chh audit ...`. No model call anywhere; the Kernel is used unchanged.

## Claim Audit (`chh audit`)

A published sklearn-scale claim ("method X beats baseline Y by >= delta") is checked on FRESH data under a
pre-registered, identical contract. Steps, each refused unless the previous one happened:

| step | who | what it does |
|---|---|---|
| write `audit.yaml` (`chh-audit/1`) | HUMAN | paper DOI/title/verbatim sentence, metric, delta, seeds, ONE budget, the two arm files, pinned OpenML datasets (sha256 of the ARFF), optional citations, redistribution flags |
| `chh audit show SPEC` | anyone | prints the spec hash (YAML content + both arms' exact code), the typed claim and its hash |
| `chh audit approve-fidelity SPEC --approver NAME --spec-hash H` | HUMAN | "the typed claim is a faithful reading of the sentence"; ledger kind `approval`, node `audit_fidelity`, bound to spec hash + claim hash |
| `chh audit prepare SPEC [--cache DIR] [--allow-download]` | host | registers the arms, PRE-REGISTERS the claim, then reads the pinned datasets from the cache (re-hashed on every read) and registers them: train split as dev data, held-out split EXTERNAL. Downloading is an owner decision (`--allow-download`) |
| `chh approve-role DATASET --approver NAME` | HUMAN | existing command: the fresh dataset really is external data (needed for CERTIFIED-VALIDATED) |
| `chh audit run SPEC --docker [--snapshot ID]` | host | every arm/seed/target runs in the candidate sandbox through the same wrapper (`audit/wrapper.py`) with the same argv, inputs, env and time limit; ALL programs run before any label is read; labels through the vault; the Kernel certifies; one look per claim |
| `chh audit report SPEC` | anyone | the report, a pure function of the certificate |
| `chh audit reply SPEC --recorded-by NAME --notified WHO --notified-on DATE (--reply-file F / --no-reply)` | HUMAN | right of reply for this exact report hash; "no reply" only after `MIN_REPLY_DAYS` |
| `chh audit approve-publication SPEC --approver NAME --report-hash H` | HUMAN | only after the reply record |
| `chh audit publish SPEC --out DIR` | host | the only export path: refused without reply + approval for the CURRENT report hash, or if the certificate was retracted; the bundle is included only if code AND data are declared redistributable, else hash-only |

**Verdict** (computed from certificate fields only, `claim_audit.verdict`):
- CERTIFIED: the Kernel issued a certificate (CERTIFIED-VALIDATED when every dataset's role is human-approved).
- NOT CONFIRMED, naming the check: any failed check other than a missed effect criterion (K6) or a too-weak claim
  (K7); or a missed criterion whose interval's upper bound lies below the claimed delta (the data rule it out).
- INCONCLUSIVE: the only failures are a missed criterion whose interval still contains delta, and/or K7.
- COULD NOT RUN: no certificate: data unavailable (not cached / hash mismatch / too small), an arm crashed or
  wrote the wrong number of predictions, or infrastructure failed. No label is read when an arm fails.

**Citations**: if the spec lists the paper's citations, `literature/cite.py` checks them (I3) against a frozen
snapshot (`--snapshot`). Since the citations come from the paper's reference list in the human-approved spec, not
from a search, their resolved ids count as "retrieved".

**Parameters** (`audit/config.py`, all UNCALIBRATED): test fraction 0.5, minimum 30 held-out rows, 200k-row cap,
14 reply days.

**Limits (stated):**
- Statement fidelity is only as good as the human approval.
- The identical contract equalises data, seeds and time budget, not tuning effort.
- Fresh datasets may come from another population: NOT CONFIRMED may mean a narrower scope.
- The wrapper parses numeric features only (nominal features become their declared index).
- Approvals are hash-chained accountability, not enforcement: nothing stops a person copying local files.
- The real OpenML download path (`data.http_fetcher`) has never been run (owner decision).
- K9 does not detect held-out labels written as a quoted list literal in the arm's code. This is a Kernel gap
  reported to kstats; the test `test_planted_label_memorising_method_quoted_literal_k9_gap` is a strict xfail until
  it is fixed.
