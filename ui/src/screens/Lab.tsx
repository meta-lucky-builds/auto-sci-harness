import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, watchLab } from "../api/client";
import type { LabLine } from "../api/types.gen";
import { IdeaGraph } from "../design/IdeaGraph";
import { Field, Loaded, Num, Problem, travels, useLoad } from "../design/kit";
import { beat, calm, useFlip } from "../design/motion";
import { buildTree, lineKeys } from "../design/timeline";

export function Lab({ sessionId }: { sessionId?: string }) {
  const sessions = useLoad(api.sessions, [], "sessions");
  const sid = sessionId ?? sessions.data?.sessions[0]?.session_id;
  if (!sid) {
    return (<><h1 className="eyebrow">Lab</h1><Loaded state={sessions}>{() =>
      <section className="void">
        <p className="display">The Lab is quiet.</p>
        <p className="lede">When a session starts, its ideas branch here as they happen, with every run on the line that made it.</p>
        <pre aria-label="Command"><code>chh exp start AGENT --project NAME</code></pre>
      </section>}</Loaded></>);
  }
  return <Workspace key={sid} sid={sid} />;
}

const WHO: Record<string, string> = { human: "you", run: "run", idea: "idea", web: "web", action: "act", session: "lab" };

/** The time slider. Every Story line is a mark; dragging replays how the session grew. */
function Scrubber({ lines, at, live, onAt, onLive, onReplay, playing }: {
  lines: LabLine[]; at: number; live: boolean; onAt: (n: number) => void; onLive: () => void; onReplay: () => void; playing: boolean;
}) {
  const [keys, tree] = useMemo(() => { const t = buildTree(lines, lines.length); return [lineKeys(lines, t), t] as const; }, [lines]);
  void tree;
  return (
    <div className="scrub">
      <button type="button" className="btn quiet" onClick={onReplay}>{playing ? "Replaying" : "Replay"}</button>
      <div className="scrub-track">
        <div className="scrub-marks" aria-hidden="true">
          {lines.map((l, i) => <i key={i} data-line data-t={keys[i]} className={`mark mark-${l.kind} ${i < at ? "past" : ""}`} style={{ left: `${(i / Math.max(1, lines.length - 1)) * 100}%` }} />)}
        </div>
        <input type="range" aria-label="Step in this session" min={0} max={lines.length} value={at} onChange={(e) => onAt(Number(e.target.value))} />
      </div>
      <span className="scrub-at">{at}/{lines.length}</span>
      <button type="button" className={`btn quiet ${live ? "is-live" : ""}`} aria-pressed={live} onClick={onLive}><span className="live-dot" />Live</button>
    </div>);
}

