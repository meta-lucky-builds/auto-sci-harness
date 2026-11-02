# The Experience API (GOAL 18.5)

One Python doorway for the UI and integrations: `custom_harness.experience.Experience`, with typed requests and views in
`experience/schemas.py`, all sentences in `experience/words.py`, and `chh exp ...` on the command line. Every view is a
pydantic model; `docs/experience_api.schema.json` is their JSON Schema (`chh exp schema`), and a test keeps the file
current, so the UI can be typed end to end.

## Verbs

| Verb | What it does | Calls |
|---|---|---|
| `whoami`, `set_researcher` | a name for the record, on this machine; it grants nothing | `human.guard` |
| `waiting` | everything pending: the human queue and this researcher's cases, each with its command | `studio.queue`, `court/display.progress` |
| `approve` | points to a waiting step, writes nothing, takes no decision | |
| `seal` | seal part of a dataset before the project's first session | `court/sealed.seal_project` |
| `start`, `stop` | a Lab session | `lab/session` |
| `explore` | one prompt to the agent | `lab/session.run_prompt` |
| `steer` | a redirect or question; always in the Story, with how many Court results this person had seen | `lab/trace`, `run_prompt` |
| `lab` | ideas, runs, costs and web connections of a session | `lab/trace` |
| `freeze` | freeze code and environment; gives the promotion id | `lab/promote.freeze` |
| `rehearse` | a mock trial on Lab data; default data: the Lab's part of the sealed project | `court/rehearsal` |
| `promote_preview` | readiness, looks and steps; registers nothing | `court/rehearsal.readiness`, `display.looks_on_target` |
| `promote` | fixes the claim for the Court, on route `generated`, `sealed` or `public` | `court/minted`, `lab/promote.handoff` |
| `mint`, `run_case` | the public random value; the trial | `court/minted` |
| `case`, `cases`, `technical` | verdict, badges, story, next step; one line per case; the Court's own words | `court/display` |
| `share` | writes a reviewer's case file | `court/display.view` |
| `paper` | a living paper, every number from the certificate | `display.autopsy`, `outputs/court_paper` |
| `record` | dead ends, opened outcomes, calibration | `lab/trace`, `display.seen`, `display.track_record` |
| `resolve` | what any number rests on | |
| `chh exp doctor` | command line only: every prerequisite of a first session, what is missing and the next step. Read-only. It never prints a key | `experience/doctor.py` |
| `chh exp toy` | command line only: the minted toy track. It runs the route up to the next step that needs a person, and carries on when run again | `experience/toy.py` |

Route names are the researcher's: `generated` is the Court's minted route, `public` the retrospective Claim Audit route.
`case` also opens a Claim Audit by its audit id: that is the "reproduce this paper" output. Its report stays a full
report, recorded as a continuous release, because it is on public data.

Not built, as GOAL 18.5 defers them: overnight mode, public challenge, integrations, visible self-improvement.
`record.harness_improvement` says "Nothing measured yet".

## Rules built in

**Reads only through kerneld.** `Experience` holds a `HostStore`, whatever store it is given. The package is on the
kerneld boundary ratchet.

**One display path.** Everything the API shows of a Court outcome comes from `court/display.py`. `view` records a release
before it returns; every `case`, `technical` and `share` call goes through it, and a reviewer is a new viewer.
`cases`, `waiting`, `record` and `resolve` open nothing: they read `progress`, `public_case` and `seen`, so listing
your work spends nothing and shows no verdict you have not opened. `tests/test_experience_ratchet.py` holds:

- a closed list, with a reason each, of every file that may call `.certificate(`;
- the API package may not touch the ledger, the vault, the levels module, a kerneld client or any writer;
- the result of a trial is dropped and never read.

The other command-line surfaces were second paths and are now gated:

| Surface | Before an outcome was shown | After it was shown | Target retired |
|---|---|---|---|
| `chh explain`, `chh bundle` | refused | refused | allowed, recorded as an autopsy view |
| `chh graph` | no label, no prediction outcome | label only | everything |
| `chh predictions`, scorecard facts | claim left out | counted | counted |

