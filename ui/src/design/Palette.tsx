// Ctrl+K: find anywhere, read what it is, go there. Results narrow as you type and the page behind lights what you are
// on. It navigates and explains. It performs nothing: no entry approves, promotes, or opens a verdict. A test holds that.
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { rememberedRead } from "./kit";
import { traceKeys } from "./motion";
import { slug } from "./timeline";

export interface Entry { kind: string; title: string; explain: string; href?: string; command?: string; trace?: string[] }

const SCREENS: Entry[] = [
  ["Today", "#/", "What needs you, and where your work stands."],
  ["Lab", "#/lab", "The live session: the tree of ideas, every run, and the Story as it is written."],
  ["Story", "#/story", "One thread through a session, each step open to read."],
  ["Rehearse", "#/rehearse", "A mock trial on Lab data. It shows everything and spends nothing."],
  ["Promote", "#/promote", "The moment an idea leaves the Lab. Its claim and code are fixed for the Court."],
  ["Waiting", "#/waiting", "Steps that need a person. They are taken in a terminal, never on a page."],
  ["Record", "#/record", "What was tried, what failed, and how your stated chances held up."],
  ["Trust board", "#/trust", "What this machine has checked, and what is not true yet."],
  ["Paper", "#/paper", "A paper whose numbers come from the record, never typed by hand."],
].map(([title, href, explain]) => ({ kind: "Screen", title, href, explain }) as Entry);

const WORDS: Entry[] = [
  ["Look", "Each time a person opens a verdict on a test dataset, that is one look. Looks are counted, because each one teaches something about the data."],
  ["Sealed data", "Part of your data set aside before work starts. Nothing in the Lab can read it."],
  ["The Lab", "Where agents explore with full power. Nothing here is evidence."],
  ["The Court", "Runs a fixed claim on data the Lab has never seen, and gives the verdict."],
  ["Story", "The record of a session: every idea, run, steer and web connection, in order."],
  ["Steer", "Your words to the agent. They always go into the Story."],
  ["Rehearsal", "A mock trial on Lab data. A pass promises nothing on fresh data."],
  ["Readiness", "A rough estimate from Lab numbers. It can warn; it never promises."],
  ["Validated", "The Court's badge for a claim that held on fresh test data."],
  ["Re-validated", "The same claim held again on a second sealed part."],
  ["Approvals", "Only a person may approve, from a terminal. This page lists the steps and shows the commands."],
].map(([title, explain]) => ({ kind: "Word", title, explain }) as Entry);

type Read = { cases?: { case_id: string; title: string }[]; sessions?: { session_id: string; project: string | null }[] };

async function liveEntries(): Promise<Entry[]> {
  const [cases, sessions, frozen, waiting] = await Promise.all([api.cases(), api.sessions(), api.frozen(), api.waiting()]);
  const out: Entry[] = [];
  for (const s of sessions.sessions) out.push({ kind: "Session", title: s.session_id, href: `#/lab/${encodeURIComponent(s.session_id)}`, explain: `A Lab session on ${s.project ?? "no project"}.` });
  for (const c of cases.cases) out.push({ kind: "Case", title: c.title, href: `#/case/${encodeURIComponent(c.case_id)}`, explain: `${c.state} Going to a case does not open its verdict.` });
  for (const f of frozen.items) out.push({ kind: "Frozen idea", title: f.title, href: `#/promote/${f.promotion_id}`, explain: f.sentence });
  for (const w of waiting.items) out.push({ kind: "Waiting", title: w.title, href: "#/waiting", explain: `${w.why} ${w.human_only ? "Only a person may do this, in a terminal." : ""}`, command: w.command });
  return out;
}

function ideaEntries(): Entry[] {
  const m = /^#\/lab(?:\/(.+))?$/.exec(window.location.hash);
  const sid = m ? (m[1] ? decodeURIComponent(m[1]) : rememberedRead<{ sessions: { session_id: string }[] }>("sessions")?.sessions[0]?.session_id) : undefined;
  const lab = sid ? rememberedRead<{ ideas: { idea_id: string; title: string; status: string }[] }>(`lab:${sid}`) : undefined;
  return (lab?.ideas ?? []).map((i) => ({ kind: "Idea", title: i.title, explain: `An idea in this session, ${i.status}. It is lit on the page behind.`, trace: [`idea:${slug(i.title)}`] }));
}

/** Letters in order, with a bonus for a word start. The first match wins ties, so the list stays where it is. */
export function score(q: string, text: string): number {
  if (!q) return 1;
  const t = text.toLowerCase(), s = q.toLowerCase();
  const i = t.indexOf(s);
  if (i >= 0) return 100 - i + (i === 0 || t[i - 1] === " " ? 50 : 0);
  let at = -1, run = 0;
  for (const c of s) { at = t.indexOf(c, at + 1); if (at < 0) return 0; run += 1; }
  return run;
}

export function Palette({ onClose }: { onClose: () => void }) {
  const [q, setQ] = useState("");
  const [at, setAt] = useState(0);
  const [live, setLive] = useState<Entry[]>([]);
  const input = useRef<HTMLInputElement>(null);
  const before = useRef<Element | null>(document.activeElement);
  useEffect(() => {
    input.current?.focus();
    liveEntries().then(setLive, () => undefined);
    const el = before.current;
    return () => { traceKeys([]); (el as HTMLElement | null)?.focus?.(); };
  }, []);
  const ideas = useMemo(ideaEntries, []);
  const found = useMemo(() => [...SCREENS, ...live, ...ideas, ...WORDS]
    .map((e) => ({ e, s: Math.max(score(q, e.title), score(q, e.kind) / 2) })).filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s).slice(0, 9).map((x) => x.e), [q, live, ideas]);
  const on = found[Math.min(at, found.length - 1)];
  useEffect(() => { traceKeys(on?.trace ?? []); }, [on]);

  function go(e: Entry | undefined) {
    if (!e?.href) return;
    onClose();
    window.location.hash = e.href.slice(1);
  }
  function key(e: React.KeyboardEvent) {
    if (e.key === "Escape") { e.stopPropagation(); onClose(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setAt((a) => Math.min(found.length - 1, a + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setAt((a) => Math.max(0, a - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); go(on); }
  }

  return (
    <div className="palette-back" onMouseDown={onClose}>
      <div className="palette" role="dialog" aria-label="Find anything" onMouseDown={(e) => e.stopPropagation()}>
        <input ref={input} className="palette-in" value={q} placeholder="Go to, or ask what something means" aria-label="Find anything"
          role="combobox" aria-expanded="true" aria-controls="palette-list"
          onChange={(e) => { setQ(e.target.value); setAt(0); }} onKeyDown={key} />
        <div className="palette-body">
          <ul id="palette-list" role="listbox" aria-label="Results">
            {found.map((e, i) => (
              <li key={`${e.kind}${e.title}`} role="option" aria-selected={e === on} className={e === on ? "on" : ""}
                onMouseEnter={() => setAt(i)} onClick={() => go(e)}>
                <span>{e.title}</span><small>{e.kind}</small></li>))}
            {found.length === 0 && <li className="none">Nothing by that name.</li>}
          </ul>
          <div className="palette-say" aria-live="polite">
            {on && <><h2>{on.title}</h2><p>{on.explain}</p>
              {on.command && <pre><code>{on.command}</code></pre>}
              {on.command && <p className="small muted">This is only shown here. Run it yourself in a terminal.</p>}
              {on.href && <p className="small muted">Enter goes there.</p>}</>}
          </div>
        </div>
      </div>
    </div>
  );
}
export type { Read };
