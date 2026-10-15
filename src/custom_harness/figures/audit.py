"""Figures whose every plotted value resolves to an artifact, re-rendered by the audit (GOAL §5 L2, invariant 8
extended to figures).

A figure is a directory:
    figure.py    the script that renders it (agent- or human-written; run ONLY inside a sandbox)
    figure.svg   its output
    figure.json  {"format": "chh-figure/1", "refs": {ref_id: {"evidence": id, "pointer": "/json/pointer"}},
                  "script_sha256", "svg_sha256", "renderer_sha256"}
A ref names an Evidence record of the store and a JSON pointer into its payload (e.g. an ExecRecord's "/score"),
or a Kernel ledger certificate the auditor allows plus a pointer into it (e.g. "/resolution/targets/<t>/lo", the
held-out effect interval); never a raw blob: a blob anyone can write is not evidence. The harness resolves the
refs and gives the script only
`inputs.json` (ref id -> value) plus the renderer (`chh_svg.py` = figures/svg.py).

`audit_figure` fails a figure (fail closed) unless ALL of these hold:
  F1 files: within size caps; figure.py / figure.svg match the hashes in figure.json
  F2 refs: every ref resolves (evidence exists, in `allowed_evidence` if given, pointer -> finite number)
  F3 static: the stored SVG is checked WITHOUT trusting the script: allowed elements/attributes only; every mark
     (rect/circle) is bound to a ref, its data-v equals the resolved value and its geometry, every tick and the
     axis equal what the trusted renderer computes from the resolved values; value labels equal the value; free
     text (title, labels, categories, legend) carries no decimal, percent or exponent number
  F4 re-render: the script, run again in a fresh sandbox on the re-resolved inputs, produces byte-identical SVG
Known limits (stated): free text may still carry integers ("93 of 100"); the category labels themselves are not
bound to anything (a script that swaps two labels is caught by F4 only if the stored SVG differs from the script's
output, not if the script itself swaps them).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..sandbox import Sandbox
from . import svg as S

FORMAT = "chh-figure/1"
NS = "{http://www.w3.org/2000/svg}"
RENDERER_SOURCE = Path(S.__file__).read_bytes()
_REF_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
# checked on the NFKC form (fullwidth digits/stops, superscripts and the percent sign fold to ASCII); \d also matches
# other scripts' digits
_FREE_NUMBER = re.compile(r"\d\s*[.,·]\s*\d|[.,·]\d|\d\s*%|\d\s*percent|\d[eE][-+−]?\d|\d\s*−\s*\d",
                          re.IGNORECASE)
_TAGS = {"svg", "title", "g", "line", "rect", "circle", "polyline", "text"}
_ATTRS = {"xmlns", "width", "height", "viewBox", "data-chh-figure", "data-renderer", "class", "x", "y", "x1", "y1",
          "x2", "y2", "cx", "cy", "r", "points", "fill", "stroke", "text-anchor", "data-ref", "data-v",
          "data-series"}


class FigureError(ValueError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@dataclass
class FigureAudit:
    ok: bool
    problems: list[str] = field(default_factory=list)
    values: dict[str, float] = field(default_factory=dict)


# ------------------------------------------------------------------ F2 refs
def _pointer(doc: Any, pointer: str) -> Any:
    if pointer == "":
        return doc
    if not pointer.startswith("/"):
        raise FigureError(f"bad JSON pointer {pointer!r}")
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(doc, list) and part.isdigit():
            doc = doc[int(part)]
        elif isinstance(doc, dict) and part in doc:
            doc = doc[part]
        else:
            raise FigureError(f"JSON pointer {pointer!r} does not resolve")
    return doc


Certificates = Callable[[str], Any]  # certificate hash -> the Kernel ledger certificate as JSON (raises if unknown)


def resolve_refs(refs: Any, store: Any, allowed_evidence: set[str] | None, *,
                 certificate_of: Certificates | None = None,
                 allowed_certificates: set[str] | None = None) -> dict[str, float]:
    """ref id -> value, read from Evidence payloads in the store, or (a {certificate, pointer} ref) from a Kernel
    ledger certificate the caller allows (`allowed_certificates`, resolved by `certificate_of`). Fail closed on
    anything else: a certificate ref with no resolver or outside the allowed set is refused."""
    if not isinstance(refs, dict) or not refs:
        raise FigureError("figure.json must name at least one ref")
    out: dict[str, float] = {}
    for rid, ref in refs.items():
        if not isinstance(rid, str) or not _REF_ID.match(rid):
            raise FigureError(f"bad ref id {rid!r}")
        if isinstance(ref, dict) and set(ref) == {"certificate", "pointer"}:
            ch = ref["certificate"]
            if certificate_of is None or allowed_certificates is None or ch not in allowed_certificates:
                raise FigureError(f"ref {rid}: certificate {ch!r} is not among the certificates this figure may plot")
            try:
                doc = certificate_of(str(ch))
            except Exception as e:  # any failure to resolve fails the ref closed
                raise FigureError(f"ref {rid}: certificate {ch!r} does not resolve ({type(e).__name__})") from e
            src = f"certificate {ch}"
        elif isinstance(ref, dict) and set(ref) == {"evidence", "pointer"}:
            eid = ref["evidence"]
            if allowed_evidence is not None and eid not in allowed_evidence:
                raise FigureError(f"ref {rid}: evidence {eid!r} is not among the evidence this figure may plot")
            try:
                ev = store.get_evidence(eid)
                doc = store.blobs.get_json(ev.payload_ref)
            except (KeyError, OSError, ValueError) as e:
                raise FigureError(f"ref {rid}: evidence {eid!r} does not resolve ({type(e).__name__})") from e
            src = str(eid)
        else:
            raise FigureError(f"ref {rid}: must be exactly {{evidence, pointer}} or {{certificate, pointer}}")
        v = _pointer(doc, str(ref["pointer"]))
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
            raise FigureError(f"ref {rid}: {src}{ref['pointer']} is not a finite number")
        out[rid] = float(v)
    return out


def inputs_json(refs: dict[str, Any], values: dict[str, float]) -> bytes:
    doc = {"refs": {rid: {**refs[rid], "value": values[rid]} for rid in sorted(values)}}
    return json.dumps(doc, sort_keys=True, indent=1).encode()


# ------------------------------------------------------------------ F3 static check of the stored SVG
def check_svg(data: bytes, values: dict[str, float]) -> list[str]:
    """Problems of a stored SVG against the resolved values (empty = passes). Trusts nothing in the SVG."""
    text = data.decode("utf-8", errors="strict")
    if "<!" in text or "<?" in text.replace('<?xml version="1.0" encoding="utf-8"?>', "", 1):
        return ["figure.svg contains a DOCTYPE, entity, comment, CDATA or processing instruction"]
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return [f"figure.svg is not well-formed XML: {e}"]
    problems: list[str] = []
    for el in root.iter():
        tag = el.tag.removeprefix(NS)
        if tag not in _TAGS:
            problems.append(f"element <{tag}> is not allowed in a figure")
        bad = sorted(set(el.attrib) - _ATTRS)
        if bad:
            problems.append(f"<{tag}> has attributes that are not allowed: {', '.join(bad)}")
        if el.tail and el.tail.strip():
            problems.append(f"stray text after <{tag}>: {el.tail.strip()[:40]!r}")
        if tag not in ("text", "title") and el.text and el.text.strip():
            problems.append(f"text inside <{tag}>: {el.text.strip()[:40]!r}")
    if problems:
        return problems
    if root.tag != NS + "svg" or any(not str(el.tag).startswith(NS) for el in root.iter()):
        return ["every element of a figure must be in the SVG namespace, under one <svg> root"]
    kind = root.get("data-chh-figure")
    if kind not in ("bar", "line", "interval"):
        return [f"unknown figure kind {kind!r}"]
    els = list(root.iter())
    marks = [e for e in els if e.tag.removeprefix(NS) in ("rect", "circle")]
    if not marks:
        return ["the figure plots no marks"]
    want = "rect" if kind == "bar" else "circle"
    plotted: list[float] = []
    if kind == "interval":  # interval segments are marks too: each bound to one ref, its data-v the artifact's value
        for seg in (e for e in els if e.tag.removeprefix(NS) == "line" and e.get("class") == "ci"):
            rid = seg.get("data-ref")
            if rid not in values:
                problems.append(f"an interval segment is not bound to a resolved ref (data-ref={rid!r})")
            elif seg.get("data-v") != S.data_v(values[rid]):
                problems.append(f"segment {rid}: data-v {seg.get('data-v')!r}, the artifact says "
                                f"{S.data_v(values[rid])}")
            else:
                plotted.append(values[rid])
    for m in marks:
        rid = m.get("data-ref")
        if m.tag.removeprefix(NS) != want or m.get("class") != "mark":
            problems.append(f"<{m.tag.removeprefix(NS)}> is not a {kind}-chart mark")
        elif rid not in values:
            problems.append(f"a mark is not bound to a resolved ref (data-ref={rid!r})")
        elif m.get("data-v") != S.data_v(values[rid]):
            problems.append(f"mark {rid}: data-v {m.get('data-v')!r}, the artifact says {S.data_v(values[rid])}")
        else:
            plotted.append(values[rid])
    if problems:
        return problems
    lo, hi, ticks = S.axis(plotted)
    if kind == "bar":
        for i, m in enumerate(marks):
            g = S.bar_geometry(len(marks), i, lo, hi, values[m.get("data-ref", "")])
            if {k: m.get(k) for k in g} != g:
                problems.append(f"bar {m.get('data-ref')}: geometry does not match its value")
    elif kind == "interval":
        for i, c in enumerate(marks):
            g = S.point_geometry(len(marks), i, lo, hi, values[c.get("data-ref", "")])
            if {k: c.get(k) for k in g} != g:
                problems.append(f"point {c.get('data-ref')}: geometry does not match its value")
    else:
        series = [e for e in els if e.tag.removeprefix(NS) == "g" and e.get("class") == "series"]
        n = max((len([c for c in s if c.tag.removeprefix(NS) == "circle"]) for s in series), default=0)
        if sum(len([c for c in s if c.tag.removeprefix(NS) == "circle"]) for s in series) != len(marks):
            problems.append("a point lies outside a series")
        for s in series:
            pts = [c for c in s if c.tag.removeprefix(NS) == "circle"]
            for i, c in enumerate(pts):
                g = S.point_geometry(n, i, lo, hi, values[c.get("data-ref", "")])
                if {k: c.get(k) for k in g} != g:
                    problems.append(f"point {c.get('data-ref')}: geometry does not match its value")
            links = [c for c in s if c.tag.removeprefix(NS) == "polyline"]
            expect = " ".join(f'{c.get("cx")},{c.get("cy")}' for c in pts)
            if len(links) != 1 or links[0].get("points") != expect:
                problems.append(f"series {s.get('data-series')!r}: its line does not join its points")
        if any(e.tag.removeprefix(NS) == "polyline" and e not in [c for s in series for c in s] for e in els):
            problems.append("a polyline lies outside a series")
    tick_lines = [e for e in els if e.tag.removeprefix(NS) == "line" and e.get("class") == "tick"]
    tick_texts = [e for e in els if e.tag.removeprefix(NS) == "text" and e.get("class") == "tick"]
    if [t.text for t in tick_texts] != [S.tick_text(t) for t in ticks]:
        problems.append("axis tick labels do not match the axis the values determine")
    if [{k: e.get(k) for k in ("x1", "y1", "x2", "y2")} for e in tick_lines] != [
            S.tick_geometry(lo, hi, t) for t in ticks]:
        problems.append("axis ticks are not where the values put them")
    for node in els:
        tag, cls = node.tag.removeprefix(NS), node.get("class")
        if tag == "line" and cls not in ("axis", "tick") and not (kind == "interval" and cls == "ci"):
            problems.append(f"<line class={cls!r}> is not allowed")
        if tag == "text" and cls == "value":
            rid = node.get("data-ref")
            if rid not in values or node.text != S.label(values[rid]):
                problems.append(f"value label {node.text!r} (ref {rid!r}) does not match its artifact")
        elif tag == "title" or (tag == "text" and cls != "tick"):
            if tag == "text" and cls not in ("title", "label", "category", "legend"):
                problems.append(f"<text class={cls!r}> is not allowed")
            if _FREE_NUMBER.search(unicodedata.normalize("NFKC", node.text or "")):
                problems.append(f"free text carries a number not bound to an artifact: {(node.text or '')[:60]!r}")
    if not problems:
        problems += _canonical(root, kind, text, values)
    return problems


def _canonical(root: ET.Element, kind: str, text: str, values: dict[str, float]) -> list[str]:
    """The stored SVG must be EXACTLY what the trusted renderer produces from the figure's own data and labels
    (read back from it). This pins every placement, style, size and element the reader sees: moved value or tick
    labels, nested or cropped viewports, extra lines, hidden marks, un-namespaced elements all differ."""
    def texts(cls: str) -> list[str]:
        return [e.text or "" for e in root.iter(NS + "text") if e.get("class") == cls]
    title_el = root.find(NS + "title")
    title = (title_el.text or "") if title_el is not None else ""
    y_label = (texts("label") or [""])[0]
    try:
        if kind == "bar":
            refs = [e.get("data-ref", "") for e in root.iter(NS + "rect")]
            expect = S.bar_chart(values, list(zip(texts("category"), refs, strict=True)), title=title,
                                 y_label=y_label)
        elif kind == "interval":
            return _canonical_interval(root, text, values, texts("category"), title, y_label)
        else:
            series = [(g.get("data-series", ""), [c.get("data-ref", "") for c in g.iter(NS + "circle")])
                      for g in root.iter(NS + "g") if g.get("class") == "series"]
            expect = S.line_chart(values, series, x_labels=texts("category"), title=title, y_label=y_label)
    except (KeyError, TypeError, ValueError) as e:
        return [f"the figure cannot be rebuilt by the trusted renderer from its own data: {e}"]
    if expect != text:
        return ["the figure is not the trusted renderer's output for its own data and labels (placement, styling, "
                "viewport or extra elements changed)"]
    return []


def _canonical_interval(root: ET.Element, text: str, values: dict[str, float], cats: list[str], title: str,
                        y_label: str) -> list[str]:
    """`_canonical` for interval charts: items are read back in document order (segments, then their point); an item
    with one segment is tried as a lower and as an upper bound (they render alike)."""
    items: list[list[tuple[str, str, str | None, str | None]]] = [[]]
    pending: list[str] = []
    n = 0
    for el in root.iter():
        tag = el.tag.removeprefix(NS)
        if tag == "line" and el.get("class") == "ci":
            pending.append(el.get("data-ref", ""))
        elif tag == "circle":
            if n >= len(cats) or len(pending) > 2:
                return ["the figure cannot be rebuilt by the trusted renderer from its own data (items)"]
            cat, est = cats[n], el.get("data-ref", "")
            opts: list[tuple[str, str, str | None, str | None]]
            if len(pending) == 2:
                opts = [(cat, est, pending[0], pending[1])]
            elif len(pending) == 1:
                opts = [(cat, est, pending[0], None), (cat, est, None, pending[0])]
            else:
                opts = [(cat, est, None, None)]
            items = [[*prefix, o] for prefix in items for o in opts]
            if len(items) > 64:  # bounded work: a figure with that many one-sided items is refused, not searched
                return ["the figure has too many one-sided interval items to rebuild"]
            pending, n = [], n + 1
    if pending or n != len(cats):
        return ["the figure cannot be rebuilt by the trusted renderer from its own data (items)"]
    try:
        if any(S.interval_chart(values, it, title=title, y_label=y_label) == text for it in items):
            return []
    except (KeyError, TypeError, ValueError) as e:
        return [f"the figure cannot be rebuilt by the trusted renderer from its own data: {e}"]
    return ["the figure is not the trusted renderer's output for its own data and labels (placement, styling, "
            "viewport or extra elements changed)"]


# ------------------------------------------------------------------ F4 re-render
def run_script(script: bytes, inputs: bytes, sandbox: Sandbox) -> bytes:
    """Run a figure script in `sandbox` (a fresh workspace) and return its figure.svg (capped)."""
    ws = sandbox.workspace
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    (ws / "chh_svg.py").write_bytes(RENDERER_SOURCE)
    (ws / "figure.py").write_bytes(script)
    (ws / "inputs.json").write_bytes(inputs)
    res = sandbox.exec([sandbox.python, "figure.py"], timeout_s=config.FIGURE_RENDER_TIMEOUT_S)
    if res.returncode != 0:
        raise FigureError(f"figure script failed (exit {res.returncode}): {res.stderr[-500:]}")
    out = ws / "figure.svg"
    if not out.is_file() or out.is_symlink():
        raise FigureError("figure script wrote no figure.svg")
    if out.stat().st_size > config.FIGURE_MAX_BYTES:
        raise FigureError(f"figure.svg is over the cap of {config.FIGURE_MAX_BYTES} bytes")
    return out.read_bytes()


def _read(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise FigureError(f"{path.name} is missing")
    if path.stat().st_size > config.FIGURE_MAX_BYTES:
        raise FigureError(f"{path.name} is over the cap of {config.FIGURE_MAX_BYTES} bytes")
    return path.read_bytes()


def make_figure(fig_dir: Path, script: str, refs: dict[str, dict[str, str]], store: Any, sandbox: Sandbox,
                allowed_evidence: set[str] | None = None, *, certificate_of: Certificates | None = None,
                allowed_certificates: set[str] | None = None) -> str:
    """Render a figure (script in the sandbox), refuse it unless it passes F3, write the three files; return the
    sha256 of figure.json (record it: the audit compares it)."""
    fig_dir = Path(fig_dir)
    values = resolve_refs(refs, store, allowed_evidence, certificate_of=certificate_of,
                          allowed_certificates=allowed_certificates)
    data = run_script(script.encode(), inputs_json(refs, values), sandbox)
    problems = check_svg(data, values)
    if problems:
        raise FigureError("the rendered figure fails the audit: " + "; ".join(problems[:5]))
    fig_dir.mkdir(parents=True, exist_ok=True)
    (fig_dir / "figure.py").write_bytes(script.encode())
    (fig_dir / "figure.svg").write_bytes(data)
    meta = {"format": FORMAT, "refs": refs, "script_sha256": _sha(script.encode()), "svg_sha256": _sha(data),
            "renderer_sha256": _sha(RENDERER_SOURCE)}
    body = json.dumps(meta, sort_keys=True, indent=1).encode()
    (fig_dir / "figure.json").write_bytes(body)
    return _sha(body)


def audit_figure(fig_dir: Path, store: Any, sandbox: Sandbox, *, allowed_evidence: set[str] | None = None,
                 meta_sha256: str | None = None, certificate_of: Certificates | None = None,
                 allowed_certificates: set[str] | None = None) -> FigureAudit:
    """F1-F4 (module docstring). Never raises for a bad figure: problems are returned, ok=False (fail closed)."""
    fig_dir = Path(fig_dir)
    try:
        meta_b = _read(fig_dir / "figure.json")
        if meta_sha256 is not None and _sha(meta_b) != meta_sha256:
            return FigureAudit(False, ["F1 figure.json differs from the one recorded when the figure was made"])
        meta = json.loads(meta_b)
        if not isinstance(meta, dict) or meta.get("format") != FORMAT:
            return FigureAudit(False, ["F1 figure.json is not a chh-figure/1 record"])
        script, data = _read(fig_dir / "figure.py"), _read(fig_dir / "figure.svg")
    except (FigureError, ValueError, OSError) as e:
        return FigureAudit(False, [f"F1 {e}"])
    problems: list[str] = []
    if _sha(script) != meta.get("script_sha256"):
        problems.append("F1 figure.py differs from the script recorded in figure.json")
    if _sha(data) != meta.get("svg_sha256"):
        problems.append("F1 figure.svg differs from the output recorded in figure.json")
    try:
        values = resolve_refs(meta.get("refs"), store, allowed_evidence, certificate_of=certificate_of,
                              allowed_certificates=allowed_certificates)
    except FigureError as e:
        return FigureAudit(False, [*problems, f"F2 {e}"])
    problems += [f"F3 {p}" for p in check_svg(data, values)]
    try:
        again = run_script(script, inputs_json(meta["refs"], values), sandbox)
    except FigureError as e:
        problems.append(f"F4 re-render failed: {e}")
    else:
        if again != data:
            problems.append("F4 the script's output on the resolved artifacts differs from the stored figure")
    return FigureAudit(not problems, problems, values)


def audit_figures(entries: Any, store: Any, sandbox_for: Any, allowed_evidence: set[str] | None, *,
                  certificate_of: Certificates | None = None,
                  allowed_certificates: set[str] | None = None) -> dict[str, Any]:
    """Audit I6 over a run's `figures` state: [{"dir", "meta_sha256"}]. "n/a" without figures; anything malformed
    fails closed. `sandbox_for(i)` returns a fresh sandbox for figure i."""
    if entries is None or entries == []:
        return {"I6": "n/a", "problems": []}
    if not isinstance(entries, list):
        return {"I6": False, "problems": ["I6: figures must be a list"]}
    problems: list[str] = []
    for i, f in enumerate(entries):
        if not isinstance(f, dict) or not isinstance(f.get("dir"), str) or not isinstance(f.get("meta_sha256"), str):
            problems.append(f"I6: figure {i} is malformed (needs dir and meta_sha256)")
            continue
        res = audit_figure(Path(f["dir"]), store, sandbox_for(i), allowed_evidence=allowed_evidence,
                           meta_sha256=f["meta_sha256"], certificate_of=certificate_of,
                           allowed_certificates=allowed_certificates)
        problems += [f"I6 figure {f['dir']}: {p}" for p in res.problems]
    return {"I6": not problems, "problems": problems}
