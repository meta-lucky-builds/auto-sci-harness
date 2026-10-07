"""Kernel-integrated end of the research loop (GOAL §4, §5 L4 steps 6 and 10; M2).

    claim    -> deterministic code (no LLM) turns the best dev candidate into a typed Claim with a forecast,
                and pre-registers it BEFORE any held-out label is read
    confirm  -> both arms run on the held-out target, every seed in a fresh sandbox; labels are read through
                the vault (logged), one ExecRecord per run
    certify  -> the Evidence Kernel issues the certificate (K0-K9, with re-execution); bundle + checklist
    report   -> rendered ONLY from the ledger and the evidence store (never from mutable run state)
    audit    -> I1 certificate resolves and matches the run state, I2 comparator integrity (K5),
                I3 citations (literature check on the run's frozen snapshot + retrieval log; n/a without),
                I4 report re-renders byte-identically + every score recomputed from stored predictions,
                I5 leak and canary scan

All of these nodes are protected from automatic harness edits (evolve/edits.py).
"""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import statistics
import time
import tokenize
from pathlib import Path
from typing import Any

from ..config import data_dir
from ..kernel import (
    DEFAULT_ARGV,
    AnalysisPlan,
    ArtifactRef,
    Claim,
    ClaimKind,
    DatasetRole,
    Effect,
    ExecRecord,
    InputRef,
    Kernel,
    KernelError,
    MetricRef,
    Risk,
    SandboxReplayer,
    Scope,
    run_program,
)
from ..kernel.bundle import build_bundle
from ..kernel.checklist import fill, publishable
from ..kernel.checklist import render as render_checklist
from ..runtime import register
from ..runtime.modules import ModuleContext
from ..verify import metrics as M
from ..verify.leak import label_windows, leak_audit
from ..verify.stats import paired_example_interval
from .modules import _dev_labels, _require_isolation, _sb, _task
from .task import TaskSpec

_NORMAL = statistics.NormalDist()


def _inline(text: Any, limit: int = 500) -> str:
    """Model-written text as ONE line (whitespace collapsed, backticks removed, bounded), so it cannot start new
    report lines (fake headings, a fake headline result) or break out of inline code spans."""
    s = " ".join(str(text).split()).replace("`", "'")
    return s if len(s) <= limit else s[:limit] + "…"


