import { useEffect, useState } from "react";
import { api } from "./api/client";
import type { Meta } from "./api/types.gen";
import { MetaContext, Note, NumberSheet, Problem } from "./design/kit";
import { Approvals, PaperBuilder, RecordScreen, Trust } from "./screens/Court";
import { Home } from "./screens/Home";
import { Lab } from "./screens/Lab";
import { Promote } from "./screens/Promote";
import { Rehearse } from "./screens/Rehearse";
import { Verdict } from "./screens/Verdict";

const NAV = [["#/", "Today"], ["#/lab", "Lab"], ["#/rehearse", "Rehearse"], ["#/promote", "Promote"], ["#/record", "Record"],
  ["#/waiting", "Waiting"], ["#/trust", "Trust board"], ["#/paper", "Paper"]] as const;
const LAB_MOOD = new Set(["", "lab", "rehearse"]);

function useHash(): string {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export function Screen({ page, arg }: { page: string; arg?: string }) {
  switch (page) {
    case "lab": return <Lab sessionId={arg} />;
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

export function App() {
  const hash = useHash();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { api.meta().then(setMeta, setError); }, []);
  const [page = "", rawArg] = hash.replace(/^#\/?/, "").split("/");
  const arg = rawArg ? decodeURIComponent(rawArg) : undefined;
  useEffect(() => { document.body.dataset.mood = LAB_MOOD.has(page) ? "lab" : "court"; }, [page]);
  return (
    <MetaContext.Provider value={meta}>
      <div className="shell">
        <nav className="nav" aria-label="Main">
          <div className="brand">Harness</div>
          {NAV.map(([to, label]) => (
            <a key={to} href={to} aria-current={to.slice(2) === page ? "page" : undefined}>{label}</a>))}
        </nav>
        <main className="main">
          {error ? <Problem error={error} /> : null}
          {meta?.notice && <Note>{meta.notice}</Note>}
          {meta && <NumberSheet><Screen page={page} arg={arg} /></NumberSheet>}
        </main>
      </div>
    </MetaContext.Provider>
  );
}
