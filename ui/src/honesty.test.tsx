// The honesty suite (GOAL 18.7): the screens cannot look more certain than the API says, and the rules of the
// design hold in the source, not only by habit.
import { cleanup, render, screen } from "@testing-library/react";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import type { Badge, Case } from "./api/types.gen";
import { BadgeChip, mayShowCertified } from "./design/CertifiedBadge";
import { MetaContext } from "./design/kit";
import { CaseView } from "./screens/Verdict";

const SRC = dirname(fileURLToPath(import.meta.url));
function sources(dir = SRC): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? sources(p) : [p];
  });
}
const code = sources().filter((p) => /\.(tsx?|css)$/.test(p) && !p.endsWith(".test.tsx") && !p.endsWith(".gen.ts"));
const rel = (p: string) => relative(SRC, p).replaceAll("\\", "/");
const read = (p: string) => readFileSync(p, "utf8");

const VALIDATED: Badge = { name: "VALIDATED", form: "VALIDATED", meaning: "m" };
const REPRODUCIBLE: Badge = { name: "REPRODUCIBLE", form: "REPRODUCIBLE", meaning: "m" };
const num = { value: 1, link: "chh://x/y/z" };

function aCase(over: Partial<Case>): Case {
  return { case_id: "c", title: "t", sentence: "s", scope: "s", verdict: "CERTIFIED", verdict_sentence: "Certified.",
    reason_code: null, reason: null, badges: [VALIDATED], second_test: null, checks_in_full: [], looks_remaining: num,
    test_rows: num, retired: false, next_step: null, command: null, rerun: null, story: null, technical: "t", ...over };
}

function show(c: Case, scripted: boolean) {
  return render(<MetaContext.Provider value={{ scripted, notice: null, researcher: "r" }}><CaseView c={c} /></MetaContext.Provider>);
}

afterEach(cleanup);

describe("certified styling", () => {
  it("needs a certified verdict, a fresh-test badge and a real store", () => {
    expect(mayShowCertified(VALIDATED, "CERTIFIED", false)).toBe(true);
    expect(mayShowCertified({ ...VALIDATED, name: "RE-VALIDATED" }, "CERTIFIED", false)).toBe(true);
    for (const v of ["NOT CERTIFIED", "INCONCLUSIVE", "COULD NOT RUN", null] as const) {
      expect(mayShowCertified(VALIDATED, v, false)).toBe(false);
    }
    expect(mayShowCertified(REPRODUCIBLE, "CERTIFIED", false)).toBe(false);
    expect(mayShowCertified({ ...VALIDATED, name: "REPLICATED" }, "CERTIFIED", false)).toBe(false);
    expect(mayShowCertified(VALIDATED, "CERTIFIED", true)).toBe(false);
  });

  it("a scripted page never shows the certified badge, and says it is an example", () => {
    const { container } = show(aCase({}), true);
    expect(container.querySelector(".badge-certified")).toBeNull();
    expect(container.querySelector(".badge-example")?.textContent).toContain("(example)");
    expect(container.querySelector(".earned")).toBeNull();
    expect(container.querySelector(".verdict-line.example")).toBeTruthy();
  });

  it("a real certified case shows it, a reproducible badge does not", () => {
    const { container } = show(aCase({ badges: [REPRODUCIBLE, VALIDATED] }), false);
    expect(container.querySelectorAll(".badge-certified")).toHaveLength(1);
    expect(container.querySelectorAll(".verdict-line.earned")).toHaveLength(1);
    expect(container.querySelector(".badge-support")?.textContent).toBe("REPRODUCIBLE");
  });

  it("a rejected case is calm grey, never red, and a crash is the machine's red", () => {
    const rejected = show(aCase({ verdict: "NOT CERTIFIED", verdict_sentence: "Not certified.", badges: [] }), false);
    expect(rejected.container.querySelector(".verdict-sentence")?.className).toContain("rejected");
    expect(rejected.container.querySelector(".earned")).toBeNull();
    cleanup();
    const crash = show(aCase({ verdict: "COULD NOT RUN", verdict_sentence: "x", badges: [] }), false);
    expect(crash.container.querySelector(".verdict-sentence")?.className).toContain("broken");
  });

  it("an unknown answer about the source counts as not real", () => {
    render(<MetaContext.Provider value={null}><BadgeChip badge={VALIDATED} verdict="CERTIFIED" scripted /></MetaContext.Provider>);
    expect(screen.getByText(/example/)).toBeTruthy();
  });
});

