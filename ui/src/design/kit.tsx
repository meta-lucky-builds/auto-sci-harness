// The small design system: everything a screen draws, so colours and rules live in one place.
import { createContext, type CSSProperties, type ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { Meta, Number as ApiNumber, Resolved } from "../api/types.gen";
import { useRolled } from "./motion";

export const MetaContext = createContext<Meta | null>(null);
export const useScripted = () => useContext(MetaContext)?.scripted ?? true; // unknown counts as not real

export function Note({ children }: { children: ReactNode }) {
  return <div className="banner">{children}</div>;
}

/** A section of a screen: a hairline and a small label, never a box. */
export function Panel({ title, children, className }: { title?: string; children: ReactNode; className?: string }) {
  return <section className={`panel ${className ?? ""}`}>{title && <h2>{title}</h2>}{children}</section>;
}

export function Problem({ error }: { error: unknown }) {
  const e = error instanceof ApiError ? error : new ApiError("Broken", error instanceof Error ? error.message : "Something broke in this page.", !(error instanceof Error));
  return <p role="alert" className={e.broken ? "broken" : "muted"}>{e.message}</p>;
}

// The last answer of each read, so a screen paints at once and a transition has something to travel to. Reads only:
// a verdict is a POST and never passes through here.
const remembered = new Map<string, unknown>();
export const rememberedRead = <R,>(key: string) => remembered.get(key) as R | undefined;
export const forgetReads = () => remembered.clear();

export function useLoad<R>(fn: () => Promise<R>, deps: unknown[], key?: string) {
  const [data, setData] = useState<R | null>(() => (key ? (remembered.get(key) as R | undefined) ?? null : null));
  const [error, setError] = useState<unknown>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const load = useCallback(() => fn().then((d) => { if (key) remembered.set(key, d); setData(d); setError(null); }, setError), deps);
  useEffect(() => { void load(); }, [load]);
  return { data, error, reload: load };
}

export function Loaded<R>({ state, children }: { state: { data: R | null; error: unknown }; children: (d: R) => ReactNode }) {
  if (state.error) return <Problem error={state.error} />;
  return state.data === null ? <p className="muted reading">Reading...</p> : <>{children(state.data)}</>;
}

/** What a number traces to on hover: its own link everywhere it appears, and the Story lines it counts. */
const COUNTS: Record<string, string> = { steps: "story-line", runs: "kind:run", steers: "kind:human", web: "kind:web", web_connections: "kind:web", found: "kind:web" };
export function numberKeys(link: string): string {
  const field = link.split("/").pop() ?? "";
  return [`n:${link}`, COUNTS[field] ?? ""].join(" ").trim();
}

const SheetContext = createContext<(n: ApiNumber) => void>(() => undefined);

/** Every number is a link: pressing it shows what the number rests on. Unknown is said, never shown as zero. */
export function Num({ n, digits, group }: { n: ApiNumber; digits?: number; group?: boolean }) {
  const open = useContext(SheetContext);
  const rolled = useRolled(n.value);
  const say = (v: number) => digits === undefined
    ? (group ? Math.round(v).toLocaleString("en-US") : String(Number.isInteger(n.value) ? Math.round(v) : v)) : v.toFixed(digits);
  const text = n.value === null || rolled === null ? "unknown" : say(rolled);
  return <button type="button" className={n.value === null ? "num unknown" : "num"} data-t={numberKeys(n.link)} aria-label={n.value === null ? "unknown" : say(n.value)} onClick={() => open(n)}>{text}</button>;
}

export function NumberSheet({ children }: { children: ReactNode }) {
  const [shown, setShown] = useState<ApiNumber | null>(null);
  const [got, setGot] = useState<Resolved | null>(null);
  const [error, setError] = useState<unknown>(null);
  const open = useCallback((n: ApiNumber) => {
    setShown(n); setGot(null); setError(null);
    api.resolve(n.link).then(setGot, setError);
  }, []);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setShown(null); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, []);
  return (
    <SheetContext.Provider value={open}>
      {children}
      {shown && (
        <aside className="sheet" aria-label="What this number rests on">
          <div className="row spread"><h2>What this number rests on</h2>
            <button type="button" className="btn quiet" onClick={() => setShown(null)}>Close</button></div>
          {error ? <Problem error={error} /> : got ? <>
            <p className="sheet-value">{String(got.value ?? "unknown")}</p>
            <p>{got.rests_on}</p>
            <pre className="small muted">{JSON.stringify(got.source, null, 1)}</pre>
          </> : <p className="muted reading">Reading...</p>}
        </aside>
      )}
    </SheetContext.Provider>
  );
}

export function Field({ label, value, onChange, area }: {
  label: string; value: string; onChange: (v: string) => void; area?: boolean;
}) {
  return (
    <label className="field">{label}
      {area ? <textarea rows={6} value={value} onChange={(e) => onChange(e.target.value)} />
        : <input value={value} onChange={(e) => onChange(e.target.value)} />}
    </label>
  );
}

/** A command a person runs. The only thing this can do is copy its text. */
export function Command({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1400); } catch { /* the text stays on screen */ }
  }
  return (
    <div className="command">
      <pre aria-label="Command"><code>{text}</code></pre>
      <button type="button" className="copy" onClick={() => void copy()}>{copied ? "Copied" : "Copy"}</button>
    </div>
  );
}

/** Colours of a verdict word. Rejected is calm; only a run that could not run is the machine's red. */
export function verdictClass(state: string): string {
  if (state === "COULD NOT RUN") return "broken";
  if (state === "NOT CERTIFIED") return "rejected";
  return "";
}

/** A name for an element that travels between two screens. Each name appears once per page. */
export function travels(kind: string, id: string): CSSProperties {
  return { viewTransitionName: `${kind}-${id.replace(/[^\w-]+/g, "_")}` } as CSSProperties;
}
