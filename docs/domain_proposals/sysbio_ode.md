# Proposal: systems-biology ODE simulation (simulated class, reusing `chh-ode-rk4`)

> **STATUS: PROPOSAL, UNVERIFIED.** Written by an AI domain-scout agent from its own knowledge on 2026-10-08. No
> citation, version number, licence or URL in this file was checked against a source in this task. Nothing here is
> approved, and no code exists for it.

Legend: **[reported]** = from the scout's memory, unverified; **[inferred]** = the scout's reasoning from GOAL.md and
the worktree code (`kernel/sim.py`, `kernel/adapters.py`, `docs/adapters_sim.md`). Nothing here is verified.

**Wet-lab and other physical/empirical claims stay BLOCKED** until the human-owned prohibited list and review
standard exist (GOAL §13.6). Simulated claims about model ODEs do not change that, and no claim here says anything
about a real organism, cell or molecule. This scout does not propose, write or decide any part of the prohibited list
or the review standard.

## 1. Summary

- Evidence class: **simulated**. Max tier: **T1/T2 within the simulator**; proposed **T2**, the same conservative
  choice `sim` made (`docs/adapters_sim.md`) [inferred].
- Reuse: the existing pinned simulator `chh-ode-rk4` (pure Python, fixed-step classical RK4, IEEE-754 doubles,
  bit-for-bit reproducible), its config format `chh-sim/1`, its K3 convergence study at h, h/2, h/4 (MIN_RATIO 8,
  Richardson `16/15 |q(h) - q(h/2)|`), its `scope_phrase`, and its K8 hook (`bounds`) [inferred from `kernel/sim.py`].
- Claim kind: `computed_value` only ("x_i(t_end) of model M = value ± tol in simulator chh-ode-rk4 (version V,
  config C)"), as today. Derived quantities (steady states, periods, bistability) are new quantity types: later
  versions, owner decision [inferred].
- K4-K7 n/a with `CHECK_APPLICABILITY[SIMULATED]`'s reasons; K0-K3, K8, K9 apply [inferred].

## 2. What the certificate would state

Exactly `sim`'s shape: "x[i](t=T) of <model> = value ± tol in simulator chh-ode-rk4 (version V, config <sha12>)",
label CERTIFIED-SIMULATED, tier T2. UNTESTED assumptions always listed: (1) the simulation matches reality; (2) the
ODE model (structure, mass-action or Hill kinetics, well-mixed compartment, deterministic continuum, no noise)
represents the biological system; (3) the parameter values represent the biological system; (4) for imported models,
the conversion from the source file (e.g. SBML) is faithful, unless the converter itself is part of the adapter
[inferred].

## 3. Model families to add to the registered family

Present today: `linear`, `damped_oscillator`, `sir` [inferred from `PARAMS` in `sim.py`]. Proposed additions, each
with exact-rational parameters and registered ranges:

| Family | Equations | Params | Notes [inferred unless marked] |
|---|---|---|---|
| `mass_action` | x' = N v(x), v_j = k_j * prod_i x_i^(a_ij) | stoichiometry N (small integers), reactant orders a_ij in {0, 1, 2}, rate constants k_j > 0 | species <= MAX_DIM (6 today), reactions <= e.g. 12; orders as integers so rates use only `*` (bit-for-bit kept). Covers A->B, A<=>B, 2A->P, E+S<=>C->E+P, closed networks. |
| `michaelis_menten` | S' = -Vmax S / (Km + S), P' = +Vmax S / (Km + S) | Vmax > 0, Km > 0 | reduced (quasi-steady-state) form; uses `/`, which IEEE-754 rounds correctly [reported], so still deterministic. |
| `toggle_switch` | u' = a1 / (1 + v^n) - u, v' = a2 / (1 + u^m) - v | a1, a2 > 0; **integer** n, m in 1..8 | Gardner-Cantor-Collins form [reported: Nature 2000]. Integer Hill exponents by repeated multiplication; non-integer exponents need `pow` from libm, which is not guaranteed correctly rounded across platforms [reported], and would break bit-for-bit K3: refused in this version. |
| `lotka_volterra` | x' = a x - b x y, y' = d x y - g y | a, b, d, g > 0; x0 > 0 | first integral V = d x - g ln x + b y - a ln y is conserved [reported]: K8 law (K8 uses a tolerance, so `ln` from libm is acceptable there). |
| `goodwin` | x' = a / (1 + z^n) - b x, y' = c x - d y, z' = e y - f z (three-stage negative feedback) | rates > 0; integer n | classic result: with equal degradation rates the loop oscillates only for n > 8 [reported: Griffith 1968]; exact equations to be fixed by the builder from the source. |

