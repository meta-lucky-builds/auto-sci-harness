import { useState } from "react";
import { api } from "../api/client";
import type { Readiness, Rehearsal, ScoreRow } from "../api/types.gen";
import { download, lightFigure } from "../design/figure";
import { Field, Loaded, Note, Num, Panel, Problem, travels, useLoad } from "../design/kit";

/** Chance of passing against test rows: one line, points you can trace, the rows with an objection marked hollow. */
export function ReadinessPanel({ r }: { r: Readiness }) {
  const w = 640, h = 150, n = r.rows.length;
  const x = (i: number) => 24 + (i / Math.max(1, n - 1)) * (w - 48);
  const y = (v: number | null) => h - 24 - (v ?? 0) * (h - 44);
  return (
    <Panel title="Readiness" className="ready">
      <p className="lede">{r.sentence}</p>
      <svg viewBox={`0 0 ${w} ${h}`} className="ready-curve" role="img" aria-label="Rough chance of passing at each size of test data">
        <line x1={24} x2={w - 24} y1={h - 24} y2={h - 24} className="axis" />
        <polyline points={r.rows.map((p, i) => `${x(i)},${y(p.chance.value as number | null)}`).join(" ")} className="curve" />
        {r.rows.map((p, i) => <circle key={i} cx={x(i)} cy={y(p.chance.value as number | null)} r={3.5} className={p.objection ? "pt hollow" : "pt"} data-t={`row:${i}`} />)}
      </svg>
      <table><tbody>
        <tr><th scope="row">Test rows</th>{r.rows.map((p, i) => <td key={i} data-t={`row:${i}`}><Num n={p.test_rows} group /></td>)}</tr>
        <tr><th scope="row">Rough chance of passing</th>{r.rows.map((p, i) => <td key={i} data-t={`row:${i}`}><Num n={p.chance} digits={2} /></td>)}</tr>
        <tr><th scope="row">Objection</th>{r.rows.map((p, i) => <td key={i} data-t={`row:${i}`}>{p.objection ? "yes" : "no"}</td>)}</tr>
      </tbody></table>
      <p className="small muted">{r.notice}</p>
    </Panel>);
}

function Strip({ scores }: { scores: ScoreRow[] }) {
  const vals = scores.map((s) => s.score.value).filter((v): v is number => v !== null);
  if (vals.length === 0) return null;
  const lo = Math.min(...vals), hi = Math.max(...vals), pad = Math.max((hi - lo) * 0.12, 1e-6);
  const w = 760, x = (v: number) => 90 + ((v - lo + pad) / (hi - lo + 2 * pad)) * (w - 120);
  return (
    <svg viewBox={`0 0 ${w} 130`} className="strip" role="img" aria-label="Score of every seed, method above baseline">
      {(["method", "baseline"] as const).map((arm, row) => <g key={arm}>
        <text x={0} y={42 + row * 50} className="strip-label">{arm}</text>
        <line x1={90} x2={w - 30} y1={38 + row * 50} y2={38 + row * 50} className="axis" />
        {scores.filter((s) => s.arm === arm && s.score.value !== null).map((s) => (
          <circle key={s.seed} cx={x(s.score.value as number)} cy={38 + row * 50} r={4} className={arm === "method" ? "pt" : "pt faint"} data-t={`seed:${s.seed}`}><title>{`Seed ${s.seed}: ${s.score.value}`}</title></circle>))}
      </g>)}
      <text x={90} y={126} className="strip-tick">{lo.toFixed(3)}</text>
      <text x={w - 30} y={126} textAnchor="end" className="strip-tick">{hi.toFixed(3)}</text>
    </svg>);
}

/** One column per seed, so the two arms read straight across. */
function Seeds({ scores }: { scores: ScoreRow[] }) {
  const seeds = [...new Set(scores.map((s) => s.seed))];
  return (
    <table className="seeds"><thead><tr><th>Seed</th>{seeds.map((s) => <th key={s} data-t={`seed:${s}`}>{s}</th>)}</tr></thead><tbody>
      {(["method", "baseline"] as const).map((arm) => <tr key={arm}><th scope="row">{arm}</th>{seeds.map((seed) => {
        const row = scores.find((s) => s.arm === arm && s.seed === seed);
        return <td key={seed} data-t={`seed:${seed}`}>{row ? <Num n={row.score} digits={3} /> : null}</td>;
      })}</tr>)}
    </tbody></table>);
}

export function Rehearse({ promotionId }: { promotionId?: string }) {
  const frozen = useLoad(api.frozen, [], "frozen");
  const [pid, setPid] = useState(promotionId ?? "");
  const [rows, setRows] = useState("1000");
  const [out, setOut] = useState<Rehearsal | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const chosen = pid || frozen.data?.items[0]?.promotion_id || "";
  const idea = frozen.data?.items.find((i) => i.promotion_id === chosen);

  async function run() {
    setBusy(true); setError(null); setOut(null);
    try {
      setOut(await api.rehearse({ promotion_id: chosen, rows: Number(rows), label_column: "label" }));
    } catch (e) { setError(e); } finally { setBusy(false); }
  }

  return (<>
    <header className="page-head">
      <h1>Rehearse</h1>
      <p className="sub">A mock trial on Lab data. It shows everything and spends nothing.</p>
    </header>
    <section className="stage">
      <Loaded state={frozen}>{(f) => f.items.length === 0 ? <section className="void"><p className="display">Nothing frozen yet.</p>
        <p className="lede">Freeze an idea in the Lab, and it can have a mock trial here, as often as you like.</p></section> : <>
        {idea && <p className="stage-claim" style={travels("idea", idea.promotion_id)}>{idea.sentence}</p>}
        <div className="pair">
          <label className="field">Frozen idea
            <select value={chosen} onChange={(e) => setPid(e.target.value)}>
              {f.items.map((i) => <option key={i.promotion_id} value={i.promotion_id}>{i.title}</option>)}
            </select></label>
          <Field label="Rows to hold back" value={rows} onChange={setRows} />
        </div>
        <button type="button" className={`btn ${busy ? "busy" : ""}`} disabled={busy || !(Number(rows) > 0)} onClick={() => void run()}>
          {busy ? "Rehearsing..." : "Rehearse"}</button>
      </>}</Loaded>
      {error ? <Problem error={error} /> : null}
    </section>
    {out && <>
      <Note>{out.notice}</Note>
      {out.ran ? <p className="lede rehearse-says">{out.would_certify ? "This rehearsal would pass. That promises nothing on fresh data."
        : "This rehearsal would not pass."}</p> : <p role="alert">It could not run: {out.reason}</p>}
      <div className="split">
        <Panel title="Scores">
          <Strip scores={out.scores} />
          <Seeds scores={out.scores} />
          <div className="after"><button type="button" className="btn quiet" disabled={out.scores.length === 0}
            onClick={() => download("rehearsal.svg", lightFigure(out.scores, "Rehearsal on Lab data: not evidence"), "image/svg+xml")}>
            Export a light figure</button></div>
        </Panel>
        <Panel title="Every check">
          <ul className="checks">{out.checks.map((c) => <li key={c.name} className="hair"><span>{c.name}</span>
            <span className={c.status === "pass" ? "muted" : "rejected"}>{c.status}</span></li>)}</ul>
        </Panel>
      </div>
      <ReadinessPanel r={out.readiness} />
      <p className="next"><a href={`#/promote/${out.promotion_id}`}>Go on to the Promote moment</a></p>
    </>}
  </>);
}
