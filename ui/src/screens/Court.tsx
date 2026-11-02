// The quieter Court screens: Record, Approvals, Trust board, paper builder.
import { useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import type { ApprovalRoute, Outcome, Paper, TrustRow } from "../api/types.gen";
import { Command, Field, Loaded, Num, Panel, Problem, useLoad, verdictClass } from "../design/kit";
import { useFlip } from "../design/motion";

const when = (o: Outcome) => (o.badges === "NONE" ? "" : o.badges.replaceAll("+", " + "));

export function RecordScreen() {
  const [q, setQ] = useState("");
  const rec = useLoad(() => api.record(q || undefined), [q], `record:${q}`);
  const ends = useRef<HTMLOListElement>(null);
  const out = useRef<HTMLOListElement>(null);
  useFlip(ends, (rec.data?.dead_ends ?? []).map((d) => d.title).join());
  useFlip(out, (rec.data?.outcomes ?? []).map((d) => d.case_id).join());
  return (<>
    <header className="page-head">
      <h1>Record</h1>
      <p className="sub">What was tried, what failed and how your stated chances held up.</p>
    </header>
    <Loaded state={rec}>{(r) => <>
      <section className="almanac" aria-label="Calibration">
        <div><dt>Claims</dt><dd><Num n={r.calibration.claims} /></dd></div>
        <div><dt>Mean squared error</dt><dd><Num n={r.calibration.mean_squared_error} digits={3} /></dd></div>
        <div><dt>Evidence, in bits</dt><dd><Num n={r.calibration.evidence_bits} digits={2} /></dd></div>
        <div><dt>Verdicts not opened</dt><dd><Num n={r.verdicts_unopened} /></dd></div>
      </section>
      <p className="small muted almanac-note">{r.calibration.note}</p>
      <div className="search"><Field label="Search" value={q} onChange={setQ} /></div>
      <div className="split">
        <Panel title="Opened outcomes">
          {r.outcomes.length === 0 ? <p className="muted">None opened.</p> :
            <ol className="outcomes" ref={out}>{r.outcomes.map((o) => <li key={o.case_id} data-k={o.case_id} className="hair">
              <a href={`#/case/${encodeURIComponent(o.case_id)}`}>{o.title}</a>
              <span className={`state ${verdictClass(o.verdict)}`}>{o.verdict}</span>
              <span className="small muted">{when(o)}</span>
            </li>)}</ol>}
        </Panel>
        <Panel title="Dead ends" className="ends">
          {r.dead_ends.length === 0 ? <p className="muted">None.</p> :
            <ol className="dead" ref={ends}>{r.dead_ends.map((d) => <li key={d.title} data-k={d.title} className="hair">
              <span className="dead-title">{d.title}</span><span className="small ash">{d.reason}</span></li>)}</ol>}
        </Panel>
      </div>
      <Panel title="How the system has improved"><p className="lede">{r.harness_improvement}</p></Panel>
    </>}</Loaded>
  </>);
}

/** This screen lists and points. It has no button that approves: a person runs the command in a terminal. */
export function Approvals() {
  const waiting = useLoad(api.waiting, [], "waiting");
  const [route, setRoute] = useState<ApprovalRoute | null>(null);
  const [error, setError] = useState<unknown>(null);
  return (<>
    <header className="page-head">
      <h1>Waiting</h1>
      <p className="sub">Steps that need you. Approvals happen in a terminal, by a person, never on this page.</p>
    </header>
    <Loaded state={waiting}>{(w) => w.items.length === 0 ? <section className="void"><p className="display">Nothing is waiting.</p></section> : (
      <ol className="queue">{w.items.map((i, n) => <li key={i.id} className="hair">
        <span className="index">{String(n + 1).padStart(2, "0")}</span>
        <div className="queue-body">
          <p className="queue-title">{i.title}</p>
          <p className="small muted">{i.why}{i.optional ? " Optional." : ""}</p>
          {route?.id === i.id && <div className="queue-how"><p className="small">{route.note}</p><Command text={route.command} /></div>}
        </div>
        <button type="button" className="btn quiet" onClick={() => { setError(null); api.howTo(i.id).then(setRoute, setError); }}>How to do this</button>
      </li>)}</ol>)}</Loaded>
    {error ? <Problem error={error} /> : null}
  </>);
}

const STATE: Record<TrustRow["state"], string> = { ok: "done", missing: "not done", yours: "yours to judge" };

export function Trust() {
  const board = useLoad(api.trust, [], "trust");
  const score = useMemo(() => board.data?.rows.filter((r) => r.state === "ok").length ?? 0, [board.data]);
  return (<>
    <header className="page-head">
      <h1>Trust board</h1>
      <p className="sub">What this machine has checked, and what is not true yet.</p>
    </header>
    <Loaded state={board}>{(b) => <>
      <section className="readout">
        <div className="readout-big">
          <p className="numeral"><Num n={b.steps_left} /></p>
          <p className="numeral-say">Steps left before a first session</p>
          <div className="dial" role="img" aria-label={`${score} of ${b.rows.length} done`}>
            {b.rows.map((r) => <i key={r.name} className={`tick tick-${r.state}`} />)}
          </div>
        </div>
        <ol className="gauge-list">{b.rows.map((r) => <li key={r.name} className="hair">
          <span className={`pip pip-${r.state}`} aria-hidden="true" />
          <span className="gauge-name">{r.name}</span>
          <span className={`gauge-state state-${r.state}`}>{STATE[r.state]}</span>
          <span className="gauge-detail small muted">{r.detail}{r.next_step && <> Next: <code>{r.next_step}</code></>}</span>
        </li>)}</ol>
      </section>
      <section className="limits">
        <h2>Not true yet</h2>
        <ul>{b.limits.map((l) => <li key={l} className="hair">{l}</li>)}</ul>
      </section>
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
    <header className="page-head">
      <h1>Paper builder</h1>
      <p className="sub">A paper whose numbers come from the record, never typed by hand. It opens once the test data is retired by a person.</p>
    </header>
    <section className="sheet-paper">
      <div className="pair"><Field label="Case" value={id} onChange={setId} /><Field label="Title" value={title} onChange={setTitle} /></div>
      <Field label="Body, with a placeholder for each number" area value={body} onChange={setBody} />
      <Field label="Numbers: placeholder name to where it comes from (JSON)" area value={refs} onChange={setRefs} />
      <button type="button" className="btn" disabled={!id || !title} onClick={() => void build()}>Build</button>
      {error ? <Problem error={error} /> : null}
      {done && <p>{done.note} <code>{done.path}</code></p>}
    </section>
  </>);
}
