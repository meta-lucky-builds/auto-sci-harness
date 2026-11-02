# Display, rehearsal, readiness and the health run (GOAL 18.5, 18.6; UPGRADES N1, N9, N13, N14, N15)

## One display path (`court/display.py`)

`chh court run`, `status` and `level` all print `display.view`, which records a release before it returns. Each (case,
viewer) pair is one release. The researcher sees `looks_remaining`: an alpha-account's free slots, else one per claim until
a label is read.

| Part of the outcome | Shown |
|---|---|
| K0, K1, K5 (independent of held-out truth) | in full, with reasons |
| K2, K3, K4, K6, K7, K8, K9 | nothing but the verdict label and one closed reason class |
| The whole certificate | only after `chh court retire TARGET --by NAME` (human); recorded as a continuous release, so the target is exposed |
| COULD NOT RUN, no label read | the full technical reason |
| COULD NOT RUN after a label read | that it happened, not the detail; one verdict bit is released |
| Badges that changed because a replica or second test finished | shown, with a `count` release on that case's target |
| The rerun command | only with the whole certificate: the bundle holds the labels |

The table is a proposal: GOAL 18.5 and UPGRADES N14 ask for a human decision on each check.

Why K2 and K8 are on the withheld side. K8's ML reasons print a held-out score and the held-out label count. K2's
early-read test treats a dataset with a byte-identical private file as the same target (`checks.related_targets`), so
its result can turn on the held-out bytes, and its reason names that dataset. Each now shows one closed class
(`NOT_FIXED_FIRST`, `RULES_OF_THE_RUN_BROKEN`); the detail is in the autopsy. K0, K1 and K5 read no label and no score
(a source scan in `tests/test_court_display.py` keeps this true). The lp, circuit, sim and formal claims have no
datasets (K1 refuses them), so no held-out label reaches their checks.

One remainder, left for the owner: K1's p0 comes from the alpha-accounts of `related_targets`, so it rests on the same
byte comparison. It is fixed in the prereg entry at registration, before any look, so K1 says nothing new afterwards.
Hiding K1 would also hide why a claim is malformed.

Other surfaces: `chh lab release-verdict` uses the same emitter on the `prompt` channel. `chh audit report` stays a full
report on public data and counts as a continuous release. `chh certificates` points Court cases to `chh court status`.
A second case on a sealed part is refused unless a human opened an alpha-account for it before its first case.

`chh explain` and `chh bundle` refuse a Court case until its target is retired. `chh graph`, `chh predictions` and the
scorecard facts leave out what nobody was shown. docs/experience_api.md has the table, and
tests/test_experience_ratchet.py keeps a closed list of every reader of a certificate.

Not routed: `chh court level` on a certificate with no Court case (research-loop certificates, which have their own
embargo), a bundle file already on disk, and direct reads of the store by the owner. A person with the store can
read everything; this path counts what the product shows.

## Rehearsal (`chh court rehearse PROMOTION --data FILE --label-column C --rows N --docker`)

The frozen arms go through the real route (seal, register, K0-K9, re-execution) on Lab data, in a throwaway store deleted
afterwards. Every check and number is returned, labelled "Lab data, not evidence". The real ledger gains no entry. The
only record in the real store is `court_rehearsal`. A file equal to a private vault file is refused, and so is the file a
project sealed or any file holding a row of a sealed part (exact values on the part's feature columns). Rehearsal
numbers can be better than fresh data: the Lab may have tuned on this very data.

## Readiness (`chh court readiness PROMOTION --dev-pairs a,b,c,d`)

Power on public inputs only: the claim and the outcome counts from Lab data. It is labelled UNCALIBRATED and can only
deny or warn. It uses the existing preflight, which simulates accuracy only; other metrics answer "not run". The measured
prior is P1.

## Health run (`chh court health --docker`)

Eight planted claims go through the real route, each in a throwaway store, with scripted programs and no model calls:
honest toy, planted real effect, null claim, leaky, p-hacked, label-reading, flaky, strawman baseline. `register` and
`register-sealed` refuse a Court version with no passing run. The run's hash goes into the claim's assumptions, so it is
inside the certificate. `court_version` hashes `kernel/`, `kerneld/`, `court/`, `verify/`, `sandbox/` and the arm wrappers:
a change to any of them needs a new run.

The first full run found a real hole: the strawman baseline was CERTIFIED-VALIDATED, because the Kernel only checks that
the method beats the comparator it was given. `court/comparator.py` now runs the frozen comparator on a split of the
public training file before any held-out label is read, and refuses one clearly worse than the trivial answer (margin
UNCALIBRATED). This is deny-only and keeps the target. A comparator above trivial but below a fair standard is still a
human judgement. A plant that fails to run counts as a wrong answer, never as a caught plant.

Positive and null salts appear here as the planted real effect and the null claim. Model-backed salts stay in `salts/`,
budget-gated as before.
