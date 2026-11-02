// Two canvas lines. The heartbeat runs along the top of every screen but the Court: amber only for Lab activity, a flat
// grey line otherwise. The pointer line is what an empty Today shows: one line across the dark that leans toward you.
import { useEffect, useRef } from "react";
import { calm, labRate, setHeart, still } from "./motion";

const colour = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function sharpen(c: HTMLCanvasElement): CanvasRenderingContext2D | null {
  const r = c.getBoundingClientRect(), d = Math.min(2, window.devicePixelRatio || 1);
  c.width = Math.round(r.width * d); c.height = Math.round(r.height * d);
  const g = c.getContext("2d");
  g?.setTransform(d, 0, 0, d, 0, 0);
  return g;
}

export function Heartbeat() {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const c = ref.current;
    if (!c) return undefined;
    let g = sharpen(c), id = 0, on = false, rate = 1, heat = 0, frac = 0, queue: number[] = [];
    const trace = new Float32Array(1024);
    let head = 0;
    const amber = colour("--amber"), grey = colour("--line");
    const draw = () => {
      id = 0;
      if (!g) return;
      const w = c.clientWidth, h = c.clientHeight, mid = h - 2, n = trace.length;
      frac += 2 * rate;
      while (frac >= 1) {
        frac -= 1; head = (head + 1) % n;
        trace[head] = queue.shift() ?? 0;
      }
      g.clearRect(0, 0, w, h);
      heat *= 0.994;
      g.beginPath();
      for (let x = 0; x < w; x += 2) {
        const v = trace[(head - Math.floor((w - x) / 2) + n * 4) % n] ?? 0;
        if (x === 0) g.moveTo(x, mid - v * (h - 5)); else g.lineTo(x, mid - v * (h - 5));
      }
      g.strokeStyle = grey; g.globalAlpha = 1; g.lineWidth = 1; g.stroke();
      if (heat > 0.01) { g.strokeStyle = amber; g.globalAlpha = Math.min(1, heat); g.stroke(); }
      const busy = heat > 0.01 || queue.length > 0 || trace.some((v) => v !== 0);
      if (busy && !calm() && !still() && rate > 0.01) id = requestAnimationFrame(draw); else on = false;
    };
    const wake = () => { if (!on) { on = true; id = requestAnimationFrame(draw); } };
    setHeart({
      beat(size) {
        heat = 1;
        if (calm()) { wake(); return; }
        const a = Math.min(1, 0.35 + size * 0.6);
        queue.push(0, a * 0.18, -a * 0.1, a, -a * 0.28, a * 0.12, 0);
        wake();
      },
      rate(r) { rate = r; wake(); },
    });
    const size = () => { g = sharpen(c); wake(); };
    window.addEventListener("resize", size);
    wake();
    return () => { setHeart(null); cancelAnimationFrame(id); window.removeEventListener("resize", size); queue = []; };
  }, []);
  return <canvas ref={ref} className="heart" aria-hidden="true" />;
}

const REST = 0.7;

/** One line across the page, resting flat, that a pointer draws up as it passes. Springs back when it leaves. */
export function PointerLine() {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const c = ref.current;
    if (!c) return undefined;
    let g = sharpen(c), id = 0, on = false, px = -1e4, py = -1e4, near = 0;
    const spacing = 6;
    let pos = new Float32Array(Math.ceil(c.clientWidth / spacing) + 2), vel = new Float32Array(pos.length);
    const line = colour("--line-lit"), text = colour("--text");
    const draw = () => {
      id = 0;
      if (!g) return;
      const w = c.clientWidth, h = c.clientHeight, mid = h * REST;
      near += (((py > -1e3) ? 1 : 0) - near) * 0.08;
      let energy = 0;
      for (let i = 0; i < pos.length; i += 1) {
        const x = i * spacing, d = (x - px) / 150, pull = Math.exp(-d * d) * (py - c.clientHeight * REST) * 0.62;
        const left = pos[i - 1] ?? 0, right = pos[i + 1] ?? 0;
        const a = (py > -1e3 ? (pull - (pos[i] as number)) * 0.05 : -(pos[i] as number) * 0.05) + (left + right - 2 * (pos[i] as number)) * 0.2;
        vel[i] = ((vel[i] as number) + a) * 0.82;
        pos[i] = (pos[i] as number) + (vel[i] as number);
        energy += Math.abs(vel[i] as number);
      }
      g.clearRect(0, 0, w, h);
      g.beginPath();
      pos.forEach((v, i) => { if (i === 0) g?.moveTo(0, mid + v); else g?.lineTo(i * spacing, mid + v); });
      g.lineWidth = 1; g.strokeStyle = line; g.stroke();
      // one small point of light rides the line, under the pointer
      if (near > 0.02) {
        const i = Math.max(0, Math.min(pos.length - 1, Math.round(px / spacing)));
        g.fillStyle = text; g.globalAlpha = near;
        g.beginPath(); g.arc(px, mid + (pos[i] as number), 2, 0, Math.PI * 2); g.fill();
        g.globalAlpha = 1;
      }
      if (energy > 0.05 || near > 0.02) id = requestAnimationFrame(draw); else on = false;
    };
    const wake = () => { if (!on) { on = true; id = requestAnimationFrame(draw); } };
    const move = (e: PointerEvent) => {
      if (calm()) return;
      const r = c.getBoundingClientRect();
      px = e.clientX - r.left; py = e.clientY - r.top;
      wake();
    };
    const leave = () => { px = -1e4; py = -1e4; wake(); };
    const size = () => {
      g = sharpen(c);
      pos = new Float32Array(Math.ceil(c.clientWidth / spacing) + 2); vel = new Float32Array(pos.length);
      wake();
    };
    window.addEventListener("pointermove", move, { passive: true });
    document.addEventListener("pointerleave", leave);
    window.addEventListener("resize", size);
    wake();
    return () => {
      cancelAnimationFrame(id);
      window.removeEventListener("pointermove", move);
      document.removeEventListener("pointerleave", leave);
      window.removeEventListener("resize", size);
    };
  }, []);
  return <canvas ref={ref} className="pointer-line" aria-hidden="true" />;
}

export { labRate };
