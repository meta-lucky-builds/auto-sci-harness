# First session checklist

Every step only a person can take before and during the first session, in order. For each one: the command, what success
looks like, and what to do if it fails. The plan behind it is docs/first_discovery_plan.md.

Run everything in a real PowerShell window that you opened yourself. The human-only commands refuse any other kind of
terminal, and they ask you to re-type part of a hash. In this file NAME means your own name, as you want it on the record.

Keep these three things in mind:
- A key is typed once, into your own terminal. Never paste one into a chat, a file in the repository, or a Lab session.
- `chh exp doctor` is safe to run at any time. It writes nothing and calls no model.
- `chh exp toy` is safe to run again. It reads where the track stands and carries on from there.

## Part 1: get ready (no model is called, no money is spent)

### 1. Open the right environment

```powershell
& C:\Users\mykids\custom-harness-data\venv\Scripts\Activate.ps1
cd C:\Users\mykids\OneDrive\Desktop\META\custom-harness
git pull --ff-only origin main
```

Success: git says "Fast-forward" or "Already up to date", and `chh --help` prints a list of commands.
On failure: if git says your files would be overwritten, run `git status` and sort that out first. The `.claude` folder it
lists as untracked is expected. If `chh` is not found, the first line did not run.

Then record one Kernel governance item that only you can record. The `court-v1` merge brought your 2026-10-23 rewording of
invariant 5 into the line the checks run on. GOAL section 10 is part of the Kernel core's fingerprint, so
`python scripts/check.py --fast` fails its governance step until a person records it. No Kernel code changed.

```powershell
python scripts/governance.py --record --kind delegated --approver NAME --delegated-by NAME --basis "GOAL 18.4 item 5" --change "invariant 5 reworded in GOAL section 10, reaching the code line at the court-v1 merge; no Kernel source changed" --note "owner rewording of 2026-10-23"
python scripts/governance.py
git add redteam/kernel_core_changes.yaml
git commit -m "governance: record the invariant 5 rewording"
git push origin HEAD
```

Success: the second command says `governance: OK`.
On failure: read what it names. To record it as a scheduled change with the 7-day cooling-off instead, see the top of
`scripts/governance.py`.

### 2. See what is missing

```powershell
chh exp doctor
```

Success: a list of rows marked `ok`, `MISSING` or `YOURS`, each `MISSING` with a next step. Today several rows are
`MISSING`: that is what the steps below are for. `YOURS` means code cannot check it, so you confirm it yourself.
On failure: if the first row says Docker does not answer, start Docker Desktop, wait until it says it is running, and run it
again.

### 3. Create a scoped key

At the Anthropic console, make a separate workspace for the Lab, set a spend limit on it (start with 5 US dollars), and
create a key in that workspace. Do not use your everyday key: the Lab refuses the usual variable names on purpose. Then, in
this terminal only:

```powershell
$env:CHH_LAB_CRED_ANTHROPIC = '<paste the key>'
```

Success: `chh exp doctor` shows `Scoped key` as ok, with the variable's name and not its value.
On failure: if the value was pasted with a stray line break, set it again. The variable lasts only as long as this window: if
you open a new window, set it again there.

### 4. Review the Lab image

The image `custom-harness-lab:py312` is already built, from `docker/lab.Dockerfile`. Read the pinned lines at the top of that
file: the base image and the three agent tool versions.

```powershell
docker image inspect custom-harness-lab:py312 --format '{{.Id}}'
```

Success: it prints `sha256:b0a4b697318a18abb2c52c03e94e1a51c2df2c145b452d4b5ca514023d99963d`. No agent tool in the image needs a login: the Lab passes your scoped key in when
a session starts. Whether each tool really accepts that key has not been tried, because that needs a model call: the first
real test is step 11.
On failure: rebuild with `docker build -f docker/lab.Dockerfile -t custom-harness-lab:py312 docker`. If you changed a
version, the new id goes in this file.

### 5. Approve the machine-learning adapter

```powershell
chh approve-adapter ml --approver NAME
```

Success: it prints the approval, and the doctor shows `Adapter approval (ml)` as ok.
On failure: read the refusal. It names what failed. A name that looks like an agent or a model is refused. lp, sim and circuit
wait until you use them.

### 6. Sign off the provider entry

```powershell
chh sign-off-provider anthropic-api --show
chh exp doctor
```

Read what the first command shows: the terms the entry records. The doctor's row `Provider sign-off (anthropic-api)` gives the
exact command with its hash. It looks like this:

```powershell
chh sign-off-provider anthropic-api --by NAME --entry-hash HASH
```

Success: the doctor shows that row as ok.
On failure: if the terms shown are out of date, correct the entry in `providers.yaml` first. Any change to it needs a new
sign-off.

### 7. Write and register the human-held check

Only you can do this, and no agent may see it (docs/human_check.md). Write a small script that tests something about the
system that you choose. Exit code 0 means it passes. Put it at
`C:\Users\mykids\custom-harness-data\human\checks\my-check.py`. Then:

```powershell
$h = (Get-FileHash C:\Users\mykids\custom-harness-data\human\checks\my-check.py -Algorithm SHA256).Hash.ToLower()
chh register-human-check my-check --by NAME --sha256 $h
python scripts/human_check.py --run
python scripts/human_check.py --status
git add redteam/human_checks.lock
git commit -m "human-held check: register"
```

Success: the status says LAST-RUN-PASSED, and the doctor shows `Human-held check` as ok.
On failure: a hash mismatch means the file changed after you hashed it: hash it again and use `--replace`. A failed run
means your check failed: look at why, without showing the check to an agent.

### 8. Review the feedback table

