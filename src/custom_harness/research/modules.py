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
from ..kernel.execution import OUT_PATH
from ..runtime import register
from ..runtime.engine import parse_json_output
from ..runtime.modules import ModuleContext
from ..sandbox.base import Sandbox, WorkspaceEscape, check_mount, protect_path, safe_path
from ..verify import metrics as M
from ..verify.leak import ESCAPE_MARKER, new_canary
from ..verify.ranking import bradley_terry, rankings_to_wins
from ..verify.stats import paired_example_interval
from .data import cv_folds, profile, read_predictions
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


# The private dev labels are stored wrapped, so their blob ref can never equal the ref of a predictions list:
# perfect dev predictions would otherwise share the labels' content hash and be scrubbed (and audited) as a
# leaked private value (live run run_cd99288e65ba4a4f).
_DEV_LABELS_KEY = "private_dev_labels"


def _dev_labels(state: dict[str, Any], ctx: ModuleContext) -> list[str]:
    return ctx.store.blobs.get_json(state["_private_dev_labels_ref"])[_DEV_LABELS_KEY]


def _run_solution(sb: Sandbox, script: str, train: str, test: str, seed: int,
                  timeout_s: float) -> tuple[list[str] | None, dict[str, Any]]:
    """Run a solution under the CLI contract in sandbox `sb` (paths relative to its workspace). The output path is
    the one held-out runs use (kernel.execution.OUT_PATH), so the command line does not reveal the run kind."""
    out_rel = OUT_PATH
    safe_path(sb.workspace, "out").mkdir(exist_ok=True)
    stale = safe_path(sb.workspace, out_rel)
    if stale.exists():
        stale.unlink()
    res = sb.exec([sb.python, script, "--train", train, "--test", test,
                   "--out", out_rel, "--seed", str(seed)], timeout_s=timeout_s)
    info: dict[str, Any] = {"returncode": res.returncode, "timed_out": res.timed_out, "wall_s": round(res.wall_s, 2),
            "stdout_tail": res.stdout[-1500:], "stderr_tail": res.stderr[-3000:]}
    try:
        out = safe_path(sb.workspace, out_rel)
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


# Total wall time of one k-fold dev evaluation, as a multiple of the task's per-run limit (cost policy, decided
# 2026-10-07 after a live candidate took 2423 s over 5 folds): solutions slower than ~2/k of the limit per fold
# cannot pass dev; held-out runs keep the full per-run limit.
DEV_TIME_FACTOR = 2.0


