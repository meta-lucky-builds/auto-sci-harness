import { useEffect, useState } from "react";
import { api, watchLab } from "../api/client";
import { Field, Loaded, Note, Num, Panel, Problem, useLoad } from "../design/kit";

export function Lab({ sessionId }: { sessionId?: string }) {
  const sessions = useLoad(api.sessions, []);
  const sid = sessionId ?? sessions.data?.sessions[0]?.session_id;
  if (!sid) {
    return (<><h1>Lab</h1><Loaded state={sessions}>{() =>
      <p className="muted">No session yet. Start one with <code>chh exp start</code>.</p>}</Loaded></>);
  }
  return <Workspace key={sid} sid={sid} />;
}

function Workspace({ sid }: { sid: string }) {
  const lab = useLoad(() => api.lab(sid), [sid]);
  const log = useLoad(() => api.labLog(sid), [sid]);
  const [drawer, setDrawer] = useState(false);
  const [text, setText] = useState("");
  const [error, setError] = useState<unknown>(null);
  useEffect(() => watchLab(sid, () => { void lab.reload(); void log.reload(); }), [sid, lab.reload, log.reload]);

  async function steer() {
    setError(null);
    try {
      await api.steer({ session_id: sid, text, send: true });
      setText("");
      await log.reload();
    } catch (e) { setError(e); }
  }

  return (<>
    <div className="row spread">
      <h1>{sid}</h1>
      <span className="amber small"><span className="live-dot" /> live</span>
    </div>
    <Loaded state={lab}>{(l) => <>
      <p className="sub">{l.notice}</p>
      <p className="small muted">{l.spending}</p>
      <div className="row small muted">
        <span>Runs <Num n={l.runs} /></span><span>Steers <Num n={l.steers} /></span>
        <span>Web <Num n={l.web_connections} /></span>
        <span>Spent $<Num n={l.cost_usd} digits={2} /></span>
        <span>Costs not known <Num n={l.costs_unknown} /></span>
      </div>
    </>}</Loaded>
    <div className="lab-cols" style={{ marginTop: 14 }}>
      <Panel title="Ideas">
        <Loaded state={lab}>{(l) => <ul className="list">{l.ideas.map((i) =>
          <li key={i.idea_id} className={i.status === "abandoned" ? "muted" : ""}>{i.title}
            <div className="small muted">{i.status}</div></li>)}</ul>}</Loaded>
      </Panel>
      <div>
        <Panel title="Agent">
          <Loaded state={log}>{(g) => <div className="log" aria-label="Story">{g.lines.map((x, i) =>
            <div key={i}><span className="muted">{x.at}</span><span className={x.who === "agent" ? "amber" : "muted"}>{x.who}</span><span>{x.text}</span></div>)}</div>}</Loaded>
        </Panel>
        <Panel title="Steer the agent">
          <Field label="Your words go into the Story." value={text} onChange={setText} />
          <button type="button" className="btn" disabled={!text.trim()} onClick={() => void steer()}>Send</button>
          {error ? <Problem error={error} /> : null}
        </Panel>
      </div>
      <div>
        <Panel title="Runs">
          <Loaded state={log}>{(g) => <ul className="list small">{g.lines.filter((x) => x.kind === "run").map((x, i) =>
            <li key={i}><code>{x.text}</code></li>)}</ul>}</Loaded>
        </Panel>
        <Panel>
          <button type="button" className="btn" aria-expanded={drawer} onClick={() => setDrawer(!drawer)}>Literature</button>
          {drawer && <Loaded state={log}>{(g) => <div style={{ marginTop: 10 }}>
            {g.reading.length === 0 && <p className="muted">Nothing searched yet.</p>}
            <ul className="list small">{g.reading.map((r, i) => <li key={i}>{r.query}
              <div className="muted">{r.source}, <Num n={r.found} /> found</div></li>)}</ul>
            {g.web.length > 0 && <><h2 style={{ marginTop: 12 }}>Sites reached</h2>
              <ul className="list small">{g.web.map((h) => <li key={h}>{h}</li>)}</ul></>}
          </div>}</Loaded>}
        </Panel>
      </div>
    </div>
    <Note>Nothing here is evidence. Only the Court can certify a claim.</Note>
  </>);
}
