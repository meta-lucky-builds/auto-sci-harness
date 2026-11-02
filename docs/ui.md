# The UI (GOAL 18.7)

`chh exp ui` serves the screens on this machine only. `chh exp ui --scripted` shows them with a scripted example: no store,
no agent, no Court. The source is in `ui/` (TypeScript, React, Vite), the server is `experience/server.py` (Python standard
library).

## Rules

| Rule | Held by |
|---|---|
| The server binds to 127.0.0.1 and has no address setting | `make_server`; `tests/test_experience_server.py` |
| It calls `Experience` and nothing else; routes are a closed list | `ROUTES`; the experience ratchet covers `server.py` |
| No route takes a human approval. `approve` points to the command and writes nothing | same tests; `honesty.test.tsx` |
| Other websites cannot use it | Host and Origin checks on every request; a token in the page for changes |
| A verdict opens only on a press, because opening is a look | `honesty.test.tsx`; end-to-end test |
| No certified styling unless the API says so, on a real store | `CertifiedBadge.tsx`; `honesty.test.tsx` |
| The green token appears only in the certified badge | static test in `honesty.test.tsx` |
| Colours live in `tokens.css`; serif only on the verdict; no gradients or light theme | static tests |
| Text passes 4.5:1 contrast on all four backgrounds | computed from `tokens.css` in `honesty.test.tsx` |
| Types come from `docs/experience_api.schema.json` | `scripts/gen-types.mjs --check` |
| Installs stay pinned, install scripts off | `ui/.npmrc`, `tests/test_ui_rules.py` |

## Differences from GOAL 18.7

- **HTTP, not HTTPS, on the loopback address.** The Python standard library cannot make a certificate, and one that browsers
  trust needs a step on each machine.
- **Server-sent events, not WebSocket.** The standard library has no WebSocket. The live feed only says "the Story grew";
  the page then reads the API.
- **No agent can be started from the UI.** `seal`, `start`, `freeze`, `mint` and `run_case` stay on the command line. The
  screens show their commands.
- **Not validated with users.** GOAL 18.7 asks for usability tests with 3 to 5 researchers after a live session.
- **Only a browser.** No desktop wrapper: GOAL lists it as optional.

## Antivirus rewrites the page

Checked on the owner's machine on 2026-11-02. Kaspersky intercepts plain HTTP, including on 127.0.0.1. Compared with what
the server sends, the page the browser receives has a Kaspersky script added, and the page's security policy is rewritten
to allow it. So a third-party script runs inside the UI and can read the page, including the token and any verdict on
screen. The server cannot prevent this. To stop it, exclude `127.0.0.1` or this application from Kaspersky's web traffic
scanning. That is the owner's choice: the UI works either way. This does not change what the Lab can reach. HTTPS might
not stop it either, as Kaspersky can also intercept encrypted traffic (not tested).

## Colours

As agreed with the owner, with one change. Rejected grey `#7C8894` reads 4.12:1 on the Court panel, under the 4.5:1
needed for text, so it became `#838F9B` (4.51:1), the nearest neighbouring grey that passes.

## Screens

Today, Lab (ideas, Story, runs, literature drawer), Rehearse, Promote, Case (verdict and Story), Record, Waiting, Trust board,
Paper. The Lab has the Lab mood; Today and Rehearse share it. The others have the stiller Court mood.

## Checks

```powershell
cd ui
npm ci --ignore-scripts
$env:CHH_PY = 'path to python with custom_harness'
npm run check     # types, unit and honesty tests, build, end-to-end in the installed Edge
```

The end-to-end run drives the real server with the scripted stand-in, in Edge, with no browser download. `tests/test_experience_server.py`
also runs a real case on a throwaway store through HTTP.
