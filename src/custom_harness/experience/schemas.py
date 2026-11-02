"""Typed requests and responses of the Experience API (GOAL 18.5, 18.7: the UI is typed end to end from these).

Every number on a main view is a `Number`: a value and the link that says what it rests on (GOAL 18.3), which
`Experience.resolve` follows. `MAIN_VIEWS` is the list a test walks; `TechnicalView` is the one view outside the jargon
budget. `api_schema()` is what docs/experience_api.schema.json holds.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Number(Model):
    value: float | int | None  # None = unknown, never a silent zero
    link: str


# ---------------------------------------------------------------- requests
class SealIn(Model):
    project: str
    data_file: str
    label_column: str
    rows: int = Field(gt=0)
    parts: int = Field(default=1, ge=1, le=2)
    beacon_round: str
    beacon_value: str


class CredentialIn(Model):
    """Where the scoped key is, never the key itself."""

    registry_key: str
    spend_cap_usd: float = Field(gt=0)
    source_env: str | None = None
    source_file: str | None = None
    scope: str = ""
    ttl_hours: float = Field(default=24.0, gt=0)


class StartIn(Model):
    agent: str
    project: str | None = None
    session_id: str | None = None
    model: str | None = None
    registry_key: str | None = None
    box_env: str | None = None
    credential: CredentialIn | None = None
    local_model_server: bool = False  # opt-in: the box may reach one port of this machine (docs/lab.md section 11)


class ExploreIn(Model):
    session_id: str
    prompt: str = Field(min_length=1)


class SteerIn(Model):
    session_id: str
    text: str = Field(min_length=1)
    send: bool = True  # False: recorded in the Story only


class FreezeIn(Model):
    session_id: str
    request_file: str


class RehearseIn(Model):
    promotion_id: str
    rows: int = Field(gt=0)
    data_file: str | None = None  # default: the Lab's part of the session's sealed project
    label_column: str = "label"


class PromoteIn(Model):
    promotion_id: str
    route: Literal["generated", "sealed", "public"]
    family: str | None = None  # generated
    beacon_round: str | None = None  # generated
    second_round: str | None = None  # generated: a second fresh test, fixed now
    project: str | None = None  # sealed
    second_part: bool = False  # sealed: part 2 as the second fresh test, fixed now
    targets_file: str | None = None  # public
    dev_pairs: tuple[int, int, int, int] | None = None  # default: the last rehearsal's counts
    n_test: int | None = None

    @model_validator(mode="after")
    def _route_fields(self) -> PromoteIn:
        need = {"generated": ("family", "beacon_round"), "sealed": ("project",), "public": ("targets_file",)}
        missing = [f for f in need[self.route] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"route {self.route!r} needs {missing}")
        return self


class ShareIn(Model):
    case_id: str
    reviewer: str = Field(min_length=1)
    out_file: str


class PaperIn(Model):
    case_id: str
    title: str
    body: str
    refs: dict[str, str]  # placeholder -> JSON pointer into the certificate


# ---------------------------------------------------------------- views
class Identity(Model):
    os_user: str
    researcher: str
    chosen: bool  # False: the OS user, no name chosen yet
    note: str = "A name for the record, on this machine. It grants nothing."


class WaitingItem(Model):
    id: str
    title: str
    why: str
    human_only: bool
    optional: bool = False
    command: str


class Waiting(Model):
    items: tuple[WaitingItem, ...]
    total: Number


class ApprovalRoute(Model):
    id: str
    title: str
    human_only: bool
    command: str
    performed: Literal[False] = False
    note: str
    binds: dict[str, Any]


class Sealed(Model):
    project: str
    lab_rows: Number
    sealed_rows: tuple[Number, ...]
    note: str


class Session(Model):
    session_id: str
    project: str | None
    agent: str | None
    box: str
    workspace: str
    notice: str


class Idea(Model):
    idea_id: str
    title: str
    status: str


class LabView(Model):
    session_id: str
    project: str | None
    notice: str
    ideas: tuple[Idea, ...]
    open_ideas: Number
    dropped_ideas: Number
    runs: Number
    steers: Number
    web_connections: Number
    cost_usd: Number
    costs_unknown: Number


class Explored(Model):
    session_id: str
    finished: bool
    reply: str
    actions: Number
    notice: str


class Steered(Model):
    session_id: str
    event_id: str
    sent: bool
    reply: str | None
    court_results_seen: Number
    note: str


class Frozen(Model):
    promotion_id: str
    session_id: str
    files: Number
    note: str


class ReadinessRow(Model):
    test_rows: Number
    chance: Number
    objection: bool


class Readiness(Model):
    sentence: str
    notice: str
    rows: tuple[ReadinessRow, ...]


class CheckRow(Model):
    name: str
    status: str


class ScoreRow(Model):
    arm: Literal["method", "baseline"]
    seed: str
    score: Number


class Rehearsal(Model):
    promotion_id: str
    notice: str
    ran: bool
    would_certify: bool | None
    reason: str | None  # why it could not run: the Lab's own data, so in full
    checks: tuple[CheckRow, ...]  # every check: Lab data, nothing held back
    scores: tuple[ScoreRow, ...]
    readiness: Readiness


class PromotePreview(Model):
    promotion_id: str
    route: str
    finding_possible: bool
    readiness: Readiness
    looks_remaining: Number
    what_happens: tuple[str, ...]


class Promoted(Model):
    promotion_id: str
    case_id: str | None
    second_case_id: str | None
    route: str
    next_step: str
    command: str


class Badge(Model):
    name: str
    form: str
    meaning: str


class Story(Model):
    session_id: str
    idea_path: tuple[str, ...]
    steps: Number
    steers: Number
    web_connections: Number
    intact: bool


class Case(Model):
    case_id: str
    title: str
    sentence: str
    scope: str
    verdict: Literal["CERTIFIED", "NOT CERTIFIED", "INCONCLUSIVE", "COULD NOT RUN"] | None
    verdict_sentence: str
    reason_code: str | None
    reason: str | None  # why a trial could not run, when nothing was spent
    badges: tuple[Badge, ...]
    second_test: str | None
    checks_in_full: tuple[CheckRow, ...]
    looks_remaining: Number
    test_rows: Number
    retired: bool
    next_step: str | None
    command: str | None
    rerun: str | None  # None until the target is retired, or for a claim that was not certified
    story: Story | None
    technical: str  # the link to the technical view


class CaseLine(Model):
    case_id: str
    title: str
    state: str
    looks_remaining: Number


class Cases(Model):
    cases: tuple[CaseLine, ...]
    total: Number


class Shared(Model):
    case_id: str
    reviewer: str
    path: str
    note: str


class Paper(Model):
    case_id: str
    path: str
    numbers: Number
    note: str


class DeadEnd(Model):
    title: str
    reason: str
    source: Literal["idea", "verdict"]


class Outcome(Model):
    case_id: str
    title: str
    verdict: str
    badges: str
    reason_code: str | None


class Calibration(Model):
    claims: Number
    mean_squared_error: Number
    evidence_bits: Number
    note: str


class Record(Model):
    researcher: str
    dead_ends: tuple[DeadEnd, ...]
    outcomes: tuple[Outcome, ...]
    verdicts_unopened: Number
    calibration: Calibration
    harness_improvement: str


class Resolved(Model):
    link: str
    value: float | int | None
    rests_on: str
    source: dict[str, Any]


class TechnicalView(Model):
    """The technical details, as the Court gives them. It shows nothing `display.view` does not."""

    case_id: str
    details: dict[str, Any]


class SessionLine(Model):
    session_id: str
    project: str | None
    agent: str | None
    box: str


class Sessions(Model):
    sessions: tuple[SessionLine, ...]
    total: Number


class LabLine(Model):
    """One line of a session's Story, as a person reads it. `at` is a time of day as text, not a measured number."""

    at: str
    who: Literal["you", "agent", "system"]
    kind: str
    text: str


