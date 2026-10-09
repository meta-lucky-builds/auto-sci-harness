"""The research report's figure (GOAL §5 L2 "figures rendered by a script stored next to them"; invariant 8 extended
to figures): the held-out effect of each certified claim with its interval, and the claim's required minimum effect.

Every plotted value is a {certificate, pointer} ref into the run's own Kernel ledger certificate (the effect estimate
and bounds are `/resolution/targets/<target>/{estimate,lo,hi}`, the minimum effect `/claim/effect/min_effect`). The
script is written by the harness (a fixed template filled with labels and ref ids, never numbers), run in a fresh
sandbox, checked (F3) before it is stored, and stored next to the figure with figure.json (figures/audit.make_figure).
The audit (I6, research.audit) re-resolves every ref against the ledger and re-renders the figure in a fresh sandbox.

Not plotted (stated): the dev-racing intervals. They are computed on the fly from private dev labels and are not a
ledgered artifact, so no ref could resolve them; plotting them would need a ledger record of the racing interval.
An infinite bound (a one-sided interval) is not plotted: only finite bounds become segments.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .audit import make_figure

FIGURE_NAME = "heldout_effect"
TITLE = "Held-out effect with its interval (Kernel certificate)"
Y_LABEL = "effect"

SCRIPT_TEMPLATE = '''"""Harness-written figure script: held-out effect and interval per claim (refs, no numbers)."""
import chh_svg as S

ITEMS = {items!r}
inp = S.load_inputs()
S.save(S.interval_chart(inp, [tuple(i) for i in ITEMS], title={title!r}, y_label={y_label!r}))
'''


def _ptr(*parts: str) -> str:
    return "".join("/" + p.replace("~", "~0").replace("/", "~1") for p in parts)


def _finite(x: Any) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def figure_spec(certs: list[tuple[str, str, dict[str, Any]]]) -> tuple[list[list[str | None]], dict[str, Any]]:
    """(items, refs) for [(label prefix, certificate hash, certificate JSON)]."""
    items: list[list[str | None]] = []
    refs: dict[str, Any] = {}
    for ci, (prefix, h, cert) in enumerate(certs):
        for ti, (target, t) in enumerate(sorted((cert.get("resolution") or {}).get("targets", {}).items())):
            if not _finite(t.get("estimate")):
                continue
            base = f"c{ci}_t{ti}"
            refs[f"{base}_est"] = {"certificate": h, "pointer": _ptr("resolution", "targets", target, "estimate")}
            lo = hi = None
            if _finite(t.get("lo")):
                lo = f"{base}_lo"
                refs[lo] = {"certificate": h, "pointer": _ptr("resolution", "targets", target, "lo")}
            if _finite(t.get("hi")):
                hi = f"{base}_hi"
                refs[hi] = {"certificate": h, "pointer": _ptr("resolution", "targets", target, "hi")}
            items.append([f"{prefix}: held-out effect on {target}", f"{base}_est", lo, hi])
        if _finite(((cert.get("claim") or {}).get("effect") or {}).get("min_effect")):
            refs[f"c{ci}_min"] = {"certificate": h, "pointer": _ptr("claim", "effect", "min_effect")}
            items.append([f"{prefix}: required minimum effect", f"c{ci}_min", None, None])
    return items, refs


def make_report_figure(state: dict[str, Any], kernel: Any, store: Any, sandbox: Any,
                       fig_dir: Path) -> dict[str, str] | None:
    """Render the report figure for the run's certificate(s); None when the run has no certificate."""
    certs: list[tuple[str, str, dict[str, Any]]] = []
    for prefix, key in (("claim", "certificate"), ("replication", "replica_certificate")):
        info = state.get(key)
        if isinstance(info, dict) and isinstance(info.get("hash"), str):
            certs.append((prefix, info["hash"], kernel.certificate(info["hash"]).model_dump(mode="json")))
    if not certs:
        return None
    items, refs = figure_spec(certs)
    if not items:
        return None
    script = SCRIPT_TEMPLATE.format(items=items, title=TITLE, y_label=Y_LABEL)
    allowed = {h for _, h, _ in certs}
    meta = make_figure(fig_dir, script, refs, store, sandbox, set(),
                       certificate_of=lambda h: kernel.certificate(h).model_dump(mode="json"),
                       allowed_certificates=allowed)
    return {"dir": str(fig_dir), "meta_sha256": meta}