Every family keeps `sim`'s parsing rules: exact rational strings, duplicate and unknown keys refused, out-of-range
values refused, `steps <= MAX_STEPS`, `t_end <= MAX_T_END` [inferred].

## 4. Stiffness: the main limitation of fixed-step RK4

- Classical RK4 is explicit; its stability interval on the negative real axis is about |h lambda| <= 2.78
  [reported]. Biochemical networks are often **stiff** (fast binding vs slow turnover; small Km; Robertson's
  chemical kinetics test problem [reported: Hairer & Wanner]), so a stable step is far smaller than accuracy needs.
- With today's caps (`MAX_STEPS` 20000, `t_end` <= 1000, so h >= t_end / 20000 and K3 also runs 4x the steps), a
  stiff model either diverges (rejected as non-finite) or fails the convergence ratio (rejected as unconverged)
  [inferred from `sim.check`]. That is **fail-closed**: no false certificate, but a whole class of models simply
  cannot be certified.
- Implications [inferred]: (i) v1 registers parameter ranges that keep typical problems non-stiff, and states the
  limitation in the adapter doc; (ii) a stiffness pre-screen (Jacobian eigenvalue estimate) may only deny or prune,
  never grant (invariant 21); (iii) real stiff support needs an implicit or Rosenbrock method (e.g. BDF, Radau IIA)
  with a Newton/linear solve per step. That is a **new simulator version** with a new convergence criterion
  (order differs from 4, so MIN_RATIO changes; adaptive step control would replace the h, h/2, h/4 study with a
  tolerance-proportionality study), new golden cases and a new human approval.

## 5. Conservation laws and other K8 invariants

1. **Mass conservation in closed mass-action networks**: every vector w with w^T N = 0 gives a conserved quantity
   w^T x. The adapter computes the left null space of N **exactly** (rational arithmetic) from the config, so the
   invariants are derived, not declared by the claimant (cannot be gamed by omission) [inferred]. Check |w^T x(T) -
   w^T x0| <= bound at the declared step.
2. **Non-negativity**: the exact flow of mass-action, MM, toggle and LV models keeps concentrations non-negative
   [reported]; RK4 with a large step can go negative, so `min x(T) >= -floor` is a K8 law (as `sir` does today).
3. **Lotka-Volterra first integral** V conserved within a bound.
4. **Symmetry**: a toggle switch with a1 = a2, n = m and u0 = v0 must give u(t) = v(t); in this deterministic
   simulator the two components are computed by identical operations, so equality should be exact [inferred].
5. **Dimensional consistency** (if units are added, owner decision): a rate constant of an order-k reaction has
   units concentration^(1-k) time^-1; the adapter can check this from N and the declared units [reported: standard
   kinetics].
6. **Sanity bounds**: finite state; tolerance <= `MAX_REL_TOLERANCE` (1/100) x max(1, |value|) as today.

## 6. Parameter ranges [inferred, UNCALIBRATED]

`sim` today caps every parameter and initial value at |.| <= 100 and t_end <= 1000. Biological concentrations span
many orders (e.g. nM to mM) and rate constants span many more, so v1 should require **non-dimensionalised** models
(values in [1e-3, 1e2]?) or add explicit units with exact rescaling. Either choice is an owner decision; widening
the existing ranges for the existing families would change `sim` and is not proposed.

## 7. Model source: SBML / BioModels (download needed: owner decision)

