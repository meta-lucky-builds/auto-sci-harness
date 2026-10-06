"""Research-loop modules (PLAN L4). Deterministic code; LLM calls only where noted.

Wired together by a HarnessSpec (see examples/research_v0.yaml) so the harness evolver can
re-wire, re-prompt and re-parameterise the loop without touching this code.

Private data rule: anything the agent must not see lives under state keys starting with
`_private` (templates refuse to render them) or only on the host filesystem.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import uuid
from pathlib import Path
from typing import Any

from ..gateway import ChatRequest, Message
from ..records import Claim, Evidence, EvidenceKind
from ..runtime import register
from ..runtime.engine import parse_json_output
from ..runtime.modules import ModuleContext
from ..sandbox.base import Sandbox, WorkspaceEscape, safe_path
from ..tiers import Action, Tier, decide
from ..verify import metrics as M
from ..verify.leak import ESCAPE_MARKER, leak_audit, new_canary, private_fingerprints
from ..verify.ranking import bradley_terry, rankings_to_wins
from ..verify.stats import paired_example_interval, t2_verdict
from .data import dev_split, profile, read_csv, read_predictions
from .task import TaskSpec, load_task


# ---------------------------------------------------------------- helpers
def _require_isolation(ctx: ModuleContext) -> None:
    if ctx.sandbox is None:
        raise RuntimeError("research modules require a sandbox")
    if ctx.sandbox.isolation.startswith("none") and os.environ.get("CHH_ALLOW_UNISOLATED") != "1":
        raise PermissionError("refusing to run agent-written code without isolation; use DockerSandbox "
                              "(or set CHH_ALLOW_UNISOLATED=1 for trusted test code only)")


def _sb(ctx: ModuleContext) -> Sandbox:
    if ctx.sandbox is None:
        raise RuntimeError("research modules require a sandbox")
    return ctx.sandbox


def _ws(ctx: ModuleContext, rel: str) -> Path:
    """Host-side access to a workspace path; refuses links planted by agent code (see safe_path)."""
    return safe_path(_sb(ctx).workspace, rel)


def _task(state: dict[str, Any]) -> TaskSpec:
    return TaskSpec.model_validate(state["task"])


def _run_solution(ctx: ModuleContext, script: str, train: str, test: str, seed: int,
                  timeout_s: float) -> tuple[list[str] | None, dict[str, Any]]:
    out_rel = f"out/preds_{uuid.uuid4().hex[:8]}.csv"
    _ws(ctx, "out").mkdir(exist_ok=True)
    sb = _sb(ctx)
    res = sb.exec([sb.python, script, "--train", train, "--test", test,
                   "--out", out_rel, "--seed", str(seed)], timeout_s=timeout_s)
    info: dict[str, Any] = {"returncode": res.returncode, "timed_out": res.timed_out, "wall_s": round(res.wall_s, 2),
            "stdout_tail": res.stdout[-1500:], "stderr_tail": res.stderr[-3000:]}
    try:
        out = _ws(ctx, out_rel)
    except WorkspaceEscape as e:
        # the agent's code planted a link where its output should be: a failed attempt by the agent,
        # recorded (and later flagged by the leak audit), never followed
        info["stderr_tail"] += f"\n{ESCAPE_MARKER}: {e}"
        info["escape_attempt"] = str(e)
        return None, info
    if res.returncode != 0 or not out.exists():
        return None, info
    try:
        return read_predictions(out), info
    except ValueError as e:
        info["stderr_tail"] += f"\n[harness] invalid predictions file: {e}"
        return None, info


def _score(task: TaskSpec, y_true: list[str], preds: list[str] | None) -> float | None:
    if preds is None:
        return None
    try:
        return M.score(task.metric, y_true, preds)
    except (ValueError, KeyError):
        return None


def _better(task: TaskSpec, a: float | None, b: float | None) -> bool:
    if a is None:
        return False
    if b is None:
        return True
    return a > b if task.higher_is_better else a < b


# ---------------------------------------------------------------- modules
@register("research.setup")
def setup(state, params, ctx: ModuleContext):
    _require_isolation(ctx)
    task_dir = Path(state["input"]["task_dir"]).resolve()
    task = load_task(task_dir)
    _ws(ctx, "data").mkdir(parents=True, exist_ok=True)
    for name in (task.train_file, task.test_features_file):
        shutil.copy2(task_dir / "public" / name, _ws(ctx, f"data/{name}"))
    shutil.copy2(task_dir / task.baseline_file, _ws(ctx, "baseline.py"))
    labels = dev_split(_ws(ctx, f"data/{task.train_file}"), task.target_column, task.dev_fraction,
                       seed=params.get("split_seed", 0), out_dir=_ws(ctx, "dev"))
    return {"task": task.model_dump(),
            "_private_task_dir": str(task_dir),
            # per-run canary: lives only in private state; if it ever shows up in a prompt, a response,
            # the trace or the workspace, private state leaked (checked by the audit, I5)
            "_private_canary": new_canary(),
            "_private_dev_labels_ref": ctx.store.blobs.put_json(labels),
            "attempts": [], "n_attempts": 0, "best": None, "route": None}


@register("research.profile_data")
def profile_data(state, params, ctx):
    return {"profile": profile(_ws(ctx, "dev/train.csv"), _task(state).target_column)}


@register("research.baseline_dev")
def baseline_dev(state, params, ctx):
    _require_isolation(ctx)
    task = _task(state)
    labels = ctx.store.blobs.get_json(state["_private_dev_labels_ref"])
    preds, info = _run_solution(ctx, "baseline.py", "dev/train.csv", "dev/valid_X.csv", 0,
                                task.solution_timeout_s)
    s = _score(task, labels, preds)
    if s is None:
        raise RuntimeError(f"baseline failed on dev split: {info['stderr_tail'][-500:]}")
    return {"baseline_dev": s}


@register("research.tournament")
def tournament(state, params, ctx):
    """K-way relative ranking with shuffled presentation order -> Bradley-Terry (PLAN L4.5)."""
    hyps = state["hypotheses"]["hypotheses"]
    ids = [h["id"] for h in hyps]
    if len(set(ids)) != len(ids):
        raise ValueError("hypothesis ids must be unique")
    rounds = params.get("rounds", 3)
    rng = random.Random(state.get("_seed", 0))
    schema = {"type": "object", "properties": {"ranking": {"type": "array", "items": {"type": "string"}}},
              "required": ["ranking"]}
    rankings, rejected = [], 0
    for _ in range(rounds):
        order = hyps[:]
        rng.shuffle(order)
        listing = "\n".join(f"- id={h['id']}: {h['idea']} (rationale: {h.get('rationale', '')})" for h in order)
        prompt = (f"Task: {state['task']['description']}\nMetric: {state['task']['metric']}\n"
                  f"Data profile: {json.dumps(state['profile'])[:6000]}\n\n"
                  f"Rank ALL of these hypotheses from most to least likely to improve the held-out metric "
                  f"over the baseline, considering feasibility given the data. Use only the ids.\n{listing}\n"
                  f'Return JSON {{"ranking": [ids best-first]}}.')
        resp = ctx.llm("judge", ChatRequest(messages=[Message(role="user", content=prompt)], json_schema=schema))
        try:
            ranking = parse_json_output(resp.text)["ranking"]
        except (ValueError, KeyError, TypeError):
            rejected += 1
            continue
        if sorted(ranking) != sorted(ids):
            rejected += 1  # not a permutation: discard rather than guess
            continue
        rankings.append(ranking)
    if not rankings:
        raise RuntimeError("judge produced no valid rankings")
    strengths = bradley_terry(ids, rankings_to_wins(rankings))
    ordered = sorted(ids, key=lambda i: -strengths[i])
    by_id = {h["id"]: h for h in hyps}
    return {"ranking": {"order": ordered, "bt": strengths, "valid_rounds": len(rankings),
                        "rejected_rounds": rejected},
            "queue": [by_id[i] for i in ordered], "current": None}


@register("research.next_hypothesis")
def next_hypothesis(state, params, ctx):
    """Pop the next ranked hypothesis; when exhausted, route to verify (if something beat the
    baseline on dev) or give up honestly."""
    queue = list(state.get("queue") or [])
    current = queue.pop(0) if queue else None
    if current is not None:
        route = "implement"
    else:
        best = state.get("best")
        route = ("verify" if best is not None and _better(_task(state), best["dev_score"], state["baseline_dev"])
                 else "give_up")
    return {"queue": queue, "current": current, "fixes_for_current": 0, "run_error": None, "impl": None,
            "route": route}


@register("research.run_candidate")
def run_candidate(state, params, ctx):
    """Write the implemented code, run it on the dev split, score, and decide the route
    deterministically (PLAN P7): fix | next | verify | give_up."""
    _require_isolation(ctx)
    task = _task(state)
    code = (state.get("impl") or {}).get("code", "")
    _ws(ctx, "solution.py").write_text(code, encoding="utf-8")
    labels = ctx.store.blobs.get_json(state["_private_dev_labels_ref"])
    preds, info = _run_solution(ctx, "solution.py", "dev/train.csv", "dev/valid_X.csv", 0,
                                task.solution_timeout_s)
    s = _score(task, labels, preds)
    error = None
    if s is None:
        error = info["stderr_tail"] or "no valid predictions produced"
        if preds is not None:
            error = f"predictions could not be scored (expected {len(labels)} rows, got {len(preds)})"
    hyp = state.get("current") or {}
    n_attempts = state.get("n_attempts", 0) + 1
    attempt = {"n": n_attempts, "hypothesis_id": hyp.get("id"), "dev_score": s, "error": error,
               "code_ref": ctx.store.blobs.put(code.encode()), "wall_s": info["wall_s"]}
    attempts = list(state.get("attempts") or []) + [attempt]
    best = state.get("best")
    if _better(task, s, best["dev_score"] if best else None):
        best = attempt

    max_attempts = params.get("max_attempts", 6)
    max_fixes = params.get("max_fixes", 2)
    fixes = state.get("fixes_for_current", 0)
    if error and fixes < max_fixes and n_attempts < max_attempts:
        route = "fix"
    elif n_attempts < max_attempts and state.get("queue"):
        route = "next"
    elif best is not None and _better(task, best["dev_score"], state["baseline_dev"]):
        route = "verify"
    else:
        route = "give_up"
    return {"dev_score": s, "run_error": error, "attempts": attempts, "n_attempts": n_attempts,
            "best": best, "route": route, "fixes_for_current": fixes + (1 if route == "fix" else 0)}


@register("research.verify")
def verify(state, params, ctx):
    """Hidden-test verification (PLAN D3). Produces T1 metric evidence per seed and a T2 statistical
    verdict, then a claim. Negative / inconclusive results become claims too."""
    _require_isolation(ctx)
    task = _task(state)
    best = state.get("best")
    if best is None:
        return {"verdict": {"verified": False, "reason": "no working candidate"}}
    seeds = params.get("seeds", [0, 1, 2])
    task_dir = Path(state["_private_task_dir"])
    _, label_rows = read_csv(task_dir / "private" / task.test_labels_file)
    y_test = [r[0] for r in label_rows]
    _ws(ctx, "solution.py").write_bytes(ctx.store.blobs.get(best["code_ref"]))
    train, test = f"data/{task.train_file}", f"data/{task.test_features_file}"

    scores: dict[str, list[float]] = {"solution": [], "baseline": []}
    preds_by_seed: dict[str, list[list[str]]] = {"solution": [], "baseline": []}
    evidence_ids: list[str] = []
    for arm, script in (("solution", "solution.py"), ("baseline", "baseline.py")):
        for seed in seeds:
            preds, info = _run_solution(ctx, script, train, test, seed, task.solution_timeout_s)
            s = _score(task, y_test, preds)
            if s is None:
                raise RuntimeError(f"{arm} failed on test (seed {seed}): {info['stderr_tail'][-500:]}")
            scores[arm].append(s)
            preds_by_seed[arm].append(preds)
            payload = {"arm": arm, "seed": seed, "metric": task.metric, "score": s,
                       "preds_ref": ctx.store.blobs.put_json(preds), "code_ref":
                       best["code_ref"] if arm == "solution" else ctx.store.blobs.put(
                           _ws(ctx, "baseline.py").read_bytes()),
                       "contract": "solution CLI v1", "train": train, "test": test}
            ev = Evidence(kind=EvidenceKind.METRIC, tier=Tier.T1_EXECUTABLE, verifier_id=f"metric:{task.metric}",
                          produced_by_run=ctx.run_id, payload_ref=ctx.store.blobs.put_json(payload),
                          summary=f"{arm} seed={seed} {task.metric}={s:.5g}")
            ctx.store.add_evidence(ev)
            evidence_ids.append(ev.id)

    seed_v = t2_verdict(scores["solution"], scores["baseline"], higher_is_better=task.higher_is_better,
                        min_seeds=params.get("min_seeds", 3))
    ex_ci = paired_example_interval(M.METRICS[task.metric], y_test, preds_by_seed["solution"],
                                    preds_by_seed["baseline"], higher_is_better=task.higher_is_better)
    # A degenerate seed check (identical difference on every seed: the method ignores the seed) carries no
    # information: it is reported as such, and the evidence then rests on test-example resampling alone.
    seed_ok = seed_v.verified or seed_v.degenerate
    verified = seed_ok and not ex_ci.degenerate and ex_ci.lo > 0
    stat_payload = {"seed_verdict": seed_v.reason, "seed_ci": seed_v.ci.__dict__ if seed_v.ci else None,
                    "seed_check_degenerate": seed_v.degenerate,
                    "example_ci": ex_ci.__dict__, "verified": verified, "scores": scores,
                    "rule": "paired-seed t CI excludes 0 (or is degenerate and reported so) AND BCa test-example "
                            "CI averaged over seeds excludes 0"}
    stat_ev = Evidence(kind=EvidenceKind.STAT, tier=Tier.T2_STATISTICAL, verifier_id="t2:seed+example-bootstrap",
                       produced_by_run=ctx.run_id, payload_ref=ctx.store.blobs.put_json(stat_payload),
                       summary=("verified" if verified else "not verified") + f": {seed_v.reason}")
    ctx.store.add_evidence(stat_ev)
    evidence_ids.append(stat_ev.id)

    mean = lambda xs: sum(xs) / len(xs)  # noqa: E731
    hyp = next((a for a in state["attempts"] if a["code_ref"] == best["code_ref"]), best)
    if verified:
        text = (f"On task '{task.id}', the solution for hypothesis {hyp.get('hypothesis_id')} improves "
                f"{task.metric} over the baseline: {mean(scores['solution']):.4g} vs "
                f"{mean(scores['baseline']):.4g} (mean over seeds {seeds}); {seed_v.reason}; test-example "
                f"95% CI [{ex_ci.lo:.4g}, {ex_ci.hi:.4g}].")
    else:
        text = (f"On task '{task.id}', the best solution (hypothesis {hyp.get('hypothesis_id')}) did NOT show a "
                f"verified improvement over the baseline on {task.metric}: {mean(scores['solution']):.4g} vs "
                f"{mean(scores['baseline']):.4g}. {seed_v.reason}; test-example CI "
                f"[{ex_ci.lo:.4g}, {ex_ci.hi:.4g}].")
    claim = Claim(text=text, evidence_ids=tuple(evidence_ids))
    ctx.store.add_claim(claim)
    return {"verdict": {"verified": verified, "claim_id": claim.id, "reason": seed_v.reason,
                        "solution_mean": mean(scores["solution"]), "baseline_mean": mean(scores["baseline"]),
                        "example_ci": [ex_ci.lo, ex_ci.hi]},
            "claims": list(state.get("claims") or []) + [claim.id]}


@register("research.report")
def report(state, params, ctx):
    """Report generated from the claim graph only; every number cites evidence (Chain-of-Evidence)."""
    task = _task(state)
    lines = [f"# Research report: {task.title}", "",
             f"Task `{task.id}` · metric `{task.metric}` "
             f"({'higher' if task.higher_is_better else 'lower'} is better)", ""]
    lines += ["## Claims", ""]
    for cid in state.get("claims") or []:
        claim = ctx.store.get_claim(cid)
        tier = ctx.store.claim_tier(cid)
        verdict = state.get("verdict", {})
        action = Action.PUBLISH_VERIFIED if verdict.get("verified") else Action.PUBLISH_SUPPORTED
        lines.append(f"- **[{tier.name}]** {claim.text}")
        lines.append(f"  - publication policy: `{decide(action, tier).value}` for `{action.value}`")
        for eid in claim.evidence_ids:
            ev = ctx.store.get_evidence(eid)
            lines.append(f"  - evidence `{eid}` ({ev.tier.name}, {ev.verifier_id}): {ev.summary}")
    lines += ["", "## Exploration log (dev split; NOT evidence for claims)", "",
              f"Baseline dev {task.metric}: {state.get('baseline_dev')}", ""]
    for a in state.get("attempts") or []:
        lines.append(f"- attempt {a['n']} hypothesis={a['hypothesis_id']} dev={a['dev_score']} "
                     f"error={'yes' if a['error'] else 'no'}")
    lines += ["", "## Hypotheses considered (ranked)", ""]
    for h in state.get("hypotheses", {}).get("hypotheses", []):
        bt = state.get("ranking", {}).get("bt", {}).get(h["id"])
        lines.append(f"- `{h['id']}` (BT={bt:.3f}): {h['idea']}" if bt is not None else f"- `{h['id']}`: {h['idea']}")
    text = "\n".join(lines) + "\n"
    return {"report": text, "report_ref": ctx.store.blobs.put(text.encode())}


@register("research.audit")
def audit(state, params, ctx):
    """Integrity audit (ScientistOne-style, PLAN L2).
    I1 claims resolve to stored evidence; I2 baseline used the same contract/data/seeds;
    I3 citations (n/a until the literature module exists); I4 metrics recomputed from raw predictions;
    I5 leak + canary audit over everything agent-visible (verify/leak.py; K9 precursor)."""
    task = _task(state)
    results: dict[str, Any] = {"I1": True, "I2": True, "I3": "n/a", "I4": True, "I5": True, "problems": []}
    _, label_rows = read_csv(Path(state["_private_task_dir"]) / "private" / task.test_labels_file)
    y_test = [r[0] for r in label_rows]
    for cid in state.get("claims") or []:
        claim = ctx.store.get_claim(cid)
        metric_payloads = []
        for eid in claim.evidence_ids:
            try:
                ev = ctx.store.get_evidence(eid)
                payload = ctx.store.blobs.get_json(ev.payload_ref)
            except (KeyError, OSError) as e:
                results["I1"] = False
                results["problems"].append(f"{cid}: evidence {eid} unresolvable ({e})")
                continue
            if ev.kind == EvidenceKind.METRIC:
                metric_payloads.append(payload)
                recomputed = M.score(payload["metric"], y_test, ctx.store.blobs.get_json(payload["preds_ref"]))
                if abs(recomputed - payload["score"]) > 1e-12:
                    results["I4"] = False
                    results["problems"].append(f"{eid}: reported {payload['score']} != recomputed {recomputed}")
        arms = {p["arm"]: p for p in metric_payloads}
        if {"solution", "baseline"} <= set(arms):
            sol = sorted(p["seed"] for p in metric_payloads if p["arm"] == "solution")
            base = sorted(p["seed"] for p in metric_payloads if p["arm"] == "baseline")
            same = (sol == base and arms["solution"]["test"] == arms["baseline"]["test"]
                    and arms["solution"]["train"] == arms["baseline"]["train"]
                    and arms["solution"]["contract"] == arms["baseline"]["contract"])
            if not same:
                results["I2"] = False
                results["problems"].append(f"{cid}: baseline not run under identical conditions")
    fp = private_fingerprints(Path(state["_private_task_dir"]) / "private", labels=y_test)
    leak = leak_audit(ctx.store.trace(ctx.run_id), ctx.sandbox.workspace if ctx.sandbox else None, fp,
                      canaries=fp["canaries"] + [state.get("_private_canary", "")],
                      extra_texts={"report": state.get("report", "")})
    results["I5"] = leak["passed"]
    results["problems"] += leak["problems"]
    results["passed"] = all(results[k] is True for k in ("I1", "I2", "I4", "I5"))
    return {"audit": results}
