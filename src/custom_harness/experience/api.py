"""The Experience API (GOAL 18.5): the one doorway the UI and integrations call. docs/experience_api.md.

Order of use: `whoami`, `seal`, `start`, `explore` / `steer`, `freeze`, `rehearse`, `promote_preview`, `promote`,
`mint`, `run_case`, `case`, `share`, `paper`, `record`. `waiting` lists what is pending, `approve` points to it,
`resolve` follows the link of any number, `technical` is the separate technical view.

Rules that hold for every verb (tests/test_experience_ratchet.py keeps them true):
  - it holds a HostStore, so private data can only be read through kerneld;
  - Court output reaches it only through `court/display.py`. The return value of `court.minted.run` is dropped;
  - it never grants: no verb writes an approval, a role, a retirement or a certificate. `approve` writes nothing;
  - a verb that is a researcher's decision names the researcher and refuses an agent's name or a call from inside a run.
    That is accountability, not authentication: on one machine the OS user is the trust root (human/guard.py).
"""

from __future__ import annotations

import functools
import getpass
import hashlib
import json
import tempfile
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any, Concatenate

from ..audit.spec import ClaimPart
from ..config import data_dir
from ..court import display as D
from ..court import health as H
from ..court import minted as C
from ..court import rehearsal as R
from ..court import sealed as Z
from ..errors import InfraError
from ..figures.audit import FigureError
from ..human.guard import HumanActionRefused, check_human_name, require_human
from ..kerneld.hoststore import host_store
from ..lab import promote as P
from ..lab import session as S
from ..lab import trace as T
from ..lab.agents import AgentOptions
from ..lab.box import DockerCLI
from ..lab.credentials import CredentialRefused, CredentialSpec
from ..lab.session import LabError
from ..lab.walls import LabWallError, lab_root, overlaps
from ..outputs.court_paper import CourtPaperError, audit_court_paper, generate
from ..store import EvidenceStore
from ..studio.queue import pending
from . import schemas as M
from . import words as W

IDENTITY_KIND = "experience_identity"
OWNER_KIND = "experience_owner"
READINESS_KIND = "experience_readiness"
PAPER_KIND = "experience_paper"
SHARE_KIND = "experience_share"
MAX_REPLY = 8000  # UNCALIBRATED (owner decision, GOAL 13.8): characters of an agent's reply on a view
PREFIX = "chh://"
OWN_STEPS = {"needs_beacon": "chh exp mint {case} --beacon VALUE", "ready_to_run": "chh exp run {case}",
             "outcome_recorded": "chh exp case {case}",
             "needs_role_approval": "chh approve-role {target} --approver NAME"}
REFUSED: tuple[type[Exception], ...] = (
    C.CourtRefused, CourtPaperError, CredentialRefused, D.DisplayRefused, FigureError, FileNotFoundError,
    HumanActionRefused, InfraError, LabError, LabWallError, P.PromoteError, R.RehearsalRefused, T.TraceRefused,
    Z.SealRefused)


