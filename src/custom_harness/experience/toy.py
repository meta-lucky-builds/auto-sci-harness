"""`chh exp toy`: the minted toy track (GOAL 18.4 item 4; docs/first_discovery_plan.md Track 1), one command.

Each run does the next steps of the route through the same doorway as everything else (`Experience`), and stops at each
step that needs a person, saying what to do. Running it again carries on. It keeps no state of its own: where the track
stands is read from the store. It writes no approval, no role and no sign-off; those stay human-only commands, printed
here for a person to run.

A certified result here is "first certified result (simulator-scoped)". It is generator-scoped and it is never called
a discovery.

A real run calls a model (the agent, inside its box) and spends the scoped key. Nothing else here does.
"""

from __future__ import annotations

import csv
import io
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import data_dir
from ..court import display as D
from ..court import rehearsal as R
from ..lab import promote as P
from ..lab import session as S
from ..research.suite import FAMILIES
from ..sandbox.base import safe_path
from . import schemas as M
from .api import Experience, Refused
from .doctor import Check, blocking, doctor

FAMILY = "rings"
RESULT_NAME = "first certified result (simulator-scoped)"
LAB_ROWS = 2000  # UNCALIBRATED (owner decision, GOAL 13.8): rows the Lab sees
REHEARSAL_ROWS = 600  # UNCALIBRATED (owner decision, GOAL 13.8): of those, held back in the mock trial
LAB_SEED = "chh-toy-lab-data-public"  # public: these rows are Lab data, drawn apart from any fresh test data
AGENT_BRIEF = f"""You are in a Lab box. This is exploration: nothing here is a finding.

The task: data/{FAMILY}-lab.csv holds points from a generator. Columns: x1, x2, n1, n2, n3, n4, label. The label is 0, 1
or 2. Find a simple method that predicts the label better than always answering the most common label.

Write three files in /workspace:
- method.py: defines build_model(seed), which returns an object with fit(X, y) and predict(X). X is a list of rows of
  floats, y a list of label strings, and predict returns a list of label strings.
- baseline.py: the same shape, and it always predicts the most common training label.
- requirements.lock: one exact name==version line for each package method.py or baseline.py imports. It may hold only
  a comment if they use just the standard library.

Use only the Python standard library, numpy and scikit-learn. Both files later run offline on new rows from the same
generator, so they read no file and use no network. Check your work on the Lab data.

Report each idea you try by adding a line to .lab/trace.jsonl, for example:
{{"kind": "idea", "client_id": "1", "payload": {{"op": "propose", "idea_id": "first", "title": "..."}}}}

Stop when the three files exist.
"""
REQUEST = f"""format: chh-lab-promote/1
title: Rings toy (simulator-scoped)
sentence: On the {FAMILY} generator, the frozen method beats the most common label by at least 0.1 accuracy.
method: method.py
baseline: baseline.py
lock: requirements.lock
claim: {{metric: accuracy, min_effect: 0.1, probability: 0.6, seeds: [0, 1, 2], n_boot: 1000, resolve_by: 2030-01-01}}
budget: {{timeout_s: 900}}
"""
STEER = ('chh exp steer {session} --text "..." (say what is wrong), then: chh exp toy --session {session} --new-freeze')


@dataclass(frozen=True)
class ToyIn:
    session_id: str = "toy-1"
    agent: str = "claude-code"
    registry_key: str = "anthropic-api"
    model: str | None = None
    credential: M.CredentialIn | None = None
    beacon_round: str | None = None
    beacon_value: str | None = None
    new_freeze: bool = False


@dataclass
class ToyReport:
    did: list[str] = field(default_factory=list)
    done: bool = False
    stopped_because: str | None = None
    next_step: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    case: dict[str, Any] | None = None

    def stop(self, why: str, step: str | None = None) -> ToyReport:
        self.stopped_because, self.next_step = why, step
        return self


def lab_csv() -> bytes:
    """The Lab's rows: drawn from the family's public generator with a public seed, never from fresh test data."""
    fam = FAMILIES[FAMILY]
    rows = fam.rows(random.Random(f"{LAB_SEED}/features"), random.Random(f"{LAB_SEED}/labels"), LAB_ROWS)
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(fam.header)
    w.writerows(rows)
    return out.getvalue().encode("utf-8")


def _home(session_id: str) -> Path:
    """The researcher's own copies, outside the lab root: `freeze` refuses a request file an agent could have made."""
    path = data_dir() / "toy" / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _started(x: Experience, sid: str) -> bool:
    return any(e.get("event") == "started" for e in S.events(x.store, sid))


def _start(x: Experience, o: ToyIn, r: ToyReport) -> ToyReport | None:
    if o.credential is None:
        return r.stop("The first run names the scoped key and its limit.",
                      "chh exp toy --credential-env CHH_LAB_CRED_ANTHROPIC --spend-cap-usd 5 --scope \"...\"")
    x.start(M.StartIn(agent=o.agent, session_id=o.session_id, model=o.model, registry_key=o.registry_key,
                      credential=o.credential))
    data = lab_csv()
    (_home(o.session_id) / f"{FAMILY}-lab.csv").write_bytes(data)
    ws = S.workspace_of(o.session_id)
    safe_path(ws, "data").mkdir(exist_ok=True)
    safe_path(ws, f"data/{FAMILY}-lab.csv").write_bytes(data)
    r.did.append(f"started Lab session {o.session_id}")
    x.explore(M.ExploreIn(session_id=o.session_id, prompt=AGENT_BRIEF))
    r.did.append("gave the agent the task")
    return None