function Workspace({ sid }: { sid: string }) {
  const lab = useLoad(() => api.lab(sid), [sid], `lab:${sid}`);
  const log = useLoad(() => api.labLog(sid), [sid], `log:${sid}`);
  const [drawer, setDrawer] = useState(false);
  const [text, setText] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [sent, setSent] = useState(false);
  const [chosen, setChosen] = useState<string | null>(null);
  const [replay, setReplay] = useState<number | null>(null); // null: the live end
  const [playing, setPlaying] = useState(false);
  const card = useRef<HTMLDivElement>(null);
  const stream = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  useEffect(() => watchLab(sid, () => { void lab.reload(); void log.reload(); }), [sid, lab.reload, log.reload]);

  const lines = log.data?.lines ?? [];
  const at = replay === null ? lines.length : Math.min(replay, lines.length);
  const shown = lines.slice(0, at);
  const ideas = log.data ? lab.data?.ideas : undefined; // no tree until the Story is read: a run is never drawn twice
  const tree = useMemo(() => buildTree(lines, at, ideas ?? []), [lines, at, ideas]);
  const keys = useMemo(() => lineKeys(lines, buildTree(lines, lines.length, ideas ?? [])), [lines, ideas]);
  const runs = shown.filter((x) => x.kind === "run");
  const open = tree.nodes.find((n) => n.id === chosen);

  // Replay plays through the steps at a steady pace, then returns to the live end.
  useEffect(() => {
    if (!playing) return undefined;
    const pace = Math.max(60, 7000 / Math.max(1, lines.length));
    const id = setInterval(() => setReplay((r) => {
      const next = (r ?? 0) + 1;
      if (next >= lines.length) { setPlaying(false); return null; }
      if (lines[next]?.kind === "run") beat(0.7);
      return next;
    }), pace);
    return () => clearInterval(id);
  }, [playing, lines]);

  useLayoutEffect(() => {
    const el = stream.current;
    if (el && stuck.current) el.scrollTop = el.scrollHeight;
  }, [shown.length]);

  async function steer() {
    setError(null); setSent(false);
    try {
      await api.steer({ session_id: sid, text, send: true });
      setText(""); setSent(true); setTimeout(() => setSent(false), 1600); beat(0.6);
      await log.reload();
    } catch (e) { setError(e); }
  }

  const count = (kind: string) => shown.filter((x) => x.kind === kind && (kind !== "run" || /\s{2,}/.test(x.text))).length;
  const replaying = replay !== null;

  return (
    <div className="lab">
      <header className="lab-head">
        <div>
          <h1 className="lab-title" style={travels("session", sid)}>{sid}</h1>
          <p className="muted small">{lab.data?.project ?? ""}{lab.data ? <> <span className="sep" /> {lab.data.notice}</> : null}</p>
        </div>
        <Loaded state={lab}>{(l) => <dl className="gauges">
          <div data-t="kind:run"><dt>Runs</dt><dd>{replaying ? count("run") : <Num n={l.runs} />}</dd></div>
          <div data-t="kind:human"><dt>Steers</dt><dd>{replaying ? count("human") : <Num n={l.steers} />}</dd></div>
          <div data-t="kind:web"><dt>Web</dt><dd>{replaying ? count("web") : <Num n={l.web_connections} />}</dd></div>
          <div><dt>Spent</dt><dd>$<Num n={l.cost_usd} digits={2} /></dd></div>
          <div><dt>Cost unknown</dt><dd><Num n={l.costs_unknown} /></dd></div>
        </dl>}</Loaded>
      </header>

      <div className="lab-floor">
        <section className="lab-graph" style={travels("graph", sid)}>
          <IdeaGraph tree={tree} root={lab.data?.project ?? sid} label="Ideas of this session" selected={chosen} onSelect={setChosen}
            onFrame={(x, y) => { if (card.current) card.current.style.transform = `translate(${Math.round(x + 18)}px, ${Math.round(y + 30)}px)`; }} />
          {open && <div className="node-card" ref={card} role="region" aria-label={open.title}>
            <div className="row spread"><b>{open.title}</b><button type="button" className="copy" onClick={() => setChosen(null)}>Close</button></div>
            <p className={`small ${open.status === "abandoned" ? "ash" : "muted"}`}>{open.status}{open.reason ? `: ${open.reason}` : ""}</p>
            {open.runs.length === 0 ? <p className="small muted">No run yet.</p> : <ul className="plain small">{open.runs.map((r) => (
              <li key={r.line} className="node-run" data-t={`line:${r.line}`}><code>{r.command}</code><span className={r.result ? "" : "amber"}>{r.result ?? "running"}</span></li>))}</ul>}
          </div>}
          <p className="graph-note">Links follow the order of the work. Drag, scroll to zoom, press an idea.</p>
        </section>

        <aside className="lab-side">
          <section className="runs">
            <h2>Runs</h2>
            <Loaded state={log}>{() => runs.length === 0 ? <p className="muted small">No run yet.</p> : (
              <RunList lines={shown} keys={keys} />)}</Loaded>
          </section>
          <section className="story-live">
            <div className="row spread"><h2>Agent</h2><button type="button" className="copy" aria-expanded={drawer} onClick={() => setDrawer(!drawer)}>Literature</button></div>
            <div ref={stream} className="log" aria-label="Story" onScroll={(e) => { const s = e.currentTarget; stuck.current = s.scrollHeight - s.scrollTop - s.clientHeight < 40; }}>
              {shown.map((x, i) => <div key={i} data-line data-t={keys[i]} className={`line line-${x.kind}`}><span className="at">{x.at}</span><span className="who">{WHO[x.kind] ?? x.who}</span><span className="said">{x.text}</span></div>)}
            </div>
            <form className="steer" onSubmit={(e) => { e.preventDefault(); if (text.trim()) void steer(); }}>
              <Field label="Your words go into the Story." value={text} onChange={setText} />
              <button type="submit" className="btn" disabled={!text.trim()}>{sent ? "Sent" : "Send"}</button>
            </form>
            {error ? <Problem error={error} /> : null}
          </section>
        </aside>
        {drawer && <aside className="drawer" aria-label="Literature">
          <div className="row spread"><h2>Literature</h2><button type="button" className="copy" onClick={() => setDrawer(false)}>Close</button></div>
          <Loaded state={log}>{(g) => <>
            {g.reading.length === 0 && <p className="muted">Nothing searched yet.</p>}
            <ul className="plain">{g.reading.map((r, i) => <li key={i} className="hair" data-t="kind:web">{r.query}
              <div className="muted small">{r.source}, <Num n={r.found} /> found</div></li>)}</ul>
            {g.web.length > 0 && <><h2>Sites reached</h2>
              <ul className="plain small">{g.web.map((h) => <li key={h} className="hair">{h}</li>)}</ul></>}
          </>}</Loaded>
        </aside>}
      </div>


      <footer className="lab-foot">
        <Scrubber lines={lines} at={at} live={!replaying} playing={playing}
          onAt={(n) => { setPlaying(false); setReplay(n >= lines.length ? null : n); }}
          onLive={() => { setPlaying(false); setReplay(null); }}
          onReplay={() => { if (calm()) { setReplay(null); return; } setChosen(null); setReplay(0); setPlaying(true); }} />
        <p className={`spend small muted ${replaying ? "replaying" : ""}`}>{replaying ? "Replaying from the Story. Nothing runs." : lab.data?.spending}</p>
      </footer>
    </div>
  );
}

function RunList({ lines, keys }: { lines: LabLine[]; keys: string[] }) {
  const rows: { i: number; text: string }[] = [];
  lines.forEach((l, i) => { if (l.kind === "run") rows.push({ i, text: l.text }); });
  const seen = new Map<string, { i: number; cmd: string; result: string | null }>();
  for (const r of rows) {
    const [cmd = r.text, result = null] = r.text.split(/\s{2,}/);
    seen.set(cmd, { i: r.i, cmd, result });
  }
  const list = [...seen.values()].reverse().slice(0, 7);
  const box = useRef<HTMLOListElement>(null);
  useFlip(box, list.map((r) => r.cmd).join());
  return (
    <ol className="run-list" ref={box}>{list.map((r) => (
      <li key={r.cmd} data-k={r.cmd} data-line data-t={keys[r.i]} className={r.result ? "" : "running"}>
        <code title={r.cmd}>{r.cmd.length > 22 ? `...${r.cmd.slice(-19)}` : r.cmd}</code><span>{r.result ?? "running"}</span>
      </li>))}</ol>);
}

