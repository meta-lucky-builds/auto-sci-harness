# Simulator adapter `sim` (simulated evidence class, GOAL §4.7)

Built 2026-10-07 on branch `m3-sim-adapter`. Files: `src/custom_harness/kernel/sim.py` (the adapter module: pinned
simulator, convergence checker, fault-injection validation `SIM_CHECKS`), `src/custom_harness/kernel/simulated.py`
(Kernel core checks K1/K3/K8 for the class, domain-agnostic), tests `tests/test_kernel_sim.py`.

## What a simulated claim says
"In simulator **chh-ode-rk4 (version 1, config <sha256>)**, component *i* of the state at *t_end* is `value` ±
`tolerance`." Nothing about reality. The certificate label is **CERTIFIED-SIMULATED**, tier **T2** (conservative), and
the assumption "the simulation matches reality ..." is always listed with `tested_by = None` (UNTESTED).

## Simulator S
Fixed-step classical RK4 in IEEE-754 doubles, pure Python, inside the trusted code: no binary, no licence, bit-for-bit
reproducible (fixed operation order; `+ - *` are correctly rounded). Registered model family: `linear` (x' = Ax,
n <= 6), `damped_oscillator`, `sir`. Every input is an exact rational string; JSON numbers, duplicate keys, unknown
keys, out-of-range parameters (|.| <= 100, t_end in (0, 1000], steps in [1, 20000]) are refused.

## How a claim is checked
- Claim: kind `computed_value`, class `simulated`, metric `sim_value`, subject = comparator = the registered config
  artifact, no datasets/seeds/effects/risk prior; `scope.conditions` exactly `{simulator, version, config}` matching the
  approved adapter and the subject hash; `statement` exactly the simulator's rendering (`sim.scope_phrase`), so no
  prose can widen the scope.
- Evidence: one registered result artifact `{"format": "chh-sim-result/1", "config_sha256", "values": [q(h), q(h/2),
  q(h/4)]}` linked with `Kernel.record_certificate`, registered after the pre-registration (K2).
- K3 re-runs S at h, h/2, h/4 and requires: reported values reproduced bit-for-bit and finite; observed order >= 3
  (|q(h)-q(h/2)| >= 8 |q(h/2)-q(h/4)|) unless at the rounding floor; |q(h) - q(h/4)| <= tol; Richardson estimate
  E = 16/15 |q(h) - q(h/2)| <= tol; |value - q(h)| + E + floor <= tol. Decisions in exact fractions.
- K8: tolerance <= 1/100 * max(1, |value|); value size; background laws at the declared step (SIR conservation and
  positivity, oscillator energy non-increase, finite state).
- K4-K7 n/a with reasons from the human-owned `CHECK_APPLICABILITY` table; K0/K2/K9 as for computational claims.

## Approval
Unusable until a human runs `chh approve-adapter sim --approver <name>`, which first runs the 17 validation checks
(closed forms cos/exp, accepted golden claim, planted wrong value, unconverged step, wrong version/simulator, result of
another config, unreproduced and NaN reported values, diverging run, NaN/float and out-of-range parameters, step bound,
loose tolerance, energy-law violation, duplicate JSON key).

## UNCALIBRATED / owner decisions
MIN_RATIO 8, ROUNDING_FLOOR 1e-9 relative, MAX_REL_TOLERANCE 1/100, the parameter ranges, MAX_STEPS 20000 (worst-case
K3 about 8 s on this laptop), tier T2 rather than T1.