def _frozen(x: Experience, o: ToyIn, r: ToyReport) -> str | None:
    rows = x.store.records_where(P.RECORD_KIND, "session_id", o.session_id)
    frozen = [p["promotion_id"] for p in rows if p.get("event") == "frozen"]
    if frozen and not o.new_freeze:
        return str(frozen[-1])
    request = _home(o.session_id) / "promote.yaml"
    request.write_text(REQUEST, encoding="utf-8")
    try:
        pid = x.freeze(M.FreezeIn(session_id=o.session_id, request_file=str(request))).promotion_id
    except Refused as e:
        if e.code != "PromoteError":
            raise
        r.stop(f"The agent's work could not be frozen: {e}", STEER.format(session=o.session_id))
        return None
    r.did.append(f"froze the code as {pid}")
    return pid


def _rehearsed(x: Experience, o: ToyIn, pid: str, r: ToyReport) -> bool:
    last = R.last_rehearsal(x.store, pid)
    if last is None:
        rep = x.rehearse(M.RehearseIn(promotion_id=pid, rows=REHEARSAL_ROWS, label_column="label",
                                      data_file=str(_home(o.session_id) / f"{FAMILY}-lab.csv")))
        r.did.append("ran the mock trial on Lab data")
        fine, why = bool(rep.would_certify), rep.reason
    else:
        fine, why = str(last.get("would_label")).startswith("CERTIFIED"), None
    if not fine:
        r.stop(f"The mock trial would not certify this, and nothing was spent. {why or ''}".strip(),
               STEER.format(session=o.session_id))
    return fine


def _case_of(x: Experience, pid: str) -> str | None:
    rows = [c for c in D.listed(x.store) if c.get("promotion_id") == pid]
    return str(rows[-1]["case_id"]) if rows else None


def _promote(x: Experience, o: ToyIn, pid: str, r: ToyReport) -> str | None:
    case_id = _case_of(x, pid)
    if case_id is not None:
        return case_id
    if o.beacon_round is None:
        r.stop("Fix a public random round that has not happened yet, before any fresh data exists.",
               f"chh exp toy --session {o.session_id} --beacon-round ROUND")
        return None
    case_id = x.promote(M.PromoteIn(promotion_id=pid, route="generated", family=FAMILY,
                                    beacon_round=o.beacon_round)).case_id
    r.did.append(f"fixed the claim as case {case_id}")
    return case_id


def _role_step(x: Experience, case_id: str) -> str:
    target = str(D.public_case(x.store, case_id)["target"])
    return next(i.command for i in x.waiting().items if target in i.command)


def _trial(x: Experience, o: ToyIn, case_id: str, r: ToyReport) -> ToyReport:
    if D.progress(x.store, case_id) == "needs_beacon":
        if o.beacon_value is None:
            return r.stop("Wait until the round is published, then give its value.",
                          f"chh exp toy --session {o.session_id} --beacon VALUE")
        x.mint(case_id, o.beacon_value)
        r.did.append("the fresh test data was made")
    if D.progress(x.store, case_id) == "needs_role_approval":
        return r.stop("A person must approve the fresh test data. This is a human-only step: run it yourself, then run "
                      "this command again.", _role_step(x, case_id))
    if D.progress(x.store, case_id) == "ready_to_run":
        view = x.run_case(case_id)
        r.did.append("ran the trial in the Court's network-free sandbox")
    else:
        view = x.case(case_id)
    r.done, r.case = True, view.model_dump(mode="json")
    validated = view.verdict == "CERTIFIED" and any(b.name == "VALIDATED" for b in view.badges)
    target = D.public_case(x.store, case_id)["target"]
    step = f"chh court retire {target} --by NAME" if view.verdict and not view.retired else view.command
    return r.stop(f"{RESULT_NAME}. {view.verdict_sentence}" if validated else view.verdict_sentence,
                  step or f"chh exp share {case_id} --reviewer NAME --out FILE")


def run(x: Experience, o: ToyIn, *, checks: Callable[..., list[Check]] = doctor) -> ToyReport:
    """Carry the toy track as far as it can go without a person."""
    r = ToyReport()
    if not _started(x, o.session_id):
        stops = blocking(checks(x.store, docker=x.docker, environ=x.environ, registry_key=o.registry_key))
        if stops:
            r.blocked_by = [f"{c.name}: {c.next_step}" for c in stops]
            return r.stop("Some steps stand before a first session.", "chh exp doctor")
        if _start(x, o, r) is not None:
            return r
    pid = _frozen(x, o, r)
    if pid is None or not _rehearsed(x, o, pid, r):
        return r
    case_id = _promote(x, o, pid, r)
    return r if case_id is None else _trial(x, o, case_id, r)
