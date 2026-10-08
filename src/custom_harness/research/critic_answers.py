"""Socratic critic, second half (GOAL L4.8, AHOIS): every answer to a critic question must cite evidence, checked in
code. The first half (the critic may only ask questions) is research/socratic.py + `research.socratic_filter`.

Flow in research_v1 / research_v2 (examples/*.yaml):

  socratic (kept questions) -> critic_evidence (module: question ids + citable evidence catalog)
    -> defend (LLM, the AUTHOR role `proposer`: answers citing catalog ids) -> check_answers (module: this check)

The check is deterministic code and fails closed. It reads the ENGINE's trace, never run state: the kept questions
from the latest `research.socratic_filter` step, the catalog from the `research.critic_evidence` step after it, and
the answers from the latest step of the answering LLM node after that (its validated output value and the prompt it
was shown). Per answer:
  - malformed (not exactly {question, answer, evidence}; wrong types; empty or over-long text; too many ids; an
    unknown question id) -> rejected (`malformed`, invariant 7);
  - no evidence id -> rejected (`uncited`);
  - any cited id that was not rendered to the answering agent (absent from its shown prompt, or a step id absent
    from the code-built catalog, or a handle not archived in that prompt) -> rejected (`not_visible`);
  - any cited id that does not resolve in this run's evidence store, or is ambiguous -> rejected (`unresolvable`);
  - any cited step that is model output or failed -> rejected (`not_evidence`);
  - any cited step/blob/evidence that is private, hidden or target-derived -> rejected (`hidden`, `private`,
    `target_derived`, `provenance_unknown`; invariants 2 and 18). Kernel Evidence rows (`ev:`) are produced only from
    held-out executions and certificates (kernel/kernel.py), so citing one is always `target_derived`.
One bad citation rejects the whole answer. A question is `cited` only if it has exactly one answer and that answer
passed; no answer is `missing`, two or more are `duplicate`.

`cited` means "the cited ids exist, are visible to the author and are not hidden". It does NOT mean the answer is
true or the evidence supports it: no code here judges meaning, and no model is asked. The model's output can never set
a status (unknown keys make an answer malformed, and statuses are computed here).

Consequence (deny-only; this check can never grant anything): if any kept critic question is not `cited`, the run's
claim is NOT publishable: `research.certify` recomputes this check from the trace (`critic_gate`) and adds a publish
blocker. The certificate, its label, the route and the hypothesis queue are untouched. A run whose critic kept no
question, or produced none, is not affected. A run whose kept questions were never checked (no check step after the
socratic step, or the answering step failed) is blocked (fail closed).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..records import StepKind, TraceStep
from ..runtime import register
from ..runtime.modules import ModuleContext
from . import evidence_refs as R

SOCRATIC_MODULE = "research.socratic_filter"
CATALOG_MODULE = "research.critic_evidence"
CHECK_MODULE = "research.check_critic_answers"
DEFAULT_ANSWERER = "defend"
MAX_ANSWERS = 20  # answers per payload (a longer list is malformed as a whole)
MAX_CITATIONS = 8  # evidence ids per answer
MAX_ANSWER_CHARS = 2000
MAX_CATALOG = 40  # most recent citable steps listed for the author
CITED = "cited"
MISSING = "missing"
DUPLICATE = "duplicate"
_QID = re.compile(r"^q\d{1,3}$")
_ANSWER_KEYS = frozenset({"question", "answer", "evidence"})


def _latest(trace: Sequence[TraceStep], pred: Any, *, after: int = -1, before: int | None = None) -> TraceStep | None:
    hits = [s for s in trace if s.index > after and (before is None or s.index < before) and pred(s)]
    return hits[-1] if hits else None


def _socratic(trace: Sequence[TraceStep], before: int | None = None) -> TraceStep | None:
    return _latest(trace, lambda s: s.kind == StepKind.MODULE and R.step_module(s) == SOCRATIC_MODULE
                   and s.error is None, before=before)


def kept_questions(step: TraceStep | None) -> list[dict[str, str]]:
    """The questions the socratic filter kept in that step, with positional ids q1, q2, ..."""
    if step is None:
        return []
    qs = (step.output.get("critique") or {}).get("questions") if isinstance(step.output.get("critique"), dict) else []
    return [{"id": f"q{i + 1}", "text": str(q)} for i, q in enumerate(qs or [])]


def _visible(core: str, prompt: str) -> bool:
    """`core` appears in the shown prompt as a whole token (so step:3 is not seen inside step:31)."""
    return re.search(r"(?<![0-9A-Za-z_:])" + re.escape(core) + r"(?![0-9A-Za-z_])", prompt) is not None


@register(CATALOG_MODULE)
def critic_evidence(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    """Question ids and the citable evidence catalog for the answering agent, built from the trace by code."""
    trace = ctx.store.trace(ctx.run_id)
    here = (trace[-1].index + 1) if trace else 0
    questions = kept_questions(_socratic(trace))
    reads = R.private_read_windows(ctx.store)
    run_reads = R.run_private_reads(ctx.store, trace, ctx.run_id)
    catalog = []
    for st in trace:
        if R.vet_step(st, before_index=here, private_reads=reads, run_reads=run_reads).ok:
            wrote = sorted(k for k in st.output if not str(k).startswith("_"))
            catalog.append({"id": f"step:{st.index}", "node": st.node_id, "wrote": wrote})
    limit = max(1, min(int(params.get("max_catalog", MAX_CATALOG)), MAX_CATALOG))
    return {"critic_evidence": {"questions": questions, "catalog": catalog[-limit:]}}


def _vet_citation(token: Any, *, answer: TraceStep, prompt: str, catalog_ids: set[str],
                  by_index: Mapping[int, TraceStep], reads: Sequence[float], run_reads: Sequence[float],
                  screen: R.PrivateScreen, ctx: ModuleContext) -> str | None:
    """None if the citation is acceptable, else its reason code."""
    if not isinstance(token, str):
        return R.MALFORMED
    m = R.STEP_RE.fullmatch(token)
    if m:
        if token not in catalog_ids or not _visible(token, prompt):
            return R.NOT_VISIBLE
        v = R.vet_step(by_index.get(int(m.group(1))), before_index=answer.index, private_reads=reads,
                       run_reads=run_reads)
        return None if v.ok else v.code
    m = R.BLOB_RE.fullmatch(token)
    if m:
        handle = m.group(1)
        if not _visible(handle, prompt):
            return R.NOT_VISIBLE
        r = R.resolve_handle(handle, answer.output.get("archived") or [])
        if not r.ok or r.digest is None:
            return r.code
        v, _text = R.vet_blob(r.digest, ctx.store, screen, ctx.archive_taint,
                              after_run_read=any(t <= answer.started_at + 1e-6 for t in run_reads))
        return None if v.ok else v.code
    m = R.EV_RE.fullmatch(token)
    if m:
        if not _visible(m.group(1), prompt):
            return R.NOT_VISIBLE
        try:
            ctx.store.get_evidence(m.group(1))
        except KeyError:
            return R.UNRESOLVABLE
        return R.TARGET_DERIVED  # Kernel evidence comes from held-out runs / certificates only
    return R.MALFORMED


def evaluate(ctx: ModuleContext, *, answerer: str = DEFAULT_ANSWERER, before: int | None = None) -> dict[str, Any]:
    """The check, recomputed from the trace of ctx.run_id (steps before `before`). Returns a record:
    {status: pass|fail|none, questions: [{id, status, codes}], stray: n, answer_step: i|None}."""
    trace = [s for s in ctx.store.trace(ctx.run_id) if before is None or s.index < before]
    soc = _socratic(trace)
    questions = kept_questions(soc)
    if not questions:
        return {"status": "none", "questions": [], "stray": 0, "answer_step": None}
    if soc is None:  # unreachable (questions come from soc); explicit for the type checker, never an assert
        return {"status": "fail", "questions": [], "stray": 0, "answer_step": None}
    cat = _latest(trace, lambda s: s.kind == StepKind.MODULE and R.step_module(s) == CATALOG_MODULE
                  and s.error is None, after=soc.index)
    ans = None if cat is None else _latest(trace, lambda s: s.kind == StepKind.LLM and s.node_id == answerer,
                                           after=cat.index)
    results: dict[str, list[str]] = {q["id"]: [] for q in questions}
    counts: dict[str, int] = {q["id"]: 0 for q in questions}
    stray = 0

    def finish(default: str | None = None) -> dict[str, Any]:
        out = []
        for q in questions:
            qid = q["id"]
            if default is not None:
                out.append({"id": qid, "status": default, "codes": [default]})
            elif counts[qid] == 0:
                out.append({"id": qid, "status": MISSING, "codes": [MISSING]})
            elif counts[qid] > 1:
                out.append({"id": qid, "status": DUPLICATE, "codes": sorted(set(results[qid])) or [DUPLICATE]})
            elif results[qid]:
                out.append({"id": qid, "status": "rejected", "codes": sorted(set(results[qid]))})
            else:
                out.append({"id": qid, "status": CITED, "codes": []})
        ok = all(r["status"] == CITED for r in out)
        return {"status": "pass" if ok else "fail", "questions": out, "stray": stray,
                "answer_step": None if ans is None else ans.index}

    if ans is None or ans.error is not None:
        return finish(MISSING)
    value = ans.output.get("value")
    answers = value.get("answers") if isinstance(value, dict) and set(value) == {"answers"} else None
    if not isinstance(answers, list) or len(answers) > MAX_ANSWERS:
        return finish(R.MALFORMED)
    prompt = R.rendered_prompt(ans)
    cat_out = cat.output.get("critic_evidence") if cat is not None else None
    catalog_ids = {str(e.get("id")) for e in (cat_out or {}).get("catalog") or [] if isinstance(e, dict)}
    by_index = {s.index: s for s in trace}
    reads = R.private_read_windows(ctx.store)
    run_reads = R.run_private_reads(ctx.store, trace, ctx.run_id)
    screen = R.PrivateScreen.build(ctx.kernel().vault, ctx.secrets)
    for a in answers:
        qid = a.get("question") if isinstance(a, dict) else None
        if not (isinstance(qid, str) and _QID.fullmatch(qid) and qid in counts):
            stray += 1  # not attributable to a kept question: counts as no answer for any question
            continue
        counts[qid] += 1
        text, ev = a.get("answer"), a.get("evidence")
        if (set(a) != _ANSWER_KEYS or not isinstance(text, str) or not text.strip()
                or len(text) > MAX_ANSWER_CHARS or not isinstance(ev, list) or len(ev) > MAX_CITATIONS):
            results[qid].append(R.MALFORMED)
            continue
        if not ev:
            results[qid].append(R.UNCITED)
            continue
        for token in ev:
            code = _vet_citation(token, answer=ans, prompt=prompt, catalog_ids=catalog_ids, by_index=by_index,
                                 reads=reads, run_reads=run_reads, screen=screen, ctx=ctx)
            if code is not None:
                results[qid].append(code)
    return finish()


@register(CHECK_MODULE)
def check_critic_answers(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    """Record the check's outcome in state (informational: `research.certify` recomputes it from the trace)."""
    return {"critic_answers": evaluate(ctx, answerer=str(params.get("answerer", DEFAULT_ANSWERER)))}


