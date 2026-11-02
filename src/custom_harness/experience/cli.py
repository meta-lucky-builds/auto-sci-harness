"""`chh exp ...`: the Experience API on the command line (GOAL 18.5). JSON on stdout. docs/experience_api.md.

  chh exp whoami [--set NAME]
  chh exp waiting
  chh exp approve ITEM                        points to the step; it never takes it
  chh exp seal PROJECT --data F --label-column C --rows N [--parts 2] --beacon-round R --beacon VALUE
  chh exp start AGENT [--project P] [--session S] [--model M] [--local-model-server]
  chh exp lab SESSION
  chh exp explore SESSION --prompt TEXT
  chh exp steer SESSION --text TEXT [--no-send]
  chh exp stop SESSION
  chh exp freeze SESSION --request FILE
  chh exp rehearse PROMOTION --rows N [--data F --label-column C]
  chh exp preview|promote PROMOTION --route generated|sealed|public [route options]
  chh exp mint CASE --beacon VALUE
  chh exp run CASE
  chh exp case CASE
  chh exp cases
  chh exp technical CASE
  chh exp share CASE --reviewer NAME --out FILE
  chh exp paper CASE --title T --body FILE --refs FILE
  chh exp record [--query WORDS]
  chh exp resolve LINK
  chh exp schema
  chh exp doctor [--registry-key K] [--json]   is this machine ready? what is missing and the next step
  chh exp toy [key options] [--beacon-round R] [--beacon VALUE] [--new-freeze]   the minted toy track, one command
  chh exp ui [--port N] [--scripted]           the web interface, on this machine only
  chh exp setup [--name N] [--spend-limit USD] [--model M]   first-run setup
  chh exp backup --out DIR --by NAME | verify-backup PATH | restore PATH --to DIR --by NAME   HUMAN: the whole store
  chh exp upgrade --backup-to DIR --by NAME    HUMAN: back up, then bring the store to this code's format
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..store import EvidenceStore


def add_parsers(sub: Any) -> None:
    e = sub.add_parser("exp", help="the Experience API: the researcher's verbs (GOAL 18.5)")
    s = e.add_subparsers(dest="exp_cmd", required=True)
    w = s.add_parser("whoami", help="who this machine's researcher is; a name grants nothing")
    w.add_argument("--set", dest="name")
    s.add_parser("waiting", help="everything pending, with the command for each step")
    a = s.add_parser("approve", help="point to a waiting step; approvals stay human-only and are never taken here")
    a.add_argument("item")
    se = s.add_parser("seal", help="seal part of a dataset before the project's first Lab session")
    se.add_argument("project")
    se.add_argument("--data", required=True)
    se.add_argument("--label-column", required=True)
    se.add_argument("--rows", type=int, required=True)
    se.add_argument("--parts", type=int, default=1)
    se.add_argument("--beacon-round", required=True)
    se.add_argument("--beacon", required=True)
    st = s.add_parser("start", help="start a Lab box with an agent inside (key options: `chh lab start`)")
    st.add_argument("agent")
    st.add_argument("--project")
    st.add_argument("--session")
    st.add_argument("--model")
    st.add_argument("--local-model-server", action="store_true",
                    help="OPT-IN: the box may reach the one host port of a local model server")
    for name in ("lab", "stop"):
        s.add_parser(name, help=f"{name} of a Lab session").add_argument("session")
    ex = s.add_parser("explore", help="one prompt to the session's agent")
    ex.add_argument("session")
    ex.add_argument("--prompt", required=True)
    sr = s.add_parser("steer", help="redirect or question the agent; every steer goes into the Story")
    sr.add_argument("session")
    sr.add_argument("--text", required=True)
    sr.add_argument("--no-send", action="store_true", help="record it without sending it")
    fz = s.add_parser("freeze", help="freeze a Lab idea, code and environment")
    fz.add_argument("session")
    fz.add_argument("--request", required=True)
    rh = s.add_parser("rehearse", help="a mock trial on Lab data: shows everything, spends nothing")
    rh.add_argument("promotion")
    rh.add_argument("--rows", type=int, required=True)
    rh.add_argument("--data")
    rh.add_argument("--label-column", default="label")
    for name in ("preview", "promote"):
        p = s.add_parser(name, help="readiness and steps" if name == "preview" else "fix the claim for the Court")
        p.add_argument("promotion")
        p.add_argument("--route", required=True, choices=["generated", "sealed", "public"])
        p.add_argument("--family")
        p.add_argument("--beacon-round")
        p.add_argument("--second-round", help="generated: fix a second fresh test now")
        p.add_argument("--project")
        p.add_argument("--second-part", action="store_true", help="sealed: fix part 2 as the second fresh test now")
        p.add_argument("--targets", help="public: a chh-lab-targets/1 file")
        p.add_argument("--dev-pairs", help="both,only_method,only_baseline,neither (default: the last rehearsal)")
        p.add_argument("--n-test", type=int)
    mi = s.add_parser("mint", help="give the public random value of the committed round")
    mi.add_argument("case")
    mi.add_argument("--beacon", required=True)
    s.add_parser("run", help="the trial, in the Court's network-free sandbox").add_argument("case")
    s.add_parser("case", help="open a case: this counts as a look").add_argument("case")
    s.add_parser("cases", help="your cases; opens none of them")
    s.add_parser("technical", help="the technical details of a case").add_argument("case")
    sh = s.add_parser("share", help="write a reviewer's case file")
    sh.add_argument("case")
    sh.add_argument("--reviewer", required=True)
    sh.add_argument("--out", required=True)
    pa = s.add_parser("paper", help="a living paper from the certificate (target retired)")
    pa.add_argument("case")
    pa.add_argument("--title", required=True)
    pa.add_argument("--body", required=True, help="a LaTeX body with \\chhnum{name} placeholders")
    pa.add_argument("--refs", required=True, help="JSON: placeholder name -> pointer into the certificate")
    rc = s.add_parser("record", help="dead ends, outcomes and calibration")
    rc.add_argument("--query")
    s.add_parser("resolve", help="what a number rests on").add_argument("link")
    s.add_parser("schema", help="the JSON Schema of every request and view")
    dr = s.add_parser("doctor", help="is this machine ready for a first session; what is missing; the next step")
    dr.add_argument("--registry-key", default="anthropic-api", help="the provider entry of the key you will use")
    dr.add_argument("--json", action="store_true")
    su = s.add_parser("setup", help="first-run setup: settings, your name, and what is left to do")
    su.add_argument("--name", help="a name for the record, on this machine")
    su.add_argument("--spend-limit", type=float, help="USD one Lab session may reach before prompts stop")
    su.add_argument("--model", help="the Lab's default model for claude-code")
    bk = s.add_parser("backup", help="HUMAN: a verified copy of the whole store, sealed answers included")
    bk.add_argument("--out", required=True, help="a folder no Lab can read")
    bk.add_argument("--by", required=True)
    s.add_parser("verify-backup", help="check a backup's files and ledger").add_argument("path")
    re = s.add_parser("restore", help="HUMAN: copy a verified backup into an empty folder")
    re.add_argument("path")
    re.add_argument("--to", required=True)
    re.add_argument("--by", required=True)
    up = s.add_parser("upgrade", help="HUMAN: back up, then bring the store to this code's format")
    up.add_argument("--backup-to", required=True)
    up.add_argument("--by", required=True)
    ui = s.add_parser("ui", help="the web interface, on this machine only (docs/ui.md)")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--scripted", action="store_true", help="a scripted example: no store, no agent, no Court")
    to = s.add_parser("toy", help="the minted toy track: runs up to the next step that needs a person")
    to.add_argument("--session", default="toy-1")
    to.add_argument("--agent", default="claude-code")
    to.add_argument("--model")
    to.add_argument("--registry-key", default="anthropic-api")
    to.add_argument("--credential-env", help="host variable CHH_LAB_CRED_<NAME> holding the scoped key")
    to.add_argument("--credential-file", help="file holding the scoped key (outside agent-readable roots)")
    to.add_argument("--spend-cap-usd", type=float, help="the limit set at the provider on this key")
    to.add_argument("--scope", default="", help="where the key lives and what limits it (recorded)")
    to.add_argument("--beacon-round", help="a public random round that has not happened yet")
    to.add_argument("--beacon", help="the published value of that round")
    to.add_argument("--new-freeze", action="store_true", help="freeze again, after steering the agent")


def _pairs(v: str | None) -> tuple[int, int, int, int] | None:
    if v is None:
        return None
    parts = [int(x) for x in v.split(",")]
    if len(parts) != 4:
        raise SystemExit("--dev-pairs needs four counts: both,only_method,only_baseline,neither")
    return parts[0], parts[1], parts[2], parts[3]


def _promote(a: argparse.Namespace, m: Any) -> Any:
    return m.PromoteIn(promotion_id=a.promotion, route=a.route, family=a.family, beacon_round=a.beacon_round,
                       second_round=a.second_round, project=a.project, second_part=a.second_part,
                       targets_file=a.targets, dev_pairs=_pairs(a.dev_pairs), n_test=a.n_test)


def _toy(a: argparse.Namespace, x: Any, m: Any) -> dict[str, Any]:
    from . import toy
    cred = None
    if a.spend_cap_usd is not None and (a.credential_env or a.credential_file):
        cred = m.CredentialIn(registry_key=a.registry_key, spend_cap_usd=a.spend_cap_usd, source_env=a.credential_env,
                              source_file=a.credential_file, scope=a.scope)
    return asdict(toy.run(x, toy.ToyIn(session_id=a.session, agent=a.agent, registry_key=a.registry_key, model=a.model,
                                       credential=cred, beacon_round=a.beacon_round, beacon_value=a.beacon,
                                       new_freeze=a.new_freeze)))


def _doctor(a: argparse.Namespace, x: Any) -> int:
    from . import doctor as d
    checks = d.doctor(x.store, docker=x.docker, environ=x.environ, registry_key=a.registry_key)
    print(json.dumps([asdict(c) for c in checks], indent=1) if a.json else d.render(checks))
    return 1 if d.blocking(checks) else 0


def _call(a: argparse.Namespace, x: Any, m: Any) -> Any:
    c = a.exp_cmd
    if c == "toy":
        return _toy(a, x, m)
    if c == "whoami":
        return x.set_researcher(a.name) if a.name else x.whoami()
    if c == "seal":
        return x.seal(m.SealIn(project=a.project, data_file=a.data, label_column=a.label_column, rows=a.rows,
                               parts=a.parts, beacon_round=a.beacon_round, beacon_value=a.beacon))
    if c == "start":
        return x.start(m.StartIn(agent=a.agent, project=a.project, session_id=a.session, model=a.model,
                                 local_model_server=a.local_model_server))
    if c == "explore":
        return x.explore(m.ExploreIn(session_id=a.session, prompt=a.prompt))
    if c == "steer":
        return x.steer(m.SteerIn(session_id=a.session, text=a.text, send=not a.no_send))
    if c == "freeze":
        return x.freeze(m.FreezeIn(session_id=a.session, request_file=a.request))
    if c == "rehearse":
        return x.rehearse(m.RehearseIn(promotion_id=a.promotion, rows=a.rows, data_file=a.data,
                                       label_column=a.label_column))
    if c == "preview":
        return x.promote_preview(_promote(a, m))
    if c == "promote":
        return x.promote(_promote(a, m))
    if c == "mint":
        return x.mint(a.case, a.beacon)
    if c == "share":
        return x.share(m.ShareIn(case_id=a.case, reviewer=a.reviewer, out_file=a.out))
    if c == "paper":
        return x.paper(m.PaperIn(case_id=a.case, title=a.title, body=Path(a.body).read_text(encoding="utf-8"),
                                 refs=json.loads(Path(a.refs).read_text(encoding="utf-8"))))
    if c == "record":
        return x.record(a.query)
    if c == "schema":
        return m.api_schema()
    one = {"approve": (x.approve, "item"), "lab": (x.lab, "session"), "stop": (x.stop, "session"),
           "run": (x.run_case, "case"), "case": (x.case, "case"), "technical": (x.technical, "case"),
           "resolve": (x.resolve, "link")}
    if c in one:
        fn, arg = one[c]
        return fn(getattr(a, arg))
    return {"waiting": x.waiting, "cases": x.cases}[c]()


def _print_step(step: Any) -> int:
    from .. import backup as B
    from ..settings import SettingsRefused
    try:
        doc = step(B)
    except (B.BackupRefused, SettingsRefused) as e:
        raise SystemExit(f"refused: {e}") from None
    print(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False))
    return 0


def _setup(a: argparse.Namespace, store: EvidenceStore) -> int:
    from .. import settings
    from . import doctor as d
    from .api import Experience, Refused
    try:
        doc = settings.save(session_spend_limit_usd=a.spend_limit, lab_default_model=a.model)
    except settings.SettingsRefused as e:
        raise SystemExit(f"refused: {e}") from None
    x = Experience(store)
    if a.name:
        try:
            x.set_researcher(a.name)
        except Refused as e:
            raise SystemExit(f"refused: {e}") from None
    print(f"Settings: {settings.settings_file()}")
    print(f"  Lab default model: {doc['lab_default_model']}")
    print(f"  Spending limit per Lab session: ${doc['session_spend_limit_usd']:.2f} (also set a limit at the provider)")
    print(f"You are recorded as: {x.whoami().researcher}. A name grants nothing.")
    print("See the screens with no agent and no spending: chh exp ui --scripted")
    print("What is left before a real session:")
    print(d.render(d.doctor(x.store, docker=x.docker, environ=x.environ)))
    return 0


STORE_STEPS = {
    "setup": _setup,
    "backup": lambda a, s: _print_step(lambda b: b.backup(s, Path(a.out), a.by)),
    "verify-backup": lambda a, s: _print_step(lambda b: b.verify(Path(a.path))),
    "restore": lambda a, s: _print_step(lambda b: b.restore(Path(a.path), Path(a.to), a.by)),
    "upgrade": lambda a, s: _print_step(lambda b: b.upgrade(s, Path(a.backup_to), a.by))}


def cmd_exp(a: argparse.Namespace, store: EvidenceStore) -> int:
    from . import schemas as m
    from .api import Experience, Refused
    if a.exp_cmd == "doctor":
        return _doctor(a, Experience(store))
    if a.exp_cmd in STORE_STEPS:
        return STORE_STEPS[a.exp_cmd](a, store)
    if a.exp_cmd == "ui":
        from .scripted import ScriptedExperience
        from .server import serve
        serve(ScriptedExperience if a.scripted else lambda: Experience(store), a.port)
        return 0
    try:
        out = _call(a, Experience(store), m)
    except Refused as e:
        raise SystemExit(f"refused: {e}") from None
    except (OSError, RuntimeError, ValueError) as e:
        if os.environ.get("CHH_DEBUG"):
            raise
        raise SystemExit(f"Something broke on this machine ({type(e).__name__}). Nothing was decided. Run `chh exp "
                         "doctor`; set CHH_DEBUG=1 and run this again to see the technical error.") from None
    doc = out.model_dump(mode="json") if isinstance(out, BaseModel) else out
    print(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False))
    return 0


COMMANDS = {"exp": cmd_exp}
