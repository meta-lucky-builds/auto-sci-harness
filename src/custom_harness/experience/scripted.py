"""A scripted stand-in for `Experience`, so the screens can be seen and tested with no agent, no Docker and no store.

Every answer is a real typed view from `schemas.py`, so a screen cannot tell it apart, and that is the danger: `meta`
says `scripted`, the UI shows that on every page, and nothing here touches a store, the Kernel or the Court. Verdicts
and badges below are examples of what the views look like. None of them was decided by anything.
"""

from __future__ import annotations

import time
from typing import Any

from . import schemas as M
from . import words as W
from .api import Refused

NOTICE = "Scripted example. Nothing on this page came from the Court, and no agent ran."
SID = "s-label-smoothing"
PID = "f-smoothing-01"
ME = "you (scripted)"
CYCLE_S = 1.4  # one more Story line each this many seconds
HOLD_LINES = 16  # the finished session rests this many steps before the example starts again
# What a live session looks like, line by line. A run with no result is one that has started. The Lab screen builds its
# tree, pulses and counters from these lines, as it does from a real Story. Nothing here ran.
LOG = (("system", "session", "start"),
       ("you", "human", "Try label smoothing against plain cross entropy on the tabular task."),
       ("agent", "idea", "propose: Plain cross entropy"),
       ("agent", "action", "read data/README.md"),
       ("agent", "run", "python train.py --baseline"),
       ("agent", "run", "python train.py --baseline  accuracy 0.8011 (dev)"),
       ("agent", "idea", "propose: Label smoothing"),
       ("agent", "run", "python train.py --smooth 0.1"),
       ("agent", "run", "python train.py --smooth 0.1  accuracy 0.8312 (dev)"),
       ("agent", "web", "connection: export.arxiv.org"),
       ("agent", "idea", "branch: Smoothing at 0.05"),
       ("agent", "run", "python train.py --smooth 0.05"),
       ("agent", "run", "python train.py --smooth 0.05  accuracy 0.8347 (dev)"),
       ("agent", "idea", "branch: Smoothing with class weights"),
       ("agent", "run", "python train.py --smooth 0.05 --weights"),
       ("agent", "run", "python train.py --smooth 0.05 --weights  accuracy 0.8290 (dev)"),
       ("agent", "idea", "abandon: Smoothing with class weights (no better than smoothing alone)"),
       ("agent", "idea", "revisit: Smoothing at 0.05"),
       ("agent", "idea", "branch: Smoothing with a cosine schedule"),
       ("agent", "run", "python train.py --smooth 0.05 --cosine"),
       ("agent", "run", "python train.py --smooth 0.05 --cosine  accuracy 0.8391 (dev)"),
       ("you", "human", "Keep the baseline fixed. Try mixup before going further."),
       ("agent", "idea", "propose: Mixup on tabular rows"),
       ("agent", "run", "python train.py --mixup 0.2"),
       ("agent", "run", "python train.py --mixup 0.2  accuracy 0.7904 (dev)"),
       ("agent", "idea", "branch: Stronger mixup"),
       ("agent", "run", "python train.py --mixup 0.4"),
       ("agent", "run", "python train.py --mixup 0.4  accuracy 0.7851 (dev)"),
       ("agent", "idea", "abandon: Stronger mixup (worse as the mixing grows)"),
       ("agent", "idea", "abandon: Mixup on tabular rows (worse than the baseline on every seed)"),
       ("agent", "web", "connection: api.semanticscholar.org"),
       ("agent", "idea", "propose: Feature dropout"),
       ("agent", "run", "python train.py --feature-dropout 0.1"),
       ("agent", "run", "python train.py --feature-dropout 0.1  accuracy 0.8122 (dev)"),
       ("agent", "idea", "branch: Dropout by column group"),
       ("agent", "run", "python train.py --group-dropout 0.1"),
       ("agent", "run", "python train.py --group-dropout 0.1  accuracy 0.8207 (dev)"),
       ("agent", "idea", "branch: Dropout on rare columns only"),
       ("agent", "run", "python train.py --group-dropout 0.1 --rare"),
       ("agent", "run", "python train.py --group-dropout 0.1 --rare  accuracy 0.8034 (dev)"),
       ("agent", "idea", "abandon: Dropout on rare columns only (loses what grouping gained)"),
       ("agent", "action", "write method.py"),
       ("agent", "run", "pytest tests/test_method.py"),
       ("agent", "run", "pytest tests/test_method.py  passed 12 (dev)"),
       ("agent", "action", "write claim.json"))
