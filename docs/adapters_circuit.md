# Circuit adapter `circuit` (computational class, T1) and the engineering task suite (GOAL M5, §15)

Built 2026-10-08/09 on branch `s3-domain` (base `a7fb608` = origin/int16-fix) by the s3-domain builder agent.
Status: **REGISTERED, UNAPPROVED.** It certifies nothing until a human runs `chh approve-adapter circuit --approver
<name>` (which first runs its validation). No agent approved it, and no real store holds an approval.

## Files
- `src/custom_harness/adapters/circuit/check.py`: the trusted checker the Kernel calls (`exact_module`): `check`
  (K3), `bounds` (K8), `scope_phrase`, `HIGHER_IS_BETTER`. Never solves: it stamps A, b from the netlist and multiplies.
- `.../validation.py`: `CIRCUIT_CHECKS`, the validation evidence `approve` runs (39 checks: golden cases + planted faults).
- `.../solver.py`: exact rational MNA (Gauss-Jordan on [A | b | I] in Fractions) that PRODUCES certificates; not part
  of the soundness argument, but hashed because the validation uses it to build golden witnesses.
- `.../screen.py`: a float screen for `verify.screen.screen_then_certify` (may only prune; NOT hashed, never called
  by the Kernel).
- `src/custom_harness/kernel/adapters.py`: the only Kernel edit, one import + `CIRCUIT` AdapterSpec + one REGISTRY
  entry (commit e688d64, with a governance `tighten` record in `redteam/kernel_core_changes.yaml`).
