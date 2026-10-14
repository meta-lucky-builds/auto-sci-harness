"""The scorecard, computed from facts alone (pure: no store, no clock, no randomness beyond the seeded bootstrap).

Every number appears inside a metric object `{"value": ..., "sources": [refs, ...], ...}` whose sources are the
artifacts it was computed from (`numbers_without_sources` checks this). A metric that cannot be computed has value
None, status "not measured" and says why; it carries no number. Nothing is dropped: every axis that fails, loses, is
inconclusive or is not measured is listed under `losses` or `not_shown` (GOAL §2.5), and the overall verdict is
"best" only if both lists are empty.
"""

from __future__ import annotations

from typing import Any

from . import config as C
from .refs import sha
from .registry import AXES, ScorecardError
from .runner import paired_compare

FORMAT = "chh-scorecard/1"
BENCH_AXES = ("A", "B", "C", "D", "E")


def _metric(value: Any, sources: list[str], **kw: Any) -> dict[str, Any]:
    if not sources:
        raise ScorecardError("internal: a number without sources")  # never emitted: invariant 8
    return {"value": value, "sources": sorted(set(sources)), **kw}


def _unmeasured(why: str) -> dict[str, Any]:
    return {"value": None, "status": "not measured", "why": why}


def _certified(c: dict[str, Any]) -> bool:
    return str(c["label"]).startswith("CERTIFIED-") and c["latest_for_claim"] and not c["retracted"]


# ------------------------------------------------------------------ axis F
def axis_f(facts: dict[str, Any]) -> dict[str, Any]:
    certs = [c for c in facts["certificates"] if _certified(c)]
    claims = facts["store_claims"]
    reports = [r for r in facts["runs"] if "report" in r]
    out: dict[str, Any] = {}
    # F1: claims without evidence (certified certificates, store claims, report claims naming no ledger certificate)
    offenders = [c["ref"] for c in certs if c["n_evidence"] == 0 or c["unresolved_evidence"] or c["load_problem"]]
    offenders += [c["ref"] for c in claims if c["n_evidence"] == 0 or c["unresolved_evidence"]]
    offenders += [r["ref"] for r in reports for cl in r["report"]["claims"] if not cl["in_ledger"]]
    population = [c["ref"] for c in certs] + [c["ref"] for c in claims] + [r["ref"] for r in reports]
    out["claims_without_evidence"] = (
        _metric(len(offenders), population, of=len(population), offenders=sorted(offenders),
                status="pass" if not offenders else "fail")
        if population else _unmeasured("no claims, certificates or reports in this store"))
    # F2: unverifiable citations
    cites = [(r["ref"], c) for r in facts["runs"] for c in r.get("citations", [])]
    cites += [(c["ref"], c) for c in facts["claim_audit_citations"]]
    bad = sorted({ref for ref, c in cites if c["status"] != "verified"})
    examined = [r["ref"] for r in facts["runs"]] + [c["ref"] for c in facts["claim_audit_citations"]]
    n_bad = sum(1 for _, c in cites if c["status"] != "verified")
    out["unverifiable_citations"] = (
        _metric(n_bad, examined, of=len(cites), offenders=bad, status="pass" if not n_bad else "fail",
                note="no citations in the campaign" if not cites else "")
        if examined else _unmeasured("no runs or audit records to look for citations in"))
    # F3: reports with checklist and bundle
    claimed = [(r["ref"], cl) for r in reports for cl in r["report"]["claims"]]
    good = [ref for ref, cl in claimed if cl["in_ledger"] and cl["checklist_missing"] == [] and cl["bundle_ledgered"]]
    no_claim = sorted(r["ref"] for r in reports if not r["report"]["claims"])
    if claimed:
        pct = 100.0 * len(good) / len(claimed)
        out["reports_with_checklist_and_bundle"] = _metric(
            pct, [ref for ref, _ in claimed], unit="%", numerator=len(good), denominator=len(claimed),
            offenders=sorted({ref for ref, _ in claimed} - set(good)), reports_without_claim=no_claim,
            status="pass" if len(good) == len(claimed) else "fail")
    else:
        out["reports_with_checklist_and_bundle"] = _unmeasured(
            f"no report in this store names a certificate ({len(no_claim)} report(s) make no claim)")
    if facts["ledger_intact"] is not True:
        out["ledger"] = {"value": None, "status": "fail", "why": "the Kernel ledger hash chain does not verify"}
    out["status"] = _combine(v.get("status", "pass") for v in out.values() if isinstance(v, dict))
    return out


