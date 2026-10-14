"""Scorecard bundle: build it from a campaign's store, and reproduce it with one command (GOAL §2.4, invariant 8).

    chh scorecard build --out DIR [--museum-log F] [--findings F] [--salts F] [--registry F] [--capabilities F]
    chh scorecard check DIR

A bundle directory holds
    facts.json       everything read from the store and the input files, each item with its ref (facts.py)
    scorecard.json   the numbers, computed from facts.json alone (axes.py); every number names its sources
    scorecard.md     the human rendering, losses first
    inputs/<sha256>  byte copies of the input files (museum log, findings registry, salts audit, registries)
    manifest.json    format, facts hash, scorecard hash, the input refs

`check` (the one command) fails closed unless ALL hold: scorecard.json equals a recomputation from facts.json;
facts.json equals a fresh extraction from the store (with the bundled input copies); every number sits in a metric
with sources; and every source ref resolves to the artifact it pins. A bundle checked without a store is reported
as not reproduced (the store refs cannot be resolved).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..store.evidence import EvidenceStore
from . import axes
from .facts import extract
from .refs import canonical, resolve, sha

INPUT_NAMES = ("museum_log", "findings", "salts_audit", "registry", "capabilities", "timesplit")


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build_scorecard(store: EvidenceStore, out_dir: Path, **inputs: Path | None) -> dict[str, Any]:
    unknown = set(inputs) - set(INPUT_NAMES)
    if unknown:
        raise ValueError(f"unknown scorecard inputs {sorted(unknown)}")
    out = Path(out_dir)
    facts = extract(store, inputs_dir=out / "inputs", **inputs)
    card = axes.build(facts)
    _write(out / "facts.json", canonical(facts))
    _write(out / "scorecard.json", canonical(card))
    _write(out / "scorecard.md", render(card).encode("utf-8"))
    refs = {name: v["ref"] for name, v in facts["inputs"].items()}
    manifest = {"format": axes.FORMAT, "facts_sha256": sha(facts), "scorecard_sha256": sha(card), "inputs": refs}
    _write(out / "manifest.json", canonical(manifest))
    return card


_REF_INPUT = {"museum": "museum_log", "findings": "findings", "salts": "salts_audit", "registry": "registry",
              "capabilities": "capabilities", "timesplit": "timesplit"}


def check_scorecard(bundle_dir: Path, store: EvidenceStore | None) -> dict[str, Any]:
    d = Path(bundle_dir)
    problems: list[str] = []
    try:
        facts = json.loads((d / "facts.json").read_text(encoding="utf-8"))
        card = json.loads((d / "scorecard.json").read_text(encoding="utf-8"))
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"reproduced": False, "problems": [f"bundle unreadable: {e}"]}
    if not all(isinstance(x, dict) for x in (facts, card, manifest)):
        return {"reproduced": False, "problems": ["bundle malformed: facts, scorecard and manifest must be objects"]}
    if manifest.get("facts_sha256") != sha(facts) or card.get("facts_sha256") != sha(facts):
        problems.append("facts.json does not match the hash in the manifest / scorecard")
    if manifest.get("scorecard_sha256") != sha(card):
        problems.append("scorecard.json does not match the hash in the manifest")
    try:
        again = axes.build(facts)
    except Exception as e:  # noqa: BLE001  fail closed: facts that cannot be recomputed are not reproduced
        problems.append(f"scorecard cannot be recomputed from facts.json: {type(e).__name__}: {e}")
    else:
        if canonical(again) != canonical(card):
            problems.append("scorecard.json differs from its recomputation from facts.json")
    problems += [f"number without sources at {p}" for p in axes.numbers_without_sources(card)]
    inputs_dir = d / "inputs"
    raw_inputs = facts.get("inputs")
    input_refs = {str(v.get("ref")) for v in (raw_inputs.values() if isinstance(raw_inputs, dict) else [])
                  if isinstance(v, dict)}
    for ref in sorted(axes.all_sources(card) | input_refs):  # every input copy, used as a source or not
        why = resolve(ref, store, inputs_dir)
        if why:
            problems.append(f"source does not resolve: {why}")
    if store is None:
        problems.append("no store given: facts were not re-extracted, store refs not resolved (not reproduced)")
    else:
        paths = {}
        inputs = facts.get("inputs") if isinstance(facts, dict) else None
        for key, v in (inputs if isinstance(inputs, dict) else {}).items():
            if key not in _REF_INPUT or not isinstance(v, dict):
                problems.append(f"facts.json names an unknown input {key!r}")
                continue
            digest = str(v.get("ref", "")).split(":")[1] if str(v.get("ref", "")).count(":") >= 2 else ""
            paths[_REF_INPUT[key]] = inputs_dir / digest
        try:
            fresh = extract(store, **paths)
        except Exception as e:  # noqa: BLE001  fail closed: an extraction that crashes is not a reproduction
            problems.append(f"facts cannot be re-extracted from the store: {type(e).__name__}: {e}")
        else:
            if canonical(fresh) != canonical(facts):
                diff = sorted(k for k in set(fresh) | set(facts) if canonical(fresh.get(k)) != canonical(facts.get(k)))
                problems.append(f"facts re-extracted from the store differ in {diff}")
    return {"reproduced": not problems, "problems": problems, "overall": card.get("overall"),
            "losses": card.get("losses"), "not_shown": card.get("not_shown")}


def _fmt(v: Any) -> str:
    if isinstance(v, dict) and "value" in v:
        if v["value"] is None:
            return f"not measured ({v.get('why', '')})"
        val = v["value"]
        if isinstance(val, float):
            val = f"{val:.6g}"
        extra = f" [{v['status']}]" if "status" in v else ""
        return f"{val}{extra}"
    return str(v)


def render(card: dict[str, Any]) -> str:
    lines = ["# Scorecard (GOAL §2)", "", f"**Overall: {card['overall']}**", "",
             "## Losses and failures (reported, GOAL §2.5)", ""]
    lines += [f"- {x}" for x in card["losses"]] or ["- none"]
    lines += ["", "## Not shown (not measured, not compared or inconclusive)", ""]
    lines += [f"- {x}" for x in card["not_shown"]] or ["- none"]
    for ax in ("A", "B", "C", "D", "E"):
        a = card["axes"][ax]
        lines += ["", f"## Axis {ax}: {a['status']}", ""]
        if not a.get("benchmarks"):
            lines.append(f"- {a.get('why', '')}")
        for b in a.get("benchmarks", []):
            lines.append(f"- `{b['benchmark']}`: verdict **{b.get('verdict', b['status'])}** vs "
                         f"{b.get('best_baseline')} (basis: {b.get('basis')}); ours {_fmt(b.get('ours', {}))}")
            for x in b.get("as_reported", []):
                lines.append(f"  - {x['system']}: {_fmt(x['value'])} ({x['label']}; {x['value']['source']})")
    for ax in ("F", "G", "H"):
        lines += ["", f"## Axis {ax}: {card['axes'][ax]['status']}", ""]
        lines += [f"- {k}: {_fmt(v)}" for k, v in card["axes"][ax].items() if k != "status"]
    cov = card["coverage"]
    lines += ["", f"## Coverage and ablations (invariant 12): {cov['status']}", ""]
    if "capabilities_with_ablation" in cov:
        lines.append(f"- capabilities with an ablation: {cov['capabilities_with_ablation']['value']} of "
                     f"{cov['capabilities_with_ablation']['of']}; gaps: {', '.join(cov['gaps']) or 'none'}")
        lines.append(f"- capabilities with a real, tested switch (code coverage only, no run): "
                     f"{cov['capabilities_with_switch']['value']} of {cov['capabilities_with_switch']['of']}")
        lines += [f"  - {r['id']}: {r['why']}" for r in cov["rows"] if r.get("status") == "gap"]
    else:
        lines.append(f"- {cov.get('why')}")
    lines += ["", f"Reproduce: `chh scorecard check <this directory>` (facts sha256 `{card['facts_sha256']}`)", ""]
    return "\n".join(lines)
