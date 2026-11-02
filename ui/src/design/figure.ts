// Exported figures are light, for papers and slides. The dark screens use tokens.css; a figure leaves the app.
import type { ScoreRow } from "../api/types.gen";

const LIGHT = { bg: "#ffffff", text: "#1b2530", method: "#1f5f8b", baseline: "#6b7681", grid: "#d5dbe0" };

/** A dot plot of the seeds of each arm, as a stand-alone SVG. Values are plain data, escaped by construction. */
export function lightFigure(scores: ScoreRow[], title: string): string {
  const values = scores.map((s) => s.score.value).filter((v): v is number => v !== null);
  const lo = Math.min(...values), hi = Math.max(...values);
  const pad = Math.max((hi - lo) * 0.1, 1e-6);
  const x = (v: number) => 90 + ((v - lo + pad) / (hi - lo + 2 * pad)) * 380;
  const esc = (t: string) => t.replace(/[<>&"]/g, (c) => `&#${c.charCodeAt(0)};`);
  const dots = scores.filter((s) => s.score.value !== null).map((s) => {
    const y = s.arm === "method" ? 50 : 90;
    const fill = s.arm === "method" ? LIGHT.method : LIGHT.baseline;
    return `<circle cx="${x(s.score.value as number).toFixed(1)}" cy="${y}" r="5" fill="${fill}"/>`;
  }).join("");
  return `<svg xmlns="http://www.w3.org/2000/svg" width="500" height="130" viewBox="0 0 500 130" font-family="sans-serif" font-size="12">`
    + `<rect width="500" height="130" fill="${LIGHT.bg}"/>`
    + `<text x="10" y="18" fill="${LIGHT.text}">${esc(title)}</text>`
    + `<text x="10" y="54" fill="${LIGHT.text}">method</text><text x="10" y="94" fill="${LIGHT.text}">baseline</text>`
    + `<line x1="90" x2="470" y1="110" y2="110" stroke="${LIGHT.grid}"/>`
    + `<text x="90" y="124" fill="${LIGHT.text}">${lo.toFixed(3)}</text>`
    + `<text x="470" y="124" text-anchor="end" fill="${LIGHT.text}">${hi.toFixed(3)}</text>${dots}</svg>`;
}

export function download(name: string, text: string, type: string): void {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}
