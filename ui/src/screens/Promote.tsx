import { useState } from "react";
import { api } from "../api/client";
import type { PromoteIn, PromotePreview, Promoted } from "../api/types.gen";
import { Command, Field, Loaded, Num, Panel, Problem, useLoad } from "../design/kit";
import { ReadinessPanel } from "./Rehearse";

type Route = PromoteIn["route"];
const ROUTES: [Route, string][] = [["generated", "Fresh generated data"], ["sealed", "My sealed data"],
  ["future", "Data that does not exist yet"], ["public", "Public data (cannot become a finding)"]];

export function Promote({ promotionId }: { promotionId?: string }) {
  const frozen = useLoad(api.frozen, []);
  const [pid, setPid] = useState(promotionId ?? "");
  const [route, setRoute] = useState<Route>("sealed");
  const [family, setFamily] = useState("");
  const [round, setRound] = useState("");
  const [project, setProject] = useState("");
  const [targets, setTargets] = useState("");
  const [rows, setRows] = useState("");
  const [preview, setPreview] = useState<PromotePreview | null>(null);
  const [sure, setSure] = useState(false);
  const [done, setDone] = useState<Promoted | null>(null);
  const [error, setError] = useState<unknown>(null);
  const chosen = pid || frozen.data?.items[0]?.promotion_id || "";

  const request = (): PromoteIn => ({
    promotion_id: chosen, route, family: family || null, beacon_round: round || null, project: project || null,
    targets_file: targets || null, n_test: route === "future" ? Number(rows) : null,
  });

  async function look() {
    setError(null); setPreview(null); setSure(false);
    try { setPreview(await api.preview(request())); } catch (e) { setError(e); }
  }

  async function go() {
    setError(null);
    try { setDone(await api.promote(request())); } catch (e) { setError(e); }
  }

  if (done) {
    return (
      <div className="moment">
        <h1>Fixed for the Court</h1>
        <p className="sub">The claim and the code can no longer change.</p>
        <Panel><p>{done.next_step}</p><Command text={done.command} /></Panel>
        {done.case_id && <p><a href={`#/case/${encodeURIComponent(done.case_id)}`}>Go to this case</a></p>}
      </div>);
  }

  return (<>
    <h1>Promote</h1>
    <p className="sub">The moment an idea leaves the Lab. Read what will happen first.</p>
    <Panel>
      <Loaded state={frozen}>{(f) => f.items.length === 0 ? <p className="muted">Nothing frozen yet.</p> : <>
        <label className="field">Frozen idea
          <select value={chosen} onChange={(e) => { setPid(e.target.value); setPreview(null); }}>
            {f.items.map((i) => <option key={i.promotion_id} value={i.promotion_id}>{i.title}</option>)}
          </select></label>
        <label className="field">Test on
          <select value={route} onChange={(e) => { setRoute(e.target.value as Route); setPreview(null); }}>
            {ROUTES.map(([v, t]) => <option key={v} value={v}>{t}</option>)}
          </select></label>
        {route === "generated" && <><Field label="Data family" value={family} onChange={setFamily} />
          <Field label="Round of the public random value" value={round} onChange={setRound} /></>}
        {(route === "sealed" || route === "future") && <Field label="Project" value={project} onChange={setProject} />}
        {route === "future" && <Field label="Rows you will hand over" value={rows} onChange={setRows} />}
        {route === "public" && <Field label="Targets file" value={targets} onChange={setTargets} />}
        <button type="button" className="btn" onClick={() => void look()}>See what will happen</button>
      </>}</Loaded>
      {error ? <Problem error={error} /> : null}
    </Panel>
    {preview && <>
      <Panel title="What happens">
        <ol>{preview.what_happens.map((s) => <li key={s}>{s}</li>)}</ol>
        <p>Looks remaining on this test data: <Num n={preview.looks_remaining} /></p>
        {!preview.finding_possible && <p>This route cannot become a finding.</p>}
        <p className="small muted">{preview.no_data_prior && <>Chance from the words alone: <Num n={preview.no_data_prior} digits={2} />. </>}{preview.prior_note}</p>
      </Panel>
      <ReadinessPanel r={preview.readiness} />
      <Panel>
        <label className="check"><input type="checkbox" checked={sure} onChange={(e) => setSure(e.target.checked)} />
          I have read this. The claim and the code stay as they are from here on.</label>
        <button type="button" className="btn" disabled={!sure} onClick={() => void go()}>Promote</button>
      </Panel>
    </>}
  </>);
}