- `src/custom_harness/research/eng_suite.py`: engineering task suite and the cross-domain (ML + engineering) suite.
- Tests: `tests/test_circuit_adapter.py`, `tests/test_engineering_suite.py` (+ the red team's file, see below).

**Placement (owner/orchestrator decision).** The `lp` and `sim` adapters live in `kernel/`. This adapter lives in
`custom_harness/adapters/circuit/` because the brief forbade other Kernel edits. The equivalent Kernel placement would
be `kernel/circuit.py` (+ `kernel/circuit_validation.py`). Consequences of the chosen placement, stated:
(1) its ~400 lines are NOT counted by `test_kernel_is_small` (< 4000 Kernel lines) although they are trusted code;
(2) it is not in the Kernel 100% branch-coverage gate (`scripts/check.py` measures `custom_harness.kernel` only);
(3) `emergence.ACCEPTANCE_PATHS` does not list `adapters/**`; (4) `mypy --strict src/custom_harness/kernel` still
type-checks it (followed import), and it passes. Its identity IS protected: the adapter hash covers
`custom_harness.adapters`, `custom_harness.adapters.circuit`, `.check`, `.validation`, `.solver`, and the evolver's
static edit check rejects any edit naming those modules or the adapter name (registry-driven, `evolve/edits.py`).

## Claim-scope vocabulary (closed)
A claim is about the **ideal linear DC model** (`chh-circuit/1`) of one registered netlist: ideal resistors (R > 0),
independent V/I sources, VCVS, VCCS, CCVS, CCCS (current-controlled sources are controlled by a vsource, SPICE style),
DC, exact rationals. Registered quantities (one metric, `circuit_value`; the hashed query in the netlist fixes which):
`node_voltage` (V), `branch_current` (A, into `pos`), `element_power` (W absorbed), `singular` (1/0), `inconsistent`
(1/0). Anything else (works when built, ratings, temperature, tolerance, reliability) is not a registered quantity
and is refused by the reader; a claim relabelled `physical` is refused at K1 (no adapter serves it).
`scope_phrase(problem, value)` renders the only statement a claim should carry, ending "this says nothing about any
physical circuit".

## Data format and reader
Strict JSON: duplicate keys, unknown keys, JSON numbers, NaN/Infinity, decimals and exponents refused; names ASCII
`[A-Za-z0-9_]{1,32}`; units from a closed SI table without prefixes (`V A ohm S W V/V A/A 1`); size bounds refused
at parse (2 MB, 40 nodes, 120 elements, 80 unknowns, 100 digits) so no check can be made to hang. Formats in the
`check.py` docstring.

## Certificates and checks
- Value quantities: `x` with A x = b AND `inverse` M with M A = I (A square, so A is regular and x is the ONLY
  solution: a singular network can never carry a value claim). Then the quantity of x must equal the claimed value.
- `singular` = 1: null vector v != 0 with A v = 0; = 0: `inverse`.
- `inconsistent` = 1: y with y^T A = 0 and y^T b != 0 (Fredholm alternative); = 0: `x` with A x = b.
- The certificate also binds `problem_sha256` and the MNA `unknowns` in registered order (catches swapped or renamed
  nodes).
- **K8** (`bounds`): SI dimension of every value vs its element equation and of the query unit; boolean range;
  value size; and, when a solution x is present, an ELEMENT-LEVEL re-check independent of the MNA stamps: KCL at
  every node including ground (no MNA row states it), every source's constitutive law, Tellegen (sum v_e i_e = 0).
- **K7: does not apply** (as for `lp`): the human-owned `CHECK_APPLICABILITY` table marks K7 n/a for the
  computational class ("correctness is proven exactly; triviality/importance are not judged"); K1 refuses a risk
  prior. K4-K6 are n/a for the same class.
- Tolerance: **exact (zero)**. Every comparison is in `Fraction`; no float is created in `check.py`.
- Metric: `circuit_value`, direction `HIGHER_IS_BETTER = True` by convention (an exact value has no better direction;
  K1 requires every claim to state the same one, as `lp` does); range: {0, 1} for the boolean quantities, any
  rational of <= 100 digits otherwise.

## Validation evidence (invariant 13)
`CIRCUIT_CHECKS` (39; 37 before the red-team encoding fix): hand-checkable golden cases (divider V(n2) = 6 V, i(V1) = -2 A, P(V1) = -20 W, P(R2) = 12 W;
controlled sources 6 / 10 / 6 / -2 V; regular divider; parallel equal sources singular but consistent; clashing
sources inconsistent; current source into an open node inconsistent) and planted faults that must be rejected FOR
THE RIGHT REASON (off-by-one value; off-by-one rational in x; corrupted inverse; swapped x entries; swapped unknown
names; certificate of the netlist with V1's nodes swapped; certificate of another problem; value claim on a
singular network with a forged inverse; missing inverse; zero null vector; non-null vector; regular network claimed
singular; inconsistency witness with y^T b = 0; boolean out of range; duplicate key; JSON number; decimal; NaN;
unknown key; prefixed unit kohm; resistor in volts; voltage query in amperes; R = 0; physical query; digit bound;
KCL-at-ground and Tellegen on a wrong solution; a float-equal but exactly wrong certificate). A test plants a checker
that accepts everything and requires every fault check to fail (tests of the tests).

## Engineering task suite (`research/eng_suite.py`)
Families (domain `engineering`, each row one circuit with random integer component values; clean target = exact
MNA value of the PRINTED features; ~1% Gaussian measurement noise from the task's own `labels` stream):
`ladder` (3-section ladder output voltage, RMSE), `bridge` (Wheatstone detector current in mA, MAE), `amplifier`
(finite-gain inverting amplifier output, RMSE). Default held-out family (LOFO): `bridge`. Keyed independent streams
`features` / `labels` / `split` per task under `task_key(master, task_id)`; master = OS entropy unless a caller passes
one (only its SHA-256 commitment is written). `check_split_isolation` passes (fail closed otherwise). Sources are
recorded in suite.yaml (`sources`). Tests check the labels against independent closed forms (Thevenin, ladder
reduction, amplifier KCL), not the MNA stamps.
`build_cross_domain_suite` puts ML synthetic and engineering tasks in one suite with a whole held-out DOMAIN, so the
**LODO gate is live**: `tests/test_engineering_suite.py` runs `evolve_generation` on table evaluators with each
domain held out in turn (reject on held-out-domain regression although the pooled gate passes; accept otherwise;
per-domain result and `ood_drop` in the ledger).

## Kernel gap found (not fixed here: needs a Kernel change by s3-kernel)
`k1_computational` (kernel/checks.py) checks neither `claim.statement` nor `scope.conditions`. So a computational
claim whose statement says "this circuit will work when built" (or scope `{"holds_in": "physical world"}`) gets
CERTIFIED-EXACT with that prose in the certificate, for `circuit` AND for `lp`. The `simulated` class already
closes this (statement must equal the simulator's `scope_phrase`, exact scope conditions). Proposed tightening
(text only; NOT applied or run by this agent, label: inferred):

```python
    # in k1_computational, after the metric-direction check
    if c.scope.conditions:  # a certificate holds for the registered instance only: no scope can widen it
        reasons.append(R(Code.K1_SCOPE,
            f"a computational claim has no scope conditions (got {sorted(c.scope.conditions)})"))
    phrase_of = getattr(importlib.import_module(str(spec.exact_module)), "scope_phrase", None) \
        if spec is not None and spec.exact_module is not None else None
    if phrase_of is not None and exact_value(c.value) is not None:
        try:
            phrase = phrase_of(ctx.store.blobs.get(c.subject.hash), exact_value(c.value))
            if c.statement != phrase:  # no free prose: a physical claim cannot ride on an exact certificate
                reasons.append(R(Code.K1_STATEMENT,
                    f"the statement must be exactly the adapter's scope rendering {phrase!r}"))
        except (OSError, ValueError) as err:
            reasons.append(R(Code.K1_UNREADABLE, f"problem instance unreadable: {exception_name(err)}"))
```
`lp` has no `scope_phrase`, so its statements would stay unchecked until it gets one (owner decision).

## Deviations and limits (stated)
- Kernel-path tests approve the adapter in a throwaway test ledger with approver "test-world", the repo's existing
  test convention (lp, sim, lean tests). No real approval exists.
- Invariant 5 is NOT MET for the adapter red team (builder and red team are both Claude models).
- Domain-scout proposals (`docs/domain_proposals/`) are unverified (written from memory, no sources checked).
- UNCALIBRATED: size bounds, the label-noise level (1%), the suite sizes.