- SBML is the standard exchange format for such models [reported: Hucka et al. 2003]; BioModels (EMBL-EBI) hosts
  curated models [reported: Le Novere et al. 2006; Malik-Sheriff et al. 2020]; curated models are reportedly under
  CC0 [reported, unsure: check per model].
- Downloading any model is forbidden to this swarm now: the owner approves which models and fetches them.
- Proposed import path [inferred]: an offline, human-run converter that accepts only a small SBML Level 3 Core subset
  (one compartment, mass-action or listed Hill laws, no events, no rules, no delays) and emits a `chh-sim` config;
  the original file's sha256 is recorded as provenance, and "the conversion is faithful" is an UNTESTED assumption
  unless the converter becomes part of the adapter (then it is hash-covered and needs approval). libSBML is LGPL
  [reported]; putting it in the trusted import closure is a footprint decision for the owner.

## 8. What would be a NEW adapter version needing human approval

`adapters.SIM` hashes `custom_harness.kernel.sim` and every project module it imports (`covered_modules`) [inferred
from `kernel/adapters.py`]. Therefore:
- **Any** edit to `sim.py` (adding a family, changing a range, a tolerance, the integrator) changes the `sim` hash:
  the current approval stops resolving, and `chh approve-adapter sim` (human-only) must run again after validation.
- Adding families while keeping `VERSION = "1"` would make "version 1" name two different programs. Proposed: bump
  the simulator version (e.g. "2") for the bio family set, and keep the existing families' code paths unchanged so
  that old configs reproduce bit-for-bit (whether version "1" configs stay accepted is an owner decision).
- Alternative: a separate module (e.g. `kernel/sim_bio.py`, adapter `sim-bio`) that imports the RK4 core from
  `sim.py`. Because of the transitive hash, a later edit to `sim.py` would then invalidate BOTH approvals; that is
  correct behaviour, but the owner should choose the layout knowingly.
- Changes that are new versions for certain: new families; new quantity types (steady state, period); units; wider
  ranges; non-integer Hill exponents; any implicit/adaptive integrator; an SBML reader inside the adapter.

## 9. Golden cases [closed forms reported, standard kinetics]

- A -> B, first order: A(t) = A0 exp(-k t) (already covered by `linear` with A = [[-k]]).
- A <=> B: A(t) = A_eq + (A0 - A_eq) exp(-(kf + kr) t), A_eq = kr (A0 + B0) / (kf + kr).
- 2A -> P with rate convention dA/dt = -2 k A^2: A(t) = A0 / (1 + 2 k A0 t) (the builder must fix the convention).
- A + B -> C with A0 = B0: A(t) = 1 / (1/A0 + k t).
- Reduced Michaelis-Menten: closed form via the Lambert W function [reported: Schnell & Mendoza 1997]; the golden
  value would be computed to high precision outside the simulator and frozen into the fixture.
- Lotka-Volterra: V(t) = V(0); small oscillations near equilibrium have period about 2 pi / sqrt(a g) [reported].
- Symmetric toggle: u(t) = v(t) exactly.
- E + S <=> C -> E + P: E + C and S + C + P conserved (derived by the left null space).

## 10. Fault-injection list

1. Planted wrong value -> "differs from the simulated value" (existing path).
2. Unconverged step on a stiff instance -> "unconverged" or "non-finite" (rejected for the right reason).
3. Wrong stoichiometry entry (planted typo breaking a conservation law) -> conservation K8 fails on a golden network
   whose conserved totals are fixed in the fixture.
4. Negative concentration from a too-large step -> non-negativity K8.
5. Non-integer Hill exponent or reaction order > 2 -> refused at parse.
6. Rate in per-minute read as per-second (factor 60), if units are added -> dimension check / golden mismatch.
7. x0 inconsistent with a declared conserved total (if totals can be declared) -> refused.
8. Version drift (config says version 1 for a bio family) -> "pinned version".
9. Result of another config; reported values not reproduced; NaN -> existing paths.
10. More species than MAX_DIM or too many reactions -> refused.
11. Imported model whose source hash differs from the recorded provenance -> refused (if the converter is in the
    adapter).
