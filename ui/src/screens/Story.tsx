// The Story: one vertical thread through a session. Each step is a point of light that opens in place.
// Separate from Verdict.tsx on purpose: this file reads Lab lines on arrival, and the verdict file may not read anything.
import { useState } from "react";
import { api } from "../api/client";
import type { LabLine } from "../api/types.gen";
import { Loaded, useLoad } from "../design/kit";
import { buildTree, lineKeys } from "../design/timeline";

const SAY: Record<string, string> = { human: "You", run: "A run", idea: "An idea", web: "The web", action: "The agent", session: "The session" };

function Step({ l, keys }: { l: LabLine; keys: string }) {
  const [open, setOpen] = useState(false);
  const [head = l.text, rest] = l.text.split(/\s{2,}/);
  const op = l.kind === "idea" ? /^([a-z_]+): (.*)$/.exec(head) : null;
  return (
    <li className={`step step-${l.kind} ${open ? "open" : ""}`} data-line data-t={keys}>
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="point" aria-hidden="true" />
        <span className="step-at">{l.at}</span>
        <span className="step-text">{op && <span className="op">{op[1]}</span>}{op ? op[2] : head}</span>
        {rest && <span className="step-result">{rest}</span>}
      </button>
      {open && <p className="step-more">{SAY[l.kind] ?? l.who}, recorded at {l.at} by {l.who}. Kind of record: {l.kind}.</p>}
    </li>);
}

export function Thread({ sid }: { sid: string }) {
  const log = useLoad(() => api.labLog(sid), [sid], `log:${sid}`);
  return (
    <Loaded state={log}>{(g) => {
      const tree = buildTree(g.lines, g.lines.length);
      const keys = lineKeys(g.lines, tree);
      return <ol className="thread" aria-label="Story">{g.lines.map((l, i) => <Step key={i} l={l} keys={keys[i] as string} />)}</ol>;
    }}</Loaded>);
}

export function StoryScreen({ sessionId }: { sessionId?: string }) {
  const sessions = useLoad(api.sessions, [], "sessions");
  const sid = sessionId ?? sessions.data?.sessions[0]?.session_id;
  return (<>
    <h1 className="eyebrow">Story</h1>
    {!sid ? <Loaded state={sessions}>{() => <section className="void"><p className="display">No story yet.</p>
      <p className="lede">Every idea, run, steer and web connection of a session is written here, in order. Nothing can be taken out.</p></section>}</Loaded>
      : <section className="story-page"><p className="story-head">{sid}</p><Thread sid={sid} /></section>}
  </>);
}
