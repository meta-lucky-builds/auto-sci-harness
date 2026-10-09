"""Research-loop modules (PLAN L4). Deterministic code; LLM calls only where noted.

Wired together by a HarnessSpec (see examples/research_v0.yaml) so the harness evolver can
re-wire, re-prompt and re-parameterise the loop without touching this code.

Private data rule: anything the agent must not see lives under state keys starting with
`_private` (templates refuse to render them) or only on the host filesystem.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from ..kernel.execution import OUT_PATH
from ..runtime import register
from ..runtime.modules import ModuleContext
from ..sandbox.base import Sandbox, WorkspaceEscape, check_mount, protect_path, safe_path
from ..verify import metrics as M
from ..verify.leak import ESCAPE_MARKER, new_canary
from ..verify.stats import paired_example_interval
from . import tree as T
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


def cpu_exec_kwargs(sb: Sandbox, cpu_s: float | None) -> dict[str, Any]:
    """CPU-time limit wiring (GOAL §16.8; the limit itself is the sandbox's, workstream flowsb). None = off (today's
    wall-clock-only behaviour). A configured limit on a sandbox whose exec() cannot enforce it is REFUSED (fail
    closed), never silently dropped."""
    if cpu_s is None:
        return {}
    import inspect
    if "cpu_s" not in inspect.signature(sb.exec).parameters:
        raise RuntimeError(f"a CPU-time limit ({cpu_s} s) is configured but {type(sb).__name__}.exec cannot enforce "
                           f"it (needs the sandbox CPU-time limit)")
    return {"cpu_s": float(cpu_s)}


def _run_solution(sb: Sandbox, script: str, train: str, test: str, seed: int,
                  timeout_s: float, cpu_s: float | None = None) -> tuple[list[str] | None, dict[str, Any]]:
    """Run a solution under the CLI contract in sandbox `sb` (paths relative to its workspace). The output path is
    the one held-out runs use (kernel.execution.OUT_PATH), so the command line does not reveal the run kind."""
    out_rel = OUT_PATH
    safe_path(sb.workspace, "out").mkdir(exist_ok=True)
    stale = safe_path(sb.workspace, out_rel)
    if stale.exists():
        stale.unlink()
    res = sb.exec([sb.python, script, "--train", train, "--test", test,
                   "--out", out_rel, "--seed", str(seed)], timeout_s=timeout_s, **cpu_exec_kwargs(sb, cpu_s))
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


def dev_contract(state: dict[str, Any], ctx: ModuleContext) -> T.RunContract:
    """The run contract of every dev evaluation (GOAL L4.7: identical across the experiment tree's nodes): the
    command line, the sandbox environment, the content hashes of every fold's input files and the budget. Built
    from the SAME values `_dev_predict` runs with, so the contract a run reports is the one it ran under."""
    task, sb = _task(state), _sb(ctx)
    cpu_s = (state.get("limits") or {}).get("cpu_s")
    data = {f"fold{k}/{part}": f[ref] for k, f in enumerate(state["dev_folds"])
            for part, ref in (("train", "train_ref"), ("valid_x", "valid_x_ref"))}
    return T.RunContract.make(
        ["{python}", "solution.py", "--train", f"in/train/{task.train_file}", "--test",
         f"in/test/{task.test_features_file}", "--out", OUT_PATH, "--seed", "{seed}"],
        {"sandbox": type(sb).__name__, "isolation": sb.isolation, "python": str(sb.python)}, data,
        {"timeout_s": task.solution_timeout_s, "dev_time_factor": DEV_TIME_FACTOR,
         **({"cpu_s": float(cpu_s)} if cpu_s is not None else {})})


def _dev_predict(ctx: ModuleContext, state: dict[str, Any], code: bytes,
                 name: str, seed: int = 0) -> tuple[list[str] | None, dict[str, Any]]:
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
    first failing fold fails the whole evaluation; its diagnostics are returned (with "fold" set). `seed` is the
    program's --seed (0 for answering runs; the experiment tree's re-measures use fresh seeds). info["contract"] is
    the run contract this evaluation ran under (`dev_contract`)."""
    task = _task(state)
    contract = dev_contract(state, ctx)
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
            return None, {"fold": k, "wall_s": round(wall, 2), "stdout_tail": "", "stderr_tail": over.strip(),
                          "timed_out": True, "contract": contract}
        timeout = min(task.solution_timeout_s, remaining)
        box = sb.spawn(root / f"fold{k}")
        safe_path(box.workspace, "solution.py").write_bytes(code)
        for rel, ref in ((train_rel, fold["train_ref"]), (test_rel, fold["valid_x_ref"])):
            p = safe_path(box.workspace, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(ctx.store.blobs.get(ref))
        out, info = _run_solution(box, "solution.py", train_rel, test_rel, seed, timeout,
                                  (state.get("limits") or {}).get("cpu_s"))
        wall += info["wall_s"]
        if out is None and info.get("timed_out") and timeout < task.solution_timeout_s:
            info["stderr_tail"] += over
        if out is not None and len(out) != fold["n"]:
            info["stderr_tail"] += f"\n[harness] fold {k}: expected {fold['n']} predictions, got {len(out)}"
            out = None
        if out is None:
            return None, {**info, "fold": k, "wall_s": round(wall, 2), "contract": contract}
        preds += out
    return preds, {"fold": None, "wall_s": round(wall, 2), "stdout_tail": "", "stderr_tail": "", "contract": contract}


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
            "attempts": [], "n_attempts": 0, "best": None, "route": None,
            # CPU-seconds per program run (setup param cpu_limit_s; None = off). Protected node: not evolvable.
            **({"limits": {"cpu_s": float(params["cpu_limit_s"])}} if params.get("cpu_limit_s") is not None else {})}


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
    # the contract the baseline ran under: the experiment tree's root is answered by this run (run_candidate)
    return {"baseline_dev": s, "baseline_dev_preds_ref": ctx.store.blobs.put_json(preds),
            "baseline_dev_contract": info["contract"].to_json()}


@register("research.tournament")
def tournament(state, params, ctx):
    """K-way relative ranking with shuffled presentation order -> Bradley-Terry (PLAN L4.5). The body lives in
    research/tournament.py (both-order pairwise mode, BT intervals, Improve-weakest / Combine, pinned incumbent)."""
    from .tournament import tournament_module
    return tournament_module(state, params, ctx)


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


# ---------------------------------------------------------------- experiment tree wiring (GOAL §5 L4.7)
# Every dev evaluation of a candidate is a run of a node of the run's ExperimentTree (research/tree.py): answered
# nodes are immutable, every run reports the contract it ran under (`dev_contract`), repairs are edits citing the
# failed attempt (ledgered, capped by the run node's `max_fixes`, which IS the tree's repair cap), and a node is
# promoted (an improve round descends from it, an improvement replaces the incumbent) only after a fresh-seed
# re-measure and dev racing (`tree.confirm`). The dev labels stay private: they are read here and handed to the
# tree, never put in state. Sandbox work in a module happens BEFORE its tree commits, so an infrastructure retry of
# the module never meets half-logged events.
HYPOTHESIS_DECISION = "which hypothesis to implement"
IMPROVE_DECISION = "one refinement of the incumbent"
ENSEMBLE_DECISION = "the pre-declared vote ensemble of distinct candidates"


def _tree_store(ctx: ModuleContext) -> Any:
    return ctx.kernel().store  # the tree's log is a records-table write (like the memory log), not a Kernel entry


def _tree(state: dict[str, Any], ctx: ModuleContext, *, write: bool = True) -> T.ExperimentTree | None:
    """The run's tree, or None before the first candidate ran. A writer refuses a tree whose log is ahead of this
    state (events logged by an interrupted execution of a step): fail closed rather than re-run unledgered."""
    if not state.get("tree_id"):
        return None
    t = T.ExperimentTree.load(_tree_store(ctx), state["tree_id"])
    if write and t.seq != state.get("tree_seq"):
        raise RuntimeError(f"experiment tree {t.id} has {t.seq} events but this state knows {state.get('tree_seq')}: "
                           f"a step was interrupted after logging; re-running it would be an unledgered re-run")
    return t


def _ensure_tree(state: dict[str, Any], ctx: ModuleContext, params: dict[str, Any],
                 labels: list[str]) -> T.ExperimentTree:
    t = _tree(state, ctx)
    if t is not None:
        return t
    task = _task(state)
    contract = dev_contract(state, ctx)
    base = _baseline_code(state, ctx)
    t = T.ExperimentTree.create(_tree_store(ctx), f"{ctx.run_id}-dev", contract, root_code=base,
                                root_config={"baseline": state["datasets"]["baseline"]}, metric=task.metric,
                                higher_is_better=task.higher_is_better, dev_labels=labels,
                                max_repairs=int(params.get("max_fixes", T.MAX_REPAIRS)))
    # the root is answered by the baseline's dev run, under the contract that run reported
    base_contract = state.get("baseline_dev_contract")
    t.record("root", T.RunOutcome(T.RunContract.from_json(base_contract) if base_contract else contract,
                                  T.code_hash(base), 0, "ok", state["baseline_dev"],
                                  ctx.store.blobs.get_json(state["baseline_dev_preds_ref"])))
    return t


def run_outcome(info: dict[str, Any], scored: bool) -> str:
    """The tree's outcome class of one dev evaluation: ok, or the implementation failure it shows."""
    if scored:
        return "ok"
    text = str(info.get("stderr_tail", ""))
    if info.get("timed_out") or "exceeded its total time budget" in text:
        return "timeout"
    if "ModuleNotFoundError" in text or "ImportError" in text:
        return "missing_dep"
    if "MemoryError" in text or info.get("returncode") in (137, -9):
        return "oom"
    return "crash"


def _fresh_seed(t: T.ExperimentTree, node_id: str) -> int:
    return 1 + max((a.seed for a in t.nodes[node_id].attempts), default=0)


def _remeasure(state: dict[str, Any], ctx: ModuleContext, code: bytes, name: str,
               seed: int) -> T.RunOutcome:
    """A fresh-seed re-run of frozen code (sandbox work only; the caller logs it)."""
    task, labels = _task(state), _dev_labels(state, ctx)
    preds, info = _dev_predict(ctx, state, code, name, seed=seed)
    s = _score(task, labels, preds)
    return T.RunOutcome(info["contract"], T.code_hash(code), seed, run_outcome(info, s is not None), s,
                        preds if s is not None else None)


def _close(t: T.ExperimentTree | None, node_id: str | None, reason: str) -> None:
    if t is not None and node_id and t.nodes[node_id].status == "provisional":
        t.abandon(node_id, reason)


def _tree_state(t: T.ExperimentTree | None) -> dict[str, Any]:
    return {"tree_id": t.id, "tree_seq": t.seq} if t is not None else {}


def promote_incumbent(state: dict[str, Any], ctx: ModuleContext, t: T.ExperimentTree,
                      best: dict[str, Any]) -> tuple[bool, str]:
    """May a new (improve) round descend from the incumbent? Only if the tree confirms it as its round's winner:
    a fresh-seed re-measure, then dev racing on the answer AND the re-measure against the round's parent and every
    answered rival (tree.confirm). Provisional siblings are abandoned first (the round is being decided)."""
    node = best.get("tree_node")
    if not node or node not in t.nodes:
        return False, "the incumbent has no experiment-tree node"
    rnd = t.rounds.get(str(t.nodes[node].round))
    if rnd is None:
        return False, "the incumbent is the tree root"
    if rnd.winner == node:
        return True, f"{node} is the confirmed winner of {rnd.id}"
    if rnd.winner is not None:
        return False, f"{rnd.id} was decided for {rnd.winner}, not the incumbent {node}"
    seed = _fresh_seed(t, node)
    run = _remeasure(state, ctx, ctx.store.blobs.get(best["code_ref"]), f"remeasure-{node}", seed)
    for o in rnd.options:
        _close(t, o, "round closed: its winner is being confirmed")
    try:
        t.record(node, run, purpose="remeasure")
        t.confirm(rnd.id, node, _dev_labels(state, ctx))
    except T.TreeRefusal as e:
        return False, str(e)
    return True, f"{node} confirmed as the winner of {rnd.id} (fresh-seed re-measure, seed {seed})"


@register("research.next_hypothesis")
def next_hypothesis(state, params, ctx):
    """Pop the next ranked hypothesis. When the queue is exhausted and a candidate beats the baseline on dev:
    refine it (improve operator, up to max_improves times), then route to the claim; otherwise give up
    honestly. (Experiment tree, GOAL L4.7: draft -> debug -> improve.) Moving on abandons the previous node if it is
    still provisional (in the tree log); an improve round opens only from an incumbent the tree confirms."""
    t = _tree(state, ctx)
    _close(t, state.get("tree_node"), "the loop moved on without a repair")
    closed = {"tree_node": None, **_tree_state(t)}
    pending = list(state.get("pending") or [])
    if pending:  # packed implementations (one call wrote several scripts) are run one by one, in order
        member = pending.pop(0)
        return {"pending": pending, "current": member["hypothesis"], "fixes_for_current": 0, "run_error": None,
                "impl": {"code": member["code"], "notes": member["notes"]}, "route": "run", **closed}
    queue = list(state.get("queue") or [])
    pack = int(params.get("pack", 1))
    if pack > 1 and queue:  # GOAL §16.8: one implement call writes up to `pack` scripts (pays the call overhead once)
        return {"queue": queue[pack:], "batch": queue[:pack], "current": None, "fixes_for_current": 0,
                "run_error": None, "impl": None, "route": "implement_batch", **closed}
    current = queue.pop(0) if queue else None
    updates: dict[str, Any] = dict(closed)
    best = state.get("best")
    beats = best is not None and _better(_task(state), best["dev_score"], state["baseline_dev"])
    can_improve = beats and state.get("n_improves", 0) < params.get("max_improves", 0)
    if current is None and can_improve and params.get("headroom_gate", False):
        ok, why = headroom(_task(state), _dev_labels(state, ctx), ctx.store.blobs.get_json(best["dev_preds_ref"]))
        if not ok:  # the improve call provably cannot be accepted by dev racing: skip it (a skipped call is recorded)
            can_improve = False
            updates["skipped_calls"] = [*(state.get("skipped_calls") or []),
                                        {"node": "improve", "incumbent": best.get("hypothesis_id"), "reason": why}]
    if current is None and can_improve:
        if t is None:
            ok, why = False, "no experiment tree (no candidate ran)"
        else:
            _require_isolation(ctx)
            ok, why = promote_incumbent(state, ctx, t, best)
            updates.update(_tree_state(t))
        if not ok:  # promotion needs a fresh re-measure and dev racing (GOAL L4.7): no round descends otherwise
            can_improve = False
            updates["skipped_calls"] = [*(updates.get("skipped_calls") or state.get("skipped_calls") or []),
                                        {"node": "improve", "incumbent": best.get("hypothesis_id"),
                                         "reason": f"experiment tree: {why}"}]
    if current is not None:
        route = "implement"
    elif can_improve:
        k = state.get("n_improves", 0) + 1
        current = {"id": f"improve-{k}", "idea": "refine the current best solution", "parent": best["hypothesis_id"],
                   "root": best.get("root_hypothesis") or best["hypothesis_id"], "tree_parent": best["tree_node"]}
        updates["best_code"] = ctx.store.blobs.get(best["code_ref"]).decode("utf-8", errors="replace")
        route = "improve"
    else:
        route = "verify" if beats else "give_up"
    return {"queue": queue, "current": current, "fixes_for_current": 0, "run_error": None, "impl": None,
            "route": route, **updates}  # updates carries tree_node=None and the tree's sequence


def _dev_better(task: TaskSpec, labels: list[str], new_preds: list[str], best_preds: list[str]) -> bool:
    """Dev racing: a refinement replaces the incumbent only if a paired dev interval says it is better
    (one-sided 90%), not merely because its dev score is higher (repeated selection on one dev estimate
    overfits it)."""
    iv = paired_example_interval(M.METRICS[task.metric], labels, [new_preds], [best_preds],
                                 higher_is_better=task.higher_is_better, alpha=0.2, n_boot=1000)
    return not iv.degenerate and iv.lo > 0


# ---------------------------------------------------------------- yield (GOAL §16.8): fewer calls, same checks
def headroom(task: TaskSpec, labels: list[str], incumbent_preds: list[str]) -> tuple[bool, str]:
    """Could a refinement of the incumbent pass dev racing (`_dev_better`) at all? The test runs the REAL racing
    function on the pointwise-best candidate: one whose dev predictions equal the dev labels (every incumbent error
    fixed, nothing broken).

    - Incumbent with no dev error: every paired difference is <= 0, so every bootstrap mean is <= 0 and the lower
      bound (one of the bootstrap values) is <= 0. No candidate can pass: exact.
    - Otherwise the verdict is COMPUTED for the pointwise-best candidate with the racing function's own bootstrap;
      BCa is not proved monotone in the differences, so "no candidate can pass" is not proved for every candidate.
      Checked against the real function on 2026-10-07: at n = 24 and n = 124 a perfect candidate fails racing for
      0 or 1 incumbent errors and passes from 2 errors on (the review's "e >= 3" estimate was off by one).
    The gate only skips model calls (deny-only, invariant 21); a wrong skip costs yield, never integrity."""
    errors = sum(1 for y, p in zip(labels, incumbent_preds, strict=True) if p != y)
    if errors == 0:
        return False, "incumbent has no dev error: no refinement can pass dev racing (exact)"
    if _dev_better(task, labels, list(labels), incumbent_preds):
        return True, f"a perfect refinement would pass dev racing ({errors} incumbent dev error(s))"
    return False, (f"even a perfect refinement fails dev racing against the incumbent ({errors} dev error(s) of "
                   f"{len(labels)}; computed with the racing function)")


#: GOAL §16.8 out-of-fold signatures: two candidates whose dev predictions agree on at least this fraction of rows
#: are the same behaviour (dedup).
SIGNATURE_AGREEMENT = 0.99  # UNCALIBRATED (owner decision, GOAL §13.8)
ENSEMBLE_SIZE = 3  # UNCALIBRATED (owner decision, GOAL §13.8): members of the one pre-declared vote ensemble


def signature(preds: list[str]) -> str:
    """Out-of-fold signature of a candidate: the hash of its dev prediction vector."""
    import hashlib
    return hashlib.sha256(json.dumps(preds, separators=(",", ":")).encode()).hexdigest()


def agreement(a: list[str], b: list[str]) -> float:
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a) if a else 1.0