12. Duplicate keys, float-encoded parameters, out-of-range parameters -> existing paths.
13. Asymmetric result for the symmetric toggle golden -> validation fails (detects a non-deterministic or
    order-dependent RHS).

## 11. Tolerances

Reuse `sim`'s UNCALIBRATED values (MIN_RATIO 8, ROUNDING_FLOOR 1e-9 relative, MAX_REL_TOLERANCE 1/100). New:
conservation bound (e.g. 1e-9 relative to the conserved total), non-negativity floor (e.g. -1e-9 x scale), LV
first-integral bound. All owner decisions.

## 12. Task suite (proposed families; LOFO)

- F1 closed mass-action networks (enzyme kinetics, binding, reversible chains).
- F2 gene-regulatory switches (toggle variants, value at t from different initial states).
- F3 oscillators (Lotka-Volterra, Goodwin; value at t).
- F4 epidemiological compartments (existing `sir`, later SEIR).
- **Held out (LOFO): F3** [inferred: oscillatory dynamics are the hardest for fixed-step convergence and most
  different from F1/F2]. For LODO, whether "systems biology" and the existing `sim` tasks count as one domain or two
  is an owner decision.
- Sources: textbook closed forms (synthetic, no download); BioModels-derived instances only after an owner-approved
  download. As for FEM, tasks should be search/design tasks with truth minted after the freeze (§16.2), because the
  agent can run the same deterministic simulator [inferred].

## 13. Compute, download, licence needs

- Compute: pure Python RK4 on <= 6 species; `docs/adapters_sim.md` reports a worst-case K3 of about 8 s on the laptop
  at MAX_STEPS [reported from that doc]. No new install for v1 (no external library) [inferred].
- Downloads: none for v1 (synthetic families); BioModels/SBML files later, owner approval.
- Licence: none for v1 (in-house code); per-model licences for BioModels imports; libSBML LGPL if used [reported].

## 14. What stays untested

Reality match; model-structure and parameter adequacy; well-mixed and deterministic assumptions (no stochastic
effects at low copy numbers); conversion fidelity for imported models; anything about stiff models (cannot be
certified in v1).

## 15. Needs owner decision

1. Extend `sim.py` (new simulator version, re-approval of `sim`) vs a separate `sim-bio` adapter importing it.
2. Whether version "1" configs stay accepted after a version bump.
3. Which families, the species/reaction caps, and parameter ranges (non-dimensionalised vs units).
4. Whether to support stiff systems later (implicit integrator = new simulator, new convergence criterion).
5. New quantity types (steady state, period) and any new claim kind.
6. BioModels downloads, per-model licence review, and whether an SBML converter lives inside the adapter.
7. All tolerances in section 11; tier T1 vs T2 (proposed T2).
8. LOFO held-out family; LODO domain boundaries.
9. The prohibited list and review standard (human-owned; untouched here).

## 16. Citations [all reported, unverified; details may be wrong]

- T. S. Gardner, C. R. Cantor, J. J. Collins, "Construction of a genetic toggle switch in Escherichia coli", Nature,
  2000.
- B. C. Goodwin, "Oscillatory behavior in enzymatic control processes", Adv. Enzyme Regul., 1965; J. S. Griffith,
  "Mathematics of cellular control processes I", J. Theor. Biol., 1968.
- S. Schnell, C. Mendoza, "Closed form solution for time-dependent enzyme kinetics", J. Theor. Biol., 1997.
- E. Hairer, G. Wanner, *Solving Ordinary Differential Equations II: Stiff and Differential-Algebraic Problems*,
  Springer (2nd ed. 1996).
- M. Hucka et al., "The systems biology markup language (SBML)", Bioinformatics, 2003.
- N. Le Novere et al., "BioModels Database", Nucleic Acids Res., 2006; R. S. Malik-Sheriff et al., "BioModels - 15
  years of sharing computational models in life science", Nucleic Acids Res., 2020.
