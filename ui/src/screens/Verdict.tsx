import { useState } from "react";
import { api } from "../api/client";
import type { Case, TechnicalView } from "../api/types.gen";
import { BadgeChip } from "../design/CertifiedBadge";
import { Command, Field, Num, Panel, Problem, useScripted, verdictClass } from "../design/kit";

/** A verdict is never fetched on arrival: opening one is a look, so a person presses for it. */
export function Verdict({ caseId }: { caseId: string }) {
  const [view, setView] = useState<Case | null>(null);
  const [error, setError] = useState<unknown>(null);

  async function open() {
    setError(null);
    try { setView(await api.openCase(caseId)); } catch (e) { setError(e); }
  }

  if (!view) {
    return (<>
      <h1>{caseId}</h1>
      <p className="sub">Opening a verdict counts as one look, and it is recorded.</p>
      <button type="button" className="btn" onClick={() => void open()}>Open the verdict</button>
      {error ? <Problem error={error} /> : null}
    </>);
  }
  return <CaseView c={view} />;
}

export function CaseView({ c }: { c: Case }) {
  const scripted = useScripted();
  const [tech, setTech] = useState<TechnicalView | null>(null);
  const [reviewer, setReviewer] = useState("");
  const [out, setOut] = useState("");
  const [shared, setShared] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  async function share() {
    setError(null);
    try { setShared((await api.share({ case_id: c.case_id, reviewer, out_file: out })).note); } catch (e) { setError(e); }
  }
  async function details() {
    setError(null);
    try { setTech(await api.technical(c.case_id)); } catch (e) { setError(e); }
  }

  return (<>
    <h1>{c.title}</h1>
    <p className="sub">{c.sentence}</p>
    <div className="verdict">
      <p className={`verdict-sentence ${verdictClass(c.verdict ?? "")}`}>{c.verdict_sentence}</p>
    </div>
    {c.reason && <p role="status">{c.reason}</p>}
    {c.badges.length > 0 && <Panel title="Badges">
      <div className="row">{c.badges.map((b) => <BadgeChip key={b.form} badge={b} verdict={c.verdict} scripted={scripted} />)}</div>
      <ul className="list small" style={{ marginTop: 12 }}>{c.badges.map((b) => <li key={b.form}><b>{b.name}.</b> {b.meaning}</li>)}</ul>
      {c.second_test && <p className="small">{c.second_test}</p>}
    </Panel>}
    <Panel title="Facts">
      <p>Test rows: <Num n={c.test_rows} />. Looks remaining: <Num n={c.looks_remaining} />.</p>
      {c.next_step && <><p>{c.next_step}</p>{c.command && <Command text={c.command} />}</>}
      {c.rerun && <><p>Rerun it in one step:</p><Command text={c.rerun} /></>}
    </Panel>
    {c.story && <Panel title="Story">
      <p>{c.story.idea_path.join(" > ")}</p>
      <p className="small">Steps <Num n={c.story.steps} />, steers <Num n={c.story.steers} />, web connections <Num n={c.story.web_connections} />.</p>
      <p className="small muted">{c.story.intact ? "The Story's chain of records is intact." : "The Story's chain of records is broken."}</p>
    </Panel>}
    <Panel title="Checks shown in full">
      <ul className="list small">{c.checks_in_full.map((k) => <li key={k.name} className="row spread"><span>{k.name}</span><span className="muted">{k.status}</span></li>)}</ul>
      <p className="small muted">The other checks stay hidden until the test data is retired, so the Lab cannot learn how to pass.</p>
      <button type="button" className="btn" onClick={() => void details()}>Technical details</button>
      {tech && <pre className="small" style={{ marginTop: 10 }}>{JSON.stringify(tech.details, null, 1)}</pre>}
    </Panel>
    <Panel title="Share with a reviewer">
      <Field label="Reviewer" value={reviewer} onChange={setReviewer} />
      <Field label="Case file to write" value={out} onChange={setOut} />
      <button type="button" className="btn" disabled={!reviewer || !out} onClick={() => void share()}>Write the case file</button>
      {shared && <p className="small">{shared}</p>}
      {error ? <Problem error={error} /> : null}
    </Panel>
  </>);
}