def _selectable(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Valid candidates: scored on dev, with stored predictions; refinements only if dev racing accepted them."""
    return [a for a in attempts if a.get("dev_score") is not None and a.get("dev_preds_ref")
            and (not str(a.get("hypothesis_id", "")).startswith("improve-") or a.get("accepted_improvement"))]


def select_incumbent(task: TaskSpec, labels: list[str], attempts: list[dict[str, Any]],
                     ctx: ModuleContext) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Tie-aware selection (GOAL §16.8). E = the candidates statistically tied with the top dev score: the top
    candidate does NOT beat them under dev racing (the same paired interval). From E pick the lowest projected
    runtime (the measured dev wall time: every candidate ran the same folds, so it orders projected held-out
    runtime), then the smallest code, then the earliest attempt. Returns (pick, record)."""
    pool = _selectable(attempts)
    if not pool:
        return None, {"rule": "tie_aware", "tied": [], "reason": "no valid candidate"}
    sign = 1.0 if task.higher_is_better else -1.0
    top = max(pool, key=lambda a: (sign * a["dev_score"], -a["n"]))
    top_preds = ctx.store.blobs.get_json(top["dev_preds_ref"])
    tied = [a for a in pool if a is top
            or not _dev_better(task, labels, top_preds, ctx.store.blobs.get_json(a["dev_preds_ref"]))]

    def cost(a: dict[str, Any]) -> tuple[float, int, int]:
        return (float(a.get("wall_s") or 0.0), len(ctx.store.blobs.get(a["code_ref"])), a["n"])

    pick = min(tied, key=cost)
    return pick, {"rule": "tie_aware", "top": top["n"], "tied": [a["n"] for a in tied], "pick": pick["n"],
                  "reason": (f"attempt {pick['n']} has the lowest dev wall time, then code size, among "
                             f"{len(tied)} candidate(s) tied with the top dev score (attempt {top['n']}) under "
                             f"dev racing")}


def distinct_members(ranked: list[dict[str, Any]], ctx: ModuleContext, k: int) -> list[dict[str, Any]]:
    """Up to k behaviourally distinct candidates, in the given order (signature dedup by agreement)."""
    kept: list[tuple[dict[str, Any], list[str]]] = []
    for a in ranked:
        p = ctx.store.blobs.get_json(a["dev_preds_ref"])
        if all(agreement(p, q) < SIGNATURE_AGREEMENT for _, q in kept):
            kept.append((a, p))
        if len(kept) == k:
            break
    return [a for a, _ in kept]


def vote(member_preds: list[list[str]]) -> list[str]:
    """Hard majority vote; a tie goes to member 0 (the selected incumbent) when it is among the tied values, else
    to the tied value of the earliest member. ENSEMBLE_WRAPPER implements the same rule."""
    from collections import Counter
    out = []
    for row in zip(*member_preds, strict=True):
        counts = Counter(row)
        top = max(counts.values())
        out.append(next(v for v in row if counts[v] == top))
    return out


ENSEMBLE_WRAPPER = '''# Harness-written hard-vote ensemble (GOAL 16.8): the members run unchanged, same arguments.
import argparse, base64, csv, os, subprocess, sys
from collections import Counter
MEMBERS = {members!r}
p = argparse.ArgumentParser()
for k in ("--train", "--test", "--out"):
    p.add_argument(k)
p.add_argument("--seed", type=int, default=0)
a = p.parse_args()
here = os.path.dirname(os.path.abspath(a.out))
preds = []
for i, src in enumerate(MEMBERS):
    path = os.path.join(here, "member%d.py" % i)
    with open(path, "w", encoding="utf-8") as f:
        f.write(base64.b64decode(src).decode("utf-8"))
    out = os.path.join(here, "member%d_preds.csv" % i)
    subprocess.run([sys.executable, path, "--train", a.train, "--test", a.test, "--out", out,
                    "--seed", str(a.seed)], check=True)
    with open(out, newline="") as f:
        rows = list(csv.reader(f))
    preds.append([r[0] for r in rows[1:]])
voted = []
for row in zip(*preds):
    counts = Counter(row)
    top = max(counts.values())
    voted.append(next(v for v in row if counts[v] == top))
with open(a.out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["prediction"])
    for v in voted:
        w.writerow([v])
'''


def ensemble_code(member_codes: list[bytes]) -> str:
    import base64
    return ENSEMBLE_WRAPPER.format(members=[base64.b64encode(c).decode("ascii") for c in member_codes])


@register("research.run_candidate")
def run_candidate(state, params, ctx):
    """Write the implemented code, score it by cross-validation on the public training data, and decide the
    route deterministically (PLAN P7): fix | next | verify | give_up.

    Every evaluation is a run of an experiment-tree node (GOAL L4.7): a new hypothesis is a new option of the
    hypothesis round, an improvement a one-option round descending from the confirmed incumbent, and code from the
    `fix` node is a ledgered repair of the node's failed attempt. The tree refuses (and logs) what the loop must not
    do: re-running unchanged code after a failure (not a repair), a repair past `max_fixes` (the node is abandoned),
    a run under another contract. A refused run is not a candidate. An improvement replaces the incumbent only if
    the tree confirms it: dev racing against the incumbent on its run AND on a fresh-seed re-measure."""
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
    best = state.get("best")
    is_improve = str(hyp.get("id", "")).startswith("improve-")
    # an improvement that wins dev racing on its run is re-measured with a fresh seed (sandbox work before any
    # tree commit); one that does not cannot be promoted, so no re-measure is spent on it
    remeasure = None
    if is_improve and s is not None and best is not None and _dev_better(
            task, labels, preds, ctx.store.blobs.get_json(best["dev_preds_ref"])):
        remeasure = _remeasure(state, ctx, code.encode("utf-8"), f"remeasure{n_attempts}", 1)

    t = _ensure_tree(state, ctx, params, labels)
    node, refused, accepted = state.get("tree_node"), None, False
    try:
        if node is None:
            if is_improve:
                rnd = t.open_round(hyp["tree_parent"], IMPROVE_DECISION)
                node = t.add_option(rnd, IMPROVE_DECISION, code, {"hypothesis": hyp.get("id")})
            else:
                rnd = state.get("tree_round")
                r = t.rounds.get(str(rnd))
                if r is None or r.winner is not None or len(r.options) >= T.ROUND_MAX_OPTIONS:
                    rnd = t.open_round("root", HYPOTHESIS_DECISION)
                node = t.add_option(rnd, HYPOTHESIS_DECISION, code, {"hypothesis": hyp.get("id")})
                state = {**state, "tree_round": rnd}
        else:
            n = t.nodes[node]
            last = n.attempts[-1] if n.attempts else None
            if last is not None and T.code_hash(code) != n.code_hash:  # code from `fix`: a ledgered repair
                t.edit(node, code, dict(n.config), repair_of=last.idx)
            # unchanged code after a failure is NOT edited: the tree refuses the re-run (REPAIR_UNLEDGERED)
        t.record(node, T.RunOutcome(info["contract"], T.code_hash(code), 0, run_outcome(info, s is not None), s,
                                    preds if s is not None else None))
        if remeasure is not None:
            t.record(node, remeasure, purpose="remeasure")
            t.confirm(str(t.nodes[node].round), node, labels)
            accepted = True
    except T.TreeRefusal as e:
        if (remeasure is not None and node in t.nodes and t.nodes[node].status == "answered"
                and e.code is T.Code.PROMOTION_REFUSED):
            accepted = False  # the improvement ran and was answered, but the re-measure did not confirm it
        else:
            refused = e
    if refused is not None:  # the run is not a candidate: its result is discarded, the refusal is in the tree log
        s, error = None, f"refused by the experiment tree: {refused}"
    status = t.nodes[node].status if node in t.nodes else "abandoned"
    attempt = {"n": n_attempts, "hypothesis_id": hyp.get("id"), "dev_score": s, "error": error,
               # the hypothesis an improvement refines (improve-k inherits its root), for the replication brief
               "root_hypothesis": hyp.get("root") or hyp.get("parent") or hyp.get("id"),
               "dev_preds_ref": ctx.store.blobs.put_json(preds) if s is not None else None,
               "code_ref": ctx.store.blobs.put(code.encode()), "wall_s": info["wall_s"], "tree_node": node,
               **({"tree_refused": refused.code.value} if refused is not None else {})}
    if is_improve and s is not None and best is not None:
        attempt["accepted_improvement"] = accepted
        if remeasure is not None:
            attempt["remeasure_dev_score"] = remeasure.metric
        if accepted:
            best = attempt
    elif _better(task, s, best["dev_score"] if best else None):
        best = attempt
    attempts = list(state.get("attempts") or []) + [attempt]
    selection: dict[str, Any] = {}
    if params.get("selection", "max") == "tie_aware" and s is not None:
        pick, record = select_incumbent(task, labels, attempts, ctx)
        best, selection = pick, {"selection": record}

    max_attempts = params.get("max_attempts", 6)
    improves_left = state.get("n_improves", 0) + (1 if is_improve else 0) < params.get("max_improves", 0)
    # the repair cap is the tree's (max_fixes, fixed when the tree was created): a provisional node after a failure
    # still has a repair left; an abandoned one has none
    if error and refused is None and status == "provisional" and n_attempts < max_attempts:
        route = "fix"
    elif n_attempts < max_attempts and (state.get("queue") or state.get("pending")
                                        or (improves_left and best is not None)):
        route = "next"
    elif best is not None and _better(task, best["dev_score"], state["baseline_dev"]):
        route = "verify"
    else:
        route = "give_up"
    if route != "fix":
        _close(t, node, "no repair: the loop moved on")
    fixes = t.nodes[node].repairs if node in t.nodes else 0
    return {"dev_score": s, "run_error": error, "attempts": attempts, "n_attempts": n_attempts, **selection,
            "best": best, "route": route, "fixes_for_current": fixes, "tree_node": node if route == "fix" else None,
            **({"tree_round": state["tree_round"]} if state.get("tree_round") else {}), **_tree_state(t),
            "n_improves": state.get("n_improves", 0) + (1 if is_improve and route != "fix" else 0)}


@register("research.execution_rank")
def execution_rank(state, params, ctx):
    """GOAL §16.8: execution is the ranker. No model call: the queue keeps the proposer's order and dev
    cross-validation decides (research_v1's K-way `research.tournament` stays available as an option)."""
    hyps = [h for h in (state.get("hypotheses") or {}).get("hypotheses", []) if isinstance(h, dict)]
    ids = [h.get("id") for h in hyps]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) for i in ids):
        raise ValueError("hypothesis ids must be unique strings")
    return {"ranking": {"order": ids, "method": "execution (dev cross-validation ranks; no tournament call)"},
            "queue": hyps, "current": None}


