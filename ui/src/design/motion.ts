// The moving parts every screen shares: reduced motion, the pointer's light, connected hover, the heart's signal,
// reordering lists and rolling numbers. Nothing here reads the API. The Court switches all of it off except the trace.
import { type RefObject, useEffect, useLayoutEffect, useRef, useState } from "react";

export const calm = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
export const still = () => document.body.dataset.mood === "court";

/** Hairlines and the one soft falloff follow the pointer through two variables. Off in the Court. */
export function installPointerLight(): () => void {
  let frame = 0, x = -999, y = -999;
  const root = document.documentElement;
  const paint = () => { frame = 0; root.style.setProperty("--mx", `${x}px`); root.style.setProperty("--my", `${y}px`); };
  const on = (e: PointerEvent) => {
    if (still() || calm()) return;
    x = e.clientX; y = e.clientY;
    if (!frame) frame = requestAnimationFrame(paint);
  };
  window.addEventListener("pointermove", on, { passive: true });
  return () => { window.removeEventListener("pointermove", on); cancelAnimationFrame(frame); };
}

/** Connected hover. Anything with `data-t="a b"` lights everything else that shares a key, across the whole page. */
export function traceKeys(keys: string[]): void {
  document.querySelectorAll(".lit").forEach((e) => e.classList.remove("lit"));
  if (keys.length === 0) { delete document.body.dataset.tracing; return; }
  document.body.dataset.tracing = "1";
  for (const k of keys) {
    document.querySelectorAll(`[data-t~="${k.replace(/["\\]/g, "\\$&")}"]`).forEach((e) => e.classList.add("lit"));
  }
}

export function installTrace(): () => void {
  const over = (e: Event) => {
    const hit = (e.target as Element | null)?.closest?.("[data-t]");
    const keys = hit?.getAttribute("data-t")?.split(" ") ?? [];
    // A Story line answers to its kind, so a count can light all its lines; hovered itself, it speaks only for its idea.
    traceKeys(hit?.hasAttribute("data-line") ? keys.filter((k) => /^(idea|line):/.test(k)) : keys);
  };
  document.addEventListener("pointerover", over);
  document.addEventListener("focusin", over);
  return () => {
    document.removeEventListener("pointerover", over);
    document.removeEventListener("focusin", over);
  };
}

/** The heart: anything live calls `beat`. `slowHeart` is how the Promote moment stops the Lab. */
type Heart = { beat: (size: number) => void; rate: (r: number) => void };
let heart: Heart | null = null;
let rate = 1;
export const setHeart = (h: Heart | null) => { heart = h; };
export const beat = (size = 1) => heart?.beat(size);
export const slowHeart = (r: number) => { rate = r; heart?.rate(r); };
export const labRate = () => rate;

/** Lists glide to their new order. Children carry `data-k`. */
export function useFlip(ref: RefObject<HTMLElement | null>, order: string): void {
  const last = useRef(new Map<string, number>());
  useLayoutEffect(() => {
    const box = ref.current;
    if (!box) return;
    const now = new Map<string, number>();
    const moving = !calm() && !still();
    box.querySelectorAll<HTMLElement>("[data-k]").forEach((el) => {
      const key = el.dataset.k as string, y = el.getBoundingClientRect().top + window.scrollY;
      now.set(key, y);
      if (!moving || !el.animate) return;
      const was = last.current.get(key);
      if (was !== undefined && was !== y) {
        el.animate([{ transform: `translateY(${was - y}px)` }, { transform: "none" }], { duration: 360, easing: "cubic-bezier(.2,.7,.1,1)" });
      } else if (was === undefined && last.current.size > 0) {
        el.animate([{ opacity: 0, transform: "translateY(6px)" }, { opacity: 1, transform: "none" }], { duration: 320, easing: "ease-out" });
      }
    });
    last.current = now;
  }, [ref, order]);
}

/** A number that rolls to its new value. It starts at the value it is given, so a first paint never counts up. */
export function useRolled(target: number | null): number | null {
  const [shown, setShown] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    if (target === null || from.current === null || from.current === target || calm() || still()) {
      from.current = target; setShown(target); return undefined;
    }
    const a = from.current, t0 = performance.now();
    let id = requestAnimationFrame(function step(t) {
      const p = Math.min(1, (t - t0) / 520), e = 1 - (1 - p) ** 3, v = p < 1 ? a + (target - a) * e : target;
      from.current = v; setShown(v);
      if (p < 1) id = requestAnimationFrame(step);
    });
    return () => cancelAnimationFrame(id);
  }, [target]);
  return shown;
}
