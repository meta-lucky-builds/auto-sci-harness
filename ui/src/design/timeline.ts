// The idea tree, rebuilt from the Story's lines up to a chosen step, so the Lab can replay how a session grew.
// The API gives an idea's title and state but not its parent. So a `propose` starts a new line of work, and a `branch`
// hangs from the idea last worked on. That is the order of work, and the graph's caption says so.
import type { Idea, LabLine } from "../api/types.gen";

export interface Run { line: number; command: string; result: string | null; value: number | null }
export interface Node {
  id: string; title: string; status: string; parent: string | null; born: number;
  runs: Run[]; lines: number[]; reason: string | null;
}
export interface Tree { nodes: Node[]; working: string | null; running: string | null }

const OP = /^([a-z_]+): (.*)$/;
const NUMBER = /(-?\d+(?:\.\d+)?)/;

function nameOf(rest: string, known: Map<string, Idea>): { title: string; reason: string | null } {
  if (known.has(rest)) return { title: rest, reason: null };
  const m = /^(.*) \((.*)\)$/.exec(rest);
  return m ? { title: m[1] as string, reason: m[2] as string } : { title: rest, reason: null };
}

/** One run line: the words, then two spaces, then what it measured. No result means it has started. */
export function parseRun(line: number, text: string): Run {
  const [command = text, result = null] = text.split(/\s{2,}/);
  return { line, command, result, value: result ? Number(NUMBER.exec(result)?.[1] ?? NaN) : null };
}

export function buildTree(lines: LabLine[], upto: number, ideas: Idea[] = []): Tree {
  const known = new Map(ideas.map((i) => [i.title, i]));
  const nodes = new Map<string, Node>();
  let working: string | null = null;
  lines.slice(0, upto).forEach((l, i) => {
    if (l.kind === "idea") {
      const m = OP.exec(l.text);
      if (!m) return;
      const op = m[1] as string;
      const { title, reason } = nameOf(m[2] as string, known);
      const id = title; // the title, so a tree built before or after the API's list has the same nodes
      let n = nodes.get(id);
      if (!n) {
        n = { id, title, status: "open", parent: op === "branch" ? working : null, born: i, runs: [], lines: [], reason: null };
        nodes.set(id, n);
      }
      n.lines.push(i);
      n.status = ({ abandon: "abandoned", merge: "merged", revisit: "open" } as Record<string, string>)[op] ?? n.status;
      if (op === "abandon") { n.reason = reason; working = n.parent; } else working = id;
    } else if (working) {
      const n = nodes.get(working) as Node;
      n.lines.push(i);
      if (l.kind === "run") {
        const run = parseRun(i, l.text), open = n.runs.find((r) => r.command === run.command && r.result === null);
        if (open) { open.result = run.result; open.value = run.value; n.lines.push(i); } else n.runs.push(run);
      }
    }
  });
  if (upto >= lines.length) {  // at the live end the API's word on each idea stands; one the lines do not name still shows
    for (const i of ideas) {
      const n = nodes.get(i.title);
      if (n) n.status = i.status;
      else nodes.set(i.title, { id: i.title, title: i.title, status: i.status, parent: null, born: lines.length, runs: [], lines: [], reason: null });
    }
  }
  const list = [...nodes.values()];
  const running = list.find((n) => n.runs.some((r) => r.result === null))?.id ?? null;
  return { nodes: list, working, running };
}

export interface Place { x: number; y: number }

/** A tidy tree from left to right: depth across, leaves down. The session itself is the root, under the key "". */
export function layout(tree: Tree, gapX: number, gapY: number): Map<string, Place> {
  const kids = new Map<string | null, Node[]>();
  for (const n of tree.nodes) kids.set(n.parent, [...(kids.get(n.parent) ?? []), n]);
  const at = new Map<string, Place>();
  let leaf = 0;
  const walk = (n: Node, depth: number): number => {
    const under = (kids.get(n.id) ?? []).map((k) => walk(k, depth + 1));
    const y = under.length ? ((under[0] as number) + (under[under.length - 1] as number)) / 2 : leaf++ * gapY;
    at.set(n.id, { x: (depth + 1) * gapX, y });
    return y;
  };
  const roots = (kids.get(null) ?? []).map((r) => walk(r, 0));
  at.set("", { x: 0, y: roots.length ? ((roots[0] as number) + (roots[roots.length - 1] as number)) / 2 : 0 });
  return at;
}

export const slug = (s: string) => s.replace(/[^\w-]+/g, "_");

/** Every Story line's keys for connected hover: its kind and the idea it belongs to. */
export function lineKeys(lines: LabLine[], tree: Tree): string[] {
  const owner = new Map<number, string>();
  for (const n of tree.nodes) for (const i of n.lines) owner.set(i, n.id);
  return lines.map((l, i) => {
    const who = owner.get(i);
    return `story-line kind:${l.kind} line:${i}${who === undefined ? "" : ` idea:${slug(who)}`}`;
  });
}