MAX_PACKED_CODE_CHARS = 60_000  # a packed member longer than this fails (alone)


@register("research.unpack_solutions")
def unpack_solutions(state, params, ctx):
    """Validate a packed implement answer member by member (invariant 7, fail closed): a missing, duplicated,
    empty or oversized member fails ONLY itself and is recorded; valid members wait in `pending` and are run one
    by one exactly like single implementations. Members for hypotheses that were not asked for are ignored."""
    batch = [h for h in (state.get("batch") or []) if isinstance(h, dict)]
    raw = state.get("impl_batch")
    sols = raw.get("solutions") if isinstance(raw, dict) else None
    by_id: dict[str, dict[str, Any]] = {}
    dup: set[str] = set()
    for sol in sols if isinstance(sols, list) else []:
        if isinstance(sol, dict) and isinstance(sol.get("id"), str):
            if sol["id"] in by_id:
                dup.add(sol["id"])
            by_id[sol["id"]] = sol
    cap = int(params.get("max_code_chars", MAX_PACKED_CODE_CHARS))
    pending, failures = list(state.get("pending") or []), list(state.get("pack_failures") or [])
    for h in batch:
        sol = by_id.get(h.get("id"))
        code = sol.get("code") if sol else None
        reason = ("duplicate id in the packed answer" if h.get("id") in dup else
                  "no script for this hypothesis in the packed answer" if sol is None else
                  "empty or non-string code" if not isinstance(code, str) or not code.strip() else
                  f"script longer than {cap} characters" if len(code) > cap else None)
        if reason:
            failures.append({"hypothesis_id": h.get("id"), "reason": reason})
        else:
            pending.append({"hypothesis": h, "code": code, "notes": str(sol.get("notes", ""))[:500]})
    ignored = sorted(set(by_id) - {h.get("id") for h in batch})
    return {"pending": pending, "pack_failures": failures, "batch": [], "impl_batch": None,
            **({"pack_ignored": ignored} if ignored else {})}


