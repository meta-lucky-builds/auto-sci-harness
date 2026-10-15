# Domain adapter proposals (GOAL §15, domain-scout output)

> **STATUS: PROPOSAL, UNVERIFIED.** Written by an AI domain-scout agent from its own knowledge on 2026-10-08. No
> citation, version number, licence or URL in this file was checked against a source in this task. Nothing here is
> approved, and no code exists for it.

Label legend used in every file of this folder:
- **[reported]** = reported from the scout's memory, UNVERIFIED (no source was opened in this task; no web access,
  no downloads). Treat every such item as a lead for a human or builder to check, not as a fact.
- **[inferred]** = the scout's own reasoning from GOAL.md and the code in this worktree (`kernel/sim.py`,
  `kernel/adapters.py`, `kernel/checks.py` `CHECK_APPLICABILITY`, `docs/adapters_sim.md`), read on 2026-10-08.
- Nothing in this folder is labelled "verified", because nothing was.

## Index

| File | Domain | Evidence class (GOAL §4.7) | Max tier | Reuses | Biggest open item |
|---|---|---|---|---|---|
| [fem_structural_thermal.md](fem_structural_thermal.md) | structural / thermal FEM | simulated | T1/T2 within the simulator (proposed T2) | the `sim` claim shape (scope phrase, h, h/2, h/4 study) | which FEM tool (none installed); how an external library is hash-pinned |
| [compbio_public_data.md](compbio_public_data.md) | computational biology on public data | data / statistical | T2 (T4 time-split) | the existing data-class Kernel path (K0-K9) | downloads, cohort-level K6 design, multiplicity policy |
| [sysbio_ode.md](sysbio_ode.md) | systems-biology ODE simulation | simulated | T1/T2 within the simulator (proposed T2, as `sim`) | `chh-ode-rk4` (`kernel/sim.py`) | stiffness (fixed-step RK4), new simulator version needs re-approval |

## Rules every proposal here follows

- **Wet-lab and other physical/empirical claims stay BLOCKED** until the human-owned prohibited list and review
  standard exist (GOAL §13.6, §15 step 2). Computational-only claims on public data and simulated claims do not
  change that, and nothing in these proposals is a step towards a physical protocol. This scout did not write,
  propose or decide any part of the prohibited list or the review standard, and lists no example categories: they
  are human-owned.
- No proposal weakens an existing check. Where a domain wants something the Kernel does not do today (FDR instead of
  Bonferroni, a "meets spec" claim kind, an adaptive or implicit integrator), it is listed as an **owner decision**
  and a Kernel or adapter change, never as a relaxation [inferred].
- Simulated claims are scoped to "in simulator S (version, config)"; "the simulation matches reality" is always an
  UNTESTED assumption (invariant 14). K4-K7 stay n/a for the simulated class as in `CHECK_APPLICABILITY` [inferred].
- Every adapter is human-approved and fixed (invariant 13); no model or evolver edits it; validation evidence
  (golden cases + fault injection) must pass before `chh approve-adapter` can record an approval (human-only action,
  not performed here). Replayed or simulated model output is never evidence (invariant 21). A new harness edit needs
  the LOFO gate and, once two or more domains exist, the LODO gate (invariant 16) [inferred from GOAL §10].

## Cross-cutting finding (applies to FEM, and to any adapter that wraps an installed package)

`adapters.covered_modules` hashes project source only; "stdlib and installed packages are pinned by the
environment, not by the approval" (`kernel/adapters.py` docstring). `sim` is pure Python inside the trusted base, so
its approval covers the whole simulator. An FEM adapter built on an installed library (scikit-fem, DOLFINx, ...)
would NOT have the library covered by its approval hash. Proposed (owner decision): the adapter refuses to run
unless the installed distribution matches a pinned identity recorded in the adapter source (e.g. the sha256 of the
wheel or of the distribution's file list, plus the container image digest), so that a library change is a fail-closed
refusal, not a silent drift [inferred].

## What this scout did NOT do

No code, no tests, no Python, no git, no downloads, no web lookups, no installs, no approvals. No file outside
`docs/domain_proposals/` was edited.
