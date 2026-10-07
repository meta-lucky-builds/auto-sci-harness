"""Hypothesis strategies for abstract histories, and the differential runner (model vs real Kernel)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import kernel_model as model
from history import ApproveRole, Artifact, Certify, ClaimSpec, Dataset, Event, History, Prereg, Read, Record
from hypothesis import strategies as st
from real_kernel import fresh

ARTIFACTS = (Artifact("m1", "method"), Artifact("m2", "method"), Artifact("b1", "baseline"))
PURPOSES = ("develop", "evaluate", "audit", "kernel", "leakscan", "reproduce")
SEEDS = (0, 1, 2)


@st.composite
def datasets(draw: Any, n: int) -> list[Dataset]:
    out = []
    for i in range(n):
        start = draw(st.sampled_from((0, 20, 40, 100 + 100 * i)))
        out.append(Dataset(id=f"d{i}", role=draw(st.sampled_from(("dev", "test", "sealed", "external", "future"))),
                           kind=draw(st.sampled_from(("features", "features", "labelled"))),
                           examples=frozenset(range(start, start + 40)),
                           private=draw(st.sampled_from((None, "A", "B", "C", f"own{i}", f"own{i}")))))
    return out


@st.composite
def claims(draw: Any, ids: list[str], n: int) -> list[ClaimSpec]:
    out = []
    for i in range(n):
        targets = tuple(draw(st.lists(st.sampled_from(ids), min_size=1, max_size=2, unique=True)))
        dev = tuple(draw(st.lists(st.sampled_from(ids), max_size=1)))
        out.append(ClaimSpec(key=f"c{i}", kind=draw(st.sampled_from(("improvement",) * 4 + ("negative_result",))),
                             subject=draw(st.sampled_from(("m1", "m2"))),
                             comparator=draw(st.sampled_from(("b1", "b1", "b1", "m2"))),
                             development=dev, targets=targets, seeds=SEEDS, alpha=0.05,
                             n_comparisons=draw(st.integers(1, 3)), min_effect=0.1, strong=draw(st.booleans())))
    return out


def honest_records(c: ClaimSpec) -> tuple[Record, ...]:
    return tuple(Record(arm, s, t) for t in c.targets for s in c.seeds for arm in ("subject", "comparator"))


def eval_reads(c: ClaimSpec) -> list[Read]:
    return [Read(r.target, "evaluate", c.key, r.arm, r.seed) for r in honest_records(c)]


@st.composite
def record_set(draw: Any, c: ClaimSpec, ids: list[str]) -> tuple[Record, ...]:
    recs = list(honest_records(c))
    what = draw(st.sampled_from(("honest",) * 5 + ("drop", "dup", "stray_seed", "stray_target", "none")))
    if what == "drop":
        recs.pop(draw(st.integers(0, len(recs) - 1)))
    elif what == "dup":
        recs.append(recs[draw(st.integers(0, len(recs) - 1))])
    elif what == "stray_seed":
        recs.append(Record("subject", 7, c.targets[0]))
    elif what == "stray_target":
        recs.append(Record("subject", 0, draw(st.sampled_from(ids))))
    elif what == "none":
        recs = []
    return tuple(recs)


@st.composite
def scripts(draw: Any) -> list[Event]:
    """A script: datasets and artifacts first (some datasets late), then 3-10 random actions."""
    ds = draw(datasets(draw(st.integers(2, 4))))
    ids = [d.id for d in ds]
    cs = draw(claims(ids, draw(st.integers(1, 3))))
    late = draw(st.lists(st.sampled_from(ds), max_size=1, unique=True))
    events: list[Event] = [d for d in ds if d not in late] + list(ARTIFACTS)
    for _ in range(draw(st.integers(3, 10))):
        kind = draw(st.sampled_from(("prereg", "prereg", "evalall", "evalall", "certify", "certify", "read",
                                     "approve", "late")))
        c = draw(st.sampled_from(cs))
        if kind == "prereg":
            events.append(Prereg(c))
        elif kind == "evalall":
            events += eval_reads(c)
        elif kind == "certify":
            events.append(Certify(c, draw(record_set(c, ids))))
        elif kind == "read":
            purpose = draw(st.sampled_from(PURPOSES))
            ck = draw(st.sampled_from([None, *[x.key for x in cs]]))
            events.append(Read(draw(st.sampled_from(ids)), purpose, ck, draw(st.sampled_from(("subject",
                               "comparator"))) if purpose == "evaluate" else None,
                               draw(st.sampled_from(SEEDS)) if purpose == "evaluate" else None))
        elif kind == "approve":
            ext = [d.id for d in ds if d.role in ("external", "future")]
            if ext:
                events.append(ApproveRole(draw(st.sampled_from(ext))))
        elif late:
            events.append(late.pop())
    events += [d for d in late]
    return events


@dataclass(frozen=True)
class Disagreement:
    where: str  # "read" or a check id / "label" / "family"
    model: Any
    real: Any
    step: int


def model_verdict(history: History, c: ClaimSpec, records: tuple[Record, ...]) -> model.Verdict:
    registered = any(isinstance(e, Prereg) and e.claim.key == c.key for e in history)
    return model.certify(history, c.key, records) if registered else model.certify_unregistered(history, c, records)


def run(script: list[Event]) -> tuple[History, list[Disagreement], int]:
    """Replay a script on both sides; returns (logged history, disagreements, number of certifications)."""
    world, tmp = fresh()
    history: list[Event] = []
    out: list[Disagreement] = []
    n_cert = 0
    try:
        for i, ev in enumerate(script):
            h = tuple(history)
            if isinstance(ev, Dataset) and ev.id in world.datasets:
                continue
            if isinstance(ev, Read):
                want = model.read_allowed(h, ev)
                logged = world.apply(ev)
                if want != bool(logged):
                    out.append(Disagreement("read", want, bool(logged), i))
                history += logged
                continue
            if isinstance(ev, Certify):
                expect = model_verdict(h, ev.claim, ev.records)
                logged = world.apply(ev)
                got = world.last
                n_cert += 1
                for k in expect.checks:
                    if expect.checks[k] != got.checks[k]:
                        out.append(Disagreement(k, expect.checks[k], got.checks[k], i))
                if expect.label != got.label:
                    out.append(Disagreement("label", expect.label, got.label, i))
                if got.family and expect.family != got.family:
                    out.append(Disagreement("family", expect.family, got.family, i))
                history += logged
                continue
            history += world.apply(ev)
    finally:
        world.close()
        tmp.cleanup()
    return tuple(history), out, n_cert
