"""Local scorecard (`chh scorecard local`): the GOAL §2 axes computable on this machine, with no download and no model
call, from the repo, one Evidence store (all its recorded runs) and the red-team artifacts. Every axis appears:
what cannot be computed is NOT MEASURED with its reason, never omitted (GOAL §2 rule 5).

Every number sits in a metric `{"value", "sources": [ref, ...]}` (invariant 8; `axes.numbers_without_sources`
checks the card) and `check_local` resolves every ref. Ref kinds: those of `refs.py` (ledger / blob / record / run /
claim) plus
    repo:<relative path>:<sha256>            a file of the repo working tree, at those bytes
    git:<commit>:<relative path>:<sha256>    a file as committed (`git show commit:path`), at those bytes
    file:<sha256>:<absolute path>            a file outside the repo (the museum log; a run bundle zip's manifest
                                             is `runbundle:<manifest sha256>:<absolute path>`)

Axes:
  A, C, D, E  pinned-benchmark axes: measured only from `scorecard_run` records (runner.py) on a pinned benchmark;
              the shipped registry is empty (downloads are an owner decision), so today NOT MEASURED.
  B (partial) self-improvement efficiency from the evolve ledger: accepted gate gain per 1k harness runs and the OOD
              drop (LOFO held-out families) of accepted edits. Only generations whose `generation_stats` record the
              harness runs they spent (since 2026-10-09) count; nothing is estimated. No re-run baseline: never a win.
  F           claims without evidence, unverifiable citations (axes.axis_f), reports passing the publication gate
              (checklist + loop label, publication.py) with a ledgered claim bundle, and runs with a Reproducibility
              Bundle (`run_bundle` record whose zip still has that manifest) and a cost record (a trace).
  G           cost per Kernel-certified LIVE claim. Units are summed per unit; an unknown unit is shown as unknown,
              never 0: USD is unknown for any run with a subscription (byos), open-weights or unpriced API call;
              GPU hours are not metered. A run whose model calls are all scripted (mock gateway) or replayed spends
              nothing real; a certificate from such runs is not a live claim.
  H           planted-exploit catch rate from a museum run log (KILLED / patches, Wilson 95% two-sided interval;
              findings not in the log are listed as not run), the red-team findings trend by date from the git
              history of redteam/findings.yaml, and the leak-audit pass rate of the store's runs.
  loop labels every loop instance of the store with its MECHANICAL/VALIDATED label (loop_labels.py, invariant 10).
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path
from typing import Any

import yaml

from ..loop_labels import audit_store
from ..publication import blockers as publication_blockers
from ..store.evidence import EvidenceStore
from . import axes
from .facts import _final_state, extract, parse_museum_log
from .refs import canonical, record_ref, resolve, run_ref, sha

FORMAT = "chh-scorecard-local/1"
FINDINGS = "redteam/findings.yaml"
Z95 = 1.959963984540054  # two-sided 95%: the 0.975 quantile of the standard normal
_metric, _unmeasured = axes._metric, axes._unmeasured

NOT_MEASURED_WHY = {
    "A": "agent-harness benchmarks (Terminal-Bench 2.x, SWE-bench Verified, GAIA) need downloads: owner decision; no "
         "pinned benchmark and no scorecard_run on this axis",
    "C": "research-execution suites (MLE-bench-style, ScienceIDE ML-repair) need downloads; our ML task suite needs "
         "the M2 exit run (about 300 live model calls: owner budget); no scorecard_run on this axis",
    "D": "no known-value bound problem and no T2-certified ML record exist (M9); no scorecard_run on this axis",
    "E": "ResearchBench needs a download; the SDE sentinel is not built; the time-split prediction benchmark needs "
         "post-cutoff data and a live budget (GOAL §6.4); no scorecard_run on this axis",
}


# ------------------------------------------------------------------ small helpers
def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials (two-sided; z = 1.96 for 95%)."""
    if n <= 0 or not 0 <= k <= n:
        raise ValueError("wilson needs 0 <= k <= n and n > 0")
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True, timeout=120).stdout


