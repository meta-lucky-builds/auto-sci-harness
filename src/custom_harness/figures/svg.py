"""Deterministic stdlib SVG renderer for figures (GOAL §5 L2, invariant 8 extended to figures).

This file is copied VERBATIM into the figure's sandbox as `chh_svg.py`, so it imports nothing outside the standard
library and nothing from this package. A figure script uses it like this:

    import chh_svg as S
    inp = S.load_inputs()                       # inputs.json, written by the harness: ref id -> resolved value
    S.save(S.bar_chart(inp, [("ours", "subject"), ("baseline", "comparator")], title="Held-out accuracy"))

Plotted values are passed as REF IDS, never as numbers: the renderer looks each value up in the harness-resolved
inputs (a raw number is refused), and every mark carries `data-ref` and `data-v` so the audit can resolve it
again. The audit (figures/audit.py) does not trust this file's output: it re-derives the axis, every mark's geometry
and every tick from the resolved values with the functions below and compares strings exactly.

Why stdlib SVG and not matplotlib: the host venv has no matplotlib, and neither does the sandbox image
(custom-harness-sandbox:py312, checked 2026-10-07); installs are not allowed. SVG text is also byte-deterministic,
which makes "re-render and compare bytes" a sharp check.
"""

from __future__ import annotations

import json
import math
from typing import Any
from xml.sax.saxutils import escape as _escape

RENDERER = "chh-svg/1"


def escape(text: str) -> str:
    """Text for element content AND attribute values (quotes escaped too)."""
    return _escape(text, {'"': "&quot;"})
W, H = 640, 400
LEFT, RIGHT, TOP, BOTTOM = 70, 20, 40, 60
COLORS = ("#2a6fdb", "#e0782b", "#3a9a5b", "#b23a48", "#7a5cc0", "#6b6b6b")


def load_inputs(path: str = "inputs.json") -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return {k: v["value"] for k, v in doc["refs"].items()}


def save(svg: str, path: str = "figure.svg") -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(svg)


def value_of(inputs: dict[str, Any], ref: Any) -> float:
    if not isinstance(ref, str):
        raise TypeError(f"plotted values must be ref ids from inputs.json, got {type(ref).__name__} {ref!r}")
    if ref not in inputs:
        raise KeyError(f"unknown ref id {ref!r}")
    v = inputs[ref]
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise TypeError(f"ref {ref!r} does not resolve to a finite number")
    return float(v)


def data_v(v: float) -> str:
    """Exact, round-trippable value text (data-v)."""
    return json.dumps(float(v))


def label(v: float) -> str:
    """Displayed value text."""
    return format(float(v), ".4g")


def px(v: float) -> str:
    return f"{v:.2f}"


def axis(values: list[float]) -> tuple[float, float, list[float]]:
    """(lo, hi, ticks): includes 0, about 5 ticks on a 1/2/5 x 10^k step."""
    lo, hi = min([0.0, *values]), max([0.0, *values])
    if hi == lo:
        hi = lo + 1.0
    raw = (hi - lo) / 5
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    k0, k1 = math.floor(lo / step + 1e-9), math.ceil(hi / step - 1e-9)
    ticks = [round(k * step, 12) for k in range(k0, k1 + 1)]
    return ticks[0], ticks[-1], ticks


def y_px(lo: float, hi: float, v: float) -> float:
    return TOP + (H - TOP - BOTTOM) * (hi - v) / (hi - lo)


def bar_geometry(n: int, i: int, lo: float, hi: float, v: float) -> dict[str, str]:
    slot = (W - LEFT - RIGHT) / n
    y0, yv = y_px(lo, hi, 0.0), y_px(lo, hi, v)
    return {"x": px(LEFT + slot * i + slot * 0.15), "y": px(min(y0, yv)), "width": px(slot * 0.7),
            "height": px(abs(y0 - yv))}


def point_geometry(n: int, i: int, lo: float, hi: float, v: float) -> dict[str, str]:
    slot = (W - LEFT - RIGHT) / n
    return {"cx": px(LEFT + slot * (i + 0.5)), "cy": px(y_px(lo, hi, v)), "r": "4.00"}


def tick_geometry(lo: float, hi: float, t: float) -> dict[str, str]:
    y = px(y_px(lo, hi, t))
    return {"x1": px(LEFT - 5), "y1": y, "x2": px(LEFT), "y2": y}


def tick_text(t: float) -> str:
    return format(t, ".6g")


def _head(kind: str, title: str) -> list[str]:
    return [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
            f'data-chh-figure="{kind}" data-renderer="{RENDERER}">',
            f"<title>{escape(title)}</title>",
            f'<text class="title" x="{px(W / 2)}" y="{px(TOP / 2)}" text-anchor="middle">{escape(title)}</text>']


