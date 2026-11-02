import { useState } from "react";
import { api } from "../api/client";
import type { Readiness, Rehearsal } from "../api/types.gen";
import { download, lightFigure } from "../design/figure";
import { Field, Loaded, Note, Num, Panel, Problem, useLoad } from "../design/kit";

export function ReadinessPanel({ r }: { r: Readiness }) {
  return (
    <Panel title="Readiness">
      <p>{r.sentence}</p><p className="small muted">{r.notice}</p>
      <table><thead><tr><th>Test rows</th><th>Rough chance of passing</th><th>Objection</th></tr></thead><tbody>
        {r.rows.map((x, i) => <tr key={i}><td><Num n={x.test_rows} /></td><td><Num n={x.chance} digits={2} /></td>
          <td>{x.objection ? "yes" : "no"}</td></tr>)}
      </tbody></table>
    </Panel>);
}

export function Rehearse({ promotionId }: { promotionId?: string }) {
  const frozen = useLoad(api.frozen, []);
  const [pid, setPid] = useState(promotionId ?? "");
  const [rows, setRows] = useState("1000");
  const [out, setOut] = useState<Rehearsal | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const chosen = pid || frozen.data?.items[0]?.promotion_id || "";

  async function run() {
    setBusy(true); setError(null); setOut(null);
    try {
      setOut(await api.rehearse({ promotion_id: chosen, rows: Number(rows), label_column: "label" }));
    } catch (e) { setError(e); } finally { setBusy(false); }
  }

  return (<>
    <h1>Rehearse</h1>
    <p className="sub">A mock trial on Lab data. It shows everything and spends nothing.</p>
    <Panel>
      <Loaded state={frozen}>{(f) => f.items.length === 0 ? <p className="muted">Nothing frozen yet.</p> : <>
        <label className="field">Frozen idea
          <select value={chosen} onChange={(e) => setPid(e.target.value)}>
            {f.items.map((i) => <option key={i.promotion_id} value={i.promotion_id}>{i.title}</option>)}
          </select></label>
        <Field label="Rows to hold back" value={rows} onChange={setRows} />
        <button type="button" className="btn" disabled={busy || !(Number(rows) > 0)} onClick={() => void run()}>
          {busy ? "Rehearsing..." : "Rehearse"}</button>
      </>}</Loaded>
      {error ? <Problem error={error} /> : null}
    </Panel>
    {out && <>
      <Note>{out.notice}</Note>
      {out.ran ? <p>{out.would_certify ? "This rehearsal would pass. That promises nothing on fresh data."
        : "This rehearsal would not pass."}</p> : <p role="alert">It could not run: {out.reason}</p>}
      <div className="cols">
        <Panel title="Scores">
          <table><thead><tr><th>Arm</th><th>Seed</th><th>Score</th></tr></thead><tbody>
            {out.scores.map((s) => <tr key={`${s.arm}${s.seed}`}><td>{s.arm}</td><td>{s.seed}</td>
              <td><Num n={s.score} digits={3} /></td></tr>)}
          </tbody></table>
          <div style={{ marginTop: 10 }}><button type="button" className="btn" disabled={out.scores.length === 0}
            onClick={() => download("rehearsal.svg", lightFigure(out.scores, "Rehearsal on Lab data: not evidence"), "image/svg+xml")}>
            Export a light figure</button></div>
        </Panel>
        <Panel title="Every check">
          <ul className="list small">{out.checks.map((c) => <li key={c.name} className="row spread"><span>{c.name}</span>
            <span className={c.status === "pass" ? "muted" : "rejected"}>{c.status}</span></li>)}</ul>
        </Panel>
      </div>
      <ReadinessPanel r={out.readiness} />
      <p><a href={`#/promote/${out.promotion_id}`}>Go on to the Promote moment</a></p>
    </>}
  </>);
}
