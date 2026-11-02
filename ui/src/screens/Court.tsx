// The quieter Court screens: Record, Approvals, Trust board, paper builder.
import { useState } from "react";
import { api } from "../api/client";
import type { ApprovalRoute, Paper } from "../api/types.gen";
import { Command, Field, Loaded, Num, Panel, Problem, useLoad, verdictClass } from "../design/kit";

export function RecordScreen() {
  const [q, setQ] = useState("");
  const rec = useLoad(() => api.record(q || undefined), [q]);
  return (<>
    <h1>Record</h1>
    <p className="sub">What was tried, what failed and how your stated chances held up.</p>
    <Field label="Search" value={q} onChange={setQ} />
    <Loaded state={rec}>{(r) => <>
      <Panel title="Calibration">
        <p>Claims <Num n={r.calibration.claims} />. Mean squared error <Num n={r.calibration.mean_squared_error} digits={3} />. Evidence <Num n={r.calibration.evidence_bits} digits={2} /> bits.</p>
        <p className="small muted">{r.calibration.note}</p>
        <p className="small">Verdicts not opened yet: <Num n={r.verdicts_unopened} /></p>
      </Panel>
      <Panel title="Opened outcomes">
        {r.outcomes.length === 0 ? <p className="muted">None opened.</p> :
          <table><thead><tr><th>Case</th><th>Verdict</th><th>Badges</th></tr></thead><tbody>
            {r.outcomes.map((o) => <tr key={o.case_id}><td><a href={`#/case/${encodeURIComponent(o.case_id)}`}>{o.title}</a></td>
              <td className={verdictClass(o.verdict)}>{o.verdict}</td><td className="small">{o.badges}</td></tr>)}
          </tbody></table>}
      </Panel>
      <Panel title="Dead ends">
        {r.dead_ends.length === 0 ? <p className="muted">None.</p> :
          <ul className="list">{r.dead_ends.map((d, i) => <li key={i}>{d.title}<div className="small muted">{d.reason}</div></li>)}</ul>}
      </Panel>
      <Panel title="How the system has improved"><p>{r.harness_improvement}</p></Panel>
    </>}</Loaded>
  </>);
}

/** This screen lists and points. It has no button that approves: a person runs the command in a terminal. */
export function Approvals() {
  const waiting = useLoad(api.waiting, []);
  const [route, setRoute] = useState<ApprovalRoute | null>(null);
  const [error, setError] = useState<unknown>(null);
  return (<>
    <h1>Waiting</h1>
    <p className="sub">Steps that need you. Approvals happen in a terminal, by a person, never on this page.</p>
    <Loaded state={waiting}>{(w) => w.items.length === 0 ? <p className="muted">Nothing is waiting.</p> : (
      <Panel><ul className="list">{w.items.map((i) => <li key={i.id}>
        <div className="row spread"><span>{i.title}</span>
          <button type="button" className="btn" onClick={() => { setError(null); api.howTo(i.id).then(setRoute, setError); }}>How to do this</button></div>
        <div className="small muted">{i.why}{i.optional ? " Optional." : ""}</div>
      </li>)}</ul></Panel>)}</Loaded>
    {error ? <Problem error={error} /> : null}
    {route && <Panel title={route.title}><p>{route.note}</p><Command text={route.command} /></Panel>}
  </>);
}

export function Trust() {
  const board = useLoad(api.trust, []);
  return (<>
    <h1>Trust board</h1>
    <p className="sub">What this machine has checked, and what is not true yet.</p>
    <Loaded state={board}>{(b) => <>
      <Panel title="Before a first session">
        <p>Steps left: <Num n={b.steps_left} /></p>
        <table><thead><tr><th>Item</th><th>State</th><th>Detail</th></tr></thead><tbody>
          {b.rows.map((r) => <tr key={r.name}><td>{r.name}</td><td>{{ ok: "done", missing: "not done", yours: "yours to judge" }[r.state]}</td>
            <td className="small">{r.detail}{r.next_step && <div className="muted">Next: {r.next_step}</div>}</td></tr>)}
        </tbody></table>
      </Panel>
      <Panel title="Not true yet"><ul className="list">{b.limits.map((l) => <li key={l}>{l}</li>)}</ul></Panel>
    </>}</Loaded>
  </>);
}

export function PaperBuilder({ caseId }: { caseId?: string }) {
  const [id, setId] = useState(caseId ?? "");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [refs, setRefs] = useState("{}");
  const [done, setDone] = useState<Paper | null>(null);
  const [error, setError] = useState<unknown>(null);

  async function build() {
    setError(null); setDone(null);
    let parsed: Record<string, string>;
    try { parsed = JSON.parse(refs) as Record<string, string>; }
    catch { setError(new Error("The list of numbers is not valid JSON.")); return; }
    try { setDone(await api.paper({ case_id: id, title, body, refs: parsed })); } catch (e) { setError(e); }
  }

  return (<>
    <h1>Paper builder</h1>
    <p className="sub">A paper whose numbers come from the record, never typed by hand. It opens once the test data is retired by a person.</p>
    <Panel>
      <Field label="Case" value={id} onChange={setId} />
      <Field label="Title" value={title} onChange={setTitle} />
      <Field label="Body, with a placeholder for each number" area value={body} onChange={setBody} />
      <Field label="Numbers: placeholder name to where it comes from (JSON)" area value={refs} onChange={setRefs} />
      <button type="button" className="btn" disabled={!id || !title} onClick={() => void build()}>Build</button>
      {error ? <Problem error={error} /> : null}
      {done && <p>{done.note} <code>{done.path}</code></p>}
    </Panel>
  </>);
}
