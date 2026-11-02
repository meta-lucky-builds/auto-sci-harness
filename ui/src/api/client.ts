// The UI's only way out of the browser: the local server's /api routes. Nothing else may call fetch.
import type * as T from "./types.gen";

export class ApiError extends Error {
  constructor(readonly code: string, message: string, readonly broken: boolean) {
    super(message);
  }
}

function token(): string {
  return document.querySelector('meta[name="chh-token"]')?.getAttribute("content") ?? "";
}

async function answer<R>(res: Promise<Response>): Promise<R> {
  let r: Response;
  try {
    r = await res;
  } catch {
    throw new ApiError("Unreachable", "The local server does not answer. Is `chh exp ui` still running?", true);
  }
  const body = await r.json().catch(() => null);
  if (!r.ok) {
    throw new ApiError(body?.code ?? "Broken", body?.message ?? "Something broke on this machine.", r.status >= 500);
  }
  return body as R;
}

function get<R>(name: string, query: Record<string, string | undefined> = {}): Promise<R> {
  const q = new URLSearchParams(Object.entries(query).filter((e): e is [string, string] => e[1] !== undefined));
  return answer<R>(fetch(`/api/${name}${q.size ? `?${q}` : ""}`));
}

function post<R>(name: string, body: unknown): Promise<R> {
  return answer<R>(fetch(`/api/${name}`, {
    method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-CHH-Token": token() },
  }));
}

export const api = {
  meta: () => get<T.Meta>("meta"),
  waiting: () => get<T.Waiting>("waiting"),
  cases: () => get<T.Cases>("cases"),
  sessions: () => get<T.Sessions>("sessions"),
  frozen: () => get<T.FrozenIdeas>("frozen"),
  trust: () => get<T.TrustBoard>("trust"),
  record: (query?: string) => get<T.Record>("record", { query }),
  resolve: (target: string) => get<T.Resolved>("resolve", { target }),
  lab: (session_id: string) => get<T.LabView>("lab", { session_id }),
  labLog: (session_id: string) => get<T.LabLog>("lab_log", { session_id }),
  /** Points to a human step. It writes nothing; there is no route that takes one. */
  howTo: (item_id: string) => get<T.ApprovalRoute>("approve", { item_id }),
  openCase: (case_id: string) => post<T.Case>("case", { case_id }),
  technical: (case_id: string) => post<T.TechnicalView>("technical", { case_id }),
  rehearse: (req: T.RehearseIn) => post<T.Rehearsal>("rehearse", req),
  preview: (req: T.PromoteIn) => post<T.PromotePreview>("promote_preview", req),
  promote: (req: T.PromoteIn) => post<T.Promoted>("promote", req),
  steer: (req: T.SteerIn) => post<T.Steered>("steer", req),
  share: (req: T.ShareIn) => post<T.Shared>("share", req),
  paper: (req: T.PaperIn) => post<T.Paper>("paper", req),
};

/** Tells `onChange` when a session's Story grew. The event names no content: the page reads the API again.
 *  One stream per session, however many parts of the page listen: a browser allows few connections to one server. */
const streams = new Map<string, { es: EventSource; subs: Set<() => void> }>();
export function watchLab(sessionId: string, onChange: () => void): () => void {
  let s = streams.get(sessionId);
  if (!s) {
    const subs = new Set<() => void>();
    const es = new EventSource(`/api/live?session_id=${encodeURIComponent(sessionId)}`);
    es.addEventListener("lab", () => subs.forEach((f) => f()));
    s = { es, subs };
    streams.set(sessionId, s);
  }
  const held = s;
  held.subs.add(onChange);
  return () => {
    held.subs.delete(onChange);
    if (held.subs.size === 0) { held.es.close(); streams.delete(sessionId); }
  };
}
