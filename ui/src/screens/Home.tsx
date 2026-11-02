import { api } from "../api/client";
import { Loaded, Num, Panel, useLoad, verdictClass } from "../design/kit";

export function Home() {
  const waiting = useLoad(api.waiting, []);
  const cases = useLoad(api.cases, []);
  const sessions = useLoad(api.sessions, []);
  const frozen = useLoad(api.frozen, []);
  return (<>
    <h1>Today</h1>
    <p className="sub">What needs you, and where your work stands.</p>
    <div className="cols">
      <Panel title="Waiting for you">
        <Loaded state={waiting}>{(w) => <>
          <p><Num n={w.total} /> waiting. <a href="#/waiting">See the list</a></p>
          <ul className="list">{w.items.slice(0, 4).map((i) => <li key={i.id}>{i.title}</li>)}</ul>
        </>}</Loaded>
      </Panel>
      <Panel title="Lab sessions">
        <Loaded state={sessions}>{(s) => s.sessions.length === 0
          ? <p className="muted">No session yet. Start one with <code>chh exp start</code>.</p>
          : <ul className="list">{s.sessions.map((x) => <li key={x.session_id}>
              <a href={`#/lab/${encodeURIComponent(x.session_id)}`}>{x.session_id}</a>
              <span className="muted small"> {x.agent} · {x.box}</span></li>)}</ul>}</Loaded>
      </Panel>
      <Panel title="Frozen ideas">
        <Loaded state={frozen}>{(f) => f.items.length === 0
          ? <p className="muted">Nothing frozen yet.</p>
          : <ul className="list">{f.items.map((x) => <li key={x.promotion_id}>{x.title}
              <div className="small"><a href={`#/rehearse/${x.promotion_id}`}>Rehearse</a> · <a href={`#/promote/${x.promotion_id}`}>Promote</a></div>
            </li>)}</ul>}</Loaded>
      </Panel>
    </div>
    <Panel title="Your cases">
      <Loaded state={cases}>{(c) => c.cases.length === 0 ? <p className="muted">No case yet.</p> : (
        <table><thead><tr><th>Case</th><th>Where it stands</th><th>Looks left</th></tr></thead><tbody>
          {c.cases.map((x) => <tr key={x.case_id}>
            <td><a href={`#/case/${encodeURIComponent(x.case_id)}`}>{x.title}</a></td>
            <td className={verdictClass(x.state)}>{x.state}</td><td><Num n={x.looks_remaining} /></td></tr>)}
        </tbody></table>)}</Loaded>
    </Panel>
  </>);
}