def _axes(lo: float, hi: float, ticks: list[float], y_label: str) -> list[str]:
    out = ['<g class="axis">',
           f'<line class="axis" x1="{px(LEFT)}" y1="{px(TOP)}" x2="{px(LEFT)}" y2="{px(H - BOTTOM)}" stroke="#333"/>',
           f'<line class="axis" x1="{px(LEFT)}" y1="{px(y_px(lo, hi, 0.0))}" x2="{px(W - RIGHT)}" '
           f'y2="{px(y_px(lo, hi, 0.0))}" stroke="#333"/>']
    for t in ticks:
        g = tick_geometry(lo, hi, t)
        out.append(f'<line class="tick" x1="{g["x1"]}" y1="{g["y1"]}" x2="{g["x2"]}" y2="{g["y2"]}" stroke="#333"/>')
        out.append(f'<text class="tick" x="{px(LEFT - 8)}" y="{g["y1"]}" text-anchor="end">{tick_text(t)}</text>')
    if y_label:
        out.append(f'<text class="label" x="{px(LEFT)}" y="{px(TOP - 6)}" text-anchor="middle">'
                   f"{escape(y_label)}</text>")
    out.append("</g>")
    return out


def bar_chart(inputs: dict[str, Any], bars: list[tuple[str, str]], title: str = "", y_label: str = "") -> str:
    """One bar per (category label, ref id)."""
    if not bars:
        raise ValueError("a bar chart needs at least one bar")
    values = [value_of(inputs, ref) for _, ref in bars]
    lo, hi, ticks = axis(values)
    out = _head("bar", title) + _axes(lo, hi, ticks, y_label) + ['<g class="marks">']
    slot = (W - LEFT - RIGHT) / len(bars)
    for i, ((cat, ref), v) in enumerate(zip(bars, values, strict=True)):
        g = bar_geometry(len(bars), i, lo, hi, v)
        out.append(f'<rect class="mark" data-ref="{escape(ref)}" data-v="{data_v(v)}" x="{g["x"]}" y="{g["y"]}" '
                   f'width="{g["width"]}" height="{g["height"]}" fill="{COLORS[0]}"/>')
        out.append(f'<text class="value" data-ref="{escape(ref)}" x="{px(LEFT + slot * (i + 0.5))}" '
                   f'y="{px(float(g["y"]) - 4)}" text-anchor="middle">{label(v)}</text>')
        out.append(f'<text class="category" x="{px(LEFT + slot * (i + 0.5))}" y="{px(H - BOTTOM + 18)}" '
                   f'text-anchor="middle">{escape(cat)}</text>')
    return "\n".join([*out, "</g>", "</svg>"]) + "\n"


def line_chart(inputs: dict[str, Any], series: list[tuple[str, list[str]]], x_labels: list[str] | None = None,
               title: str = "", y_label: str = "") -> str:
    """Points (and a connecting polyline) per (series name, [ref id per x position])."""
    if not series or not all(refs for _, refs in series):
        raise ValueError("a line chart needs at least one non-empty series")
    n = max(len(refs) for _, refs in series)
    values = [[value_of(inputs, r) for r in refs] for _, refs in series]
    lo, hi, ticks = axis([v for vs in values for v in vs])
    out = _head("line", title) + _axes(lo, hi, ticks, y_label) + ['<g class="marks">']
    slot = (W - LEFT - RIGHT) / n
    for j, ((name, refs), vs) in enumerate(zip(series, values, strict=True)):
        color = COLORS[j % len(COLORS)]
        out.append(f'<g class="series" data-series="{escape(name)}">')
        pts = []
        for i, (ref, v) in enumerate(zip(refs, vs, strict=True)):
            g = point_geometry(n, i, lo, hi, v)
            pts.append(f'{g["cx"]},{g["cy"]}')
            out.append(f'<circle class="mark" data-ref="{escape(ref)}" data-v="{data_v(v)}" cx="{g["cx"]}" '
                       f'cy="{g["cy"]}" r="{g["r"]}" fill="{color}"/>')
        out.append(f'<polyline class="link" points="{" ".join(pts)}" fill="none" stroke="{color}"/>')
        out.append(f'<text class="legend" x="{px(W - RIGHT)}" y="{px(TOP + 14 * j)}" text-anchor="end" '
                   f'fill="{color}">{escape(name)}</text>')
        out.append("</g>")
    for i, xl in enumerate(x_labels or []):
        out.append(f'<text class="category" x="{px(LEFT + slot * (i + 0.5))}" y="{px(H - BOTTOM + 18)}" '
                   f'text-anchor="middle">{escape(xl)}</text>')
    return "\n".join([*out, "</g>", "</svg>"]) + "\n"