# ------------------------------------------------------------------ extraction (reads the store, repo and log)
def _modes(store: EvidenceStore, run_id: str) -> tuple[list[dict[str, Any]], Any]:
    """Every model call of the run (step provenance and module calls) with its step's USD and how many real calls
    share that step: a step records only a TOTAL cost, so a call's own price is known only when it is the step's
    single real call."""
    from ..records import Cost
    calls: list[dict[str, Any]] = []
    total = Cost()
    for step in store.trace(run_id):
        total = total + step.cost
        provs = [step.provenance.model_dump(mode="json")] if step.provenance else []
        out = step.output if isinstance(step.output, dict) else {}
        provs += [c.get("provenance") or {} for c in out.get("_module_llm_calls", []) or [] if isinstance(c, dict)]
        here = [{"mode": str(p.get("mode")), "replayed": bool(p.get("replayed")), "backend": str(p.get("backend")),
                 "usd": float(step.cost.usd)} for p in provs if p]
        n_real = sum(1 for c in here if c["mode"] != "mock" and not c["replayed"])
        calls += [{**c, "step_real_calls": n_real} for c in here]
    return calls, total


def _priced(c: dict[str, Any]) -> bool:
    """A real call's USD is known only for an API call that is the single real call of a step reporting USD > 0
    (subscription, open-weights and unpriced API calls carry usd 0.0 = unknown in records.Cost)."""
    return c["mode"] == "api" and c["usd"] > 0 and c["step_real_calls"] == 1