class Reading(Model):
    source: str
    query: str
    found: Number


class LabLog(Model):
    session_id: str
    lines: tuple[LabLine, ...]
    reading: tuple[Reading, ...]  # the literature drawer: what the agent searched
    web: tuple[str, ...]  # the hosts it reached, most recent last


class FrozenIdea(Model):
    promotion_id: str
    session_id: str
    title: str
    sentence: str


class FrozenIdeas(Model):
    items: tuple[FrozenIdea, ...]
    total: Number


class TrustRow(Model):
    name: str
    state: Literal["ok", "missing", "yours"]
    detail: str
    next_step: str | None
    needed_for: str


class TrustBoard(Model):
    rows: tuple[TrustRow, ...]
    steps_left: Number
    limits: tuple[str, ...]  # what is not true yet, in plain words


class Meta(Model):
    """What is answering the UI. `scripted`: nothing it shows came from the Court, whatever a page looks like."""

    scripted: bool
    notice: str | None
    researcher: str


MAIN_VIEWS: tuple[type[Model], ...] = (
    Identity, Waiting, Sealed, Session, LabView, Explored, Steered, Frozen, Rehearsal, PromotePreview, Promoted, Case,
    Cases, Shared, Paper, Record, Sessions, LabLog, FrozenIdeas, TrustBoard, Meta)
REQUESTS: tuple[type[Model], ...] = (SealIn, CredentialIn, StartIn, ExploreIn, SteerIn, FreezeIn, RehearseIn, PromoteIn,
                                     ShareIn, PaperIn)
OTHER_VIEWS: tuple[type[Model], ...] = (ApprovalRoute, Resolved, TechnicalView)


def api_schema() -> dict[str, Any]:
    """The JSON Schema of every request and view."""
    return {m.__name__: m.model_json_schema() for m in (*REQUESTS, *MAIN_VIEWS, *OTHER_VIEWS)}