Open `src/custom_harness/court/display.py` and read `TRUTH_INDEPENDENT`. Those checks are shown in full to a researcher before
a target retires. The table's comment says why each one cannot change with the held-out labels. It is a proposal until you
accept it.

Success: you agree. Nothing to run.
On failure: if you disagree with an entry, do not start. Ask for a change: it is a Court change, and step 9 then has to run
after it.

### 9. Run the Court health check

```powershell
chh court health --docker
```

It takes a few minutes, uses the network-free Docker sandbox, and calls no model. It must run on the version you will use, so
run it after the last code change.

Success: it ends with `"passed": true`, and the doctor shows `Court health run` as ok.
On failure: the output names the planted claim that went wrong. Do not go on. A failed health run means the Court gave a
wrong answer on a claim whose right answer is known.

### 10. Check that nothing is left

```powershell
chh exp doctor
```

Success: the last line says `Nothing stands before a first session.` The rows for the sealed-data track may still be
`MISSING`: the toy track does not need them.

## Part 2: the toy track (this calls a model and spends your key, up to its limit)

The result, if it certifies, is called "first certified result (simulator-scoped)". It is about a generator. It is never
called a discovery.

### 11. Start it

```powershell
chh exp toy --credential-env CHH_LAB_CRED_ANTHROPIC --spend-cap-usd 5 --scope "Lab workspace, 5 USD limit"
```

It starts a Lab box, gives the agent its task, waits for it, freezes the code, and runs a mock trial on Lab data. This can
take a while. The output is JSON: `did` is what it did, `stopped_because` is why it stopped, `next_step` is what to do.

Success: it stops and asks you to fix a public random round.
On failure:
- `blocked_by` is filled: step 10 was not clean. Run `chh exp doctor`.
- "could not be frozen", or "would not certify": the agent's work is not good enough yet, and nothing was spent. Say what is
  wrong with `chh exp steer toy-1 --text "..."`, then run `chh exp toy --new-freeze`.
- A docker or box error: `chh lab status toy-1` shows the box. A session id can be used once, so pick a new one with
  `--session toy-2` on every `chh exp toy` from then on.

### 12. Fix a public random round

This value is what makes the fresh test data impossible to know in advance. It must be a round that has not happened yet.
drand publishes one round every 3 seconds. Take the current round and add 400, which is about 20 minutes:

```powershell
$url = 'https://api.drand.sh/52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971/public'
$round = (Invoke-RestMethod "$url/latest").round + 400
"quicknet-$round"
chh exp toy --beacon-round "quicknet-$round"
```

Success: it says it fixed the claim as a case, and asks for the round's value.
On failure: "unlikely to certify" means the readiness check objects. That setting is UNCALIBRATED and can only warn or refuse;
lower the claim or steer the agent. A refusal naming the health run means step 9 is not current: run it again.

### 13. Give the round's value, once it is published

Wait until the round has passed, then:

```powershell
$value = (Invoke-RestMethod "$url/$round").randomness
chh exp toy --beacon $value
```

Success: it says the fresh test data was made, and prints the role approval as the next step.
On failure: a 404 or "not found" from drand means the round has not happened yet: wait. The value is typed by you and is not
checked offline, so use the value of exactly the round you fixed.

### 14. Approve the fresh test data

Run the command it printed. It looks like this:

```powershell
chh approve-role TARGET --approver NAME --note "minted after the freeze" --dataset-hash HASH
```

This is your step and no command takes it for you.
Success: it prints the approval.
On failure: read the refusal. A wrong hash means you copied it badly: copy the whole printed command.

### 15. Run the trial

```powershell
chh exp toy
```

Success: `done` is true and `stopped_because` starts with "first certified result (simulator-scoped)".
On failure: a verdict of "Not certified" is a real outcome, not a fault: the sentence says why in one closed reason. "The trial could not run" says why; if no answer was read, the test data is not spent, and
`chh exp run CASE` tries again.

### 16. Read the full record and have it rerun

The full record opens only when you retire the test data. That is your decision: once you read the full reasons, you carry
them into any later work on this generator. Run the command `chh exp toy` printed:

```powershell
chh court retire TARGET --by NAME
chh exp case CASE
chh exp share CASE --reviewer "REVIEWER NAME" --out C:\Users\mykids\Desktop\case.json
```

Success: the case shows the rerun command. A reviewer, on their own machine or yours, runs that one command. It looks like
`chh reproduce PATH --docker`.
On failure: if the rerun differs, do not count the result, and report it.

### 17. Record the two numbers, and close the key

```powershell
chh exp record
```

The track is done when one claim is VALIDATED, a reviewer has rerun it, and these are written down: certified claims per
budget spent, and false certifications found. `chh lab costs toy-1` gives the money spent.

Then revoke the key at the provider, and close this terminal.

Success: the key shows as revoked at the provider.

## Part 3: the sealed-data track, when you are ready

This is the track that can carry a real headline. It needs Part 1 and two more of your own decisions.

### 18. Choose the dataset

It must be data no Lab has seen, kept outside the lab root and never given to a Lab. Only you know where it has been.

### 19. Set up a separate OS user

docs/lab.md section 7 step 3. Do this before sealing. The doctor's `Separate OS user` row shows whether this user can still
read the truth folder.

### 20. Size and seal it

Size each sealed part first: `chh court readiness` on Lab outcome counts (UNCALIBRATED, it can only warn). Then fix a random
round as in step 12, and seal before the project's first session:

```powershell
chh lab seal PROJECT --data FILE --label-column C --rows N --parts 2 --beacon-round ROUND --beacon VALUE
```

Success: it prints the Lab's row count and the sealed row counts.
From there: `chh lab start --project PROJECT ...`, then the same route, with `chh exp promote PROMOTION --route sealed
--project PROJECT`. Docs: docs/sealed_projects.md.