def _hypotheses(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The hypothesis list from (evolvable, model-written) state, parsed defensively: trusted code must not crash
    on a malformed value an evolved harness wrote; anything that is not a list of objects counts as empty."""
    raw = state.get("hypotheses")
    items = raw.get("hypotheses") if isinstance(raw, dict) else None
    return [h for h in items if isinstance(h, dict)] if isinstance(items, list) else []


def _labels(raw: bytes) -> list[str]:
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
    return [r[0] for r in rows[1:] if r]


def _kernel(ctx: ModuleContext) -> Kernel:
    """A Kernel whose replayer re-executes in fresh sandboxes of the same kind as the run's sandbox."""
    sb = _sb(ctx)
    root = sb.workspace.parent / f"{sb.workspace.name}-replay"
    return ctx.kernel(replayer=SandboxReplayer(lambda key: sb.spawn(root / key)))


def register_task(ctx: ModuleContext, task: TaskSpec, task_dir: Path) -> dict[str, str]:
    """Register the task's data and baseline in the vault and return their ids (content-based, see
    TaskSpec.dataset_ids). The labels are read from disk here, once, to register them; afterwards only through the
    vault. A task whose canary differs from the canary registered for the same data is refused: its own canary
    would never be scanned for (tasks sharing data must share the canary)."""
    vault = ctx.kernel().vault
    train = (task_dir / "public" / task.train_file).read_bytes()
    test_x = (task_dir / "public" / task.test_features_file).read_bytes()
    test_y = (task_dir / "private" / task.test_labels_file).read_bytes()
    baseline = (task_dir / task.baseline_file).read_bytes()

    def digest(*parts: bytes) -> str:  # fixed-length per-file digests: no separator ambiguity
        return hashlib.sha256(b"".join(hashlib.sha256(b).digest() for b in parts)).hexdigest()

    ids = task.dataset_ids(digest(train, test_x, test_y), digest(train, test_x, test_y, baseline))
    vault.register_dataset(ids["train"], DatasetRole.DEV, public={task.train_file: train}, private={})
    canary_path = task_dir / "private" / task.canary_file
    canary = canary_path.read_text(encoding="utf-8").strip() if canary_path.exists() else None
    vault.register_dataset(ids["test"], DatasetRole.TEST, public={task.test_features_file: test_x},
                           private={task.test_labels_file: test_y}, canary=canary)
    if vault.canary(ids["test"]) != canary:
        raise ValueError(f"task {task.id}: its canary differs from the canary registered for the same held-out data "
                         f"({ids['test']}); tasks sharing data must share the canary")
    vault.register_artifact(ids["baseline"], baseline, "baseline")
    return ids


# ------------------------------------------------------------------ claim (pre-registration)
def forecast(task: TaskSpec, state: dict[str, Any], ctx: ModuleContext, floor: float, fraction: float,
             candidate_key: str = "best", fixed_delta: float | None = None) -> tuple[float, float, float, float]:
    """(min effect, probability, predicted lo, predicted hi) from the dev estimate: out-of-fold predictions of
    k-fold cross-validation on the public training data, paired with the baseline's out-of-fold predictions.

    The claimed minimum effect is max(task floor, fraction x the lower bound of a two-sided 90% paired BCa
    interval over dev examples): the claim asserts what the dev evidence supports, with a margin, instead of the
    weakest bound (which K7 rightly rejects as trivially safe). The probability is P(held-out effect >= that
    minimum) under a normal model of the dev estimate. Two known optimistic biases, NOT corrected here and
    measured by the prediction ledger (calibration): dev picked the best of several candidates (winner's
    curse), and the bootstrap resamples out-of-fold examples as if independent, ignoring the variance between
    the k fitted models (no unbiased estimator of k-fold CV variance exists; Bengio & Grandvalet 2004)."""
    best = state[candidate_key]
    labels = _dev_labels(state, ctx)
    ref_s, ref_b = best.get("dev_preds_ref"), state.get("baseline_dev_preds_ref")
    if not ref_s or not ref_b:
        return (fixed_delta if fixed_delta is not None else floor), 0.5, 0.0, 0.0
    iv = paired_example_interval(M.METRICS[task.metric], labels, [ctx.store.blobs.get_json(ref_s)],
                                 [ctx.store.blobs.get_json(ref_b)], higher_is_better=task.higher_is_better,
                                 alpha=0.1, n_boot=1000)
    if iv.degenerate:
        delta = fixed_delta if fixed_delta is not None else max(floor, fraction * iv.estimate)
        return delta, (0.95 if iv.estimate >= delta else 0.05), iv.estimate, iv.estimate
    delta = fixed_delta if fixed_delta is not None else max(floor, fraction * iv.lo)
    se = (iv.hi - iv.lo) / (2 * _NORMAL.inv_cdf(0.95))
    q = _NORMAL.cdf((iv.estimate - delta) / se) if se > 0 else 0.5
    return delta, min(0.95, max(0.05, q)), iv.lo, iv.hi


def _preflight(task: TaskSpec, state: dict[str, Any], ctx: ModuleContext, claim: Claim,
               candidate_key: str) -> Any:
    """Run the certifiability preflight (research/preflight.py) on PUBLIC numbers only: the held-out row count (from
    the target's public feature file), the paired dev outcome (public training labels and out-of-fold predictions)
    and the family size from the public prereg ledger. No held-out label is read; nothing is written to the ledger."""
    from ..kernel.checks import related_targets
    from .preflight import DEFAULT_CONFIG, PreflightResult, PublicInputs, dev_pairs, preflight
    kernel = ctx.kernel()
    target = claim.generalization_targets[0]
    test_x = kernel.vault.read_public(target, task.test_features_file)
    n_test = len(list(csv.reader(io.StringIO(test_x.decode("utf-8"))))) - 1
    related = related_targets(kernel.vault, target)
    registered = {e.body.get("claim_hash") for e in kernel.ledger.entries("prereg")
                  if related & set(e.body.get("claim", {}).get("generalization_targets", []))}
    family = max(claim.analysis_plan.n_comparisons, len(registered - {claim.claim_hash()}) + 1)
    best = state[candidate_key]
    if not (best.get("dev_preds_ref") and state.get("baseline_dev_preds_ref")):
        return PreflightResult("unsupported", None, None, None, None, None, 0, DEFAULT_CONFIG.theta,
                               ["no paired dev predictions: nothing to simulate from (no objection)"])
    pairs = dev_pairs(_dev_labels(state, ctx), ctx.store.blobs.get_json(best["dev_preds_ref"]),
                          ctx.store.blobs.get_json(state["baseline_dev_preds_ref"]))
    plan = claim.analysis_plan
    return preflight(PublicInputs(metric=task.metric, higher_is_better=task.higher_is_better, n_test=n_test,
                                  dev_pairs=pairs, min_effect=claim.effect.min_effect, alpha=plan.alpha,
                                  family_size=family, seeds=plan.seeds, n_boot=plan.n_boot, kind=claim.kind))


@register("research.claim")
def make_claim(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    """params: seeds, alpha, n_comparisons, delta_fraction; prefix (state-key prefix, "" or "replica_");
    candidate (state key of the candidate, default "best"); same_delta_as (state key of an earlier claim whose
    minimum effect this claim must clear, e.g. a replication of "claim")."""
    task = _task(state)
    p = params.get("prefix", "")
    candidate_key = params.get("candidate", "best")
    best = state.get(candidate_key)
    if best is None:
        raise RuntimeError("no candidate to claim about")
    fixed = None
    if params.get("same_delta_as"):
        fixed = Claim.model_validate(state[params["same_delta_as"]]).effect.min_effect
    kernel = ctx.kernel()
    ids = state["datasets"]
    subject = kernel.vault.register_artifact(f"{task.id}/solution/{best['code_ref'][:16]}",
                                             ctx.store.blobs.get(best["code_ref"]), "method")
    baseline = kernel.vault.artifacts()[ids["baseline"]]
    delta, q, lo, hi = forecast(task, state, ctx, task.effect_floor(state["baseline_dev"]),
                                fraction=params.get("delta_fraction", 0.5), candidate_key=candidate_key,
                                fixed_delta=fixed)
    hid = _inline(best.get("hypothesis_id"), 80)  # model-written id: never raw in a claim statement
    who = ("an independent re-implementation (written from the claim text, without the code) of hypothesis "
           f"{hid}") if p else f"the solution for hypothesis {hid}"
    claim = Claim(
        kind=ClaimKind.IMPROVEMENT,
        statement=f"On task {task.id}, {who} beats the task baseline on {task.metric} by at least {delta:.4g} "
                  f"on held-out data",
        subject=ArtifactRef(artifact_id=subject.artifact_id, hash=subject.hash),
        comparator=ArtifactRef(artifact_id=baseline.artifact_id, hash=baseline.hash),
        metric=MetricRef(id=task.metric, higher_is_better=task.higher_is_better),
        effect=Effect(min_effect=delta, predicted_lo=lo, predicted_hi=hi, probability=q),
        scope=Scope(datasets=(ids["train"], ids["test"])), development=(ids["train"],),
        generalization_targets=(ids["test"],),
        # n_comparisons: the PRE-DECLARED size of the multiplicity family on this test set. K4 uses
        # max(declared, actual registered claims), so declaring a campaign's planned number of claims per task
        # keeps certification strictness constant during the campaign (no drift against later candidates).
        analysis_plan=AnalysisPlan(seeds=tuple(params.get("seeds", (0, 1, 2))), alpha=params.get("alpha", 0.05),
                                   n_comparisons=int(params.get("n_comparisons", 1))),
        assumptions=("the k-fold cross-validation estimate on the public training data is representative of "
                     "held-out performance",),
        risk=Risk(), resolve_by=time.time() + 86400)
    if params.get("preflight", False):  # GOAL §16.8: deny-only; it never registers or certifies anything
        result = _preflight(task, state, ctx, claim, candidate_key)
        if result.denied:
            return {f"{p}preflight": result.as_record(), "route": "preflight_denied"}
        extra = {f"{p}preflight": result.as_record()}
    else:
        extra = {}
    entry = kernel.preregister(claim)
    if extra:
        return {f"{p}claim": claim.model_dump(mode="json"), f"{p}claim_hash": claim.claim_hash(),
                f"{p}prereg_seq": entry.seq, **extra, "route": "confirm"}
    return {f"{p}claim": claim.model_dump(mode="json"), f"{p}claim_hash": claim.claim_hash(),
            f"{p}prereg_seq": entry.seq}


# ------------------------------------------------------------------ confirm
@register("research.confirm")
def confirm(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    _require_isolation(ctx)
    task = _task(state)
    p = params.get("prefix", "")
    claim = Claim.model_validate(state[f"{p}claim"])
    h = claim.claim_hash()
    kernel = ctx.kernel()
    vault = kernel.vault
    ids = state["datasets"]
    train = vault.read_public(ids["train"], task.train_file)
    test = vault.read_public(ids["test"], task.test_features_file)
    n_rows = len(list(csv.reader(io.StringIO(test.decode("utf-8"))))) - 1
    refs = {"train": InputRef(dataset_id=ids["train"], file=task.train_file,
                              blob=vault.dataset(ids["train"]).files[task.train_file].blob),
            "test": InputRef(dataset_id=ids["test"], file=task.test_features_file,
                             blob=vault.dataset(ids["test"]).files[task.test_features_file].blob)}
    sb = _sb(ctx)
    env = sb.environment()
    evidence: list[str] = []
    failures: list[str] = []
    # Idempotent: on a retry or resume, reuse the records this run already made for this claim, so an
    # infrastructure retry never repeats a held-out evaluation (which K4 would rightly count as unreported).
    done: dict[tuple[str, int], str] = {}
    for ev in ctx.store.evidence_for_run(ctx.run_id):
        try:
            prior = ExecRecord.model_validate(ctx.store.blobs.get_json(ev.payload_ref))
        except ValueError:
            continue
        if prior.claim_hash == h:
            done[(prior.arm, prior.seed)] = ev.id
    for seed in claim.analysis_plan.seeds:
        for arm, ref in (("subject", claim.subject), ("comparator", claim.comparator)):
            if (arm, seed) in done:
                evidence.append(done[(arm, seed)])
                continue
            box = sb.spawn(sb.workspace.parent / f"{sb.workspace.name}-confirm" / f"{p}{arm}-{seed}")
            preds, diag = run_program(box, code=ctx.store.blobs.get(ref.hash), entrypoint="solution.py",
                                      argv=DEFAULT_ARGV, inputs={"train": (task.train_file, train),
                                                                 "test": (task.test_features_file, test)},
                                      seed=seed, timeout_s=task.solution_timeout_s)
            # validate the shape BEFORE touching the labels: an unusable output never causes a label read
            if preds is None or len(preds) != n_rows:
                failures.append(f"{arm} seed {seed}: no valid predictions ({diag[-300:]})")
                continue
            labels = _labels(vault.read(ids["test"], task.test_labels_file, purpose="evaluate", run_id=ctx.run_id,
                                        claim_hash=h, arm=arm, seed=seed))
            rec = ExecRecord(claim_hash=h, arm=arm, artifact_hash=ref.hash, inputs=refs, target=ids["test"],  # type: ignore[arg-type]
                             label_file=task.test_labels_file, seed=seed, env=env,
                             limits={"timeout_s": float(task.solution_timeout_s)},
                             preds_ref=ctx.store.blobs.put_json(preds), metric=task.metric,
                             score=M.score(task.metric, labels, preds), run_id=ctx.run_id)
            evidence.append(kernel.record_evaluation(rec))
    return {f"{p}evidence_ids": evidence, f"{p}confirm_failures": failures}


# ------------------------------------------------------------------ certify
def _dev_scheme_text(state: dict[str, Any]) -> str:
    d = state.get("dev_scheme") or {}
    return (f"{'stratified ' if d.get('stratified') else ''}{d.get('k', '?')}-fold cross-validation "
            f"(split seed {d.get('seed', '?')}, {d.get('n', '?')} examples)")


def _models_used(ctx: ModuleContext) -> list[str]:
    seen: set[str] = set()
    for step in ctx.store.trace(ctx.run_id):
        provs = [step.provenance.model_dump(mode="json")] if step.provenance else []
        out = step.output if isinstance(step.output, dict) else {}
        provs += [c.get("provenance", {}) for c in out.get("_module_llm_calls", []) or []]
        for p in provs:
            if p:
                seen.add(f"{p.get('backend')}/{p.get('model')} ({p.get('mode')})")
    return sorted(seen)


@register("research.certify")
def certify(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    _require_isolation(ctx)
    p = params.get("prefix", "")
    kernel = _kernel(ctx)
    claim = Claim.model_validate(state[f"{p}claim"])
    cert = kernel.certify(claim, state.get(f"{p}evidence_ids") or [])
    h = cert.cert_hash()
    bundle_path = data_dir() / "bundles" / f"{h[:20]}.zip"
    bundle_hash = build_bundle(kernel, h, bundle_path)
    n_hyp = len(_hypotheses(state))
    policy = state.get("model_policy") or {}
    models = [*_models_used(ctx),  # invariant 5 status, never omitted (a missing record is reported as unknown)
              f"model families {policy.get('families', 'unknown')}; invariant 5 (judge/replicator family differs "
              f"from proposer/executor family): {policy.get('invariant_5', 'UNKNOWN (no policy record)')}"]
    checklist = fill(cert, run_costs={ctx.run_id: ctx.store.run_cost(ctx.run_id)}, models=models,
                     bundle_hash=bundle_hash,
                     selection_policy=(f"{n_hyp} model-proposed hypotheses ranked by a K-way tournament; "
                                       f"candidates scored by {_dev_scheme_text(state)} on the public training "
                                       f"data (out-of-fold predictions); the best dev candidate became the single "
                                       f"pre-registered claim of this run")
                     if not p else ("independent replication: a separate model call re-implemented the "
                                    "certified hypothesis from the claim text without seeing its code; the "
                                    "replica was checked on dev, then pre-registered at the same minimum effect"),
                     human_in_the_loop="none (fully automated run)",
                     novelty="not performed: the literature module does not exist yet (GOAL M5)")
    ok, blockers = publishable(cert, checklist)
    recs = [ExecRecord.model_validate(ctx.store.blobs.get_json(ctx.store.get_evidence(e).payload_ref))
            for e in cert.evidence_ids]
    target = claim.generalization_targets[0]
    certified = cert.label.value in ("CERTIFIED-INTERNAL", "CERTIFIED-VALIDATED")
    route = "replicate" if (certified and not p and params.get("replicate", False)) else "report"
    brief = None
    refused = None
    if route == "replicate":  # what the replicator may see: the claim and the idea, never the code
        best = state.get("best") or {}
        root = str(best.get("root_hypothesis") or best.get("hypothesis_id", ""))
        hyps = {h.get("id"): h for h in _hypotheses(state)}
        task = _task(state)
        brief = {"claim": claim.statement, "hypothesis": root,
                 "idea": hyps.get(root, {}).get("idea") or "(idea text unavailable)",
                 "refined": str(best.get("hypothesis_id", "")).startswith("improve-"),
                 # recomputed here from the vault's registered training bytes, never taken from (evolvable) state
                 "profile": _trusted_profile(ctx, task, state["datasets"]["train"])}
        original = ctx.store.blobs.get(best["code_ref"]).decode("utf-8", errors="replace")
        copied = copied_code_lines(original, f"{brief['claim']}\n{brief['idea']}")
        if copied >= 2:
            # the idea text is model-written state an evolved harness can rewrite after the code exists
            refused = f"replication brief contains {copied} line(s) of the original code verbatim"
            brief, route = None, "report"
    return {"route": route,
            f"{p}certificate": {"hash": h, "label": cert.label.value, "tier": cert.tier, "failed": cert.failed(),
                            "bundle_hash": bundle_hash, "bundle_path": str(bundle_path),
                            "effect": cert.resolution.get("targets", {}).get(target, {}).get("estimate"),
                            "subject_mean": _mean([r.score for r in recs if r.arm == "subject"]) if recs else None,
                            "comparator_mean": _mean([r.score for r in recs if r.arm == "comparator"])
                            if recs else None},
            f"{p}checklist": checklist, f"{p}publishable": ok, f"{p}publish_blockers": blockers,
            **({"replication_brief": brief} if brief else {}),
            **({"replication_refused": refused} if refused else {})}


def _trusted_profile(ctx: ModuleContext, task: TaskSpec, train_id: str) -> dict[str, Any]:
    import tempfile

    from .data import profile
    data = ctx.kernel().vault.read_public(train_id, task.train_file)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / task.train_file
        p.write_bytes(data)
        return profile(p, task.target_column)


def copied_code_lines(code: str, text: str, min_len: int = 25) -> int:
    """How many lines of `code` appear in `text`: substantial lines (>= min_len characters after collapsing
    whitespace, comment lines excluded) verbatim, OR statements of `code` with >= BRIEF_MIN_TOKENS normalised tokens
    that occur in `text` as code with renamed local names, other literals, quotes or spacing (see
    _statement_signatures; candidate code in the text = each line, the part after its last ': ', and every
    `backtick` span, dedented). Deep paraphrase (code retold in prose, restructured) is not detected."""
    hay = " ".join(text.split())
    lines = {" ".join(ln.split()) for ln in code.splitlines()}
    verbatim = {ln for ln in lines if len(ln) >= min_len and not ln.startswith("#") and ln in hay}
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return len(verbatim)
    local, aliases = _defined(tree)
    library = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - local - set(aliases)
    original = {s for s in _statement_signatures(code) if len(s) >= BRIEF_MIN_TOKENS}
    found: set[tuple[str, ...]] = set()
    for cand in _code_candidates(text):
        try:
            # a snippet has no context: every name that is not one of the original's library names (or import
            # aliases) is treated as a renamed local
            found |= _statement_signatures(cand, context=(library, aliases))
        except (SyntaxError, ValueError, RecursionError):
            continue
    return max(len(verbatim), len(original & found))


# ------------------------------------------------------------------ rename-invariant code comparison
#: refusal threshold of structural_similarity (replica vs original); also the default `max_similarity`.
MAX_STRUCTURAL_SIMILARITY = 0.8
SHINGLE_K = 8  # token shingle length over the normalised AST token sequence
MIN_SHINGLES, MIN_STATEMENTS = 20, 4  # below this, a side has too little non-boilerplate code to judge
STATEMENT_MIN_TOKENS = 6  # statements shorter than this are too generic to count
BRIEF_MIN_TOKENS = 8


def _code_candidates(text: str) -> list[str]:
    import re
    import textwrap
    cands = [text, textwrap.dedent(text)]
    for ln in text.splitlines():
        cands += [ln.strip(), ln.rsplit(": ", 1)[-1].strip()]
    cands += re.findall(r"`([^`]+)`", text)
    return [c for c in dict.fromkeys(cands) if c]


def _defined(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """(names the code defines itself: variables, functions, classes, parameters, ...; import alias -> real name)."""
    names: set[str] = set()
    aliases: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.alias) and n.asname:
            aliases[n.asname] = n.name
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            names.update(n.names)
    return names - set(aliases), aliases


def _label(n: ast.AST, names: set[str], aliases: dict[str, str], keep: set[str] | None = None) -> str | None:
    """Normalised token for one AST node: local names -> '_', aliases -> the imported name, literals -> their type;
    library names, attributes and keyword names are kept (with `keep`: only those names, all others -> '_').
    None = skip the node."""
    if isinstance(n, ast.expr_context):
        return None
    if isinstance(n, ast.Name):
        if keep is not None:
            return aliases.get(n.id) or (n.id if n.id in keep else "_")
        return aliases.get(n.id) or ("_" if n.id in names else n.id)
    if isinstance(n, ast.Attribute):
        return "." + n.attr
    if isinstance(n, ast.Constant):
        return "C:" + type(n.value).__name__
    if isinstance(n, ast.keyword):
        return "kw:" + (n.arg or "**")
    if isinstance(n, ast.alias):
        return "import:" + n.name
    return type(n).__name__


def _is_docstring(n: ast.AST) -> bool:
    return isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)


def _token_sequence(src: str) -> list[str]:
    tree = ast.parse(src)
    names, aliases = _defined(tree)
    out: list[str] = []

    def visit(n: ast.AST) -> None:
        if _is_docstring(n):
            return
        lab = _label(n, names, aliases)
        if lab is None:
            return
        out.append(lab)
        for c in ast.iter_child_nodes(n):
            visit(c)
    visit(tree)
    return out


_BODY_FIELDS = ("body", "orelse", "finalbody", "handlers")


def _statement_signatures(src: str, context: tuple[set[str], dict[str, str]] | None = None
                          ) -> set[tuple[str, ...]]:
    """Each statement as its normalised token tuple, nested statement bodies excluded (a compound statement's header
    is its own signature), >= STATEMENT_MIN_TOKENS tokens. Insensitive to statement order and inserted statements.
    `context` = (library names to keep, import aliases) of ANOTHER program, for snippets without their own imports."""
    tree = ast.parse(src)
    names, aliases = _defined(tree)
    keep = None
    if context is not None:
        keep, aliases = context[0], {**context[1], **aliases}
    sigs: set[tuple[str, ...]] = set()

    def flat(n: ast.AST, out: list[str]) -> None:
        lab = _label(n, names, aliases, keep)
        if lab is None:
            return
        out.append(lab)
        for f, v in ast.iter_fields(n):
            if f in _BODY_FIELDS and isinstance(v, list) and v and isinstance(v[0], (ast.stmt, ast.excepthandler)):
                continue
            for c in v if isinstance(v, list) else [v]:
                if isinstance(c, ast.AST):
                    flat(c, out)
    for n in ast.walk(tree):
        if isinstance(n, ast.stmt) and not _is_docstring(n):
            toks: list[str] = []
            flat(n, toks)
            if len(toks) >= STATEMENT_MIN_TOKENS:
                sigs.add(tuple(toks))
    return sigs


def _containment(a: set[Any], b: set[Any], minimum: int) -> float | None:
    if len(a) < minimum or len(b) < minimum:
        return None
    shared = len(a & b)
    return max(shared / len(a), shared / len(b))


def structural_similarity(a: str, b: str, boilerplate: str | None = None) -> float:
    """Rename-invariant similarity of two programs in [0, 1]: the larger of (1) the containment of normalised token
    shingles (SHINGLE_K) and (2) the containment of normalised statements, each in either direction (so a copy padded
    with extra code, or a partial copy, still scores high), after removing everything that also occurs in
    `boilerplate` (the task's baseline: the CLI contract and shared I/O). Renaming, aliasing imports, changing
    literals/quotes/formatting/comments/docstrings, reordering top-level statements or inserting statements does
    not lower it. A side with too little non-boilerplate code is not judged (0.0), nor is code that does not parse.
    Not detected (stated residual): deep paraphrase, i.e. the same algorithm restructured (different control flow,
    different library calls)."""
    try:
        ta, tb = _token_sequence(a), _token_sequence(b)
        sa, sb = _statement_signatures(a), _statement_signatures(b)
        tbase = _token_sequence(boilerplate) if boilerplate else []
        sbase = _statement_signatures(boilerplate) if boilerplate else set()
    except (SyntaxError, ValueError, RecursionError):
        return 0.0

    def shingles(t: list[str]) -> set[tuple[str, ...]]:
        return {tuple(t[i:i + SHINGLE_K]) for i in range(len(t) - SHINGLE_K + 1)}
    base_sh = shingles(tbase)
    scores = [_containment(shingles(ta) - base_sh, shingles(tb) - base_sh, MIN_SHINGLES),
              _containment(sa - sbase, sb - sbase, MIN_STATEMENTS)]
    return max((s for s in scores if s is not None), default=0.0)


# ------------------------------------------------------------------ independent replication (GOAL L4.9)
def code_lines(s: str) -> set[str]:
    """Non-trivial logical lines of Python source as token sequences: comments, blank lines, indentation and
    line wrapping do not matter (so padding every line with a comment, or re-wrapping, does not hide a copy).
    Source that does not tokenize falls back to whitespace-normalised physical lines."""
    out: set[str] = set()
    try:
        cur: list[str] = []
        for tok in tokenize.generate_tokens(io.StringIO(s).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
                            tokenize.ENDMARKER):
                continue
            if tok.type == tokenize.NEWLINE:
                line = " ".join(cur)
                if len(line.replace(" ", "")) > 8:
                    out.add(line)
                cur = []
            else:
                cur.append(tok.string)
        if cur and len("".join(cur)) > 8:
            out.add(" ".join(cur))
        return out
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return {" ".join(ln.split()) for ln in s.splitlines() if len(ln.strip()) > 8}


def code_similarity(a: str, b: str) -> float:
    """Jaccard overlap of the sets of non-trivial logical lines (see code_lines). Cheap and conservative: only
    flags near-copies; it says nothing about deeper copying (renamed variables, reordered or paraphrased code)."""
    la, lb = code_lines(a), code_lines(b)
    return len(la & lb) / len(la | lb) if la and lb else 0.0


@register("research.replica_dev")
def replica_dev(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    """Score the replicator's code (written WITHOUT the original code) by the same cross-validation as every
    candidate. It becomes a replication candidate only if it works and beats the baseline on dev; otherwise the
    replication fails honestly."""
    from .modules import _baseline_code, _better, _dev_predict, _score, _ws
    _require_isolation(ctx)
    task = _task(state)
    code = (state.get("replica_impl") or {}).get("code", "")
    original = ctx.store.blobs.get((state.get("best") or {})["code_ref"]).decode("utf-8", errors="replace")
    similarity = code_similarity(code, original)
    # rename-invariant comparison; what the task's baseline shares with both (CLI contract, I/O) is not evidence
    structural = structural_similarity(code, original,
                                       _baseline_code(state, ctx).decode("utf-8", errors="replace"))
    why = None
    if similarity >= params.get("max_similarity", 0.8):
        why = f"{similarity:.0%} of lines shared with the original"
    elif structural >= params.get("max_structural_similarity", MAX_STRUCTURAL_SIMILARITY):
        why = f"structurally {structural:.0%} identical to the original (names, literals and layout ignored)"
    if why is not None:
        # a near-copy of the original code is not an independent replication, whatever produced it
        return {"replica_best": {"hypothesis_id": "replication", "dev_score": None, "dev_preds_ref": None,
                                 "code_ref": ctx.store.blobs.put(code.encode()), "error": f"not independent: {why}"},
                "route": "report"}
    _ws(ctx, "replica.py").write_text(code, encoding="utf-8")
    labels = _dev_labels(state, ctx)
    preds, info = _dev_predict(ctx, state, code.encode("utf-8"), "replica")
    s = _score(task, labels, preds)
    cand = {"hypothesis_id": f"replication-of-{(state.get('best') or {}).get('hypothesis_id')}", "dev_score": s,
            "similarity_to_original": round(similarity, 3), "structural_similarity": round(structural, 3),
            "dev_preds_ref": ctx.store.blobs.put_json(preds) if s is not None else None,
            "code_ref": ctx.store.blobs.put(code.encode()),
            "error": None if s is not None else (info["stderr_tail"][-1500:] or "no valid predictions")}
    ok = s is not None and _better(task, s, state["baseline_dev"])
    return {"replica_best": cand, "route": "replica_claim" if ok else "report"}


# ------------------------------------------------------------------ report (rendered from stored artifacts only)
def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


_CERTIFIED = ("CERTIFIED-INTERNAL", "CERTIFIED-VALIDATED")


def headline(state: dict[str, Any]) -> bool:
    """GOAL L4.9: a headline result = certified AND an independent re-implementation certified at the same bar,
    where independent includes invariant 5 (the replicator is not of the author's model family)."""
    return ((state.get("certificate") or {}).get("label") in _CERTIFIED
            and (state.get("replica_certificate") or {}).get("label") in _CERTIFIED
            and (state.get("model_policy") or {}).get("invariant_5") == "met")


def loop_label(state: dict[str, Any]) -> str:
    """GOAL §6.1: VALIDATED only if the run's accepted claim was certified on external or future data
    (CERTIFIED-VALIDATED); a run that optimised and confirmed on internal data is MECHANICAL."""
    return "VALIDATED" if (state.get("certificate") or {}).get("label") == "CERTIFIED-VALIDATED" else "MECHANICAL"


def render_report(state: dict[str, Any], ctx: ModuleContext) -> str:
    task = _task(state)
    lines = [f"# Research report: {task.title}", "",
             f"Task `{task.id}` · metric `{task.metric}` ({'higher' if task.higher_is_better else 'lower'} is better)",
             "", f"Loop label (GOAL §6.1): **{loop_label(state)}** · invariant 5 (model families): "
                 f"{_inline((state.get('model_policy') or {}).get('invariant_5', 'UNKNOWN (no policy record)'))}",
             ""]
    info = state.get("certificate")
    if info:
        kernel = ctx.kernel()
        cert = kernel.certificate(info["hash"])
        claim = cert.claim
        lines += ["## Result (Evidence Kernel certificate)", "",
                  f"**{cert.label.value}** · tier T{cert.tier} · certificate `{info['hash']}`", "",
                  f"- Claim: {claim['statement']}",
                  f"- Claim hash `{cert.claim_hash}`, pre-registered at ledger entry {cert.prereg_seq}",
                  f"- Verification position: {cert.vg_ladder}"]
        recs = [ExecRecord.model_validate(ctx.store.blobs.get_json(ctx.store.get_evidence(e).payload_ref))
                for e in cert.evidence_ids]
        for arm in ("subject", "comparator"):
            scores = [r.score for r in recs if r.arm == arm]
            lines.append(f"- {arm}: mean {task.metric} {_mean(scores):.6g} over {len(scores)} held-out run(s)")
        for target, t in cert.resolution.get("targets", {}).items():
            lines.append(f"- Held-out effect on `{target}`: {t['estimate']:+.6g}, interval "
                         f"[{t['lo']:+.6g}, {t['hi']:+.6g}] (one-sided level {1 - t['alpha_eff']:.6g}); "
                         f"required minimum effect {claim['effect']['min_effect']:.6g}")
        lines += ["", "### Kernel checks", ""]
        for kid, res in cert.checks.items():
            lines.append(f"- {kid} {res.status}" + (": " + "; ".join(res.reasons) if res.reasons else ""))
        lines += ["", render_checklist(state.get("checklist") or {}),
                  f"Reproduce: `chh reproduce {info['bundle_path']} --docker`", ""]
        rep = state.get("replica_certificate")
        lines += ["## Independent replication", ""]
        if rep:
            rcert = kernel.certificate(rep["hash"])
            lines += [f"**{rcert.label.value}** · certificate `{rep['hash']}`", f"- Claim: {rcert.claim['statement']}"]
            lines += [f"- {kid} {res.status}" + (": " + "; ".join(res.reasons) if res.reasons else "")
                      for kid, res in rcert.checks.items() if res.status == "fail"]
        elif state.get("replication_refused"):
            lines.append(f"Replication refused: {_inline(state['replication_refused'])}.")
        elif state.get("replica_best"):
            why = state["replica_best"].get("error") or "it did not beat the baseline on dev"
            lines.append(f"Replication attempted; no replication claim was made ({_inline(why)}).")
        else:
            lines.append("No independent replication was performed.")
        lines += ["", f"**Headline result** (certified AND independently replicated, replicator of a different "
                      f"model family): {'yes' if headline(state) else 'no'}", ""]
    else:
        pf = state.get("preflight") or {}
        if pf.get("verdict") == "deny":
            lines += ["## Result", "", "No claim was registered: the certifiability preflight (GOAL §16.8, deny-only) "
                      f"estimated P(certify) = {pf.get('p_certify')} < theta {pf.get('theta')} (UNCALIBRATED) from "
                      "public numbers. No held-out label was read.", ""]
        else:
            lines += ["## Result", "", "No claim was made: no candidate beat the baseline on the dev estimate.", ""]
    lines += [f"## Exploration log (dev estimate: {_dev_scheme_text(state)} on public training data; "
              f"NOT evidence)", "",
              f"Baseline dev {task.metric}: {state.get('baseline_dev')}", ""]
    for a in state.get("attempts") or []:
        lines.append(f"- attempt {a['n']} hypothesis={_inline(a['hypothesis_id'], 80)} dev={a['dev_score']} "
                     f"error={'yes' if a['error'] else 'no'}")
    lines += ["", "## Hypotheses considered (model-written text, quoted; not claims)", ""]
    for hyp in _hypotheses(state):
        lines.append(f"> `{_inline(hyp.get('id'), 80)}`: {_inline(hyp.get('idea'))}")
    return "\n".join(lines) + "\n"


@register("research.report")
def report(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    text = render_report(state, ctx)
    return {"report": text, "report_ref": ctx.store.blobs.put(text.encode())}


# ------------------------------------------------------------------ audit
@register("research.audit")
def audit(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    """I1 certificate resolves and the run state agrees with the ledger; I2 comparator integrity (K5);
    I3 citations (`citation_audit`: "n/a" without citations); I4 the report re-renders byte-identically from
    stored artifacts and every held-out score recomputes from its stored predictions; I5 leak/canary scan.
    passed = I1, I2, I4, I5 True and I3 True or "n/a" (anything else fails closed)."""
    task = _task(state)
    res: dict[str, Any] = {"I1": True, "I2": True, "I3": "n/a", "I4": True, "I5": True, "problems": []}
    cites = citation_audit(state, ctx.run_id)
    res["I3"] = cites["I3"]
    res["problems"].extend(cites["problems"])
    if cites["I3"] != "n/a":
        res["citations"] = cites

    def fail(key: str, msg: str) -> None:
        res[key] = False
        res["problems"].append(msg)

    kernel = ctx.kernel()
    ids = state["datasets"]
    for p in ("", "replica_"):
        _audit_certificate(state, p, ctx, kernel, ids, task, fail)
    try:
        rerendered = render_report(state, ctx)
    except (KernelError, KeyError, ValueError) as e:
        fail("I4", f"report cannot be re-rendered from stored artifacts: {e}")
    else:
        if rerendered != state.get("report"):
            fail("I4", "the report differs from its re-render from stored artifacts (edited or not generated "
                       "from the certificate)")
    test = kernel.vault.dataset(ids["test"])
    raw = kernel.vault.read(ids["test"], task.test_labels_file, purpose="leakscan", run_id=ctx.run_id)
    fp = {"hashes": {f.blob: f"{ids['test']}/{name}" for name, f in test.files.items() if f.private},
          "canaries": [], "label_windows": label_windows(_labels(raw)), "contents": [raw]}
    canaries = [c for c in (kernel.vault.canary(ids["test"]), state.get("_private_canary")) if c]
    leak = leak_audit(ctx.store.trace(ctx.run_id), _sb(ctx).workspace, fp, canaries,
                      extra_texts={"report": state.get("report", "")})
    for problem in leak["problems"]:
        fail("I5", problem)
    res["passed"] = (all(res[k] is True for k in ("I1", "I2", "I4", "I5"))
                     and (res["I3"] is True or res["I3"] == "n/a"))
    return {"audit": res}


def citation_audit(state: dict[str, Any], session: str) -> dict[str, Any]:
    """Audit I3 (docs/literature.md): the run's `citations` checked with an OFFLINE client on the run's frozen
    snapshot `literature_snapshot` (an id in $CHH_DATA_DIR/literature) against the run's RetrievalLog (session = run
    id), I3 = i3_verdict(...)["I3"]. No citations -> "n/a". Anything that prevents the check (no or unknown snapshot,
    malformed citations, a retrieval-log integrity error) -> I3 False (fail closed). Both keys are reserved state
    (evolve/edits.py), so only trusted nodes can set them."""
    raw = state.get("citations")
    if raw is None or raw == []:
        return {"I3": "n/a", "problems": []}
    from pydantic import ValidationError

    from ..literature.cite import Citation, check_citations, i3_verdict
    from ..literature.client import offline_client
    from ..literature.http import LiteratureError
    from ..literature.retrieval import RetrievalLog
    from ..literature.snapshot import SnapshotStore
    snap = state.get("literature_snapshot")
    out: dict[str, Any] = {"I3": False, "snapshot": snap if isinstance(snap, str) else None, "session": session}
    try:
        if not isinstance(raw, list):
            raise ValueError(f"citations must be a list, got {type(raw).__name__}")
        cites = [Citation.model_validate(c) for c in raw]
        if not isinstance(snap, str) or not snap:
            raise ValueError("no frozen literature snapshot (literature_snapshot) for the cited papers")
        lit = SnapshotStore(data_dir() / "literature")
        client = offline_client(lit, snap)
        retrieved = RetrievalLog(lit, session).retrieved_ids()
        verdict = i3_verdict(check_citations(cites, client, retrieved))
    except (ValueError, ValidationError, LiteratureError, OSError) as e:
        return out | {"problems": [f"I3: citations cannot be checked (fail closed): {e}"]}
    return out | {"I3": verdict["I3"], "problems": verdict["problems"], "counts": verdict["counts"],
                  "results": verdict["results"], "as_of": client.as_of.isoformat() if client.as_of else None}


def _audit_certificate(state: dict[str, Any], p: str, ctx: ModuleContext, kernel: Kernel, ids: dict[str, str],
                       task: TaskSpec, fail: Any) -> None:
    """I1/I2/I4 for one certificate in the run state (prefix "" = the claim, "replica_" = its replication)."""
    info = state.get(f"{p}certificate")
    if info:
        try:
            cert = kernel.certificate(info["hash"])
        except KernelError as e:
            fail("I1", f"certificate does not resolve: {e}")
        else:
            recs = [ExecRecord.model_validate(ctx.store.blobs.get_json(ctx.store.get_evidence(e).payload_ref))
                    for e in cert.evidence_ids]
            means = {arm: _mean([r.score for r in recs if r.arm == arm]) if recs else None
                     for arm in ("subject", "comparator")}
            if (cert.label.value != info.get("label") or cert.claim_hash != state.get(f"{p}claim_hash")
                    or info.get("subject_mean") != means["subject"]
                    or info.get("comparator_mean") != means["comparator"]):
                fail("I1", "run state disagrees with the ledger certificate or its evidence (label, claim or means)")
            if cert.checks["K5"].status != "pass":
                fail("I2", "comparator integrity (K5) failed: " + "; ".join(cert.checks["K5"].reasons))
            labels = _labels(kernel.vault.read(ids["test"], task.test_labels_file, purpose="kernel",
                                               run_id=ctx.run_id, claim_hash=cert.claim_hash))
            for eid in cert.evidence_ids:
                rec = ExecRecord.model_validate(ctx.store.blobs.get_json(ctx.store.get_evidence(eid).payload_ref))
                again = M.score(rec.metric, labels, ctx.store.blobs.get_json(rec.preds_ref))
                if abs(again - rec.score) > 1e-9:
                    fail("I4", f"{eid}: reported {rec.score!r}, recomputed {again!r}")
    elif state.get(f"{p}claim_hash"):
        fail("I1", f"a {p or 'main '}claim was pre-registered but no certificate was issued")