def _run_fact(store: EvidenceStore, run_id: str, bundles: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    calls, cost = _modes(store, run_id)
    real = [c for c in calls if c["mode"] != "mock" and not c["replayed"]]
    unpriced = [c for c in real if not _priced(c)]
    steps = store.trace(run_id)
    fact: dict[str, Any] = {
        "ref": run_ref(store, run_id), "run_id": run_id, "n_steps": len(steps),
        "model_calls": len(calls), "real_model_calls": len(real),
        "spend": "none (no model call)" if not calls else "scripted (mock or replayed calls only: no real spend)"
        if not real else "real",
        "cost": {"tokens": cost.prompt_tokens + cost.completion_tokens, "quota_units": cost.quota_units,
                 "wall_s": cost.wall_s, "usd": None if unpriced else cost.usd, "usd_unknown_calls": len(unpriced),
                 "gpu_hours": cost.gpu_hours if cost.gpu_hours > 0 else None, "estimated": cost.estimated}}
    bl = []
    for rec in bundles.get(run_id, []):
        row: dict[str, Any] = {"ref": record_ref("run_bundle", rec), "error": rec.get("error")}
        if not rec.get("error"):
            row["path"], row["manifest_sha256"] = rec.get("path"), rec.get("manifest_sha256")
            from ..store.run_bundle import RunBundleError, read_run_bundle_bytes
            try:
                _, got = read_run_bundle_bytes(Path(str(rec.get("path"))))
                row["present"] = got == rec.get("manifest_sha256")
                if row["present"]:
                    row["zip_ref"] = f"runbundle:{got}:{Path(str(rec.get('path'))).resolve()}"
            except (OSError, RunBundleError, ValueError):
                row["present"] = False
        bl.append(row)
    fact["run_bundles"] = bl
    state = _final_state(store, run_id)
    if isinstance(state, dict) and "report" in state:
        rep: dict[str, Any] = {"claims": []}
        for p in ("", "replica_"):
            info = state.get(f"{p}certificate")
            if isinstance(info, dict):
                rep["claims"].append({"prefix": p, "cert_hash": str(info.get("hash")),
                                      "gate_blockers": publication_blockers(state.get(f"{p}checklist"),
                                                                            is_loop=True),
                                      "publishable": state.get(f"{p}publishable") is True})
        fact["report"] = rep
    return fact


def _evolve_facts(store: EvidenceStore) -> dict[str, Any]:
    stats = [{"ref": record_ref("generation_stats", b), "parent": b.get("parent"),
              "accepted": b.get("accepted"), "harness_runs": b.get("harness_runs"),
              "loop_label": b.get("loop_label")} for b in store.records("generation_stats")]
    edits = [{"ref": record_ref("harness_edit", b), "parent": b.get("parent"), "accepted": b.get("accepted"),
              "actual_gate_delta": b.get("actual_gate_delta"),
              "ood_drop": ((b.get("lofo") or {}).get("ood_drop") if isinstance(b.get("lofo"), dict) else None),
              "loop_label": b.get("loop_label")} for b in store.records("harness_edit")]
    return {"generation_stats": stats, "harness_edits": edits}


def _museum_fact(path: Path) -> dict[str, Any]:
    data = Path(path).read_bytes()
    text = data.decode("utf-8", errors="replace")
    first = text.splitlines()[0] if text.strip() else ""
    head = None
    for ln in text.splitlines()[:6]:  # locked_run.py writes "worktree: <path>  HEAD: <sha>"
        if "HEAD:" in ln:
            head = ln.split("HEAD:", 1)[1].strip().split()[0] if ln.split("HEAD:", 1)[1].strip() else None
    if head is None and first[:7] and all(c in "0123456789abcdef" for c in first.split(" ")[0]) \
            and len(first.split(" ")[0]) >= 7:
        head = first.split(" ")[0]
    outcomes: dict[str, str] = {}
    for o in parse_museum_log(text):
        outcomes[o["finding"]] = o["verdict"]  # the last outcome of a finding counts (a re-run after a repair)
    return {"ref": f"file:{_sha_bytes(data)}:{Path(path).resolve()}", "log_head": head,
            "outcomes": dict(sorted(outcomes.items()))}


def _findings_fact(repo: Path, use_git: bool) -> dict[str, Any]:
    p = repo / FINDINGS
    data = p.read_bytes()
    doc = yaml.safe_load(data.decode("utf-8")) or {}
    rows = doc.get("findings") or []
    out: dict[str, Any] = {
        "ref": f"repo:{FINDINGS}:{_sha_bytes(data)}",
        "statuses": {str(f.get("id")): str(f.get("status")) for f in rows},
        "patched": sorted(str(f.get("id")) for f in rows if f.get("museum"))}
    if not use_git:
        out["history"] = None
        out["history_problem"] = "git history not read (--no-git)"
        return out
    try:
        log = _git(repo, "log", "--format=%H %cI", "--", FINDINGS).decode().split("\n")
        head = _git(repo, "rev-parse", "HEAD").decode().strip()
    except (OSError, subprocess.SubprocessError) as e:
        out["history"], out["history_problem"] = None, f"git unavailable: {type(e).__name__}"
        return out
    by_day: dict[str, dict[str, Any]] = {}
    for line in reversed([x for x in log if x.strip()]):  # oldest first; the LAST commit of a day stands for it
        commit, when = line.split(" ", 1)
        try:
            blob = _git(repo, "show", f"{commit}:{FINDINGS}")
            d = yaml.safe_load(blob.decode("utf-8")) or {}
        except (subprocess.SubprocessError, yaml.YAMLError, UnicodeDecodeError):
            continue
        st = [str(f.get("status")) for f in d.get("findings") or [] if isinstance(f, dict)]
        by_day[when[:10]] = {"date": when[:10], "commit": commit,
                             "ref": f"git:{commit}:{FINDINGS}:{_sha_bytes(blob)}", "total": len(st),
                             "fixed": st.count("fixed"), "open": st.count("open"), "accepted": st.count("accepted")}
    out["history"] = [by_day[k] for k in sorted(by_day)]
    out["git_head"] = head
    return out


def extract_local(store: EvidenceStore, repo: Path, *, museum_log: Path | None = None,
                  use_git: bool = True) -> dict[str, Any]:
    repo = Path(repo).resolve()
    base = extract(store)  # certificates, store claims, runs (reports, citations, leak audits), scorecard runs
    bundles: dict[str, list[dict[str, Any]]] = {}
    for b in store.records("run_bundle"):
        bundles.setdefault(str(b.get("run_id")), []).append(b)
    runs = [_run_fact(store, r.id, bundles) for r in reversed(store.list_runs(limit=2**62))]
    # certified claims: the runs whose evidence they rest on decide whether they are live
    by_run = {r["run_id"]: r for r in runs}
    for c in base["certificates"]:
        try:
            from ..kerneld.scope import ScopedKernel
            cert = ScopedKernel(store).certificate(c["cert_hash"])
            prod = sorted({str(store.get_evidence(e).produced_by_run) for e in cert.evidence_ids})
        except Exception:  # noqa: BLE001  fail closed: an unloadable certificate is never a live claim
            prod = []
        c["evidence_runs"] = prod
        spends = {by_run[r]["spend"] for r in prod if r in by_run}
        c["live"] = "real" in spends
    facts = {"format": FORMAT, "base": base, "runs": runs, "evolve": _evolve_facts(store),
             "loops": audit_store(store), "findings": _findings_fact(repo, use_git),
             "museum": _museum_fact(museum_log) if museum_log is not None else None,
             "repo": str(repo)}
    json.loads(canonical(facts))
    return facts


# ------------------------------------------------------------------ axes (pure: from facts)
def axis_b(f: dict[str, Any]) -> dict[str, Any]:
    ev = f["evolve"]
    stats, edits = ev["generation_stats"], ev["harness_edits"]
    out: dict[str, Any] = {"baseline": {"value": None, "status": "not compared",
                                        "why": "no re-run baseline self-improvement campaign exists"}}
    if not stats:
        out["gain_per_1k_rollouts"] = _unmeasured("no evolve generation is recorded in this store")
        out["ood_drop_after_evolution"] = _unmeasured("no evolve generation is recorded in this store")
        out["status"] = "not measured"
        return out
    counted = [s for s in stats if isinstance(s["harness_runs"], int) and not isinstance(s["harness_runs"], bool)]
    uncounted = [s["ref"] for s in stats if s not in counted]
    parents = {s["parent"] for s in counted}
    acc = [e for e in edits if e["accepted"] is True and e["parent"] in parents
           and isinstance(e["actual_gate_delta"], int | float)]
    runs = sum(s["harness_runs"] for s in counted)
    if counted and runs > 0:
        gain = sum(float(e["actual_gate_delta"]) for e in acc)
        refs = [s["ref"] for s in counted] + [e["ref"] for e in acc]
        out["gain_per_1k_rollouts"] = _metric(1000.0 * gain / runs, refs,
                                              harness_runs=runs, accepted_edits=len(acc), generations=len(counted),
                                              generations_without_run_count=uncounted,
                                              note="accepted edits are matched to generations by parent hash (two "
                                                   "campaigns from one parent in one store are not told apart); "
                                                   "sum of accepted edits' gate mean differences per 1000 harness "
                                                   "runs (fixed objective, evolve/evaluate.py); internal gate data: "
                                                   "MECHANICAL unless every gate run was CERTIFIED-VALIDATED")
    else:
        out["gain_per_1k_rollouts"] = _unmeasured(
            f"{len(stats)} generation(s) recorded, none with a harness-run count (recorded since 2026-10-09)")
    drops = [(e["ref"], k, v) for e in acc if isinstance(e["ood_drop"], dict) for k, v in e["ood_drop"].items()
             if isinstance(v, int | float)]
    worse = [{"edit": r, "target": k, "mean_diff": float(v)} for r, k, v in drops if v < 0]
    out["ood_drop_after_evolution"] = (  # one row per (accepted edit, held-out target): none overwrites another
        _metric([{"edit": r, "target": k, "mean_diff": float(v)} for r, k, v in drops], [r for r, _, _ in drops],
                regressions=worse, status="fail" if worse else "pass",
                note="held-out family/domain mean difference (candidate - parent) of each accepted edit (LOFO "
                     "gate); a negative value is an OOD drop and is reported as a loss")
        if drops else _unmeasured("no accepted edit with a held-out family/domain result"))
    measured = [v for v in out.values() if isinstance(v, dict) and v.get("sources")]
    out["status"] = ("fail" if any(v.get("status") == "fail" for v in measured) else
                     "not compared" if measured else "not measured")
    return out


def axis_f(f: dict[str, Any]) -> dict[str, Any]:
    base = axes.axis_f(f["base"])
    out: dict[str, Any] = {k: v for k, v in base.items() if k in ("claims_without_evidence",
                                                                   "unverifiable_citations", "ledger")}
    certs = {c["cert_hash"]: c for c in f["base"]["certificates"]}
    claimed = [(r["ref"], cl) for r in f["runs"] for cl in (r.get("report") or {}).get("claims", [])]
    bad: dict[str, list[str]] = {}
    srcs: list[str] = []
    for ref, cl in claimed:
        c = certs.get(cl["cert_hash"])
        srcs += [ref] + ([c["ref"]] + [b["ref"] for b in c["bundles"]] if c else [])
        why = list(cl["gate_blockers"]) + ([] if c and c["bundles"] else ["claim bundle not ledgered"])
        if why:
            bad.setdefault(f"{ref}#{cl['prefix'] or 'claim'}", []).extend(why)
    out["reports_with_checklist_and_bundle"] = (
        _metric(100.0 * (len(claimed) - len(bad)) / len(claimed), srcs, unit="%",
                numerator=len(claimed) - len(bad), denominator=len(claimed),
                offenders=[{"report": k, "why": v} for k, v in sorted(bad.items())],
                publishable=sum(1 for _, cl in claimed if cl["publishable"]),
                status="pass" if not bad else "fail",
                note="GOAL §2 F: every report's checklist complete under the publication gate (non-blank answers, "
                     "loop label item) and its claim bundle ledgered; `publishable` additionally needs a "
                     "certified label")
        if claimed else _unmeasured("no report in this store names a certificate"))
    runs = f["runs"]
    ok = [r for r in runs if r["n_steps"] > 0 and any(b.get("present") for b in r["run_bundles"])]
    out["runs_with_bundle_and_cost_record"] = (
        _metric(100.0 * len(ok) / len(runs), [r["ref"] for r in runs] + [  # the run_bundle records and zips too
            x for r in runs for b in r["run_bundles"] for x in (b["ref"], b.get("zip_ref")) if x],
                unit="%", numerator=len(ok),
                denominator=len(runs), offenders=sorted(r["run_id"] for r in runs if r not in ok),
                status="pass" if len(ok) == len(runs) else "fail",
                note="invariant 9: a run_bundle record whose zip still has that manifest, and a trace (cost record)")
        if runs else _unmeasured("no runs in this store"))
    out["status"] = axes._combine(v.get("status", "pass") for v in out.values() if isinstance(v, dict))
    return out


def axis_g(f: dict[str, Any]) -> dict[str, Any]:
    runs = f["runs"]
    if not runs:
        return {"status": "not measured", "total_cost": _unmeasured("no runs in this store"),
                "cost_per_certified_live_claim": _unmeasured("undefined: 0 certified claims (no runs in this store)")}
    certified = [c for c in f["base"]["certificates"] if axes._certified(c)]
    live = [c for c in certified if c["live"]]
    usd_unknown = [r["run_id"] for r in runs if r["cost"]["usd"] is None]
    total = {"tokens": sum(r["cost"]["tokens"] for r in runs),
             "quota_units": sum(r["cost"]["quota_units"] for r in runs),
             "wall_s": round(sum(r["cost"]["wall_s"] for r in runs), 3),
             "usd": "unknown" if usd_unknown else sum(r["cost"]["usd"] for r in runs),
             "gpu_hours": "not metered" if all(r["cost"]["gpu_hours"] is None for r in runs)
             else sum(r["cost"]["gpu_hours"] or 0.0 for r in runs)}
    out: dict[str, Any] = {
        "total_cost": _metric(total, [r["ref"] for r in runs], runs=len(runs),
                              runs_with_real_spend=sum(1 for r in runs if r["spend"] == "real"),
                              runs_scripted=sum(1 for r in runs if r["spend"].startswith("scripted")),
                              runs_usd_unknown=usd_unknown,
                              estimated=any(r["cost"]["estimated"] for r in runs),
                              note="USD unknown when any call is subscription (byos), open-weights or unpriced; "
                                   "tokens of byos/mock calls are estimates")}
    if live:
        per = {k: (v / len(live) if isinstance(v, int | float) else v) for k, v in total.items()}
        out["cost_per_certified_live_claim"] = _metric(per, [r["ref"] for r in runs] + [c["ref"] for c in live],
                                                       certified_live_claims=len(live))
    else:
        out["cost_per_certified_live_claim"] = _unmeasured(
            f"undefined: 0 certified live claims ({len(certified)} certified claim(s) in this store, all from "
            f"scripted or model-free runs)" if certified else "undefined: 0 certified claims")
    out["baseline"] = {"value": None, "status": "not compared",
                       "why": "no re-run baseline reports a cost per Kernel-certified claim"}
    out["status"] = "not compared" if live else "not measured"
    return out


def axis_h(f: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    fd, mu = f["findings"], f["museum"]
    if mu is None:
        out["planted_exploit_catch_rate"] = _unmeasured(
            "no museum run log given (`python scripts/museum.py`, through the machine-wide limiter; pass --museum-log)")
    else:
        patched = fd["patched"]
        ran = {k: v for k, v in mu["outcomes"].items() if k in patched}
        killed = sorted(k for k, v in ran.items() if v == "KILLED")
        not_run = sorted(set(patched) - set(ran))
        unknown = sorted(set(mu["outcomes"]) - set(patched))
        if ran:
            lo, hi = wilson(len(killed), len(ran))
            lh, rh = mu["log_head"], fd.get("git_head")
            stale = (None if lh and rh and rh.startswith(lh) else
                     "the museum log names no commit: its verdicts cannot be tied to this code" if not lh else
                     "the registry's commit is unknown (git history not read)" if not rh else
                     f"the museum log was made at {lh}, this checkout is at {rh[:12]}: stale")
            out["planted_exploit_catch_rate"] = _metric(
                len(killed) / len(ran), [mu["ref"], fd["ref"]], numerator=len(killed), denominator=len(ran),
                ci95_wilson_two_sided=[lo, hi], not_killed=sorted(set(ran) - set(killed)),
                museum_patches_not_in_log=not_run, log_findings_not_in_registry=unknown,
                log_head=mu["log_head"], registry_head=fd.get("git_head"),
                stale=stale, status="pass" if len(killed) == len(ran) and not not_run and not stale else "fail",
                note="KILLED = at least one of the finding's tests fails on the re-introduced loophole")
        else:
            out["planted_exploit_catch_rate"] = _unmeasured("the museum log names no finding of the registry")
    st = fd["statuses"]
    n_open = sum(1 for v in st.values() if v == "open")
    n_acc = sum(1 for v in st.values() if v == "accepted")
    hist = fd.get("history")
    out["red_team_findings"] = _metric(
        {"total": len(st), "open": n_open, "accepted_limitations": n_acc,
         "unfixed_rate": (n_open + n_acc) / len(st) if st else None},
        [fd["ref"]], open=sorted(k for k, v in st.items() if v == "open"),
        status="pass" if n_open + n_acc == 0 else "fail",
        note="open = not fixed; accepted = a known limitation that still works for the attacker (counted as unfixed)")
    if hist:
        out["red_team_trend_by_date"] = _metric(
            [{k: h[k] for k in ("date", "commit", "total", "fixed", "open", "accepted")} for h in hist],
            [h["ref"] for h in hist], points=len(hist),
            note="last commit of each day touching redteam/findings.yaml (this checkout's history); a finding's "
                 "date is when the registry recorded it, not when it was found")
    else:
        out["red_team_trend_by_date"] = _unmeasured(fd.get("history_problem") or "no git history of the registry")
    leak = axes.axis_h(f["base"])["leak_audit_pass_rate"]
    out["leak_audit_pass_rate"] = leak
    out["salts"] = _unmeasured("no salt campaign audit (needs owner: salt budget, GOAL §16.9)")
    out["status"] = axes._combine(v.get("status", "pass") for v in out.values() if isinstance(v, dict))
    return out


def loops(f: dict[str, Any]) -> dict[str, Any]:
    a = f["loops"]
    if not a["instances"]:
        return {"instances": _unmeasured("no loop instance (research run with a report, evolve generation) in this "
                                         "store"), "status": "not measured"}
    return {"instances": _metric(a["counts"], [i["ref"] for i in a["instances"]], n=len(a["instances"]),
                                 unlabelled=[{"ref": i["ref"], "problem": i["problem"]} for i in a["unlabelled"]],
                                 status="pass" if not a["unlabelled"] else "fail",
                                 note="GOAL §6.1: only VALIDATED loops support the self-improvement claim"),
            "status": "pass" if not a["unlabelled"] else "fail"}


def build_local(f: dict[str, Any]) -> dict[str, Any]:
    bench = axes.bench_axes(f["base"])
    card_axes: dict[str, Any] = {}
    for ax in ("A", "C", "D", "E"):
        a = dict(bench[ax])
        if a["status"] == "not measured" and not a.get("benchmarks"):
            a["why"] = NOT_MEASURED_WHY[ax]
        card_axes[ax] = a
    card_axes["B"] = axis_b(f)
    if bench["B"].get("benchmarks"):
        card_axes["B"]["benchmarks"] = bench["B"]["benchmarks"]
    card_axes["F"], card_axes["G"], card_axes["H"] = axis_f(f), axis_g(f), axis_h(f)
    lp = loops(f)
    not_measured: list[str] = []
    losses: list[str] = []
    for ax in sorted(card_axes):
        a = card_axes[ax]
        if a["status"] in ("fail", "lose"):
            losses.append(f"axis {ax}: {a['status']}")
        for k, v in a.items():
            if isinstance(v, dict) and v.get("status") == "not measured":
                not_measured.append(f"axis {ax} {k}: NOT MEASURED ({v.get('why')})")
            elif isinstance(v, dict) and v.get("status") == "not compared":
                not_measured.append(f"axis {ax} {k}: NOT COMPARED ({v.get('why')})")
            elif isinstance(v, dict) and v.get("status") in ("fail", "lose"):
                losses.append(f"axis {ax} {k}: {v['status']}")
        if a["status"] == "not measured" and "why" in a:
            not_measured.append(f"axis {ax}: NOT MEASURED ({a['why']})")
    if lp["status"] == "fail":
        losses.append("loop labels (invariant 10): unlabelled loop instance(s)")
    return {"format": FORMAT, "facts_sha256": sha(f), "axes": card_axes, "loop_labels": lp, "losses": losses,
            "not_measured": not_measured,
            "overall": "NOT best: " + (f"{len(losses)} loss/failure(s), " if losses else "")
                       + f"{len(not_measured)} item(s) not measured or not compared (local axes only; no benchmark "
                         f"was run)"}


# ------------------------------------------------------------------ checking every source
def resolve_local(ref: str, store: EvidenceStore | None, repo: Path) -> str | None:
    kind, _, rest = ref.partition(":")
    try:
        if kind == "repo":
            rel, _, h = rest.rpartition(":")
            p = Path(repo) / rel
            return None if p.is_file() and _sha_bytes(p.read_bytes()) == h else f"{ref}: file missing or changed"
        if kind == "git":
            commit, _, more = rest.partition(":")
            rel, _, h = more.rpartition(":")
            return None if _sha_bytes(_git(Path(repo), "show", f"{commit}:{rel}")) == h else f"{ref}: differs"
        if kind == "runbundle":
            from ..store.run_bundle import RunBundleError, read_run_bundle_bytes
            h, _, path = rest.partition(":")
            try:
                return None if read_run_bundle_bytes(Path(path))[1] == h else f"{ref}: manifest changed"
            except (RunBundleError, ValueError):
                return f"{ref}: not a readable run bundle"
        if kind == "file":
            h, _, path = rest.partition(":")
            p = Path(path)
            return None if p.is_file() and _sha_bytes(p.read_bytes()) == h else f"{ref}: file missing or changed"
    except (OSError, subprocess.SubprocessError) as e:
        return f"{ref}: {type(e).__name__}"
    return resolve(ref, store, None)


def check_local(card: dict[str, Any], store: EvidenceStore | None, repo: Path) -> list[str]:
    problems = [f"number without sources at {p}" for p in axes.numbers_without_sources(card)]
    problems += [why for ref in sorted(axes.all_sources(card)) if (why := resolve_local(ref, store, repo))]
    return problems


# ------------------------------------------------------------------ rendering
def _val(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, dict):
        return ", ".join(f"{k}={_val(x)}" for k, x in v.items())
    if isinstance(v, list):
        return f"[{len(v)} item(s)]"
    return str(v)


def render_table(card: dict[str, Any]) -> str:
    rows = [("axis", "metric", "value", "status")]
    for ax in sorted(card["axes"]):
        a = card["axes"][ax]
        metrics = [(k, v) for k, v in a.items() if isinstance(v, dict) and ("value" in v or "status" in v)]
        if not metrics:
            rows.append((ax, "-", "-", str(a["status"]).upper()))
        for k, v in metrics:
            if v.get("value") is None:
                rows.append((ax, k, "-", str(v.get("status", "not measured")).upper()))
            else:
                extra = ""
                if "ci95_wilson_two_sided" in v:
                    lo, hi = v["ci95_wilson_two_sided"]
                    extra = f" ({v['numerator']}/{v['denominator']}; Wilson 95% two-sided [{lo:.3f}, {hi:.3f}]"
                    if v.get("museum_patches_not_in_log"):
                        extra += f"; {len(v['museum_patches_not_in_log'])} registry patch(es) NOT in the log"
                    if v.get("stale"):
                        extra += "; STALE: " + str(v["stale"])
                    extra += ")"
                elif "numerator" in v:
                    extra = f" ({v['numerator']}/{v['denominator']})"
                shown = _val(v["value"]) + (" (estimated)" if v.get("estimated") is True else "")
                if k == "red_team_trend_by_date":
                    shown = " -> ".join(f"{h['date']} open {h['open']} + accepted {h['accepted']} of {h['total']}"
                                        for h in v["value"])
                rows.append((ax, k, shown + extra, str(v.get("status", "measured"))))
    lp = card["loop_labels"]
    lv = lp["instances"]
    rows.append(("loops", "loop instances by label", _val(lv["value"]) if lv.get("value") is not None else "-",
                 lp["status"]))
    w = [max(len(r[i]) for r in rows) for i in range(3)]
    lines = [f"Local scorecard ({card['format']}): {card['overall']}", ""]
    for i, r in enumerate(rows):
        lines.append(f"{r[0]:<{w[0]}}  {r[1]:<{w[1]}}  {r[2]:<{w[2]}}  {r[3]}")
        if i == 0:
            lines.append("-" * (sum(w) + 6 + max(len(x[3]) for x in rows)))
    lines += ["", "Losses and failures (reported, GOAL §2.5):"]
    lines += [f"  - {x}" for x in card["losses"]] or ["  - none"]
    lines += ["", "HONESTY FOOTER: every axis or metric below is NOT MEASURED or NOT COMPARED, with its reason "
                  "(GOAL §2 rule 5: reported, not hidden):"]
    lines += [f"  - {x}" for x in card["not_measured"]] or ["  - none"]
    lines += ["", f"Every number names its source artifact (invariant 8; `--json` lists them); facts sha256 "
                  f"{card['facts_sha256']}."]
    return ("\n".join(lines) + "\n").replace("§", "sec. ")  # ASCII: Windows consoles print cp1252
