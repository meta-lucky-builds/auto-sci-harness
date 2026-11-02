"""`chh exp doctor`: is this machine ready for a first session? Every prerequisite of docs/first_discovery_plan.md, what
is missing, and the exact next step. Read-only: it writes nothing, takes no human step, and calls no model. It never
prints a key: for a key it looks at names and whether a value is set.

Three states. `ok`: checked. `missing`: checked, and something is left to do. `yours`: code cannot check it, so it is
listed for a person to confirm.

`needed_for` says what a `missing` row holds up. Only `BOTH` rows stop the toy run (`blocking`).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..court import health as H
from ..court import sealed as Z
from ..court.display import TRUTH_INDEPENDENT
from ..lab.box import LAB_IMAGE, DockerCLI
from ..lab.egress import GATEWAY_IMAGE
from ..lab.walls import static_problems
from ..sandbox.docker import CANDIDATE_IMAGE
from ..store import EvidenceStore
from ..studio.queue import pending
from ..truth_layout import truth_root

OK, MISSING, YOURS = "ok", "missing", "yours"
BOTH, SEALED_TRACK, WHEN_ASKED, WHEN_USED = "both tracks", "sealed-data track", "when a case asks", "only if you use it"
DEFAULT_REGISTRY_KEY = "anthropic-api"
LAB_BUILD = "docker build -f docker/lab.Dockerfile -t custom-harness-lab:py312 docker"
IMAGES = (("Court sandbox image", CANDIDATE_IMAGE,
           "docker build -f docker/sandbox-candidate.Dockerfile -t custom-harness-sandbox:py312-candidate docker"),
          ("Lab gateway image", GATEWAY_IMAGE,
           "docker build -f docker/sandbox.Dockerfile -t custom-harness-sandbox:py312 docker"),
          ("Lab image", LAB_IMAGE, LAB_BUILD))


@dataclass(frozen=True)
class Check:
    name: str
    state: str
    detail: str
    next_step: str | None = None
    needed_for: str = BOTH


def blocking(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.state == MISSING and c.needed_for == BOTH]


def _answers(docker: DockerCLI, args: list[str]) -> bool:
    try:
        return docker.call(args, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker(docker: DockerCLI) -> list[Check]:
    if not _answers(docker, ["version", "--format", "{{.Server.Version}}"]):
        step = "Start Docker Desktop, wait until it says it is running, then run this again."
        return [Check("Docker", MISSING, "The Docker engine does not answer.", step),
                *(Check(n, MISSING, "Not checked: Docker does not answer.", s) for n, _, s in IMAGES)]
    rows = [Check("Docker", OK, "The Docker engine answers.")]
    for name, image, step in IMAGES:
        here = _answers(docker, ["image", "inspect", "--format", "{{.Id}}", image])
        rows.append(Check(name, OK, f"{image} is here.") if here else Check(name, MISSING, f"{image} is not here.", step))
    return rows


def _walls() -> Check:
    problems = static_problems()
    if not problems:
        return Check("Lab walls", OK, "The lab root, the image recipe and the truth layout pass.")
    return Check("Lab walls", MISSING, "; ".join(problems)[:600], "chh lab check")


def _keys(environ: Mapping[str, str]) -> Check:
    names = sorted(k for k, v in environ.items() if k.startswith("CHH_LAB_CRED_") and v)
    if names:
        return Check("Scoped key", OK, f"Set in this terminal: {', '.join(names)}. Values are not shown.")
    return Check("Scoped key", MISSING, "No CHH_LAB_CRED_* variable is set in this terminal.",
                 "Create a scoped, spend-capped key at the provider, then in this terminal: "
                 "$env:CHH_LAB_CRED_ANTHROPIC = '<paste the key>'  (docs/first_session_checklist.md step 3)")


def _queue_row(name: str, items: list[dict[str, Any]], *, done: str, needed_for: str = BOTH) -> Check:
    if not items:
        return Check(name, OK, done, None, needed_for)
    return Check(name, MISSING, f"{len(items)} waiting for a person.", str(items[0]["decide"]), needed_for)


def _queue(items: list[dict[str, Any]], registry_key: str) -> list[Check]:
    def kinds(*wanted: str) -> list[dict[str, Any]]:
        return [i for i in items if i["kind"] in wanted]

    ml = [i for i in kinds("adapter") if i.get("name") == "ml"]
    others = [i for i in kinds("adapter") if i.get("name") != "ml"]
    provider = [i for i in kinds("provider_signoff") if i.get("key") == registry_key]
    rows = [_queue_row("Adapter approval (ml)", ml, done="The ml adapter is approved."),
            _queue_row("Other adapters (lp, sim, circuit)", others, done="All approved.", needed_for=WHEN_USED),
            _queue_row(f"Provider sign-off ({registry_key})", provider, done="Signed off within the allowed age."),
            _queue_row("Human-held check", kinds("human_check", "human_check_run"),
                       done="Registered, with a passing run of its exact file."),
            _queue_row("Role approvals", kinds("role"), done="None waiting.", needed_for=WHEN_ASKED)]
    broken = kinds("queue_error")
    if broken:
        rows.append(Check("The list of human steps", MISSING, str(broken[0].get("error")), "chh queue"))
    return rows


def _health(store: EvidenceStore) -> Check:
    if H.passing(store) is None:
        return Check("Court health run", MISSING, "No passing run for this Court version.",
                     "chh court health --docker")
    return Check("Court health run", OK, "A passing run exists for this Court version.")


def _dataset(store: EvidenceStore) -> Check:
    if any(r.get("event") == "sealed" for r in store.records(Z.RECORD_KIND)):
        return Check("Sealed dataset", OK, "A project is sealed.", None, SEALED_TRACK)
    return Check("Sealed dataset", MISSING, "No project is sealed yet.",
                 "chh lab seal PROJECT --data FILE --label-column C --rows N --beacon-round R --beacon VALUE",
                 SEALED_TRACK)


def _os_user(root: Path) -> Check:
    step = "Set this up before sealing private data: docs/lab.md section 7 step 3."
    try:
        with os.scandir(root):
            return Check("Separate OS user", MISSING, f"This user can read {root}.", step, SEALED_TRACK)
    except FileNotFoundError:
        return Check("Separate OS user", YOURS, f"{root} does not exist yet, so nothing shows who can read it.", step,
                     SEALED_TRACK)
    except PermissionError:
        return Check("Separate OS user", OK, f"This user cannot read {root}.", None, SEALED_TRACK)


def doctor(store: EvidenceStore, *, docker: DockerCLI | None = None, environ: Mapping[str, str] | None = None,
           registry_key: str = DEFAULT_REGISTRY_KEY) -> list[Check]:
    """Every prerequisite, in the order a person should work through them."""
    items = pending(store)
    return [*_docker(docker or DockerCLI()), _walls(), _keys(os.environ if environ is None else environ),
            *_queue(items, registry_key), _health(store),
            Check("Feedback table", YOURS, f"Shown in full to the researcher: {', '.join(TRUTH_INDEPENDENT)}.",
                  "Read TRUTH_INDEPENDENT in src/custom_harness/court/display.py and decide it is right."),
            Check("Public random source", YOURS, "Typed by a person; code does not check it offline.",
                  "Pick a drand round that has not happened yet: https://drand.love (see the checklist).", WHEN_ASKED),
            _dataset(store), _os_user(truth_root())]


def render(checks: list[Check]) -> str:
    """The guided text of `chh exp doctor`."""
    marks = {OK: "ok     ", MISSING: "MISSING", YOURS: "YOURS  "}
    lines = []
    for c in checks:
        lines.append(f"[{marks[c.state]}] {c.name} ({c.needed_for}): {c.detail}")
        if c.next_step:
            lines.append(f"          next: {c.next_step}")
    left = blocking(checks)
    lines.append(f"{len(left)} step(s) stand before a first session." if left else "Nothing stands before a first session.")
    return "\n".join(lines)