@register("research.ensemble")
def ensemble(state, params, ctx):
    """GOAL §16.8: ONE pre-declared exact-dev ensemble. Members: the selected incumbent plus the next best
    behaviourally distinct valid candidates (out-of-fold signatures, dedup by agreement), ENSEMBLE_SIZE in all.
    Its dev predictions are the hard vote of the members' STORED out-of-fold vectors (exact for deterministic
    members: same folds, seed 0; no model call). It replaces the incumbent only if it wins dev racing (paired
    interval), never on a point comparison. With verify_dev (default), the harness-written wrapper is then run on the
    dev folds and must reproduce the stored vote exactly. Classification metrics only (a hard vote needs labels)."""
    task = _task(state)
    best = state.get("best")
    rec: dict[str, Any] = {"rule": f"hard vote of {ENSEMBLE_SIZE} distinct candidates", "used": False}
    if task.metric not in M.CLASSIFICATION or best is None or not best.get("dev_preds_ref"):
        return {"ensemble": {**rec, "reason": "not applicable (needs a classification metric and an incumbent)"}}
    labels = _dev_labels(state, ctx)
    attempts = list(state.get("attempts") or [])
    sign = 1.0 if task.higher_is_better else -1.0
    others = sorted((a for a in _selectable(attempts) if a["n"] != best["n"]),
                    key=lambda a: (-sign * a["dev_score"], a["n"]))
    if params.get("members") == "diversity":  # GOAL §8.11: members only from the error-decorrelated set
        div = state.get("diversity")
        if not (isinstance(div, dict) and div.get("enabled") and isinstance(div.get("kept"), list)):
            return {"ensemble": {**rec, "reason": "members: diversity, but no diversity record (fail closed)"}}
        rec["diversity_excluded"] = [a["n"] for a in others if a["n"] not in div["kept"]]
        others = [a for a in others if a["n"] in div["kept"]]
    members = distinct_members([best, *others], ctx, ENSEMBLE_SIZE)
    rec["members"] = [a["n"] for a in members]
    if len(members) < ENSEMBLE_SIZE or members[0] is not best:
        return {"ensemble": {**rec, "reason": f"fewer than {ENSEMBLE_SIZE} behaviourally distinct candidates"}}
    voted = vote([ctx.store.blobs.get_json(a["dev_preds_ref"]) for a in members])
    s = _score(task, labels, voted)
    rec["dev_score"] = s
    if not _dev_better(task, labels, voted, ctx.store.blobs.get_json(best["dev_preds_ref"])):
        return {"ensemble": {**rec, "reason": "the vote does not beat the incumbent under dev racing"}}
    code = ensemble_code([ctx.store.blobs.get(a["code_ref"]) for a in members])
    wall = sum(float(a.get("wall_s") or 0.0) for a in members)
    if not params.get("verify_dev", True):  # GOAL L4.7: promotion needs a run and a fresh re-measure in the tree
        return {"ensemble": {**rec, "reason": "not used: with verify_dev off the wrapper never runs, and the "
                                              "experiment tree promotes only after a run and a fresh re-measure"}}
    _require_isolation(ctx)
    preds, info = _dev_predict(ctx, state, code.encode("utf-8"), "ensemble")
    wall = info["wall_s"]
    if preds != voted:
        return {"ensemble": {**rec, "reason": "the wrapper did not reproduce the stored vote on dev "
                                              "(a member is nondeterministic or failed)"}}
    remeasure = _remeasure(state, ctx, code.encode("utf-8"), "ensemble-remeasure", 1)
    t = _tree(state, ctx)
    if t is None:
        return {"ensemble": {**rec, "reason": "no experiment tree (no candidate ran)"}}
    rnd = t.open_round("root", ENSEMBLE_DECISION)
    node = t.add_option(rnd, ENSEMBLE_DECISION, code, {"members": rec["members"]})
    try:
        t.record(node, T.RunOutcome(info["contract"], T.code_hash(code), 0, "ok", s, voted))
        t.record(node, remeasure, purpose="remeasure")
        t.confirm(rnd, node, labels)
    except T.TreeRefusal as e:
        return {"ensemble": {**rec, "reason": f"experiment tree: {e}"}, **_tree_state(t)}
    attempt = {"n": len(attempts) + 1, "hypothesis_id": "ensemble-vote", "dev_score": s, "error": None,
               "root_hypothesis": best.get("root_hypothesis") or best.get("hypothesis_id"),
               "dev_preds_ref": ctx.store.blobs.put_json(voted), "code_ref": ctx.store.blobs.put(code.encode()),
               "wall_s": wall, "members": rec["members"], "tree_node": node}
    return {"ensemble": {**rec, "used": True, "reason": "the vote beats the incumbent under dev racing and the "
                                                        "experiment tree confirmed it after a fresh re-measure"},
            "attempts": [*attempts, attempt], "best": attempt, **_tree_state(t)}