_MEANING = W.BADGES


def _badge(name: str, form: str | None = None) -> M.Badge:
    return M.Badge(**W.badge_text(form or name))


class ScriptedExperience:
    """The methods the server calls, with the signatures of `Experience`. Opening a case is held in memory only."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.opened: set[str] = set()
        self.facts: dict[str, float | int | None] = {}
        self.steers: list[tuple[int, str]] = []  # the step each steer arrived at, and its words

    def _n(self, name: str, value: float | int | None) -> M.Number:
        link = f"chh://scripted/example/{name}"
        self.facts[link] = value
        return M.Number(value=value, link=link)

    def _case_facts(self) -> dict[str, dict[str, Any]]:
        v = "Certified. On data generated by the tabular-rings family, the method beats the baseline on accuracy by " \
            "at least 0.02; generator-scoped."
        return {
            "c-smoothing": {"title": "Label smoothing beats plain cross entropy",
                            "sentence": "Label smoothing beats plain cross entropy by at least 0.02 accuracy.",
                            "verdict": "CERTIFIED", "verdict_sentence": v, "reason_code": None, "looks": 0,
                            "badges": (_badge("REPRODUCIBLE"), _badge("VALIDATED"), _badge("RE-VALIDATED")),
                            "second": W.SECOND_TEST["passed"], "rerun": None},
            "c-cosine": {"title": "A cosine schedule beats a step schedule",
                         "sentence": "A cosine schedule beats a step schedule by at least 0.01 accuracy.",
                         "verdict": "NOT CERTIFIED", "reason_code": "EFFECT_NOT_SHOWN", "looks": 0, "badges": (),
                         "verdict_sentence": f"{W.VERDICTS['NOT CERTIFIED']} {W.REASONS['EFFECT_NOT_SHOWN']}",
                         "second": None, "rerun": None},
            "c-public": {"title": "Dropout helps on a public benchmark",
                         "sentence": "Dropout beats no dropout by at least 0.01 accuracy on a public dataset.",
                         "verdict": "CERTIFIED", "reason_code": None, "looks": 0,
                         "verdict_sentence": f"{W.VERDICTS['CERTIFIED']} On public data, the method beats the baseline "
                                             f"on accuracy by at least 0.01. {W.RETROSPECTIVE}",
                         "badges": (_badge("REPRODUCIBLE"),), "second": None, "rerun": None},
            "c-crash": {"title": "A wider network beats a deeper one",
                        "sentence": "A wider network beats a deeper one by at least 0.02 accuracy.",
                        "verdict": "COULD NOT RUN", "reason_code": None, "looks": 1, "badges": (),
                        "verdict_sentence": W.VERDICTS["COULD NOT RUN"], "second": None, "rerun": None,
                        "reason": f"The method stopped with an error before reading any answer. {W.RETRY}"},
            "c-weights": {"title": "Class weights help the rare classes",
                          "sentence": "Class weights beat no weights by at least 0.03 on macro accuracy.",
                          "verdict": "INCONCLUSIVE", "reason_code": "TOO_CLOSE_TO_CALL", "looks": 1, "badges": (),
                          "verdict_sentence": W.VERDICTS["INCONCLUSIVE"], "second": None, "rerun": None}}

    # the verbs ---------------------------------------------------------
    def meta(self) -> M.Meta:
        return M.Meta(scripted=True, notice=NOTICE, researcher=ME)

    def whoami(self) -> M.Identity:
        return M.Identity(os_user="scripted", researcher=ME, chosen=True)

    def set_researcher(self, name: str) -> M.Identity:
        raise Refused("Scripted", "A scripted example has no name to change.")

    def waiting(self) -> M.Waiting:
        items = (M.WaitingItem(id="role-1", title=W.WAITING["role"][0], why=W.WAITING["role"][1], human_only=True,
                               command="chh approve-role generated-tabular-rings-7 --approver NAME"),
                 M.WaitingItem(id="health", title=W.WAITING["health"][0], why=W.WAITING["health"][1], human_only=False,
                               command="chh court health --docker"),
                 M.WaitingItem(id="open-c-smoothing", title=W.WAITING["outcome_recorded"][0],
                               why=W.WAITING["outcome_recorded"][1], human_only=False,
                               command="chh exp case c-smoothing"))
        return M.Waiting(items=items, total=self._n("waiting", len(items)))

    def approve(self, item_id: str) -> M.ApprovalRoute:
        for it in self.waiting().items:
            if it.id == item_id:
                return M.ApprovalRoute(id=it.id, title=it.title, human_only=it.human_only, command=it.command,
                                       note=W.HUMAN_ONLY, binds={})
        raise Refused("NotWaiting", f"nothing is waiting under {item_id!r}")

    def cases(self) -> M.Cases:
        rows = tuple(M.CaseLine(case_id=c, title=f["title"], state=f["verdict"] if c in self.opened
                                else W.STEPS["outcome_recorded"], looks_remaining=self._n(f"looks-{c}", f["looks"]))
                     for c, f in self._case_facts().items())
        return M.Cases(cases=rows, total=self._n("cases", len(rows)))

    def case(self, case_id: str) -> M.Case:
        f = self._case_facts().get(case_id)
        if f is None:
            raise Refused("NoCase", f"no case {case_id!r}")
        self.opened.add(case_id)
        story = M.Story(session_id=SID, idea_path=("Label smoothing",), steps=self._n("steps", len(LOG)),
                        steers=self._n("steers", 1), web_connections=self._n("web", 1), intact=True)
        return M.Case(case_id=case_id, title=f["title"], sentence=f["sentence"], scope=f["sentence"],
                      verdict=f["verdict"], verdict_sentence=f["verdict_sentence"], reason_code=f["reason_code"],
                      reason=f.get("reason"), badges=f["badges"], second_test=f["second"],
                      checks_in_full=tuple(M.CheckRow(name=W.CHECK_PLAIN[k], status="pass")
                                           for k in ("K0", "K1", "K5")),
                      looks_remaining=self._n(f"looks-{case_id}", f["looks"]),
                      test_rows=self._n(f"rows-{case_id}", 2000), retired=False, next_step=None, command=None,
                      rerun=f["rerun"], story=story, technical=f"chh://case/{case_id}/technical")

    def technical(self, case_id: str) -> M.TechnicalView:
        self.case(case_id)
        return M.TechnicalView(case_id=case_id, details={"note": NOTICE})

    def sessions(self) -> M.Sessions:
        line = M.SessionLine(session_id=SID, project="tabular-rings", agent="scripted agent", box="scripted")
        return M.Sessions(sessions=(line,), total=self._n("sessions", 1))

    def _shown(self) -> int:
        """The Lab is alive: one more line every few seconds, a rest at the end, then the example runs again."""
        step = int((time.monotonic() - self.started) / CYCLE_S) % (len(LOG) - 3 + HOLD_LINES)
        return min(len(LOG), 3 + step)

    def _spending(self, runs: int) -> str:
        return f"Recorded spend ${0.06 * runs:.2f} of the $5.00 limit for this session, plus 1 call(s) of unknown cost."

    def lab(self, session_id: str) -> M.LabView:
        self._known(session_id)
        shown = LOG[:self._shown()]
        status: dict[str, str] = {}
        for _, kind, text in shown:
            if kind == "idea":
                op, _, rest = text.partition(": ")
                status[rest.split(" (")[0]] = {"abandon": "abandoned"}.get(op, "open")
        runs = sum(x[1] == "run" and "  " in x[2] for x in shown)
        return M.LabView(session_id=session_id, project="tabular-rings", notice=W.LAB_NOTICE,
                         ideas=tuple(M.Idea(idea_id=t.casefold().replace(" ", "-"), title=t, status=s)
                                     for t, s in status.items()),
                         open_ideas=self._n("open", sum(s == "open" for s in status.values())),
                         dropped_ideas=self._n("dropped", sum(s == "abandoned" for s in status.values())),
                         runs=self._n("runs", runs),
                         steers=self._n("steers", sum(x[1] == "human" for x in shown) + len(self.steers)),
                         web_connections=self._n("web", sum(x[1] == "web" for x in shown)),
                         cost_usd=self._n("cost", round(0.06 * runs, 2)), costs_unknown=self._n("cost-unknown", 1),
                         spending=self._spending(runs))

    def lab_log(self, session_id: str) -> M.LabLog:
        self._known(session_id)
        now = self._shown()
        at = [f"10:{i // 2:02d}:{(i % 2) * 30:02d}" for i in range(len(LOG))]
        rows = [(i, who, kind, text) for i, (who, kind, text) in enumerate(LOG[:now])]
        rows += [(min(i, now) - 1, "you", "human", t) for i, t in self.steers]  # a steer stays through a rerun
        rows.sort(key=lambda r: r[0])
        lines = [M.LabLine(at=at[i], who=who, kind=kind, text=text)  # type: ignore[arg-type]
                 for i, who, kind, text in rows]
        seen = sum(x[1] == "web" for x in LOG[:now])
        return M.LabLog(session_id=session_id, lines=tuple(lines),
                        web=("export.arxiv.org", "api.semanticscholar.org")[:seen],
                        reading=(M.Reading(source="arxiv", query="label smoothing tabular", found=self._n("found", 14)),
                                 M.Reading(source="semantic scholar", query="mixup on tabular data",
                                           found=self._n("found-2", 9))))

    def _known(self, session_id: str) -> None:
        if session_id != SID:
            raise Refused("LabError", f"no session {session_id!r}")

    def steer(self, req: M.SteerIn) -> M.Steered:
        self._known(req.session_id)
        self.steers.append((self._shown(), req.text))
        return M.Steered(session_id=req.session_id, event_id=f"scripted-{len(self.steers)}", sent=False, reply=None,
                         court_results_seen=self._n("seen", len(self.opened)), note="Recorded in the Story.",
                         spending=self._spending(0))

    def explore(self, req: M.ExploreIn) -> M.Explored:
        self._known(req.session_id)
        return M.Explored(session_id=req.session_id, finished=True, reply="(a scripted reply: no agent ran)",
                          actions=self._n("actions", len(LOG)), notice=W.LAB_NOTICE, spending=self._spending(0))

    def frozen(self) -> M.FrozenIdeas:
        items = (M.FrozenIdea(promotion_id=PID, session_id=SID, title="Label smoothing beats plain cross entropy",
                              sentence="Label smoothing beats plain cross entropy by at least 0.02 accuracy."),
                 M.FrozenIdea(promotion_id="f-group-dropout-02", session_id=SID, title="Dropout by column group",
                              sentence="Dropout by column group beats no dropout by at least 0.015 accuracy."))
        return M.FrozenIdeas(items=items, total=self._n("frozen", len(items)))

    def _readiness(self) -> M.Readiness:
        rows = tuple(M.ReadinessRow(test_rows=self._n(f"size-{n}", n), chance=self._n(f"chance-{n}", p), objection=o)
                     for n, p, o in ((250, 0.22, True), (500, 0.41, True), (1000, 0.63, False), (2000, 0.78, False),
                                     (5000, 0.93, False)))
        return M.Readiness(sentence="No objection at 2000 test rows.", notice=W.READINESS_NOTICE, rows=rows)

    def rehearse(self, req: M.RehearseIn) -> M.Rehearsal:
        scores = tuple(M.ScoreRow(arm=arm, seed=str(s), score=self._n(f"{arm}-{s}", v))  # type: ignore[arg-type]
                       for arm, vals in (("method", (0.838, 0.834, 0.841, 0.829, 0.846, 0.836, 0.833)),
                                         ("baseline", (0.802, 0.799, 0.806, 0.811, 0.795, 0.804, 0.801)))
                       for s, v in enumerate(vals))
        return M.Rehearsal(promotion_id=req.promotion_id, notice=W.REHEARSAL_NOTICE, ran=True, would_certify=True,
                           reason=None, scores=scores, readiness=self._readiness(),
                           checks=tuple(M.CheckRow(name=t, status="pass") for t in W.CHECK_PLAIN.values()))

    def rehearsal_details(self, promotion_id: str) -> M.TechnicalView:
        return M.TechnicalView(case_id=promotion_id, details={"note": NOTICE})

    def promote_preview(self, req: M.PromoteIn) -> M.PromotePreview:
        return M.PromotePreview(promotion_id=req.promotion_id, route=req.route, finding_possible=req.route != "public",
                                readiness=self._readiness(), looks_remaining=self._n("looks-new", 1),
                                what_happens=W.PROMOTE_STEPS[req.route], no_data_prior=self._n("prior", 0.22),
                                prior_note=W.PRIOR_NOTE)

    def promote(self, req: M.PromoteIn) -> M.Promoted:
        return M.Promoted(promotion_id=req.promotion_id, case_id="c-smoothing", second_case_id=None, route=req.route,
                          next_step=W.STEPS["outcome_recorded"], command="chh exp case c-smoothing")

    def share(self, req: M.ShareIn) -> M.Shared:
        raise Refused("Scripted", "A scripted example writes no files.")

    def paper(self, req: M.PaperIn) -> M.Paper:
        raise Refused("DisplayRefused", "The paper opens with the whole record, once its test data is retired by a "
                                        "person. Scripted example: nothing was written.")

    def record(self, query: str | None = None) -> M.Record:
        facts = self._case_facts()
        outcomes = tuple(M.Outcome(case_id=c, title=facts[c]["title"], verdict=facts[c]["verdict"],
                                   badges="+".join(b.name for b in facts[c]["badges"]) or "NONE",
                                   reason_code=facts[c]["reason_code"]) for c in sorted(self.opened))
        ends = [M.DeadEnd(title="Mixup on tabular rows", reason="worse than the baseline on every seed", source="idea"),
                M.DeadEnd(title="Stronger mixup", reason="worse as the mixing grows", source="idea"),
                M.DeadEnd(title="Smoothing with class weights", reason="no better than smoothing alone", source="idea"),
                M.DeadEnd(title="Dropout on rare columns only", reason="loses what grouping gained", source="idea")]
        ends += [M.DeadEnd(title=o.title, reason=W.REASONS.get(str(o.reason_code), o.verdict), source="verdict")
                 for o in outcomes if o.verdict == "NOT CERTIFIED"]
        if query:
            q = query.casefold()
            ends = [e for e in ends if q in f"{e.title} {e.reason}".casefold()]
            outcomes = tuple(o for o in outcomes if q in o.title.casefold())
        cal = M.Calibration(claims=self._n("cal-n", len(outcomes)), mean_squared_error=self._n("cal-mse", None),
                            evidence_bits=self._n("cal-bits", None), note=W.CALIBRATION)
        return M.Record(researcher=ME, dead_ends=tuple(ends), outcomes=outcomes, calibration=cal,
                        verdicts_unopened=self._n("unopened", len(facts) - len(self.opened)),
                        harness_improvement=W.IMPROVEMENT)

    def trust(self) -> M.TrustBoard:
        rows = (M.TrustRow(name="Docker", state="ok", detail="Scripted example.", next_step=None,
                           needed_for="both tracks"),
                M.TrustRow(name="Lab box image", state="ok", detail="Scripted example.", next_step=None,
                           needed_for="the Lab"),
                M.TrustRow(name="Spending limit", state="ok", detail="Scripted example.", next_step=None,
                           needed_for="the Lab"),
                M.TrustRow(name="Public random value", state="ok", detail="Scripted example.", next_step=None,
                           needed_for="generated data"),
                M.TrustRow(name="Court health run", state="missing", detail="Scripted example.",
                           next_step="chh court health --docker", needed_for="both tracks"),
                M.TrustRow(name="Feedback table", state="yours", detail="Scripted example.",
                           next_step="Read the table and decide it is right.", needed_for="both tracks"))
        return M.TrustBoard(rows=rows, steps_left=self._n("left", 1), limits=W.TRUST_LIMITS)

    def resolve(self, target: str) -> M.Resolved:
        if target not in self.facts:
            raise Refused("BadLink", f"nothing recorded under {target!r}")
        return M.Resolved(link=target, value=self.facts[target], rests_on=NOTICE, source={"scripted": True})
