# First-discovery plan (GOAL 18.4 item 4, 18.8; UPGRADES N12)

Two tracks, in order. The first proves the route. The second can carry a headline.

UPGRADES N12 lists three tracks (minted, sealed, future). GOAL 18.4 item 4 is the owner's decision and has two: a minted
toy first, then the headline on sealed real data or future data. This plan follows GOAL, so sealed and future are the two
options of the second track. No future-data project template exists yet (P1), so sealed data is the only option that
can start now.

## Track 1: minted toy, to prove the route

- What: a Lab agent finds a method on a synthetic generator family; the Court mints fresh data after the freeze.
- Name it "first certified result (simulator-scoped)". It is never called a discovery.
- Route: `chh court health`, `chh lab start`, `chh lab promote`, `chh court rehearse`, `chh court register`,
  `chh court mint`, `chh approve-role`, `chh court run --docker`, `chh court status`.
- What it proves: that the real route runs from a Lab session to a certificate with a real agent in a real box. Today
  that has been run only with scripted programs on a local sandbox (see Status).
- Done when: one claim is VALIDATED, a reviewer reruns its bundle, and the two scorecard numbers are recorded.

## Track 2: headline, on sealed real data or future data

- What: a question on a dataset the Lab cannot download: the owner's or a partner's data, sealed before the first session.
  Future data is the other option, once its project template exists.
- Route: `chh lab seal` first, then the same route with `chh lab start --project` and `chh court register-sealed`.
  Two sealed parts allow RE-VALIDATED.
- Announce "first certified discovery" only from this track.
- Size each sealed part with the power check before sealing: `chh court readiness` on Lab outcome counts. It is
  UNCALIBRATED and can only warn.

## Owner setup needed

| Step | For | Why it is yours |
|---|---|---|
| Build the Lab image from `docker/lab.Dockerfile`, with reviewed CLI versions and a pinned base | both tracks | versions are a review decision |
| Scoped, spend-capped API keys for the agents | both | money and credentials |
| Adapter approval: `chh approve-adapter ml` (and lp, sim, circuit when used) | both | human-only |
| The providers.yaml sign-off | both | human-only |
| The human-held check (invariant 22) | both | no agent may see it |
| Review the feedback table `TRUTH_INDEPENDENT` in `court/display.py` | both | GOAL 18.5 and UPGRADES N14 ask for a human decision per check |
| Run `chh court health --docker` on the final Court version | both | it must run on the real candidate sandbox |
| A beacon source (for example a drand round) | both | the value is typed by a human and not checked offline |
| `chh approve-role` for each target | both | human-only |
| Choose the Track 2 dataset; it must be outside the lab root and never given to a Lab | Track 2 | only you know where the data has been |
| A separate OS user for private data | Track 2 | system setting |

## Status

Built and tested with scripted programs on a local sandbox: seal, program contract, rehearsal, health run, display.
Not done: any run with a real agent in a real Lab box, and any run of the health suite in the Docker candidate sandbox
(the Lab image does not exist yet).