# ------------------------------------------------------------------ axis G
def axis_g(facts: dict[str, Any]) -> dict[str, Any]:
    runs = facts["runs"]
    certs = [c for c in facts["certificates"] if _certified(c)]
    out: dict[str, Any] = {}
    if not runs:
        return {"status": "not measured", "total_cost": _unmeasured("no runs in this store")}
    run_refs = [r["ref"] for r in runs]
    total = {u: sum(float(r["cost"][u]) for r in runs) for u in ("usd", "gpu_hours", "quota_units", "tokens")}
    estimated = any(r["cost"]["estimated"] for r in runs)
    out["total_cost"] = _metric(total, run_refs, estimated=estimated, runs=len(runs))
    if certs:
        out["cost_per_certified_claim"] = _metric({u: v / len(certs) for u, v in total.items()},
                                                  run_refs + [c["ref"] for c in certs], certified_claims=len(certs),
                                                  estimated=estimated)
    else:
        out["cost_per_certified_claim"] = _unmeasured("0 Kernel-certified claims: cost per certified claim is "
                                                      "undefined (all spend bought no certified claim)")
    out["baseline"] = {"value": None, "status": "not compared",
                       "why": "no re-run baseline reports a cost per Kernel-certified claim"}
    out["status"] = "not measured" if not certs else "not compared"
    return out


# ------------------------------------------------------------------ axis H
def axis_h(facts: dict[str, Any]) -> dict[str, Any]:
    inp = facts["inputs"]
    out: dict[str, Any] = {}
    museum = inp.get("museum")
    if museum and museum["outcomes"]:
        o = museum["outcomes"]
        killed = sum(1 for x in o if x["verdict"] == "KILLED")
        out["planted_exploit_catch_rate"] = _metric(
            killed / len(o), [museum["ref"]], numerator=killed, denominator=len(o),
            not_killed=sorted(x["finding"] for x in o if x["verdict"] != "KILLED"),
            status="pass" if killed == len(o) else "fail")
    else:
        out["planted_exploit_catch_rate"] = _unmeasured("no museum run log given (`python scripts/museum.py`)")
    fnd = inp.get("findings")
    if fnd and fnd["statuses"]:
        st = fnd["statuses"]
        n_open = sum(1 for v in st.values() if v == "open")
        out["red_team_open_rate"] = _metric(n_open / len(st), [fnd["ref"]], numerator=n_open, denominator=len(st),
                                            open=sorted(k for k, v in st.items() if v == "open"),
                                            status="pass" if n_open == 0 else "fail",
                                            note="one snapshot: a trend needs successive scorecards")
    else:
        out["red_team_open_rate"] = _unmeasured("no findings registry given (redteam/findings.yaml)")
    audits = [r for r in facts["runs"] if "leak_audit_passed" in r]
    if audits:
        ok = sum(1 for r in audits if r["leak_audit_passed"])
        rate = ok / len(audits)
        out["leak_audit_pass_rate"] = _metric(rate, [r["ref"] for r in audits], numerator=ok,
                                              denominator=len(audits),
                                              status="pass" if rate >= C.LEAK_AUDIT_REQUIRED_RATE else "fail")
    else:
        out["leak_audit_pass_rate"] = _unmeasured("no run in this store has a leak audit (I5)")
    salts = inp.get("salts")
    if salts:
        a = salts["audit"]
        fc = a.get("false_certification") or {}
        flag = bool(a.get("pooled_inflation_flag"))
        out["salts"] = _metric({"false_certification": fc, "inflated": a.get("inflated"),
                                "coverage": a.get("coverage"), "pooled_inflation_z": a.get("pooled_inflation_z")},
                               [salts["ref"]], pooled_inflation_flag=flag,
                               status="fail" if flag or int(fc.get("k") or 0) > 0 else "pass")
    else:
        out["salts"] = _unmeasured("no salt campaign audit given (needs owner: salt budget, GOAL §16.9)")
    out["status"] = _combine(v.get("status", "pass") for v in out.values() if isinstance(v, dict))
    return out