def critic_gate(ctx: ModuleContext) -> tuple[bool, str]:
    """(True, "") when every critic round of the run had all its kept questions answered with checked citations, or no
    round kept a question; otherwise (False, a publish-blocker reason). Recomputed from the trace; run state is never
    consulted. Each `research.socratic_filter` step is a round, checked by the latest check step before the next round
    (a later empty round cannot erase an earlier round's open questions). A filter step that failed, or that ran with
    `max_questions` < 1 (the critic switched off by a parameter edit), blocks (fail closed)."""
    trace = ctx.store.trace(ctx.run_id)
    rounds = [s for s in trace if s.kind == StepKind.MODULE and R.step_module(s) == SOCRATIC_MODULE]
    problems: list[str] = []
    for i, soc in enumerate(rounds):
        mq = R.step_params(soc).get("max_questions", 5)
        if soc.error is not None or isinstance(mq, bool) or not isinstance(mq, int) or mq < 1:
            problems.append(f"round {i + 1}: the critic filter failed or was switched off")
            continue
        questions = kept_questions(soc)
        if not questions:
            continue
        nxt = rounds[i + 1].index if i + 1 < len(rounds) else None
        chk = _latest(trace, lambda s: s.kind == StepKind.MODULE and R.step_module(s) == CHECK_MODULE
                      and s.error is None, after=soc.index, before=nxt)
        if chk is None:
            problems.append(f"round {i + 1}: {len(questions)} kept question(s) were never checked for answers "
                            f"citing evidence")
            continue
        rec = evaluate(ctx, answerer=str(R.step_params(chk).get("answerer", DEFAULT_ANSWERER)), before=chk.index)
        open_q = [q for q in rec["questions"] if q["status"] != CITED]
        if rec["status"] != "pass" or open_q or not rec["questions"]:
            codes = sorted({c for q in open_q for c in q["codes"]})
            problems.append(f"round {i + 1}: {len(open_q)} of {len(rec['questions'])} question(s) without an answer "
                            f"citing checked evidence ({', '.join(codes)})")
    if not problems:
        return True, ""
    return False, "critic (GOAL L4.8): " + "; ".join(problems)


__all__ = ["CATALOG_MODULE", "CHECK_MODULE", "CITED", "check_critic_answers", "critic_evidence", "critic_gate",
           "evaluate", "kept_questions"]
