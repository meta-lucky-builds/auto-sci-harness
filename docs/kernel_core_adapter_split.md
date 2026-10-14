# Kernel core / adapter split (M3 exit item; GOAL §5 "Domain adapters", §11 M3)

Status: built on branch `s3-kernel` (commit ee0ef1d, 2026-10-09/14). Bit-identity: see "Proof" below.

## What moved
Before the split, `kernel/checks.py` (the core) held the ML adapter's logic for K1 and K3-K8. It now lives in
`kernel/ml.py`, the ML adapter's **procedures** module, moved verbatim (same code, same reason strings, same order):

| Check | Procedure (`kernel/ml.py`) | What it is |
|---|---|---|
| K1 | `metric_reasons` | the metric registry: id known, direction matches |
| K3 | `recompute`, `replay_failure`, `is_regression` | reported vs recomputed score; when a replay reproduces (exact predictions or metric within the registered tolerance); how non-interference compares decisions |
| K4 | `statistics`, `plan_code_divergence`, `criterion`, `REGISTERED_TESTS` | the registered test (paired t over seeds + BCa over examples), plan-code divergence, the claim's criterion |
| K5 | `conformance` | improvement against a registered baseline; arms run under identical record fields |
| K7 | `risk` | the noise model (normal, resampling SE) and which claim kinds have one |
| K8 | `record_bounds` | metric range, prediction count, empty predictions |

What stays in the core (domain-agnostic data-class logic): provenance (K0), the claim's form, datasets and plan
bounds (K1), pre-registration and peeking (K2), the replay protocol, the zero-failure demonstration, earlier
failures and the non-interference protocol (K3), plan-following, multiplicity, alpha-accounts and releases (K4),
arm/code/metric identity (K5), held-out roles, twins, shared examples and development use (K6), label-file and
private-input rules (K8), leaks (K9). The exact, simulated and formal classes were already adapter-driven.

## Interface
`AdapterSpec.procedures` names the module (it must be one of the adapter's `modules`, so it is part of the adapter's
content hash). The core finds it with `adapters.procedures_for(evidence_class, metric)`: the REGISTRY, not the
approval (K1 judges the approval; the other checks run as before when K1 fails). If no registered adapter serves the
claim's (class, metric), the first adapter (by name) that has procedures is used: such a claim fails K1 anyway, and
this keeps its other checks exactly as before the split (stated: this is the one place where the core chooses
procedures it was not asked for; it never makes a claim pass).
Names that moved stay importable from `kernel.checks` (`TargetStats`, `_criterion`, `plan_code_divergence`,
`REGISTERED_TESTS`, `SCORE_TOLERANCE`) through a module `__getattr__`, for research/preflight.py and tests.
Monkeypatching them must now target `kernel.ml` (one test changed accordingly: `tests/test_kernel_s16.py`
plan-code divergence test).

## Consequences
- The ML adapter's hash changes (new module `custom_harness.kernel.ml`, new `procedures` key): **the human must re-run
  `chh approve-adapter ml`** before live runs can certify. No other adapter's hash changes from the split itself.
- Mutation testing of K4/K6 (GOAL §16.6, s3-assure's `scripts/mutate.py`) must now include `kernel/ml.py`, where the
  K4 statistics and criterion live (reported to the orchestrator; not changed here).
- Governance: a non-tightening core change, recorded as an owner-delegated deviation from the 7-day cooling-off.

## Proof of bit-identity
Differential test (sub-agent `diff`, branch `s3-kernel-diff`, `docs/kernel_differential.md`): every `Kernel.certify`
of the Kernel test suite and of seeded random histories, recorded on OLD (311c3e1, pre-split) and NEW (the split),
compared on label, tier and every check's status, codes and reasons (only proven-nondeterministic ids masked). The
result is reported in the s3-kernel final report; this note does not claim it.
