# The evidence embargo: a campaign that can finish (`--evidence embargo`)

Status: built on branch `evolve-embargo`, 2026-11-03. **Nothing here has been run**: not its tests, not the gate, not a
campaign. The owner asked for the code to be written without running them. Treat every statement below as reasoned from
the code until `tests/test_evolve_embargo.py` and the full gate have passed.

## The problem

A legacy campaign halts. Every evolve-split run registers claims on its target and releases verdict bits to the proposer.
K4 multiplies each claim's term by every release logged before it, so the family grows exponentially and
`multiplicity.check_run` stops the campaign (`docs/r2-2_transcript_bound.md`). That accounting is correct and is not
loosened. `docs/alpha_accounts.md` already names the way out: release fewer bits, an owner decision. This is that decision,
made opt-in.

## The rule

| Step | Evidence | Reaches the proposer | Spends held-out truth |
|---|---|---|---|
| Evolve-split runs: the harness cut before its first claim (`dev_view`) | dev score on public data | yes | no |
| Race, then fresh re-measure, one difference per task | same | yes | no |
| Cost cap on the fresh units (`embargo_max_cost_increase`, default 0.1) | tokens | yes (`cost`) | no |
| Gate runs: replication off, `n_comparisons` raised to the bound (`gate_view`) | Kernel verdicts | no | yes |
| Sign test over gate tasks at `gate_alpha / generations` (`flips_gate`) | certificates won vs lost | one bit | yes |
| Each held-out family: no net loss of certificates (`lofo_flips`) | same | inside the bit | yes |

- **A dev gain alone cannot be accepted.** Acceptance needs gate tasks where the candidate earned a certificate the parent
  did not.
- **More compute alone cannot be accepted.** The cost cap stands in for the same-budget baseline of GOAL 18.6.
- **After the gate, one bit.** A rejection reads `held_out_gate` whatever rejected it. The real stage is in the host-only
  field `embargoed_stage`.
- **The bound is known up front.** Only gate runs claim, and each generation releases one bit, so
  `N = base + sum_g arms(g) x tasks x claim nodes x seeds x (1 + retries) x reach x 2^g` (`gate_family_bound`). `check_run`
  still compares every run with the Kernel's own number: an unforeseen release halts the campaign, it never loosens.
- **VALIDATED.** The loop label is VALIDATED only when the edit was accepted and the certificates earned on the won tasks
  are all CERTIFIED-VALIDATED. The legacy rule required every run in both arms to be certified, so a real gain from
  uncertified to certified could never earn it. On a suite of internal held-out data the label stays MECHANICAL, correctly.
- **Fail closed.** A private read attributed to a dev-only run stops the campaign (`EmbargoBroken`). A gate too small for
  the sign test to pass, an alpha-account on a gate target, or an exposed target refuses the campaign before any run.

## How to run it

```powershell
chh evolve examples/research_v2.yaml --suite SUITE.yaml --proposer KIND --role executor=KIND --generations 2 --evidence embargo --campaign-id NAME
```

The Kernel is unchanged: no file under `kernel/` was edited, so the governance fingerprint should not move.

## Scale, by hand

At the defaults, `gate_alpha` 0.1 over 2 generations gives level 0.05, so **at least 5 gate tasks must flip with none lost**.
With 6 gate tasks, 1 seed and 1 infra retry, per-arm claims are 12, so N = 2x12 + 12x2 = 48 and K4 tests each claim at
0.05 / 48 = 0.001 one-sided. That is strict but reachable with a large effect and enough held-out rows. The legacy
campaign's own note computed about 2^128.

## What this does not do

- **It has not been run.** The first run may find mistakes that reading did not. The end-to-end test in
  `tests/test_evolve_embargo.py` is the one most likely to.
- **Low power, by design.** A parent already certified on most gate tasks leaves nothing to win, so such a suite accepts
  nothing. The default suite has few gate tasks, so check its size against `min_gate_tasks` first.
- **Not a bound over adaptive proposers.** The level is split over generations, not over the transcripts of the accept bits.
  A proposer adapting to them is controlled by K4's accounting of each claim, not by this sign test.
- **"No regression observed" is not "no regression".** With two or three tasks in a held-out family the rule cannot show
  there is none. The legacy rule could not either: it mostly rejected everything.
- **Cost cap per generation.** It compares a candidate with its parent, so cost can compound by 1.1 per accepted generation.
- **Evolution optimises dev, the gate checks truth.** The proposer never sees why a candidate failed the gate, so it can
  learn slowly. That is the price of not teaching it to cheat (GOAL 18.1).
- **Not covered:** `--islands`, branching, temporal reveals and ablation arms are refused under this mode. The time-split
  experiment (GOAL 6.4) is still not wired to any of this, so **a VALIDATED self-improvement result remains unproven**.
- **The values are UNCALIBRATED** (`gate_alpha`, `embargo_max_cost_increase`, owner decision, GOAL 13.8).