def _dev_predict(ctx: ModuleContext, state: dict[str, Any], code: bytes,
                 name: str) -> tuple[list[str] | None, dict[str, Any]]:
    """Out-of-fold dev predictions (k-fold cross-validation on the public training data).

    The program runs once per fold, each time in a FRESH sandbox whose workspace holds only that fold's training
    rows and validation features: never the run workspace, which holds the full public training file and hence
    every validation label (a program reading it could score itself perfectly on dev). The workspace layout,
    file names, command line and seed are those of held-out runs (kernel.execution.run_program), so a program
    cannot tell a dev fold from a held-out evaluation by names, and the fold files use the byte conventions of the
    held-out input files (data.cv_folds). It still can by ROW COUNTS (stated limitation, not fixable without changing
    the estimator): a fold trains on (k-1)/k of the public rows and predicts 1/k of them, so len(test)/len(train) is
    1/(k-1) on dev but n_test/n_train held-out, and the training size differs from the profile's row count. Impact is
    bounded: dev scores only select and forecast; evidence is the held-out runs the Kernel replays.
    Predictions are concatenated in fold order, which is the order of the private dev labels. The
    first failing fold fails the whole evaluation; its diagnostics are returned (with "fold" set)."""
    task = _task(state)
    sb = _sb(ctx)
    root = sb.workspace.parent / f"{sb.workspace.name}-dev" / f"{name}-{uuid.uuid4().hex[:8]}"
    preds: list[str] = []
    wall = 0.0
    budget = DEV_TIME_FACTOR * task.solution_timeout_s  # total over all folds (each fold also keeps the per-run cap)
    over = (f"\n[harness] dev evaluation exceeded its total time budget ({DEV_TIME_FACTOR:g} x the task's "
            f"{task.solution_timeout_s:g} s per-run limit, over {len(state['dev_folds'])} folds)")
    train_rel, test_rel = f"in/train/{task.train_file}", f"in/test/{task.test_features_file}"
    for k, fold in enumerate(state["dev_folds"]):
        remaining = budget - wall
        if remaining <= 0:
            return None, {"fold": k, "wall_s": round(wall, 2), "stdout_tail": "", "stderr_tail": over.strip()}
        timeout = min(task.solution_timeout_s, remaining)
        box = sb.spawn(root / f"fold{k}")
        safe_path(box.workspace, "solution.py").write_bytes(code)
        for rel, ref in ((train_rel, fold["train_ref"]), (test_rel, fold["valid_x_ref"])):
            p = safe_path(box.workspace, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(ctx.store.blobs.get(ref))
        out, info = _run_solution(box, "solution.py", train_rel, test_rel, 0, timeout)
        wall += info["wall_s"]
        if out is None and info.get("timed_out") and timeout < task.solution_timeout_s:
            info["stderr_tail"] += over
        if out is not None and len(out) != fold["n"]:
            info["stderr_tail"] += f"\n[harness] fold {k}: expected {fold['n']} predictions, got {len(out)}"
            out = None
        if out is None:
            return None, {**info, "fold": k, "wall_s": round(wall, 2)}
        preds += out
    return preds, {"fold": None, "wall_s": round(wall, 2), "stdout_tail": "", "stderr_tail": ""}


def _baseline_code(state: dict[str, Any], ctx: ModuleContext) -> bytes:
    """The baseline exactly as registered in the vault: the dev comparator is byte-identical to the held-out one."""
    return ctx.store.blobs.get(ctx.kernel().vault.artifacts()[state["datasets"]["baseline"]].hash)


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
    # the hidden labels must never be inside the agent's mount (invariant 2): protect the task's private files for
    # every sandbox created from now on, and refuse the run's own workspace if it overlaps them
    protect_path(task_dir / "private")
    check_mount(_sb(ctx).workspace)
    _ws(ctx, "data").mkdir(parents=True, exist_ok=True)
    for name in (task.train_file, task.test_features_file):
        shutil.copy2(task_dir / "public" / name, _ws(ctx, f"data/{name}"))
    shutil.copy2(task_dir / task.baseline_file, _ws(ctx, "baseline.py"))
    # Dev estimate = k-fold cross-validation over the public training file (params: dev_folds, split_seed).
    # setup is a protected node, so the evolver cannot change how dev evidence is produced.
    k, split_seed = int(params.get("dev_folds", 5)), int(params.get("split_seed", 0))
    stratified = task.metric in M.CLASSIFICATION
    folds = cv_folds(_ws(ctx, f"data/{task.train_file}"), task.target_column, k, split_seed, stratified,
                     test_csv=_ws(ctx, f"data/{task.test_features_file}"))
    labels = [y for f in folds for y in f["labels"]]  # fold order = the order of out-of-fold predictions
    from ..kernel import EvidenceClass
    from ..kernel.adapters import resolve
    adapter, why = resolve(ctx.kernel().ledger, EvidenceClass.DATA, task.metric)
    if adapter is None:  # fail fast, before any model call: no claim of this run could be certified (K1)
        raise RuntimeError("no human-approved adapter for this task: " + "; ".join(why))
    from .claims import register_task  # local import: claims.py builds on the helpers in this module
    datasets = register_task(ctx, task, task_dir)
    return {"task": task.model_dump(), "datasets": datasets,
            "dev_scheme": {"kind": "kfold", "k": k, "seed": split_seed, "stratified": stratified,
                           "n": len(labels)},
            "dev_folds": [{"train_ref": ctx.store.blobs.put(f["train"]),
                           "valid_x_ref": ctx.store.blobs.put(f["valid_x"]), "n": len(f["labels"])} for f in folds],
            # per-run canary: lives only in private state; if it ever shows up in a prompt, a response,
            # the trace or the workspace, private state leaked (checked by the audit, I5)
            "_private_canary": new_canary(),
            "_private_dev_labels_ref": ctx.store.blobs.put_json({_DEV_LABELS_KEY: labels}),
            "attempts": [], "n_attempts": 0, "best": None, "route": None}


@register("research.profile_data")
def profile_data(state, params, ctx):
    """Profile of the full public training file (aggregate statistics only; ideation is conditioned on it)."""
    return {"profile": profile(_ws(ctx, f"data/{_task(state).train_file}"), _task(state).target_column)}


@register("research.baseline_dev")
def baseline_dev(state, params, ctx):
    _require_isolation(ctx)
    task = _task(state)
    labels = _dev_labels(state, ctx)
    preds, info = _dev_predict(ctx, state, _baseline_code(state, ctx), "baseline")
    s = _score(task, labels, preds)
    if s is None:
        raise RuntimeError(f"baseline failed in cross-validation (fold {info.get('fold')}): "
                           f"{info['stderr_tail'][-500:]}")
    return {"baseline_dev": s, "baseline_dev_preds_ref": ctx.store.blobs.put_json(preds)}


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


@register("research.socratic_filter")
def socratic_filter(state, params, ctx):
    """Question-only critic (AHOIS, GOAL L4.8): enforced in CODE, not by the prompt. A kept item is a single
    question (ends with '?', one '?', bounded length) and contains no code or directive phrasing; everything
    else is dropped and counted, so a critic cannot smuggle answers or instructions into later prompts."""
    from .socratic import is_genuine_question  # the rules and their stated limits live there
    raw = (state.get("critique_raw") or {}).get("questions", []) or []
    kept, dropped = [], 0
    for q in raw:
        text = str(q).strip()
        if is_genuine_question(text):
            kept.append(text)
        else:
            dropped += 1
    return {"critique": {"questions": kept[: params.get("max_questions", 5)], "dropped": dropped}}


@register("research.next_hypothesis")
def next_hypothesis(state, params, ctx):
    """Pop the next ranked hypothesis. When the queue is exhausted and a candidate beats the baseline on dev:
    refine it (improve operator, up to max_improves times), then route to the claim; otherwise give up
    honestly. (Experiment tree, GOAL L4.7: draft -> debug -> improve.)"""
    queue = list(state.get("queue") or [])
    current = queue.pop(0) if queue else None
    updates: dict[str, Any] = {}
    best = state.get("best")
    beats = best is not None and _better(_task(state), best["dev_score"], state["baseline_dev"])
    if current is not None:
        route = "implement"
    elif beats and state.get("n_improves", 0) < params.get("max_improves", 0):
        k = state.get("n_improves", 0) + 1
        current = {"id": f"improve-{k}", "idea": "refine the current best solution", "parent": best["hypothesis_id"],
                   "root": best.get("root_hypothesis") or best["hypothesis_id"]}
        updates["best_code"] = ctx.store.blobs.get(best["code_ref"]).decode("utf-8", errors="replace")
        route = "improve"
    else:
        route = "verify" if beats else "give_up"
    return {"queue": queue, "current": current, "fixes_for_current": 0, "run_error": None, "impl": None,
            "route": route, **updates}


def _dev_better(task: TaskSpec, labels: list[str], new_preds: list[str], best_preds: list[str]) -> bool:
    """Dev racing: a refinement replaces the incumbent only if a paired dev interval says it is better
    (one-sided 90%), not merely because its dev score is higher (repeated selection on one dev estimate
    overfits it)."""
    iv = paired_example_interval(M.METRICS[task.metric], labels, [new_preds], [best_preds],
                                 higher_is_better=task.higher_is_better, alpha=0.2, n_boot=1000)
    return not iv.degenerate and iv.lo > 0


@register("research.run_candidate")
def run_candidate(state, params, ctx):
    """Write the implemented code, score it by cross-validation on the public training data, and decide the
    route deterministically (PLAN P7): fix | next | verify | give_up."""
    _require_isolation(ctx)
    task = _task(state)
    code = (state.get("impl") or {}).get("code", "")
    _ws(ctx, "solution.py").write_text(code, encoding="utf-8")  # kept in the run workspace for the record/audit
    labels = _dev_labels(state, ctx)
    preds, info = _dev_predict(ctx, state, code.encode("utf-8"), f"attempt{state.get('n_attempts', 0) + 1}")
    s = _score(task, labels, preds)
    error = None
    if s is None:
        error = info["stderr_tail"] or "no valid predictions produced"
        if preds is not None:
            error = f"predictions could not be scored (expected {len(labels)} rows, got {len(preds)})"
    hyp = state.get("current") or {}
    n_attempts = state.get("n_attempts", 0) + 1
    attempt = {"n": n_attempts, "hypothesis_id": hyp.get("id"), "dev_score": s, "error": error,
               # the hypothesis an improvement refines (improve-k inherits its root), for the replication brief
               "root_hypothesis": hyp.get("root") or hyp.get("parent") or hyp.get("id"),
               "dev_preds_ref": ctx.store.blobs.put_json(preds) if s is not None else None,
               "code_ref": ctx.store.blobs.put(code.encode()), "wall_s": info["wall_s"]}
    best = state.get("best")
    is_improve = str(hyp.get("id", "")).startswith("improve-")
    if is_improve and s is not None and best is not None:
        attempt["accepted_improvement"] = _dev_better(task, labels, preds, ctx.store.blobs.get_json(
            best["dev_preds_ref"]))
        if attempt["accepted_improvement"]:
            best = attempt
    elif _better(task, s, best["dev_score"] if best else None):
        best = attempt
    attempts = list(state.get("attempts") or []) + [attempt]

    max_attempts = params.get("max_attempts", 6)
    max_fixes = params.get("max_fixes", 2)
    fixes = state.get("fixes_for_current", 0)
    improves_left = state.get("n_improves", 0) + (1 if is_improve else 0) < params.get("max_improves", 0)
    if error and fixes < max_fixes and n_attempts < max_attempts:
        route = "fix"
    elif n_attempts < max_attempts and (state.get("queue") or (improves_left and best is not None)):
        route = "next"
    elif best is not None and _better(task, best["dev_score"], state["baseline_dev"]):
        route = "verify"
    else:
        route = "give_up"
    return {"dev_score": s, "run_error": error, "attempts": attempts, "n_attempts": n_attempts,
            "best": best, "route": route, "fixes_for_current": fixes + (1 if route == "fix" else 0),
            "n_improves": state.get("n_improves", 0) + (1 if is_improve and route != "fix" else 0)}