**Two more releases.** The badges of a case can change when its replica or second test finishes, and that outcome rests on
the other target, so showing it is a `count` release on that target. A COULD NOT RUN after a label was read is a verdict
bit on the target.

**The feedback split** is `court/display.py`, unchanged: see `docs/court_rehearsal.md`. A case's `checks_in_full` lists the
independent checks by plain name; their reasons are in `technical`. The bundle holds the labels, so the rerun command
appears only with the autopsy. A reviewer's file before retirement has the verdict, badges and Story but no rerun.

**Rehearsal stays on Lab data.** A rehearsal refuses the file a project sealed, and any file holding a row of a sealed
part, compared on the part's feature columns. Before this it refused only whole private files, so the raw file or a
subset gave scores on sealed labels.

**Never grants.** No verb writes an approval, a role or a certificate. The one retirement that happens without a person is
a spent generated target (below). `approve` has one parameter, the item
id. Steps that are the researcher's decision (`seal`, `start`, `steer`, `freeze`, `promote`, `mint`) refuse an agent's name
and a call from inside a run. `court retire` and `lab release-verdict` now apply the same check. This is accountability,
not authentication: on one machine the OS user is the trust root, and `human/guard.py` says so. A Lab box cannot reach
this doorway: host and LAN are blocked (docs/lab.md).

**Plain words.** `words.jargon_problems` is the jargon budget as a check. The full-session test runs it on every main view.
Identifiers, commands and the researcher's own words are skipped. `technical` is the one view outside it.

**Every number is a link.** A number on a main view is a `Number`: a value and a `chh://kind/id/field` link. A ratchet walks
the types, and the session test resolves every link and compares the value. An unknown cost is `None`, never zero.

**Verdict words.** CERTIFIED only if the Kernel's label says so; REJECTED reads NOT CERTIFIED with one closed reason code.
A Court case never reads INCONCLUSIVE yet: the difference from NOT CERTIFIED depends on the interval, which the feedback
split withholds, and N17 (P1) has not been built. A certified claim that is not VALIDATED says it is not a finding.

**One-look targets.** `case`, `technical` and `share` go through `display.open_case`: `view`, and then a generated (minted)
target with no look left retires by itself. So the first look shows the verdict, the closed reason and the checks in full,
and the next look shows everything, with the rerun command. UPGRADES N14 allows this: a target retires when its slots
are spent or the owner retires it, and GOAL 16.4 says the same for slots. It does not weaken anything on this route,
because a generated target can never be used again and holds no real data, and the view is recorded first, so an
outcome nobody opened stays out of every other surface. The owner decided this on 2026-11-02.

Three cases stay as they were, on purpose:
- A sealed target needs a person: `chh court retire TARGET --by NAME`. Its certificate and bundle carry labels of real
  rows, and a second sealed part of the same data may be unopened. Opening them on a look would be an irreversible step
  nobody chose, and it would give full feedback before a later claim on the same data.
- A target waits while a replica or second test of the case has no outcome.
- A COULD NOT RUN never retires a target.

## Transport

GOAL 18.7 lists HTTPS on localhost and WebSocket under the UI stack, and 18.5 asks for a Python doorway, so the transport
is left to the UI stage. No web, WebSocket or TLS library is installed. When it is built it must bind to localhost only and
must call this class and nothing else.

## Limits

- Identity is a name on one machine. A person with the store can read everything; this path counts what the product shows.
- On sealed data a reviewer's `share` needs a human retirement before it can carry a rerun. On generated data it carries
  one once the verdict has been opened.
- The rows check catches exact rows on matching columns, and renamed, reordered or rounded copies of at least five rows
  (`court/rehearsal.py`). Noise added, columns added or rounding below three decimals still pass it.
- A Claim Audit's COULD NOT RUN shows its report with no release: it names no target to release on.
- `chh reproduce` of a bundle file already on disk is not gated; the file itself holds what it holds (docs/court_rehearsal.md).
- The measured no-data prior of readiness is P1. Readiness is power only, UNCALIBRATED, and can only warn.
