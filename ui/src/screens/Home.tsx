import { useEffect, useRef } from "react";
import { api, watchLab } from "../api/client";
import type { Cases, TrustBoard } from "../api/types.gen";
import { PointerLine } from "../design/Heartbeat";
import { IdeaGraph } from "../design/IdeaGraph";
import { Command, Loaded, Num, travels, useLoad, verdictClass } from "../design/kit";
import { useFlip } from "../design/motion";
import { buildTree } from "../design/timeline";

/** The first screen a person sees with no data: one sentence, one line that answers the pointer, one first move. */
function Dark({ trust }: { trust: TrustBoard | null }) {
  const next = trust?.rows.find((r) => r.state === "missing" && r.next_step)?.next_step;
  const left = trust?.steps_left.value;
  return (
    <section className="dark">
      <PointerLine />
      <div className="dark-words">
        <p className="display">Nothing has<br />been tried yet.</p>
        <p className="lede">Give it a question. Agents explore freely in the Lab. Then the Court tests what they found, on data nothing has seen.</p>
      </div>
      <div className="dark-first">
        <h2>{left ? `First move: ${left} step${left === 1 ? "" : "s"} left on this machine` : "First move"}</h2>
        <Command text={next && next.startsWith("chh") ? next : "chh exp setup"} />
        <p className="small muted"><a href="#/trust">See what this machine has checked</a> <span className="sep" /> <a href="#/lab">See the Lab</a></p>
      </div>
    </section>
  );
}

function Live({ sid, project }: { sid: string; project: string | null }) {
  const lab = useLoad(() => api.lab(sid), [sid], `lab:${sid}`);
  const log = useLoad(() => api.labLog(sid), [sid], `log:${sid}`);
  useEffect(() => watchLab(sid, () => { void lab.reload(); void log.reload(); }), [sid, lab.reload, log.reload]);
  const lines = log.data?.lines ?? [];
  const tree = buildTree(lines, lines.length, log.data ? lab.data?.ideas ?? [] : []);
  return (
    <a className="live-card" href={`#/lab/${encodeURIComponent(sid)}`} aria-label={sid}>
      <div className="live-graph" style={travels("graph", sid)}>
        <IdeaGraph compact tree={tree} root={project ?? sid} label={`Ideas of ${sid}`} />
      </div>
      <span className="live-name" style={travels("session", sid)}>{sid}</span>
      <span className={`live-state ${tree.running ? "amber" : ""}`}>{tree.running ? "running" : "resting"}</span>
    </a>
  );
}

function Ledger({ c }: { c: Cases }) {
  const box = useRef<HTMLOListElement>(null);
  useFlip(box, c.cases.map((x) => `${x.case_id}${x.state}`).join());
  return (
    <ol className="ledger" ref={box}>
      {c.cases.map((x, i) => (
        <li key={x.case_id} data-k={x.case_id} className="hair">
          <span className="index">{String(i + 1).padStart(2, "0")}</span>
          <a href={`#/case/${encodeURIComponent(x.case_id)}`} style={travels("case", x.case_id)}>{x.title}</a>
          <span className={`state ${verdictClass(x.state)}`}>{x.state}</span>
          <span className="meter">Looks left <Num n={x.looks_remaining} /></span>
        </li>))}
    </ol>);
}

export function Home() {
  const waiting = useLoad(api.waiting, [], "waiting");
  const cases = useLoad(api.cases, [], "cases");
  const sessions = useLoad(api.sessions, [], "sessions");
  const frozen = useLoad(api.frozen, [], "frozen");
  const empty = sessions.data?.sessions.length === 0 && cases.data?.cases.length === 0 && frozen.data?.items.length === 0;
  const trust = useLoad(() => (empty ? api.trust() : Promise.resolve(null)), [empty]);
  const first = sessions.data?.sessions[0];
  // Lists follow the session live. They hold only where each case stands: reading them opens nothing.
  useEffect(() => (first ? watchLab(first.session_id, () => { void waiting.reload(); void cases.reload(); void frozen.reload(); }) : undefined),
    [first?.session_id, waiting.reload, cases.reload, frozen.reload]); // eslint-disable-line react-hooks/exhaustive-deps
  const need = waiting.data?.total;

  if (empty) return <><h1 className="eyebrow">Today</h1><Dark trust={trust.data} /></>;
  return (<>
    <h1 className="eyebrow">Today</h1>
    <section className="today">
      <div className="today-need">
        {need && <p className="numeral"><Num n={need} /></p>}
        <p className="numeral-say">{need?.value === 1 ? "step needs you" : "steps need you"}</p>
        <Loaded state={waiting}>{(w) => <ul className="plain">
          {w.items.slice(0, 4).map((i) => <li key={i.id}><a href="#/waiting">{i.title}</a></li>)}
        </ul>}</Loaded>
      </div>
      <div className="today-live">
        {first ? <Live sid={first.session_id} project={first.project} /> : null}
      </div>
    </section>
    <Loaded state={frozen}>{(f) => f.items.length > 0 && (
      <section className="block">
        <h2>Frozen ideas</h2>
        <ul className="frozen">{f.items.map((x) => <li key={x.promotion_id} className="hair">
          <span style={travels("idea", x.promotion_id)}>{x.title}</span>
          <span className="links"><a href={`#/rehearse/${x.promotion_id}`}>Rehearse</a><a href={`#/promote/${x.promotion_id}`}>Promote</a></span>
        </li>)}</ul>
      </section>)}</Loaded>
    <section className="block">
      <h2>Your cases</h2>
      <Loaded state={cases}>{(c) => c.cases.length === 0 ? <p className="muted">No case yet.</p> : <Ledger c={c} />}</Loaded>
    </section>
  </>);
}
