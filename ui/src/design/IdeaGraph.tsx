// The idea tree: real SVG, so each idea is a focusable, labelled control. React draws what exists; a small spring
// engine moves it, writing straight to the elements, and rests when nothing moves. This file may not use the green:
// the Lab certifies nothing. A static test holds that.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { calm, labRate } from "./motion";
import { layout, type Node, slug, type Tree } from "./timeline";

const STIFF = 170, DAMP = 23, CARRY = 0.72, DT = 1 / 60;
const short = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}...` : s);

interface Body { x: number; y: number; vx: number; vy: number; tx: number; ty: number; ox: number; oy: number; s: number; vs: number; held: boolean }
interface Pulse { id: string; t: number; speed: number; big: boolean }

export interface Move { select: (id: string | null) => void }

export function IdeaGraph({ tree, root, compact, selected, onSelect, onFrame, label }: {
  tree: Tree; root: string; compact?: boolean; selected?: string | null; onSelect?: (id: string | null) => void;
  onFrame?: (x: number, y: number) => void; label: string;
}) {
  const svg = useRef<SVGSVGElement>(null);
  const world = useRef<SVGGElement>(null);
  const pulseLayer = useRef<SVGGElement>(null);
  const bodies = useRef(new Map<string, Body>());
  const view = useRef({ x: 0, y: 0, k: 1, tx: 0, ty: 0, tk: 1, user: false });
  const pulses = useRef<Pulse[]>([]);
  const frame = useRef(0);
  const wake = useRef<() => void>(() => undefined);
  const treeRef = useRef(tree);
  const selectedRef = useRef(selected);
  const frameRef = useRef(onFrame);
  treeRef.current = tree; selectedRef.current = selected; frameRef.current = onFrame;
  const [, repaint] = useState(0);

  const gapX = compact ? 168 : 210, gapY = compact ? 44 : 52;
  const places = useMemo(() => layout(tree, gapX, gapY), [tree, gapX, gapY]);
  const parents = useMemo(() => new Map(tree.nodes.map((n) => [n.id, n.parent ?? ""])), [tree]);

  const bezier = (a: Body, b: Body, t: number) => {
    const mx = (a.x + b.x) / 2, u = 1 - t;
    return {
      x: u * u * u * a.x + 3 * u * u * t * mx + 3 * u * t * t * mx + t * t * t * b.x,
      y: u * u * u * a.y + 3 * u * u * t * a.y + 3 * u * t * t * b.y + t * t * t * b.y,
    };
  };

  /** Fits the tree in the frame, unless the person has moved the view. */
  const fit = (force = false) => {
    const el = svg.current, v = view.current;
    if (!el || (v.user && !force)) return;
    if (force) v.user = false;
    const w = el.clientWidth || 800, h = el.clientHeight || 500;
    let x0 = 0, x1 = 0, y0 = Infinity, y1 = -Infinity;
    for (const p of places.values()) { x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); y0 = Math.min(y0, p.y); y1 = Math.max(y1, p.y); }
    if (y0 === Infinity) { y0 = 0; y1 = 0; }
    const right = compact ? 150 : 230, pad = compact ? 16 : 44;
    const room = Math.min(compact ? 1.25 : 1.15, (w - pad * 2) / (x1 - x0 + right), (h - pad * 2) / (y1 - y0 + 30 || 1));
    v.tk = compact ? room : Math.max(0.6, room); // on a small screen the words stay readable and the tree pans
    v.tx = pad - x0 * v.tk;
    v.ty = h / 2 - ((y0 + y1) / 2) * v.tk;
  };

  // New ideas grow out of their parent; everything else eases to its new place.
  useLayoutEffect(() => {
    const m = bodies.current;
    for (const [id, p] of places) {
      const b = m.get(id);
      if (b) { b.tx = p.x; b.ty = p.y; continue; }
      const from = m.get(parents.get(id) ?? "") ?? { x: p.x, y: p.y };
      m.set(id, { x: from.x, y: from.y, vx: 0, vy: 0, tx: p.x, ty: p.y, ox: 0, oy: 0, s: id === "" ? 1 : 0, vs: 0, held: false });
    }
    for (const id of m.keys()) if (!places.has(id)) m.delete(id);
    fit();
    if (calm()) {
      for (const b of m.values()) { b.x = b.tx + b.ox; b.y = b.ty + b.oy; b.s = 1; }
      const v = view.current; v.x = v.tx; v.y = v.ty; v.k = v.tk;
    }
    wake.current();
  }, [places]); // eslint-disable-line react-hooks/exhaustive-deps

  // A run that starts sends light down the path; while it runs, the light keeps coming. A result sends one bright pulse.
  const seen = useRef(new Map<string, number>());
  useEffect(() => {
    for (const n of tree.nodes) {
      const done = n.runs.filter((r) => r.result !== null).length, was = seen.current.get(n.id);
      if (was !== undefined && done > was) pulses.current.push({ id: n.id, t: 0, speed: 1.5, big: true });
      seen.current.set(n.id, done);
    }
    for (const id of seen.current.keys()) if (!tree.nodes.some((n) => n.id === id)) seen.current.delete(id);
    wake.current();
  }, [tree]);

  useEffect(() => {
    const el = svg.current;
    if (!el) return undefined;
    let running = false, last = 0, spare = 0, sinceRun = 1;

    const step = () => {
      const m = bodies.current, v = view.current, rate = labRate();
      const dt = DT * rate;
      let moving = false;
      const order = [...m.keys()].sort((a, b) => (a === "" ? -1 : b === "" ? 1 : 0));
      const depth = (id: string) => { let d = 0, c = id; while (c !== "" && d < 40) { c = parents.get(c) ?? ""; d += 1; } return d; };
      order.sort((a, b) => depth(a) - depth(b));
      for (const id of order) {
        const b = m.get(id) as Body, p = id === "" ? undefined : m.get(parents.get(id) ?? "");
        if (!b.held) {
          const cx = p ? (p.x - (p.tx + p.ox)) * CARRY : 0, cy = p ? (p.y - (p.ty + p.oy)) * CARRY : 0;
          b.vx += (STIFF * (b.tx + b.ox + cx - b.x) - DAMP * b.vx) * dt;
          b.vy += (STIFF * (b.ty + b.oy + cy - b.y) - DAMP * b.vy) * dt;
          b.x += b.vx * dt; b.y += b.vy * dt;
        }
        b.vs += (STIFF * (1 - b.s) - DAMP * b.vs) * dt;
        b.s += b.vs * dt;
        if (Math.abs(b.vx) + Math.abs(b.vy) + Math.abs(b.vs) > 0.02 || b.held) moving = true;
      }
      for (const k of ["x", "y", "k"] as const) {
        const t = v[`t${k}` as "tx" | "ty" | "tk"], gap = t - v[k];
        v[k] += gap * (v.user ? 0.35 : 0.08) * rate;
        if (Math.abs(gap) > (k === "k" ? 0.0005 : 0.05)) moving = true;
      }
      const running_ = treeRef.current.running;
      sinceRun += dt;
      if (running_ && !calm() && sinceRun > 0.8 && rate > 0.5) { sinceRun = 0; pulses.current.push({ id: running_, t: 0, speed: 0.9, big: false }); }
      pulses.current = pulses.current.filter((q) => { q.t += q.speed * dt; return q.t < 1 && m.has(q.id); });
      return moving || pulses.current.length > 0 || (running_ !== null && !calm());
    };

    const paint = () => {
      const m = bodies.current, v = view.current;
      world.current?.setAttribute("transform", `translate(${v.x.toFixed(2)} ${v.y.toFixed(2)}) scale(${v.k.toFixed(4)})`);
      el.querySelectorAll<SVGGElement>("[data-node]").forEach((g) => {
        const b = m.get(g.dataset.node as string);
        if (b) g.setAttribute("transform", `translate(${b.x.toFixed(2)} ${b.y.toFixed(2)}) scale(${Math.max(0, b.s).toFixed(3)})`);
      });
      el.querySelectorAll<SVGPathElement>("[data-edge]").forEach((p) => {
        const id = p.dataset.edge as string, b = m.get(id), a = m.get(parents.get(id) ?? "");
        if (!a || !b) return;
        const mx = ((a.x + b.x) / 2).toFixed(1);
        p.setAttribute("d", `M${a.x.toFixed(1)} ${a.y.toFixed(1)}C${mx} ${a.y.toFixed(1)} ${mx} ${b.y.toFixed(1)} ${b.x.toFixed(1)} ${b.y.toFixed(1)}`);
      });
      const layer = pulseLayer.current;
      if (layer) {
        while (layer.childElementCount < pulses.current.length) {
          const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
          c.setAttribute("class", "pulse");
          layer.appendChild(c);
        }
        [...layer.children].forEach((c, i) => {
          const q = pulses.current[i];
          if (!q) { c.setAttribute("r", "0"); return; }
          const b = m.get(q.id), a = m.get(parents.get(q.id) ?? "");
          if (!a || !b) return;
          const at = bezier(a, b, q.t), fade = Math.sin(q.t * Math.PI);
          c.setAttribute("cx", at.x.toFixed(1)); c.setAttribute("cy", at.y.toFixed(1));
          c.setAttribute("r", ((q.big ? 3.4 : 2.2) * (0.5 + fade / 2)).toFixed(2));
          c.setAttribute("opacity", fade.toFixed(2));
        });
      }
      const sel = selectedRef.current, b = sel !== undefined && sel !== null ? m.get(sel) : undefined;
      if (b) frameRef.current?.(b.x * v.k + v.x, b.y * v.k + v.y);
    };

    const tick = (t: number) => {
      frame.current = 0;
      spare = Math.min(spare + (last ? t - last : 16.7), 50);
      last = t;
      let on = false;
      while (spare >= 16.6) { on = step(); spare -= 16.7; }
      paint();
      if (on || labRate() < 1) frame.current = requestAnimationFrame(tick); else { running = false; last = 0; }
    };
    wake.current = () => {
      paint();
      if (!running) { running = true; frame.current = requestAnimationFrame(tick); }
    };
    wake.current();
    const size = typeof ResizeObserver === "function" ? new ResizeObserver(() => { fit(); wake.current(); }) : null;
    size?.observe(el);
    return () => { cancelAnimationFrame(frame.current); running = false; size?.disconnect(); };
  }, [parents]); // eslint-disable-line react-hooks/exhaustive-deps

  // Pan, zoom, drag. A press that does not move is a click.
  useEffect(() => {
    const el = svg.current;
    if (!el || compact) return undefined;
    const zoomAt = (px: number, py: number, by: number) => {
      const v = view.current, k = Math.min(3, Math.max(0.3, v.tk * by));
      v.tx = px - ((px - v.tx) / v.tk) * k; v.ty = py - ((py - v.ty) / v.tk) * k; v.tk = k; v.user = true;
      wake.current();
    };
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      if (e.ctrlKey || Math.abs(e.deltaY) >= Math.abs(e.deltaX)) zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * 0.0016));
    };
    let grab: { id: string | null; sx: number; sy: number; x: number; y: number; moved: boolean; pid: number } | null = null;
    const down = (e: PointerEvent) => {
      if (e.button !== 0) return;
      const g = (e.target as Element).closest<SVGGElement>("[data-node]");
      const id = g?.dataset.node ?? null, v = view.current;
      const b = id !== null ? bodies.current.get(id) : undefined;
      grab = { id, sx: e.clientX, sy: e.clientY, x: b ? b.x : v.tx, y: b ? b.y : v.ty, moved: false, pid: e.pointerId };
      if (b) b.held = true;
      el.setPointerCapture(e.pointerId);
      el.classList.add("grabbing");
      wake.current();
    };
    const move = (e: PointerEvent) => {
      if (!grab) return;
      const dx = e.clientX - grab.sx, dy = e.clientY - grab.sy, v = view.current;
      if (Math.abs(dx) + Math.abs(dy) > 4) grab.moved = true;
      if (!grab.moved) return;
      if (grab.id !== null) {
        const b = bodies.current.get(grab.id);
        if (b) { b.x = grab.x + dx / v.k; b.y = grab.y + dy / v.k; b.vx = 0; b.vy = 0; }
      } else { v.tx = grab.x + dx; v.ty = grab.y + dy; v.user = true; }
      wake.current();
    };
    const up = () => {
      if (!grab) return;
      const b = grab.id !== null ? bodies.current.get(grab.id) : undefined;
      if (b) { b.held = false; if (grab.moved) { b.ox = b.x - b.tx; b.oy = b.y - b.ty; } }
      if (!grab.moved) onSelectRef.current?.(grab.id === "" ? null : grab.id);
      grab = null;
      el.classList.remove("grabbing");
      wake.current();
    };
    const key = (e: KeyboardEvent) => {
      const r = el.getBoundingClientRect();
      if (e.key === "+" || e.key === "=") zoomAt(r.width / 2, r.height / 2, 1.2);
      else if (e.key === "-") zoomAt(r.width / 2, r.height / 2, 1 / 1.2);
      else if (e.key === "0") { reset(); }
      else return;
      e.preventDefault();
    };
    const reset = () => { for (const b of bodies.current.values()) { b.ox = 0; b.oy = 0; } fit(true); wake.current(); };
    resetRef.current = reset;
    el.addEventListener("wheel", wheel, { passive: false });
    el.addEventListener("pointerdown", down);
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
    el.addEventListener("keydown", key);
    el.addEventListener("dblclick", reset);
    return () => {
      el.removeEventListener("wheel", wheel);
      el.removeEventListener("pointerdown", down);
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
      el.removeEventListener("keydown", key);
      el.removeEventListener("dblclick", reset);
    };
  }, [compact, places]); // eslint-disable-line react-hooks/exhaustive-deps
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const resetRef = useRef<() => void>(() => undefined);
  useEffect(() => { repaint((x) => x + 1); }, [selected]);

  /** Arrow keys walk the tree: left to the parent, right to the first child, up and down among neighbours. */
  function walk(e: React.KeyboardEvent, n: Node) {
    const sibs = tree.nodes.filter((x) => x.parent === n.parent), at = sibs.indexOf(n);
    const to = { ArrowLeft: tree.nodes.find((x) => x.id === n.parent), ArrowRight: tree.nodes.find((x) => x.parent === n.id),
      ArrowUp: sibs[at - 1], ArrowDown: sibs[at + 1] }[e.key];
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect?.(n.id); return; }
    if (to) { e.preventDefault(); svg.current?.querySelector<SVGGElement>(`[data-node="${CSS.escape(to.id)}"]`)?.focus(); }
  }

  /** The last result, in the run's own words. Which way is better is the run's to say, not the graph's. */
  const latest = (n: Node) => [...n.runs].reverse().find((r) => r.result)?.result?.replace(/ \(.*\)$/, "") ?? null;

  return (
    <div className={`graph ${compact ? "graph-compact" : ""}`}>
      <svg ref={svg} role="group" aria-label={label} tabIndex={compact ? undefined : -1}>
        <g ref={world}>
          <g>{tree.nodes.map((n) => (
            <path key={n.id} data-edge={n.id} className={`edge edge-${n.status}`} data-t={`idea:${slug(n.id)}`} />))}</g>
          <g ref={pulseLayer} aria-hidden="true" />
          <g data-node="" className="node node-root" aria-hidden="true">
            <circle r="3" /><text x="-2" y="-14" className="root-label">{short(root, 28)}</text>
          </g>
          {tree.nodes.map((n) => {
            const on = tree.running === n.id, value = latest(n);
            return (
              <g key={n.id} data-node={n.id} data-t={`idea:${slug(n.id)}`} tabIndex={compact ? undefined : 0}
                role={compact ? undefined : "button"} aria-expanded={compact ? undefined : selected === n.id}
                aria-label={compact ? undefined : `${n.title}. ${n.status}. ${n.runs.length} runs.${on ? " Running now." : ""}`}
                className={`node node-${n.status} ${on ? "running" : ""} ${selected === n.id ? "chosen" : ""} ${tree.working === n.id ? "working" : ""}`}
                onKeyDown={(e) => walk(e, n)}>
                <circle className="hit" r="16" />
                {on && <circle className="halo" r="11" />}
                <circle className="dot" r={n.status === "abandoned" ? 3 : 4.5} />
                <text x="12" y="1" className="label">{short(n.title, compact ? 24 : 34)}</text>
                {!compact && <text x="12" y="15" className="node-note">{value ? `${value}  ` : ""}{n.runs.length > 0 ? `${n.runs.length} run${n.runs.length > 1 ? "s" : ""}` : "no run"}</text>}
              </g>);
          })}
        </g>
      </svg>
      {!compact && <button type="button" className="fit" onClick={() => resetRef.current()}>Fit</button>}
    </div>
  );
}
