# User guide

For the owner and a few trusted researchers. This is a private tool: one person's machine, one store. It is not hardened
for public use.

## What it does

Agents explore freely in the **Lab**. When an idea looks real, you **promote** it to the **Court**, which tests the frozen
code on data the Lab never saw. The Court gives a verdict. Lab results are never evidence, and nothing an agent writes can
produce a verdict.

Six words are enough: Project, Lab, Case, Verdict and Badges, Story, Record.

## Install (Windows)

Needs Python 3.12 or newer. The web UI also needs Node.js LTS, only to build it.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

It makes a Python environment in `%USERPROFILE%\custom-harness-data\venv`, installs `chh`, installs the UI packages
exactly as locked, with install scripts off, and builds the UI. `-DryRun` shows the steps and changes nothing. If Node is
missing, `chh` still works and the script says how to add the UI.

Docker Desktop is needed for real sessions and trials. It is not needed for the scripted example or the tutorial.

**Mac and Linux are untested.** The Python code has no known Windows-only requirement outside the install script, but
nobody has run it there. On those systems the steps would be `python -m venv`, `pip install -e .`, then `npm ci
--ignore-scripts && npm run build` in `ui/`.

## First run

```powershell
chh exp setup --name YOUR-NAME
chh exp ui --scripted
```

`setup` writes your settings and lists what is left before a real session. The second command opens the screens at
<http://127.0.0.1:8765/> with a scripted example: no store, no agent, no spending. Run `chh exp ui` without `--scripted`
for your real store. `chh exp doctor` says what is missing at any time. `docs/tutorial_ml.md` is a 10-minute start.

## Badges

A verdict and its badges are computed by code, never by a model.

| You see | It means |
|---|---|
| **VALIDATED** (green) | Confirmed on fresh test data that did not exist or was sealed while the Lab worked. This is a finding. |
| **RE-VALIDATED** (green) | Confirmed a second time on separate fresh data, chosen in advance, with nothing changed. |
| REPRODUCIBLE (blue outline) | Run again from the frozen code with the same result, on one machine. It does not mean true. |
| REPLICATED (blue outline) | A different agent rebuilt it from the words alone and agreed. Replicas can share mistakes. |
| Not certified (grey) | The claim failed. That is a real outcome, and it goes in your Record. |
| Could not run (red) | Something broke. If no answer was read, nothing was spent. |

Green appears only for those two badges, only on a real store. On a scripted page they are dashed and marked "example".

## Looks

Test data can only be looked at a limited number of times, because every look teaches something. Opening a verdict counts
and is recorded, so the UI opens one only when you press the button. Before a target is retired, a failed claim tells you
one plain reason and no numbers: this stops the Lab learning how to pass. The full details open once a person retires the
test data with `chh court retire`, and that data can then never be used again.

## Spending

| Limit | Where it is | What it does |
|---|---|---|
| The limit on the key | At the provider | The real cap. Always set one. |
| Per-session limit | `chh exp setup --spend-limit USD` (default $5, not calibrated) | Stops the next prompt |

The Lab screen and every reply show recorded spend against the limit. A warning starts at 80%. This limit is not a hard
cap: a prompt already running can go over it, and `chh lab attach` (a live terminal session) is not stopped by it. A call
whose cost is unknown stays unknown, never zero; more than 10 in one session stops it, because then nobody can say what was
spent.

The Lab's default model for `claude-code` is `claude-haiku-5-5`, so a forgotten `--model` flag cannot start an expensive
session. Change it with `chh exp setup --model NAME`, or name a model for one session.

## Backup and restore

```powershell
chh exp backup --out D:\chh-backups --by YOUR-NAME
chh exp verify-backup D:\chh-backups\chh-backup-20261102T101500
chh exp restore D:\chh-backups\chh-backup-20261102T101500 --to D:\restored-store --by YOUR-NAME
```

**A backup holds the sealed answers.** Without them a restored store could not run a trial. Keep it where no Lab and no
agent can read it. The command refuses a folder inside the Lab folder or inside the store, and only a person may run it. A
OneDrive folder would upload the sealed answers, so do not use one.

`verify-backup` checks every file's hash and the ledger's chain. `restore` verifies first and only writes to a new or empty
folder. If private data sits under a separate OS user, run the backup as that user.

## Upgrading

After updating the code, `chh exp doctor` shows a `Stored data` row. If it says to upgrade:

```powershell
chh exp upgrade --backup-to D:\chh-backups --by YOUR-NAME
```

It makes and verifies a backup first, then changes the store one step at a time. It never rewrites a ledger row. A store
newer than the code is refused and left alone. The first upgrade changes only the format number.

## When something goes wrong

| You see | Do this |
|---|---|
| `refused: ...` | The message says why. A refusal changed nothing. |
| `Something broke ...` | Run `chh exp doctor`. Set `CHH_DEBUG=1` and run it again to see the technical error. |
| The UI says the local server does not answer | `chh exp ui` stopped. Start it again. |
| The UI is not built | Run the install script. |
| Prompts stopped | The spending limit was reached. The message says how to raise it. |

## Limits worth knowing

- A name on this machine is a label, not a login. Whoever holds this machine holds its records.
- The UI uses plain HTTP on this machine's loopback address. See `docs/ui.md`, including what antivirus software does to it.
- No agent has run in a real Lab box yet, and no discovery has been certified.