# ------------------------------------------------------------------ axes A-E and the ablations
def _latest(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in runs:
        out[r["system"]] = r  # in store order: the latest run of each system
    return out


def _bench_rows(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for r in facts["scorecard_runs"]:
        by.setdefault(r["benchmark"], []).append(r)
    rows: dict[str, dict[str, Any]] = {}
    for key, runs in sorted(by.items()):
        ours_all = [r for r in runs if r["role"] == "ours"]
        base = _latest([r for r in runs if r["role"] == "baseline"])
        reported = [x for x in facts["as_reported"] if x["benchmark"] == key]
        row: dict[str, Any] = {"benchmark": key, "axis": runs[0]["axis"]}
        if not ours_all:
            row.update(status="not measured", why="no run of our system on this benchmark")
            rows[key] = row
            continue
        ours = ours_all[-1]
        hib = bool(ours["higher_is_better"])
        row["ours"] = _metric(ours["mean"], [ours["ref"], f"blob:{ours['results_blob']}"], n=ours["n"],
                              system=ours["system"], runs_of_ours=len(ours_all),
                              note="latest run of our system; earlier runs are counted in runs_of_ours")
        comps: list[dict[str, Any]] = []
        for name, b in sorted(base.items()):
            if b["content_hash"] != ours["content_hash"]:
                comps.append({"system": name, "status": "not compared", "why": "different benchmark bytes"})
                continue
            c = paired_compare(ours["scores"], b["scores"], hib)
            comps.append({"system": name, "basis": "re-run",
                          "baseline": _metric(b["mean"], [b["ref"], f"blob:{b['results_blob']}"], n=b["n"]),
                          "ours_minus_baseline": _metric(c, [ours["ref"], b["ref"]])})
        row["baselines"] = comps  # re-runs first, then as-reported stand-ins (each labelled by basis)
        rerun_systems = {c["system"] for c in comps if c.get("basis") == "re-run"}
        latest_reported = {x["system"]: x for x in reported}  # latest record per system
        row["as_reported"] = []
        for name, x in sorted(latest_reported.items()):
            entry: dict[str, Any] = {"system": name, "label": "as reported (not re-run)",
                                     "value": _metric(x["value"], [x["ref"]], source=x["source"])}
            if name in rerun_systems:
                entry["used"] = "no: this system was re-run; the re-run decides (GOAL §2.3)"
            else:  # GOAL §2.3: the as-reported number stands in only where the code cannot be re-run, labelled
                c = paired_compare(ours["scores"], {k: x["value"] for k in ours["scores"]}, hib)
                entry["used"] = "yes: no re-run of this system exists"
                entry["ours_minus_as_reported"] = _metric(c, [ours["ref"], x["ref"]])
                comps.append({"system": name, "basis": "as reported (not re-run)",
                              "baseline": entry["value"], "ours_minus_baseline": entry["ours_minus_as_reported"]})
            row["as_reported"].append(entry)
        candidates = [c for c in comps if c.get("basis")]
        if candidates:
            sign = 1.0 if hib else -1.0
            best = max(candidates, key=lambda c: (sign * c["baseline"]["value"], c["basis"] == "re-run"))
            row.update(best_baseline=best["system"], basis=best["basis"],
                       verdict=best["ours_minus_baseline"]["value"]["verdict"])
        else:
            row.update(verdict="no baseline", basis=None)
        row["status"] = {"win": "pass", "lose": "lose", "inconclusive": "inconclusive"}.get(row["verdict"],
                                                                                            "not compared")
        rows[key] = row
    return rows


def _timesplit_row(ts: dict[str, Any]) -> dict[str, Any]:
    """Axis E's time-split prediction benchmark (GOAL §6.4) from a `chh timesplit compare` output: evolved vs seed
    harness on the LAST window. 'pass' only for the verdict 'evolved better'; anything else is listed with its
    reasons."""
    comp = ts.get("comparison") or {}
    windows = comp.get("windows") or []
    last = windows[-1] if windows else {}
    row: dict[str, Any] = {"benchmark": "time-split prediction benchmark (GOAL §6.4)", "axis": "E",
                           "basis": "seed harness re-run on the same frozen benchmark", "verdict": comp.get("verdict"),
                           "reasons": list(comp.get("reasons") or [])}
    if last.get("gain_diff") is not None:
        row["evolved_minus_seed_last_window"] = _metric(
            {"gain_bits": last["gain_diff"], "brier": last.get("brier_diff"), "pairs": last.get("n_pairs")},
            [ts["ref"]])
    row["status"] = "pass" if comp.get("verdict") == "evolved better" else "inconclusive"
    return row


def bench_axes(facts: dict[str, Any]) -> dict[str, Any]:
    rows = _bench_rows(facts)
    pinned = facts["inputs"].get("registry", {}).get("benchmarks", [])
    out: dict[str, Any] = {}
    for ax in BENCH_AXES:
        mine = [r for r in rows.values() if r["axis"] == ax]
        unrun = sorted(b["key"] for b in pinned if b["axis"] == ax and b["key"] not in rows)
        if ax == "E" and facts["inputs"].get("timesplit"):
            mine = [*mine, _timesplit_row(facts["inputs"]["timesplit"])]
        if not mine:
            out[ax] = {"status": "not measured", "benchmarks": [], "pinned_but_not_run": unrun,
                       "why": "no pinned benchmark on this axis has a recorded run (real benchmarks need downloads: "
                              "needs owner)"}
            continue
        out[ax] = {"benchmarks": mine, "pinned_but_not_run": unrun,
                   "status": _combine([r["status"] for r in mine] + (["not measured"] if unrun else []))}
    return out


def coverage(facts: dict[str, Any]) -> dict[str, Any]:
    caps = facts["inputs"].get("capabilities")
    if not caps:
        return {"status": "not measured", "why": "no capability registry given"}
    ours = {}
    for r in facts["scorecard_runs"]:
        if r["role"] == "ours":
            ours[r["benchmark"]] = r
    rows, gaps, switched = [], [], []
    for cap in caps["capabilities"]:
        # a row counts as switched only with a real, tested switch in the closed registry (registry.switch_fact)
        if not cap.get("has_switch"):
            gaps.append(cap["id"])
            rows.append({"id": cap["id"], "status": "gap", "switch": cap.get("switch"),
                         "why": cap.get("why") or "no ablation switch"})
            continue
        sw = cap["switch"]
        switched.append(cap["id"])
        abl = [r for r in facts["scorecard_runs"] if r["role"] == "ablation" and r["disabled"] == [sw]]
        if not abl:
            gaps.append(cap["id"])
            rows.append({"id": cap["id"], "status": "gap", "switch": sw,
                         "why": f"switch {sw!r}: no ablation run recorded"})
            continue
        res = []
        for a in abl:
            full = ours.get(a["benchmark"])
            if (full is None or full["content_hash"] != a["content_hash"] or full["system"] != a["system"]
                    or full["system_version"] != a["system_version"]):
                res.append({"benchmark": a["benchmark"], "status": "not compared",
                            "why": "no run of the same full system (name and version) on the same benchmark bytes"})
                continue
            c = paired_compare(full["scores"], a["scores"], bool(a["higher_is_better"]))
            res.append({"benchmark": a["benchmark"], "full_minus_ablated": _metric(c, [full["ref"], a["ref"]]),
                        "contribution": {"win": "shown", "lose": "ablated is better",
                                         "inconclusive": "not shown"}[c["verdict"]]})
        if not any("full_minus_ablated" in r for r in res):
            gaps.append(cap["id"])
            rows.append({"id": cap["id"], "switch": sw, "status": "gap", "results": res,
                         "why": "no ablation run is comparable with a run of the full system"})
            continue
        rows.append({"id": cap["id"], "switch": sw, "status": "ablated", "results": res})
    n_ok = len(caps["capabilities"]) - len(gaps)
    return {"capabilities_with_ablation": _metric(n_ok, [caps["ref"]], of=len(caps["capabilities"])),
            "capabilities_with_switch": _metric(len(switched), [caps["ref"]], of=len(caps["capabilities"])),
            "gaps": gaps, "rows": rows, "status": "pass" if not gaps else "fail"}


def _combine(statuses: Any) -> str:
    s = list(statuses)
    for worst in ("fail", "lose", "not measured", "inconclusive", "not compared"):
        if worst in s:
            return worst
    return "pass"


def build(facts: dict[str, Any]) -> dict[str, Any]:
    axes: dict[str, Any] = bench_axes(facts)
    axes["F"], axes["G"], axes["H"] = axis_f(facts), axis_g(facts), axis_h(facts)
    cov = coverage(facts)
    losses: list[str] = []
    not_shown: list[str] = []
    for ax in AXES:
        st = axes[ax]["status"]
        (losses if st in ("fail", "lose") else not_shown if st != "pass" else []).append(f"axis {ax}: {st}")
        for name, v in axes[ax].items():
            if isinstance(v, dict) and v.get("status") in ("fail", "lose"):
                losses.append(f"axis {ax} {name}: {v['status']}")
        for b in axes[ax].get("benchmarks", []) if isinstance(axes[ax].get("benchmarks"), list) else []:
            if b["status"] in ("lose",):
                losses.append(f"axis {ax} {b['benchmark']}: lose vs {b.get('best_baseline')} ({b.get('basis')})")
    if cov["status"] != "pass":
        (losses if cov["status"] == "fail" else not_shown).append(
            f"coverage (§2.1, invariant 12): {cov['status']}" + (f", {len(cov['gaps'])} capability(ies) without an "
                                                                 f"ablation" if cov.get("gaps") else ""))
    overall = "best on every axis" if not losses and not not_shown else (
        f"NOT best: {len(losses)} loss/failure(s), {len(not_shown)} axis(es) not shown")
    return {"format": FORMAT, "facts_sha256": sha(facts), "axes": axes, "coverage": cov, "losses": losses,
            "not_shown": not_shown, "overall": overall,
            "parameters": {"confidence": C.CONFIDENCE, "n_boot": C.N_BOOT, "boot_seed": C.BOOT_SEED,
                           "source": "custom_harness/scorecard/config.py (UNCALIBRATED)"}}


def numbers_without_sources(card: Any, path: str = "", covered: bool = False) -> list[str]:
    """Paths of numbers in the scorecard that are not inside a metric with sources (`parameters` are constants and
    `facts_sha256` is a hash string, so neither is a measured number)."""
    out: list[str] = []
    if isinstance(card, dict):
        here = covered or bool(card.get("sources"))
        for k, v in card.items():
            if path == "" and k == "parameters":
                continue
            out += numbers_without_sources(v, f"{path}/{k}", here)
    elif isinstance(card, list):
        for i, v in enumerate(card):
            out += numbers_without_sources(v, f"{path}/{i}", covered)
    elif isinstance(card, int | float) and not isinstance(card, bool) and not covered:
        out.append(path)
    return out


def all_sources(card: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(card, dict):
        for k, v in card.items():
            if k == "sources" and isinstance(v, list):
                out |= {str(x) for x in v}
            else:
                out |= all_sources(v)
    elif isinstance(card, list):
        for v in card:
            out |= all_sources(v)
    return out
