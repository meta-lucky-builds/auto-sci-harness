# Campaign wall-clock budget (GOAL §5 L1)

A `Campaign` (`src/custom_harness/budget.py`) caps a whole campaign: many runs, their forks and resumes, plus
charges outside any run. It has two time units. They measure different things, and a campaign can cap either or both.

| unit | meaning | where it comes from |
|---|---|---|
| `wall_s` | **summed step time** | the sum of every member run's trace-step `wall_s` (each step's measured duration), plus journalled backend-reported `wall_s` for a step with no trace row, plus `charge_external` costs |
| `elapsed_s` | **calendar time since the campaign opened** | `clock() - opened_at`, where `opened_at` is written once into the `campaign_open` record |

`elapsed_s` is a campaign-only unit. It is not in the per-run `UNITS` because it is not a `Cost` field: the per-run
`Budget` is unchanged.

## wall_s: summed step time

Derived from the store every time it is read, so a crash cannot lose it and a retry cannot add it twice. What it
does and does not count (tested in `tests/test_campaign_wallclock.py`):
- Across runs and processes: the cap is charged across all member runs. A new `Campaign` object on the same store
  sees the same total, and the next run is refused before its first step once the cap is reached.
- Fork: the fork's trace holds only its own steps. The parent's prefix is counted once, in the parent.
- Hard kill, then resume: no double charge. The killed step gets an `interrupted` trace row carrying the journalled
  model-call cost, which replaces the journal entry, and the re-executed step gets its own row with its own measured
  time.
- Not counted: concurrent runs add up (two runs of 10 s each running in parallel are 20 s); time between steps,
  between runs and outside the engine; the time a killed step spent in flight (the journal records the
  backend-reported `wall_s`, which is 0 for the mock backend and for any backend that reports none); time lost to
  infrastructure failures (deliberately uncharged, GOAL L1).

## elapsed_s: calendar time

- **One clock per campaign.** `opened_at` (wall-clock `time.time()`, injectable as `clock=` for tests) is written
  into the `campaign_open` record when the campaign is first opened. Every later `Campaign(store, id)` object, whether
  in the same process, a new process after a crash, a resume or a fork, reads that same open time. Nothing about the
  clock is kept as a counter, so it cannot be reset by a crash or charged twice by a fork.
- **Checked before every step.** `Campaign.check()` includes it, and the engine already calls `check()` before every
  step (no engine change). `remaining("elapsed_s")` gives `cap - elapsed` (negative once over).
- **Fails closed:**
  - A `campaign_open` record without a usable `opened_at` (missing, written before this unit existed, or malformed:
    not a finite number) cannot carry an `elapsed_s` cap. `Campaign(...)` raises `CampaignError`. It is never read as
    0 elapsed. Such an old campaign keeps working for its other units, and `elapsed()` on it raises `CampaignError`.
  - A clock earlier than `opened_at`, a non-finite clock, or a clock earlier than a time the same `Campaign` object
    already read (a backwards jump) raises `CampaignClockError`. It subclasses `BudgetExceeded`, so the engine stops
    the run before the step with status `BUDGET_EXCEEDED` and the clock message as the detail. It never continues on
    a negative or reset elapsed time.
  - Opening with a clock that returns no finite time is refused, and nothing is recorded.
- **Caps are fixed (kept).** Re-opening an existing campaign with other caps is refused, including adding an
  `elapsed_s` cap to a campaign that was opened without one. A different time budget needs a new campaign id.
  `opened_at` is not part of the caps: re-opening with the same caps keeps the original open time.
- **Open race.** If two processes both find no `campaign_open` record and both append one, the first record in the
  store is the campaign for every reader. After appending, `Campaign.__init__` re-reads it and holds itself to that
  record's caps and open time. If its own caps differ, it is refused. Before this change, each racer used its own
  caps for its whole life.

## Honest limits

- It is wall-clock time, not a monotonic clock. A process restart cannot use `time.monotonic()` across processes, so
  the persisted reference has to be a wall-clock timestamp. Consequences:
  - Time while the machine sleeps or hibernates, or while no process is running the campaign, counts as elapsed.
    This is intended for a calendar budget, but it means a paused campaign still burns its `elapsed_s` budget.
  - A forward clock jump (an NTP step, a manual change) counts as elapsed and can end a campaign early. That is the
    safe direction.
  - A backward jump is detected only past the open time, or within one `Campaign` object. A new process whose clock
    went backwards but is still after `opened_at` sees a smaller elapsed time than an earlier process saw, so
    `elapsed_s` can undercount by the size of that jump. No high-water mark is persisted, because that would add a
    store write to every step's check.
  - Machines with different clocks sharing one store (a remote worker) see different elapsed times. The open time is
    the opener's clock.
- As with every campaign cap, `elapsed_s` is checked between steps, not during one. A long step can overshoot the cap
  by its own duration. Nothing interrupts a step in flight when the cap passes.
- `wall_s` is unchanged by this work and keeps the gaps listed above. Use `elapsed_s` for "this campaign may take at
  most N hours of real time", and `wall_s` for "at most N seconds of compute time across its steps".
