import { useState } from "react";
import { api } from "../api/client";
import type { PromoteIn, PromotePreview, Promoted } from "../api/types.gen";
import { Command, Field, Loaded, Num, Panel, Problem, travels, useLoad } from "../design/kit";
import { PromoteMoment } from "../design/Moment";
import { ReadinessPanel } from "./Rehearse";

type Route = PromoteIn["route"];
const ROUTES: [Route, string][] = [["generated", "Fresh generated data"], ["sealed", "My sealed data"],
  ["future", "Data that does not exist yet"], ["public", "Public data (cannot become a finding)"]];

export function Promote({ promotionId }: { promotionId?: string }) {
  const frozen = useLoad(api.frozen, [], "frozen");
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
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const chosen = pid || frozen.data?.items[0]?.promotion_id || "";
  const idea = frozen.data?.items.find((i) => i.promotion_id === chosen);

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
    try { setDone(await api.promote(request())); setPlaying(true); } catch (e) { setError(e); }
  }

  if (done) {
    return (<>
      <section className="fixed moment">
        <p className="eyebrow">{idea?.title}</p>
        <h1 className="display">Fixed for the Court</h1>
        <div className="fixed-line" aria-hidden="true" />
        <p className="sub">The claim and the code can no longer change.</p>
        <p>{done.next_step}</p>
        <Command text={done.command} />
        {done.case_id && <p className="next"><a href={`#/case/${encodeURIComponent(done.case_id)}`}>Go to this case</a></p>}
      </section>
      {playing && <PromoteMoment sentence={idea?.sentence ?? ""} onDone={() => setPlaying(false)} />}
    </>);
  }

  return (<>
    <header className="page-head">
      <h1>Promote</h1>
      <p className="sub">The moment an idea leaves the Lab. Read what will happen first.</p>
    </header>
    <section className="stage">
      <Loaded state={frozen}>{(f) => f.items.length === 0 ? <section className="void"><p className="display">Nothing frozen yet.</p>
        <p className="lede">An idea is frozen from a Lab session, with its code. Then it can be rehearsed and promoted here.</p></section> : <>
        {idea && <p className="stage-claim" style={travels("idea", idea.promotion_id)}>{idea.sentence}</p>}
        <div className="pair">
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
        </div>
        <button type="button" className="btn" onClick={() => void look()}>See what will happen</button>
      </>}</Loaded>
      {error ? <Problem error={error} /> : null}
    </section>
    {preview && <>
      <Panel title="What happens">
        <ol className="happens">{preview.what_happens.map((s, i) => <li key={s}><span className="index">{i + 1}</span>{s}</li>)}</ol>
        <p>Looks remaining on this test data: <Num n={preview.looks_remaining} /></p>
        {!preview.finding_possible && <p>This route cannot become a finding.</p>}
        <p className="small muted">{preview.no_data_prior && <>Chance from the words alone: <Num n={preview.no_data_prior} digits={2} />. </>}{preview.prior_note}</p>
      </Panel>
      <ReadinessPanel r={preview.readiness} />
      <section className="commit">
        <label className="check"><input type="checkbox" checked={sure} onChange={(e) => setSure(e.target.checked)} />
          I have read this. The claim and the code stay as they are from here on.</label>
        <button type="button" className="btn door-btn" disabled={!sure} onClick={() => void go()}>Promote</button>
      </section>
    </>}
  </>);
}
