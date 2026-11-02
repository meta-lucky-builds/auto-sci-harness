import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { api, watchLab } from "./api/client";
import type { Meta } from "./api/types.gen";
import { Heartbeat } from "./design/Heartbeat";
import { MetaContext, NumberSheet, Problem } from "./design/kit";
import { beat, calm, installPointerLight, installTrace } from "./design/motion";
import { Palette } from "./design/Palette";
import { Peek } from "./design/Peek";
import { Approvals, PaperBuilder, RecordScreen, Trust } from "./screens/Court";
import { Home } from "./screens/Home";
import { Lab } from "./screens/Lab";
import { Promote } from "./screens/Promote";
import { Rehearse } from "./screens/Rehearse";
import { StoryScreen } from "./screens/Story";
import { Verdict } from "./screens/Verdict";

const NAV = [["#/", "Today"], ["#/lab", "Lab"], ["#/story", "Story"], ["#/rehearse", "Rehearse"], ["#/promote", "Promote"],
  ["#/waiting", "Waiting"], ["#/record", "Record"], ["#/trust", "Trust board"], ["#/paper", "Paper"]] as const;
const MOOD: Record<string, string> = { "": "lab", lab: "lab", rehearse: "lab", promote: "lab", case: "court" };

/** A move between screens is one continuous change where the browser can do it, with shared parts travelling across. */
function useHash(): string {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => {
      const next = window.location.hash;
      const change = () => flushSync(() => setHash(next));
      const go = (document as Document & { startViewTransition?: (f: () => void) => unknown }).startViewTransition;
      if (go && !calm()) go.call(document, change); else change();
    };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export function Screen({ page, arg }: { page: string; arg?: string }) {
  switch (page) {
    case "lab": return <Lab sessionId={arg} />;
    case "story": return <StoryScreen sessionId={arg} />;
    case "rehearse": return <Rehearse key={arg} promotionId={arg} />;
    case "promote": return <Promote key={arg} promotionId={arg} />;
    case "case": return arg ? <Verdict key={arg} caseId={arg} /> : <Home />;
    case "record": return <RecordScreen />;
    case "waiting": return <Approvals />;
    case "trust": return <Trust />;
    case "paper": return <PaperBuilder caseId={arg} />;
    default: return <Home />;
  }
}

/** The heart listens to the newest session from every screen. It stops at the Court's door. */
function Pulse({ active }: { active: boolean }) {
  useEffect(() => {
    if (!active) return undefined;
    let stop = (): void => undefined, gone = false, count = -1;
    api.sessions().then((s) => {
      const sid = s.sessions[0]?.session_id;
      if (gone || !sid) return;
      stop = watchLab(sid, () => {
        api.labLog(sid).then((l) => {
          if (count >= 0 && l.lines.length > count) beat(l.lines[l.lines.length - 1]?.kind === "run" ? 1 : 0.4);
          count = l.lines.length;
        }, () => undefined);
      });
    }, () => undefined);
    return () => { gone = true; stop(); };
  }, [active]);
  return null;
}

/** One small marker under the current page, gliding between links. */
function Nav({ page }: { page: string }) {
  const box = useRef<HTMLElement>(null);
  const mark = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const m = mark.current, a = box.current?.querySelector<HTMLElement>('a[aria-current="page"]');
    if (!m) return;
    m.style.opacity = a ? "1" : "0";
    if (a) { m.style.width = `${a.offsetWidth}px`; m.style.transform = `translateX(${a.offsetLeft}px)`; }
  });
  return (
    <nav ref={box} className="nav" aria-label="Main">
      {NAV.map(([to, label]) => (
        <a key={to} href={to} aria-current={to.slice(2) === page ? "page" : undefined}>{label}</a>))}
      <span ref={mark} className="nav-mark" aria-hidden="true" />
    </nav>
  );
}

export function App() {
  const hash = useHash();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [palette, setPalette] = useState(false);
  useEffect(() => { api.meta().then(setMeta, setError); }, []);
  const [page = "", rawArg] = hash.replace(/^#\/?/, "").split("/");
  const arg = rawArg ? decodeURIComponent(rawArg) : undefined;
  const mood = MOOD[page] ?? "hall";
  useEffect(() => { document.body.dataset.mood = mood; document.body.dataset.page = page || "today"; }, [mood, page]);
  useEffect(() => {
    const undo = [installPointerLight(), installTrace()];
    const key = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPalette((p) => !p); }
    };
    window.addEventListener("keydown", key);
    return () => { undo.forEach((f) => f()); window.removeEventListener("keydown", key); };
  }, []);
  return (
    <MetaContext.Provider value={meta}>
      <div className="shell">
        <header className="bar">
          <a className="brand" href="#/" aria-label="Harness, go to Today"><span className="brand-mark" aria-hidden="true" />Harness</a>
          <Nav page={page} />
          <div className="bar-end">
            {meta?.scripted && <span className="badge badge-example">example</span>}
            <button type="button" className="find" onClick={() => setPalette(true)}>Find<kbd>Ctrl K</kbd></button>
          </div>
          <Heartbeat />
          <Pulse active={mood !== "court"} />
        </header>
        <main className="main">
          {error ? <Problem error={error} /> : null}
          {meta && <NumberSheet><Screen page={page} arg={arg} /></NumberSheet>}
        </main>
        {meta?.notice && <footer className="notice">{meta.notice}</footer>}
      </div>
      <Peek />
      {palette && <Palette onClose={() => setPalette(false)} />}
    </MetaContext.Provider>
  );
}