class Refused(RuntimeError):
    """The one error of the API: `code` is the kind of refusal, the message says why in words."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def refusing[T: "Experience", **A, B](fn: Callable[Concatenate[T, A], B]) -> Callable[Concatenate[T, A], B]:
    @functools.wraps(fn)
    def call(self: T, *a: A.args, **k: A.kwargs) -> B:
        try:
            return fn(self, *a, **k)
        except REFUSED as e:
            raise Refused(type(e).__name__, str(e)) from None
    return call


def link(kind: str, ident: str, field: str) -> str:
    return f"{PREFIX}{kind}/{urllib.parse.quote(ident, safe='')}/{field}"


def num(kind: str, ident: str, field: str, value: float | int | None) -> M.Number:
    return M.Number(value=value, link=link(kind, ident, field))


class Experience:
    """One researcher's doorway onto one store. `docker` and `sandbox` are injected: `sandbox(root)` gives the sandbox
    factory for that root, and the default is the Court's network-free Docker sandbox."""

    def __init__(self, store: EvidenceStore, *, docker: DockerCLI | None = None,
                 sandbox: Callable[[Path], Any] | None = None, environ: dict[str, str] | None = None):
        self.store = host_store(store)
        self.docker = docker
        self.sandbox = sandbox
        self.environ = environ

    # ---------------------------------------------------------------- whoami
    def whoami(self) -> M.Identity:
        user = getpass.getuser()
        rows = self.store.records_where(IDENTITY_KIND, "os_user", user)
        return M.Identity(os_user=user, researcher=rows[-1]["researcher"] if rows else user, chosen=bool(rows))

    @refusing
    def set_researcher(self, name: str) -> M.Identity:
        self.store.append_record(IDENTITY_KIND, {"os_user": getpass.getuser(), "at": time.time(),
                                                 "researcher": require_human(name, what="the researcher's name")})
        return self.whoami()

    def _human(self) -> str:
        return require_human(self.whoami().researcher, what="the researcher taking this step")

    def _own(self, kind: str, ident: str, name: str) -> None:
        self.store.append_record(OWNER_KIND, {"key": f"{kind}:{ident}", "researcher": name, "at": time.time()})

    def _owner(self, kind: str, ident: str) -> str | None:
        rows = self.store.records_where(OWNER_KIND, "key", f"{kind}:{ident}")
        return str(rows[-1]["researcher"]) if rows else None

    def _session_is_mine(self, sid: str) -> bool:
        owner, me = self._owner("session", sid), self.whoami()
        if owner is not None:
            return owner == me.researcher
        return any(e.get("event") == "started" and e.get("by") == me.os_user for e in S.events(self.store, sid))

    def _my_cases(self) -> list[dict[str, Any]]:
        me = self.whoami().researcher
        return [c for c in D.listed(self.store) if self._owner("case", c["case_id"]) == me
                or self._session_is_mine(str(c["session_id"]))]

    # ---------------------------------------------------------------- waiting / approve
    def _queue_items(self) -> list[tuple[M.WaitingItem, dict[str, Any]]]:
        out: list[tuple[M.WaitingItem, dict[str, Any]]] = []
        for it in pending(self.store):
            kind = str(it["kind"])
            title, why = W.WAITING[kind]
            ident = hashlib.sha256(json.dumps([kind, it.get("binds"), it.get("source")], sort_keys=True,
                                              default=str).encode()).hexdigest()[:16]
            out.append((M.WaitingItem(id=ident, title=title, why=why, human_only=True,
                                      optional=bool(it.get("optional")), command=str(it["decide"])),
                        dict(it.get("binds") or {})))
        return out

    def _case_items(self) -> list[tuple[M.WaitingItem, dict[str, Any]]]:
        out: list[tuple[M.WaitingItem, dict[str, Any]]] = []
        me = self.whoami().researcher
        if H.passing(self.store) is None:
            title, why = W.WAITING["health"]
            out.append((M.WaitingItem(id="court-health", title=title, why=why, human_only=False,
                                      command="chh court health --docker"), {}))
        for c in self._my_cases():
            step = D.progress(self.store, c["case_id"])
            # a role approval is already on the queue; an opened verdict needs nothing
            if step == "needs_role_approval" or (step == "outcome_recorded" and D.seen(self.store, c["case_id"], me)):
                continue
            title, why = W.WAITING[step]
            out.append((M.WaitingItem(id=f"{step}-{c['case_id']}", title=title, why=why, human_only=False,
                                      command=OWN_STEPS[step].format(case=c["case_id"], target=c["target"])),
                        {"case_id": c["case_id"]}))
        return out

    def waiting(self) -> M.Waiting:
        """Everything pending, from the human queue and from this researcher's cases. Writes nothing."""
        items = tuple(i for i, _ in (*self._case_items(), *self._queue_items()))
        return M.Waiting(items=items, total=num("waiting", "all", "total", len(items)))

    @refusing
    def approve(self, item_id: str) -> M.ApprovalRoute:
        """Points to the step and takes nothing: there is no way to pass a decision here. Writes nothing."""
        for item, binds in (*self._case_items(), *self._queue_items()):
            if item.id == item_id:
                return M.ApprovalRoute(id=item.id, title=item.title, human_only=item.human_only, command=item.command,
                                       note=W.HUMAN_ONLY, binds=binds)
        raise Refused("NotWaiting", f"nothing is waiting under {item_id!r}")

    # ---------------------------------------------------------------- seal
    @refusing
    def seal(self, req: M.SealIn) -> M.Sealed:
        name = self._human()
        body = Z.seal_project(self.store, req.project, Path(req.data_file), label_column=req.label_column,
                              rows=req.rows, parts=req.parts, beacon_round=req.beacon_round,
                              beacon_value=req.beacon_value)
        self._own("project", req.project, name)
        return M.Sealed(project=req.project, lab_rows=num("seal", req.project, "lab_rows", body["n_lab"]),
                        sealed_rows=tuple(num("seal", req.project, f"part{i}", n)
                                          for i, n in enumerate(body["n_parts"], 1)),
                        note="Sealed before any Lab session of this project. That nobody gave this file to a Lab some "
                             "other way rests on you, and is listed as an assumption on every claim.")

    # ---------------------------------------------------------------- start / explore / steer
    def _session(self, sid: str) -> M.Session:
        row = S.status(self.store, sid, docker=self.docker)[0]
        rec = S.started(self.store, sid, allow_stopped=True)
        return M.Session(session_id=sid, project=rec.get("project"), agent=row["agent"], box=str(row["box"]),
                         workspace=str(row["workspace"]), notice=W.LAB_NOTICE)

    @refusing
    def start(self, req: M.StartIn) -> M.Session:
        name = self._human()
        cred = None
        if req.credential is not None:
            c = req.credential
            cred = CredentialSpec(registry_key=c.registry_key, box_env=req.box_env or "UNSET",
                                  spend_cap_usd=c.spend_cap_usd, scope=c.scope, source_env=c.source_env,
                                  source_file=c.source_file, ttl_s=c.ttl_hours * 3600)
        rec = S.start(self.store, req.agent, AgentOptions(registry_key=req.registry_key, model=req.model,
                                                          box_env=req.box_env),
                      cred, session_id=req.session_id, docker=self.docker, environ=self.environ, project=req.project)
        self._own("session", rec["session_id"], name)
        return self._session(rec["session_id"])

    def _facts_session(self, sid: str) -> dict[str, Any]:
        tree = T.idea_tree(self.store, sid)
        costs = T.costs(self.store, sid)
        web = [e for e in T.events(self.store, sid, "web") if e["payload"].get("op") == "connection"]
        return {"ideas": len(tree), "open_ideas": sum(n["status"] == "open" for n in tree.values()),
                "dropped_ideas": sum(n["status"] == "abandoned" for n in tree.values()),
                "runs": len(T.events(self.store, sid, "run")), "actions": len(T.events(self.store, sid, "action")),
                "steers": sum(e["payload"].get("op") == "steer" for e in T.events(self.store, sid, "human")),
                "web_connections": len(web), "cost_usd": costs["usd_total"],
                "costs_unknown": costs["unknown_usd_events"], "steps": len(T.events(self.store, sid))}

    @refusing
    def lab(self, session_id: str) -> M.LabView:
        rec = S.started(self.store, session_id, allow_stopped=True)
        f = self._facts_session(session_id)
        n = functools.partial(num, "session", session_id)
        return M.LabView(session_id=session_id, project=rec.get("project"), notice=W.LAB_NOTICE,
                         ideas=tuple(M.Idea(idea_id=i, title=str(v["title"]), status=str(v["status"]))
                                     for i, v in T.idea_tree(self.store, session_id).items()),
                         open_ideas=n("open_ideas", f["open_ideas"]), dropped_ideas=n("dropped_ideas",
                                                                                      f["dropped_ideas"]),
                         runs=n("runs", f["runs"]), steers=n("steers", f["steers"]),
                         web_connections=n("web_connections", f["web_connections"]),
                         cost_usd=n("cost_usd", f["cost_usd"]), costs_unknown=n("costs_unknown", f["costs_unknown"]))

    def _prompt(self, session_id: str, prompt: str) -> tuple[str, bool]:
        rec = S.run_prompt(self.store, session_id, prompt, docker=self.docker)
        return Path(rec["transcript"]).read_text(encoding="utf-8")[:MAX_REPLY], rec["returncode"] == 0

    @refusing
    def explore(self, req: M.ExploreIn) -> M.Explored:
        reply, ok = self._prompt(req.session_id, req.prompt)
        return M.Explored(session_id=req.session_id, finished=ok, reply=reply, notice=W.LAB_NOTICE,
                          actions=num("session", req.session_id, "actions",
                                      self._facts_session(req.session_id)["actions"]))

    @refusing
    def steer(self, req: M.SteerIn) -> M.Steered:
        """A steer is always in the Story, with how many Court results this person had seen: a person who read a
        verdict carries it into the Lab, and only released results can have been read through this doorway."""
        name = self._human()
        S.started(self.store, req.session_id)
        raw = req.text.encode("utf-8")
        count = D.results_seen(self.store, name)
        ev = T.record(self.store, req.session_id, "human",
                      {"op": "steer", "prompt_sha256": hashlib.sha256(raw).hexdigest(),
                       "prompt_blob": self.store.blobs.put(raw), "note": f"court results seen so far: {count}"},
                      actor=T.human(name))
        reply = self._prompt(req.session_id, req.text)[0] if req.send else None
        return M.Steered(session_id=req.session_id, event_id=ev["event_id"], sent=req.send, reply=reply,
                         court_results_seen=num("viewer", name, "court_results", count),
                         note="Recorded in the Story.")

    @refusing
    def stop(self, session_id: str) -> M.Session:
        S.stop(self.store, session_id, docker=self.docker)
        return self._session(session_id)

    # ---------------------------------------------------------------- freeze / rehearse
    @refusing
    def freeze(self, req: M.FreezeIn) -> M.Frozen:
        name = self._human()
        body = P.freeze(self.store, req.session_id, Path(req.request_file), docker=self.docker)
        self._own("promotion", body["promotion_id"], name)
        return M.Frozen(promotion_id=body["promotion_id"], session_id=req.session_id,
                        files=num("promotion", body["promotion_id"], "files", body["n_files"]),
                        note="The code and its environment are frozen. Nothing here is a finding yet.")

    def _factories(self, root: Path) -> Any:
        if self.sandbox is not None:
            return self.sandbox(Path(root))
        from types import SimpleNamespace

        from ..audit.cli import _factory
        return _factory(SimpleNamespace(docker=True, dev_sandbox=False), Path(root) / "sandboxes")

    def _claim_part(self, pid: str) -> ClaimPart:
        return P.PromoteRequest.model_validate(P._promotion(self.store, pid)[0]["request"]).claim

    def _readiness(self, pid: str, raw: dict[str, Any]) -> M.Readiness:
        self.store.append_record(READINESS_KIND, {"promotion_id": pid, "readiness": raw, "at": time.time()})
        n = functools.partial(num, "readiness", pid)
        rows = tuple(M.ReadinessRow(test_rows=n(f"rows:{r['n_test']}", r["n_test"]),
                                    chance=n(f"chance:{r['n_test']}", r["p_certify"]), objection=bool(r["denied"]))
                     for r in raw["sizes"])
        if raw["verdict"] == "not run":
            sentence = "Not estimated: it needs Lab outcome counts and the accuracy measure."
        elif raw["verdict"] == "no objection":
            sentence = f"No objection at {raw['smallest_size_with_no_objection']} test rows."
        else:
            text = str(raw["verdict"])
            sentence = f"{text[0].upper()}{text[1:]}."
        return M.Readiness(sentence=sentence, notice=W.READINESS_NOTICE, rows=rows)

    @refusing
    def rehearse(self, req: M.RehearseIn) -> M.Rehearsal:
        """A mock trial on Lab data. Default data: the Lab's part of the session's sealed project."""
        frozen = P._promotion(self.store, req.promotion_id)[0]
        if req.data_file is not None:
            report = self._rehearse(req, Path(req.data_file))
        else:
            project = S.started(self.store, frozen["session_id"], allow_stopped=True).get("project")
            if project is None:
                raise Refused("NoLabData", "this session has no sealed project: name the Lab data file")
            with tempfile.TemporaryDirectory(prefix="chh-exp-") as tmp:
                path = Path(tmp) / "lab.csv"
                path.write_bytes(Z.lab_part(self.store, project))
                report = self._rehearse(req, path)
        ran = report["state"] == "RAN"
        names = W.CHECK_PLAIN
        n = functools.partial(num, "rehearsal", req.promotion_id)
        scores = tuple(M.ScoreRow(arm="method" if arm == "subject" else "baseline", seed=str(seed),
                                  score=n(f"score:{arm}:{seed}", value))
                       for arm, by in (report.get("scores") or {}).items() for seed, value in by.items())
        return M.Rehearsal(
            promotion_id=req.promotion_id, notice=W.REHEARSAL_NOTICE, ran=ran,
            would_certify=str(report["would_label"]).startswith("CERTIFIED") if ran else None,
            reason=report.get("reason"), scores=scores,
            checks=tuple(M.CheckRow(name=names[k], status=str(v["status"]))
                         for k, v in (report.get("checks") or {}).items()),
            readiness=self._readiness(req.promotion_id, report["readiness"]))

    def _rehearse(self, req: M.RehearseIn, path: Path) -> dict[str, Any]:
        return R.rehearse(self.store, req.promotion_id, path, label_column=req.label_column, rows=req.rows,
                          sandbox_factory=self._factories)

    # ---------------------------------------------------------------- promote (readiness inside)
    def _pairs(self, req: M.PromoteIn) -> tuple[int, int, int, int] | None:
        if req.dev_pairs is not None:
            return req.dev_pairs
        pairs = (R.last_rehearsal(self.store, req.promotion_id) or {}).get("dev_pairs")
        return None if pairs is None else (int(pairs[0]), int(pairs[1]), int(pairs[2]), int(pairs[3]))

    def _route_looks(self, req: M.PromoteIn) -> M.Number:
        if req.route != "sealed":
            return num("target", "new", "looks", 1)
        seal = Z.sealed_record(self.store, str(req.project))
        if seal is None:
            raise Refused("NotSealed", f"project {req.project!r} is not sealed")
        target = seal["targets"][0]
        return num("target", target, "looks", D.looks_on_target(self.store, target))

    @refusing
    def promote_preview(self, req: M.PromoteIn) -> M.PromotePreview:
        """What promoting would do: readiness, looks and the steps. Registers nothing."""
        ready = self._readiness(req.promotion_id, R.readiness(self._claim_part(req.promotion_id), self._pairs(req)))
        return M.PromotePreview(promotion_id=req.promotion_id, route=req.route, finding_possible=req.route != "public",
                                readiness=ready, looks_remaining=self._route_looks(req),
                                what_happens=W.PROMOTE_STEPS[req.route])

    @refusing
    def promote(self, req: M.PromoteIn) -> M.Promoted:
        """Fix the claim for the Court. The researcher's decision: it spends the look it names."""
        name = self._human()
        pid = req.promotion_id
        if req.route == "public":
            hand = P.handoff(self.store, pid, Path(str(req.targets_file)))
            self._own("audit", hand["audit_id"], name)
            return M.Promoted(promotion_id=pid, case_id=hand["audit_id"], second_case_id=None, route=req.route,
                              next_step=W.PUBLIC_NEXT, command=hand["next"][0])
        if req.route == "generated":
            body = C.register_promotion(self.store, pid, str(req.family), beacon_round=str(req.beacon_round),
                                        dev_pairs=self._pairs(req), n_test=req.n_test,
                                        revalidation_round=req.second_round)
        else:
            body = C.register_sealed(self.store, pid, str(req.project), revalidate=req.second_part)
        second = (body.get("revalidation") or {}).get("case_id")
        for case_id in filter(None, (body["case_id"], second)):
            self._own("case", case_id, name)
        step, command = self._step(body["case_id"])
        return M.Promoted(promotion_id=pid, case_id=body["case_id"], second_case_id=second, route=req.route,
                          next_step=step, command=command)

    def _step(self, case_id: str) -> tuple[str, str]:
        step = D.progress(self.store, case_id)
        return W.STEPS[step], OWN_STEPS[step].format(case=case_id, target=D.public_case(self.store, case_id)["target"])

    @refusing
    def mint(self, case_id: str, beacon_value: str) -> M.Case:
        """Give the public random value: the fresh test data is then made, after the freeze."""
        self._human()
        C.mint(self.store, case_id, beacon_value)
        return self.case(case_id)

    @refusing
    def run_case(self, case_id: str) -> M.Case:
        """The trial. Its result is read only through the one display path."""
        C.run(self.store, case_id, self._factories(data_dir() / "sandboxes" / f"court-{case_id}"))
        return self.case(case_id)

    # ---------------------------------------------------------------- case
    def _story(self, claim_hash: str) -> M.Story | None:
        try:
            s = T.story(self.store, claim_hash)
        except T.TraceRefused:
            return None
        f = self._facts_session(s["session_id"])
        n = functools.partial(num, "session", s["session_id"])
        return M.Story(session_id=s["session_id"], idea_path=tuple(s["idea_path"]), steps=n("steps", f["steps"]),
                       steers=n("steers", f["steers"]), web_connections=n("web_connections", f["web_connections"]),
                       intact=not s["chain_problems"])

    def _case_view(self, v: dict[str, Any], pub: dict[str, Any], *, rows: int | None, step: tuple[str, str] | None,
                   reason: str | None) -> M.Case:
        cid, claim = str(pub["case_id"]), v["claim"]
        route = str(v["route"])
        kind = "audit" if "audit_id" in v else "case"
        retro = route == "retrospective" or bool(v.get("retrospective"))
        scope = W.scope_sentence(claim, family=pub.get("family"), route="retrospective" if retro else route)
        verdict = v["verdict"] if "verdict" in v else W.verdict_of(v.get("label"), str(v["state"]))
        held = list(v.get("held", []))
        return M.Case(
            case_id=cid, title=str(claim["title"]), sentence=str(claim["sentence"]), scope=scope, verdict=verdict,
            verdict_sentence=W.verdict_sentence(verdict, held, scope, v.get("reason_class"), retrospective=retro),
            reason_code=v.get("reason_class"), reason=reason, retired=bool(v.get("retired")),
            badges=tuple(M.Badge(**W.badge_text(f)) for f in v.get("badges", [])),
            second_test=W.SECOND_TEST.get(str(v.get("revalidation"))),
            checks_in_full=tuple(M.CheckRow(name=W.CHECK_PLAIN[k], status=str(c["status"]))
                                 for k, c in v.get("checks", {}).items()),
            looks_remaining=num(kind, cid, "looks_remaining", v.get("looks_remaining")),
            test_rows=num(kind, cid, "test_rows", rows), next_step=step[0] if step else None,
            command=step[1] if step else None, rerun=v.get("rerun"), story=self._story(str(pub["claim_hash"])),
            technical=link("case", cid, "technical"))

    @refusing
    def case(self, case_id: str) -> M.Case:
        """A Court case or a Claim Audit (reproduce-this-paper): verdict, badges, story, rerun, next step. Opening a
        decided case records a release: this is the one display path."""
        me = self.whoami().researcher
        if not D.is_case(self.store, case_id):
            return self._audit_case(case_id, me)
        v = D.view(self.store, case_id, me)
        pub = D.public_case(self.store, case_id)
        step: tuple[str, str] | None = None
        reason = None
        if v["state"] == "no outcome yet":
            step = self._step(case_id)
        elif v["state"] == "COULD NOT RUN":
            reason = W.FAILED_AFTER_READ if v["labels_read"] else str(v["reason"])
            step = None if v["labels_read"] else (W.RETRY, OWN_STEPS["ready_to_run"].format(case=case_id))
        return self._case_view(v, pub, rows=pub["n_test"], step=step, reason=reason)

    def _audit_case(self, audit_id: str, viewer: str) -> M.Case:
        a = D.view_audit(self.store, audit_id, viewer)
        if a is None:
            raise Refused("NoCase", f"no case {audit_id!r}")
        v = {**a, "route": "retrospective", "state": "AUDIT", "looks_remaining": 0,
             "verdict": W.AUDIT_VERDICTS[str(a["verdict"])]}
        return self._case_view(v, {"case_id": audit_id, "claim_hash": a["claim_hash"]}, rows=None, step=None,
                               reason=None)

    @refusing
    def technical(self, case_id: str) -> M.TechnicalView:
        """The separate technical view: the Court's own words, through the same display path."""
        me = self.whoami().researcher
        if D.is_case(self.store, case_id):
            return M.TechnicalView(case_id=case_id, details=D.view(self.store, case_id, me))
        return M.TechnicalView(case_id=case_id, details=D.view_audit(self.store, case_id, me) or {})

    def rehearsal_details(self, promotion_id: str) -> M.TechnicalView:
        """Everything the last rehearsal recorded: Lab data, nothing held back."""
        return M.TechnicalView(case_id=promotion_id, details=R.last_rehearsal(self.store, promotion_id) or {})

    def cases(self) -> M.Cases:
        """This researcher's cases and where each stands. It opens none of them."""
        me = self.whoami().researcher
        rows = []
        for c in self._my_cases():
            cid = str(c["case_id"])
            shown = D.seen(self.store, cid, me)
            state = W.verdict_of(shown["label"], str(shown["label"])) if shown else W.STEPS[D.progress(self.store, cid)]
            rows.append(M.CaseLine(case_id=cid, title=str(c["claim"]["title"]), state=str(state),
                                   looks_remaining=num("case", cid, "looks_remaining",
                                                       D.looks_remaining(self.store, c))))
        return M.Cases(cases=tuple(rows), total=num("cases", "all", "total", len(rows)))

    @refusing
    def share(self, req: M.ShareIn) -> M.Shared:
        """The reviewer's case file. It is a view by that reviewer, so it is recorded and released as one."""
        if overlaps(Path(req.out_file), lab_root()):
            raise Refused("InsideLab", "a case file is written outside the lab root")
        viewer = f"reviewer:{check_human_name(req.reviewer, what='the reviewer')}"
        v = D.view(self.store, req.case_id, viewer)
        pub = D.public_case(self.store, req.case_id)
        doc = self._case_view(v, pub, rows=pub["n_test"], step=None, reason=None)
        Path(req.out_file).write_text(doc.model_dump_json(indent=1), encoding="utf-8")
        self.store.append_record(SHARE_KIND, {"case_id": req.case_id, "reviewer": req.reviewer, "path": req.out_file,
                                              "at": time.time()})
        return M.Shared(case_id=req.case_id, reviewer=req.reviewer, path=req.out_file,
                        note="The reviewer sees what you see. The rerun command appears once the target is retired.")

    @refusing
    def paper(self, req: M.PaperIn) -> M.Paper:
        """A living paper: every number comes from the certificate. It opens with the whole certificate, once the
        target is retired."""
        v = D.autopsy(self.store, req.case_id, self.whoami().researcher)
        h, cert = str(v["cert_hash"]), v["autopsy"]
        refs = {k: {"certificate": h, "pointer": p} for k, p in req.refs.items()}
        built = generate(req.title, req.body, refs, certificate_of=lambda _: cert, allowed_certificates={h})
        problems = audit_court_paper(req.title, req.body, built.tex, built.manifest, certificate_of=lambda _: cert,
                                     allowed_certificates={h})
        if problems:
            raise Refused("PaperDiffers", "; ".join(problems))
        out = data_dir() / "papers" / f"{req.case_id}.tex"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(built.tex, encoding="utf-8")
        self.store.append_record(PAPER_KIND, {"case_id": req.case_id, "numbers": len(refs), "path": str(out),
                                              "tex_sha256": built.manifest["tex_sha256"], "at": time.time()})
        return M.Paper(case_id=req.case_id, path=str(out), numbers=num("paper", req.case_id, "numbers", len(refs)),
                       note="Every number in it comes from the certificate.")

    # ---------------------------------------------------------------- record
    def _dead_ideas(self) -> list[M.DeadEnd]:
        out = []
        for sid in sorted({str(r.get("session_id")) for r in self.store.records(S.RECORD_KIND)}):
            if self._session_is_mine(sid):
                tree = T.idea_tree(self.store, sid)
                out += [M.DeadEnd(title=str(tree[e["payload"]["idea_id"]]["title"]) or e["payload"]["idea_id"],
                                  reason=str(e["payload"]["reason"]), source="idea")
                        for e in T.events(self.store, sid, "idea") if e["payload"].get("op") == "abandon"]
        return out

    def _calibration(self, me: str) -> M.Calibration:
        t = D.track_record(self.store, me)
        n = functools.partial(num, "record", me)
        return M.Calibration(claims=n("claims", t["n"]), mean_squared_error=n("mean_squared_error", t["brier"]),
                             evidence_bits=n("evidence_bits", t["evidence_bits"]), note=W.CALIBRATION)

    def _unopened(self, me: str) -> int:
        return sum(D.progress(self.store, c["case_id"]) == "outcome_recorded"
                   and D.seen(self.store, c["case_id"], me) is None for c in self._my_cases())

    def record(self, query: str | None = None) -> M.Record:
        """Dead ends, outcomes and calibration. It shows only what this researcher already opened, and opens nothing."""
        me = self.whoami().researcher
        outcomes, ends = [], self._dead_ideas()
        for c in self._my_cases():
            shown = D.seen(self.store, c["case_id"], me)
            if shown is None:
                continue
            title = str(c["claim"]["title"])
            verdict = str(W.verdict_of(shown["label"], str(shown["label"])))
            code = shown.get("reason_class")
            outcomes.append(M.Outcome(case_id=c["case_id"], title=title, verdict=verdict,
                                      badges=str(shown.get("badge")), reason_code=code))
            if verdict == "NOT CERTIFIED":
                ends.append(M.DeadEnd(title=title, reason=W.REASONS.get(str(code), verdict), source="verdict"))
        if query:
            q = query.casefold()
            ends = [e for e in ends if q in f"{e.title} {e.reason}".casefold()]
            outcomes = [o for o in outcomes if q in o.title.casefold()]
        return M.Record(researcher=me, dead_ends=tuple(ends), outcomes=tuple(outcomes),
                        verdicts_unopened=num("record", me, "unopened", self._unopened(me)),
                        calibration=self._calibration(me), harness_improvement=W.IMPROVEMENT)

    # ---------------------------------------------------------------- every number is a link
    def _facts(self, kind: str, ident: str) -> dict[str, Any]:
        if kind == "case":
            pub = D.public_case(self.store, ident)
            return {**pub, "looks_remaining": D.looks_remaining(self.store, pub), "test_rows": pub["n_test"]}
        if kind == "audit":
            return {"looks_remaining": 0, "test_rows": None}
        if kind == "seal":
            seal = Z.sealed_record(self.store, ident) or {}
            parts = {f"part{i}": n for i, n in enumerate(seal.get("n_parts", ()), 1)}
            return {"lab_rows": seal.get("n_lab"), "seal_seq": seal.get("seal_seq"),
                    "data_sha256": seal.get("data_sha256"), **parts}
        if kind == "session":
            return self._facts_session(ident)
        if kind == "promotion":
            return {"files": P._promotion(self.store, ident)[0]["n_files"]}
        if kind == "rehearsal":
            scores = (R.last_rehearsal(self.store, ident) or {}).get("scores") or {}
            return {f"score:{a}:{s}": v for a, by in scores.items() for s, v in by.items()}
        if kind == "readiness":
            rows = self.store.records_where(READINESS_KIND, "promotion_id", ident)
            sizes = rows[-1]["readiness"]["sizes"] if rows else []
            return {**{f"rows:{r['n_test']}": r["n_test"] for r in sizes},
                    **{f"chance:{r['n_test']}": r["p_certify"] for r in sizes}}
        if kind == "target":
            return {"looks": 1 if ident == "new" else D.looks_on_target(self.store, ident)}
        if kind == "viewer":
            return {"court_results": D.results_seen(self.store, ident)}
        if kind == "record":
            t = D.track_record(self.store, ident)
            return {"claims": t["n"], "mean_squared_error": t["brier"], "evidence_bits": t["evidence_bits"],
                    "unopened": self._unopened(ident)}
        if kind == "paper":
            rows = self.store.records_where(PAPER_KIND, "case_id", ident)
            return {"numbers": rows[-1]["numbers"]} if rows else {}
        if kind == "waiting":
            return {"total": int(self.waiting().total.value or 0)}
        if kind == "cases":
            return {"total": int(self.cases().total.value or 0)}
        raise Refused("BadLink", f"no links of kind {kind!r}")

    @refusing
    def resolve(self, target: str) -> M.Resolved:
        """What a number on a view rests on. `technical` links go to `technical`."""
        parts = target.removeprefix(PREFIX).split("/")
        if not target.startswith(PREFIX) or len(parts) != 3:
            raise Refused("BadLink", f"not a link: {target!r}")
        kind, ident, field = parts[0], urllib.parse.unquote(parts[1]), parts[2]
        facts = self._facts(kind, ident)
        if field not in facts:
            raise Refused("BadLink", f"{target!r}: nothing recorded under that name")
        return M.Resolved(link=target, value=facts[field], rests_on=W.RESTS_ON[kind], source=facts)