describe("the source", () => {
  it("uses the green token only in the certified badge rule, and that class only in its component", () => {
    const value = (read(join(SRC, "design/tokens.css")).match(/--certified: (#[0-9a-f]{6})/i) as RegExpMatchArray)[1] as string;
    const green = code.filter((p) => read(p).includes("--certified") || read(p).toLowerCase().includes(value.toLowerCase())).map(rel).sort();
    expect(green).toEqual(["design/styles.css", "design/tokens.css"]);
    const css = read(join(SRC, "design/styles.css")).split("\n");
    const rules = css.filter((l) => l.includes("--certified"));
    expect(rules).toHaveLength(1);
    expect(rules[0]).toMatch(/^\.badge-certified, \.verdict-line\.earned \{ --earned: var\(--certified\); \}$/);
    expect(css.filter((l) => l.includes("--earned")).map((l) => l.split(" {")[0])).toEqual(
      [".badge-certified, .verdict-line.earned", ".badge-certified", ".verdict-line.earned"]);
    for (const cls of ["badge-certified", "earned"]) {
      expect(code.filter((p) => p.endsWith("sx") && read(p).includes(cls)).map(rel)).toEqual(["design/CertifiedBadge.tsx"]);
    }
  });

  it("defines colours in tokens and the light figure only", () => {
    const hex = /#[0-9a-f]{6}\b/i;
    const bad = code.filter((p) => hex.test(read(p)) && !["design/tokens.css", "design/figure.ts"].includes(rel(p)));
    expect(bad).toEqual([]);
  });

  it("has no decoration: light only at the pointer, a glow only on the marks that earn one, no light theme", () => {
    const rules = read(join(SRC, "design/styles.css")).split("\n");
    expect(rules.filter((l) => l.includes("gradient"))).toHaveLength(2); // the two fall-offs at the pointer, in one comment-marked block
    expect(rules.filter((l) => l.includes("gradient")).every((l) => l.includes("at var(--mx) var(--my)"))).toBe(true);
    const glows = rules.filter((l) => l.includes("box-shadow")).map((l) => l.split(" {")[0]?.trim());
    expect(glows).toEqual([".ring", ".scrub input::-webkit-slider-thumb", ".moment-point", ".step button:hover .point, .step.open .point", ".verdict-line.earned"]);
    for (const p of code.filter((x) => x.endsWith(".css"))) {
      expect(read(p)).not.toMatch(/linear-gradient|text-shadow|filter:|backdrop|prefers-color-scheme/);
    }
  });

  it("the moving surfaces stay honest: no green, and the Lab's colour never reaches the Court", () => {
    for (const f of ["design/IdeaGraph.tsx", "design/Moment.tsx", "design/Heartbeat.tsx", "design/Palette.tsx", "design/Peek.tsx"]) {
      expect(read(join(SRC, f))).not.toMatch(/--certified|--earned|badge-certified|VerdictLine/);
    }
    const css = read(join(SRC, "design/styles.css"));
    for (const rule of css.match(/^\.(moment|node|edge|pulse|halo|dot|mark)[^{]*\{[^}]*\}/gm) ?? []) expect(rule).not.toMatch(/earned|certified/);
    expect(css).toMatch(/body\[data-mood="court"\] \*, [^{]*\{ animation: none !important; transition: none !important; \}/);
    expect(read(join(SRC, "App.tsx"))).toMatch(/Pulse active=\{mood !== "court"\}/);
  });

  it("the Promote moment says nothing was decided, and plays only after the API answered", () => {
    expect(read(join(SRC, "design/Moment.tsx"))).toContain("Nothing has been decided.");
    expect(read(join(SRC, "design/Moment.tsx"))).not.toMatch(/\b(certified|validated|proved|passed|verdict)\b/i);
    expect(read(join(SRC, "screens/Promote.tsx"))).toMatch(/setDone\(await api\.promote\(request\(\)\)\); setPlaying\(true\);/);
  });

  it("the palette and the previews perform nothing and open no verdict", () => {
    for (const f of ["design/Palette.tsx", "design/Peek.tsx"]) {
      const src = read(join(SRC, f));
      expect(src).not.toMatch(/api\.(openCase|technical|rehearse|preview|promote|steer|share|paper|howTo)\b/);
      expect(src).not.toMatch(/method: "POST"|\bfetch\(/);
    }
    expect(read(join(SRC, "design/kit.tsx"))).not.toMatch(/remembered\.set\("?case/);
    expect(read(join(SRC, "api/client.ts"))).toMatch(/openCase: \(case_id: string\) => post</); // a verdict stays a POST, outside the read memory
  });

  it("no screen names a font of its own", () => {
    for (const p of code.filter((x) => x.endsWith("sx"))) expect(read(p)).not.toMatch(/font-family|fontFamily/);
  });

  it("uses the serif only for the verdict sentence", () => {
    const css = read(join(SRC, "design/styles.css")).split("\n").filter((l) => l.includes("--serif"));
    expect(css).toHaveLength(1);
    expect(css[0]).toContain(".verdict-sentence");
  });

  it("talks to nothing but the local API", () => {
    const fetchers = code.filter((p) => /\bfetch\(|XMLHttpRequest|new WebSocket|EventSource\(/.test(read(p))).map(rel);
    expect(fetchers).toEqual(["api/client.ts"]);
    expect(read(join(SRC, "api/client.ts"))).not.toMatch(/https?:\/\//);
    for (const p of code) expect(read(p)).not.toMatch(/https?:\/\/(?!www\.w3\.org)/);
  });

  it("has no route that takes a human approval", () => {
    const client = read(join(SRC, "api/client.ts"));
    expect(client).not.toMatch(/approve_|sign_off|register_human|retire|"seal"|"mint"/);
    expect(client.match(/post<[^>]+>\("(\w+)"/g)?.map((s) => s.split('"')[1]).sort()).toEqual(
      ["case", "paper", "promote", "promote_preview", "rehearse", "share", "steer", "technical"]);
    expect(read(join(SRC, "screens/Court.tsx"))).not.toMatch(/>\s*Approve\b/);
  });

  it("spends no look without a press: the verdict screen does not open a case on arrival", () => {
    const src = read(join(SRC, "screens/Verdict.tsx"));
    expect(src).not.toContain("useEffect");
    expect(src).not.toContain("useLoad");
  });

  it("keeps internal terms off the screens", () => {
    const words = /\b(K[0-9]|T[0-6]|VG[0-9]?|MECHANICAL|Kernel|kerneld|ledger|alpha|hoststore|prereg\w*|held-out|heldout|mint(ed)?|beacon|vault|tier|autopsy|bundle)\b/;
    const texts = code.filter((p) => p.endsWith(".tsx")).flatMap((p) => [...read(p).replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "")
      .matchAll(/>([^<>{}]+)</g)].map((m) => `${rel(p)}: ${m[1]}`));
    expect(texts.length).toBeGreaterThan(30);
    expect(texts.filter((t) => words.test(t.split(": ").slice(1).join(": ")))).toEqual([]);
  });
});

describe("contrast", () => {
  const tokens = Object.fromEntries([...read(join(SRC, "design/tokens.css")).matchAll(/--([a-z0-9-]+): (#[0-9a-f]{6})/gi)].map((m) => [m[1], m[2]]));
  const lum = (hex: string) => {
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
      .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)) as [number, number, number];
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a: string, b: string) => {
    const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p) as [number, number];
    return (x + 0.05) / (y + 0.05);
  };
  const backs = ["lab-bg", "lab-panel", "court-bg", "court-panel"];
  it.each(["text", "text-2", "ash", "certified", "support", "rejected", "broken", "amber"])("%s reads on every background (AA, 4.5:1)", (fg) => {
    for (const b of backs) expect(ratio(tokens[fg] as string, tokens[b] as string), `${fg} on ${b}`).toBeGreaterThanOrEqual(4.5);
  });
  it("the canvas is true black", () => {
    expect(tokens["lab-bg"]).toBe("#000000");
    expect(tokens["court-bg"]).toBe("#000000");
  });
});

describe("types", () => {
  it("are generated from the API's schema, and the file names it", () => {
    const gen = read(resolve(SRC, "api/types.gen.ts"));
    expect(gen.startsWith("// Generated by scripts/gen-types.mjs")).toBe(true);
    expect(gen).toContain("export interface Case {");
    expect(gen).toContain('verdict: "CERTIFIED" | "NOT CERTIFIED" | "INCONCLUSIVE" | "COULD NOT RUN" | null;');
  });
});
