// The small design system: everything a screen draws, so colours and rules live in one place.
import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { Meta, Number as ApiNumber, Resolved } from "../api/types.gen";

export const MetaContext = createContext<Meta | null>(null);
export const useScripted = () => useContext(MetaContext)?.scripted ?? true; // unknown counts as not real

export function Note({ children }: { children: ReactNode }) {
  return <div className="banner">{children}</div>;
}

export function Panel({ title, children }: { title?: string; children: ReactNode }) {
  return <section className="panel">{title && <h2>{title}</h2>}{children}</section>;
}

export function Problem({ error }: { error: unknown }) {
  const e = error instanceof ApiError ? error : new ApiError("Broken", "Something broke in this page.", true);
  return <p role="alert" className={e.broken ? "broken" : "muted"}>{e.message}</p>;
}

export function useLoad<R>(fn: () => Promise<R>, deps: unknown[]) {
  const [data, setData] = useState<R | null>(null);
  const [error, setError] = useState<unknown>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const load = useCallback(() => fn().then((d) => { setData(d); setError(null); }, setError), deps);
  useEffect(() => { void load(); }, [load]);
  return { data, error, reload: load };
}

export function Loaded<R>({ state, children }: { state: { data: R | null; error: unknown }; children: (d: R) => ReactNode }) {
  if (state.error) return <Problem error={state.error} />;
  return state.data === null ? <p className="muted">Reading...</p> : <>{children(state.data)}</>;
}

const SheetContext = createContext<(n: ApiNumber) => void>(() => undefined);

/** Every number is a link: pressing it shows what the number rests on. Unknown is said, never shown as zero. */
export function Num({ n, digits }: { n: ApiNumber; digits?: number }) {
  const open = useContext(SheetContext);
  const text = n.value === null ? "unknown" : digits === undefined ? String(n.value) : n.value.toFixed(digits);
  return <button type="button" className="num" onClick={() => open(n)}>{text}</button>;
}

export function NumberSheet({ children }: { children: ReactNode }) {
  const [shown, setShown] = useState<ApiNumber | null>(null);
  const [got, setGot] = useState<Resolved | null>(null);
  const [error, setError] = useState<unknown>(null);
  const open = useCallback((n: ApiNumber) => {
    setShown(n); setGot(null); setError(null);
    api.resolve(n.link).then(setGot, setError);
  }, []);
  return (
    <SheetContext.Provider value={open}>
      {children}
      {shown && (
        <aside className="sheet" aria-label="What this number rests on">
          <div className="row spread"><h2>What this number rests on</h2>
            <button type="button" className="btn" onClick={() => setShown(null)}>Close</button></div>
          {error ? <Problem error={error} /> : got ? <>
            <p><span className="num">{String(got.value ?? "unknown")}</span></p>
            <p>{got.rests_on}</p>
            <pre className="small muted">{JSON.stringify(got.source, null, 1)}</pre>
          </> : <p className="muted">Reading...</p>}
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

export function Command({ text }: { text: string }) {
  return <pre aria-label="Command"><code>{text}</code></pre>;
}

/** Colours of a verdict word. Rejected is calm; only a run that could not run is the machine's red. */
export function verdictClass(state: string): string {
  if (state === "COULD NOT RUN") return "broken";
  if (state === "NOT CERTIFIED") return "rejected";
  return "";
}
