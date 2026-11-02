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
| The green token reaches two things only: the certified badge and the line under an earned verdict | static test in `honesty.test.tsx` |
| Colours live in `tokens.css`; serif only on the verdict; no light theme. Gradients only as the two fall-offs at the pointer; glows only on a closed list | static tests |
| Text passes 4.5:1 contrast on all four backgrounds, and the canvas is true black | computed from `tokens.css` in `honesty.test.tsx` |
| The graph, heartbeat, palette, previews and the Promote moment cannot use the green | static test in `honesty.test.tsx`; computed colours in the end-to-end test |
| The Promote moment plays only after the API answered, says nothing was decided, and can be skipped | `honesty.test.tsx`, `screens.test.tsx`, end-to-end test |
| The palette and previews perform nothing, and no list or preview opens a verdict | static test; `screens.test.tsx`; end-to-end test |
| Nothing moves in the Court; a number can still be traced by hovering | `document.getAnimations()` is empty in the end-to-end test |
| Reduced motion: the moment is a plain fade, the graph does not pulse | end-to-end test |
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

## Colours and type

True black, with depth from hairlines. Green `#3FE0B2` only for VALIDATED and RE-VALIDATED from the Court on a real store.
Amber `#F2A93B` only for Lab activity. Slate `#8FB0D6`, outlined, for supporting facts. Rejected is grey `#919BA5`, the dead
ends ash `#80807B`. Red `#E8706C` only for a broken machine or a crashed run. Every text colour reads at 4.5:1 or better on
all four backgrounds; the test computes it.

Fonts are self-hosted from pinned packages, Latin only: Inter Tight for the interface, Instrument Serif for the verdict
sentence and nothing else, Geist Mono for numbers.

## How it reacts

- **Connected hover.** Anything with `data-t` lights what shares a key across the page: an idea, its runs, its Story lines
  and its marks on the time slider; a number and the lines it counts; a badge and its meaning.
- **The graph** (`IdeaGraph.tsx`) is SVG moved by a small spring engine that rests when nothing moves. Drag a node, drag the
  background to pan, scroll to zoom, press to open an idea in place. Tab reaches each idea; arrows walk the tree; `+`, `-`, `0`.
- **Replay.** The tree is rebuilt in the browser from the Story's lines up to any step (`timeline.ts`), so the time slider
  calls no API and works on a real session too. In the Lab header, counts in a replay come from the same lines.
- **Live.** The server's event only says "the Story grew". The page then reads the API, and new parts animate in.
- **Moving between screens** uses the browser's view transitions: the session name and the graph travel from Today to the
  Lab, a case title travels to the case. Without that browser feature the page simply changes.
- **Ctrl+K** finds and explains as you type, and lights an idea on the page behind. It only goes to pages.
- `chh exp ui --scripted` plays a looping session, so all of this can be seen with no agent.

## What the graph does not know

The API gives an idea's title and state, not its parent. So the graph places a `propose` as a new line of work, and hangs a
`branch` from the idea last worked on; a run belongs to that idea too. On a real session this is the order of the work,
and could differ from the parent recorded in the Story, so the graph's caption says so. Adding `parent_id` to `Idea` in the
Experience API would make it exact. A graph node shows the last result in the run's own words, because the graph cannot
know which direction is better. A run with no result in its line reads as running until a later line finishes it.

## Screens

Today, Lab (idea graph, Story, runs, time slider, literature drawer), Story, Rehearse, Promote, Case (verdict and Story), Record,
Waiting, Trust board, Paper. Three moods: `lab` is alive (Today, Lab, Rehearse, Promote), `hall` is a quiet readout (Story, Record,
Waiting, Trust board, Paper), and `court` is still: no animation, no pointer light, no heartbeat.

## Checks

```powershell
cd ui
npm ci --ignore-scripts
$env:CHH_PY = 'path to python with custom_harness'
npm run check     # types, unit and honesty tests, build, end-to-end in the installed Edge
```

The end-to-end run drives the real server with the scripted stand-in, in Edge, with no browser download. `tests/test_experience_server.py`
also runs a real case on a throwaway store through HTTP.
