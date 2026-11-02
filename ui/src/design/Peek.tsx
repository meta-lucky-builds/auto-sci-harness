// Hover a link and see what it opens, from reads that spend nothing. A case shows where it stands, never its verdict:
// this file may read lists only, and a static test keeps `openCase` and every other POST out of it.
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { Cases, FrozenIdeas, LabView, Sessions, TrustBoard, Waiting } from "../api/types.gen";
import { rememberedRead } from "./kit";
import { calm, still } from "./motion";

export interface Glance { title: string; lines: string[] }

export async function glance(href: string): Promise<Glance | null> {
  const [page = "", raw] = href.replace(/^#\/?/, "").split("/");
  const arg = raw ? decodeURIComponent(raw) : undefined;
  const nOf = (n: { value: number | null }) => String(n.value ?? "unknown");
  switch (page) {
    case "": { const w = await api.waiting(); return { title: "Today", lines: [`${nOf(w.total)} waiting for you`] }; }
    case "lab": case "story": {
      const sid = arg ?? (rememberedRead<Sessions>("sessions") ?? await api.sessions()).sessions[0]?.session_id;
      if (!sid) return { title: page === "lab" ? "Lab" : "Story", lines: ["No session yet"] };
      const l: LabView = await api.lab(sid);
      return { title: sid, lines: [`${l.ideas.length} ideas, ${nOf(l.open_ideas)} open`, `${nOf(l.runs)} runs`] };
    }
    case "case": {
      const c = (rememberedRead<Cases>("cases") ?? await api.cases()).cases.find((x) => x.case_id === arg);
      return c ? { title: c.title, lines: [c.state, `Looks left: ${nOf(c.looks_remaining)}`] } : null;
    }
    case "rehearse": case "promote": {
      const f = rememberedRead<FrozenIdeas>("frozen") ?? await api.frozen();
      const i = f.items.find((x) => x.promotion_id === arg) ?? f.items[0];
      return i ? { title: i.title, lines: [i.sentence] } : { title: page, lines: ["Nothing frozen yet"] };
    }
    case "waiting": { const w: Waiting = await api.waiting(); return { title: "Waiting", lines: w.items.slice(0, 3).map((i) => i.title) }; }
    case "trust": { const t: TrustBoard = await api.trust(); return { title: "Trust board", lines: [`Steps left before a first session: ${nOf(t.steps_left)}`] }; }
    case "record": return { title: "Record", lines: ["What was tried, what failed, how your chances held up"] };
    case "paper": return { title: "Paper", lines: ["Numbers from the record, never typed by hand"] };
    default: return null;
  }
}

export function Peek() {
  const [card, setCard] = useState<(Glance & { x: number; y: number }) | null>(null);
  const timer = useRef(0);
  useEffect(() => {
    let current: string | null = null;
    const over = (e: PointerEvent) => {
      const a = (e.target as Element | null)?.closest?.('a[href^="#/"]') as HTMLAnchorElement | null;
      const href = a?.getAttribute("href") ?? null;
      if (href === current) return;
      current = href;
      clearTimeout(timer.current);
      setCard(null);
      if (!a || !href || still() || e.pointerType === "touch" || href === window.location.hash) return;
      timer.current = window.setTimeout(() => {
        glance(href).then((g) => {
          if (!g || current !== href) return;
          const r = a.getBoundingClientRect();
          setCard({ ...g, x: Math.min(window.innerWidth - 320, r.left), y: r.bottom + 10 > window.innerHeight - 120 ? r.top - 104 : r.bottom + 10 });
        }, () => undefined);
      }, 260);
    };
    const clear = () => { current = null; clearTimeout(timer.current); setCard(null); };
    document.addEventListener("pointerover", over);
    window.addEventListener("hashchange", clear);
    window.addEventListener("scroll", clear, { passive: true });
    return () => {
      document.removeEventListener("pointerover", over);
      window.removeEventListener("hashchange", clear);
      window.removeEventListener("scroll", clear);
      clearTimeout(timer.current);
    };
  }, []);
  if (!card) return null;
  return (
    <div className={`peek ${calm() ? "" : "peek-in"}`} style={{ left: card.x, top: card.y }} role="tooltip">
      <b>{card.title}</b>
      {card.lines.map((l) => <span key={l}>{l}</span>)}
    </div>
  );
}
