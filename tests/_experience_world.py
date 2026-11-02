"""A full session on a throwaway store, for the Experience API tests: real kerneld, real Kernel, scripted programs, a
fake docker for the Lab box and the local sandbox for the arms. No model is called. The human steps (adapter approval,
role approval, retirement) are played by the fixture on this store only, outside the API, as a person would do them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _lab_fakes import FakeDocker, docker
from custom_harness.experience import Experience
from custom_harness.experience import schemas as M
from custom_harness.experience.words import jargon_problems
from custom_harness.kernel import Kernel
from custom_harness.lab import session as S
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from test_sealed_project import ARMS, BEACON, health_stand_in, rings_csv

REQUEST = """format: chh-lab-promote/1
title: Radius rule
sentence: The radius rule beats the most common label by at least 0.1 accuracy.
method: method.py
baseline: baseline.py
lock: requirements.lock
idea: radius
claim: {metric: accuracy, min_effect: 0.1, probability: 0.6, seeds: [0, 1, 2], n_boot: 1000, resolve_by: 2030-01-01}
budget: {timeout_s: 120}
time_window: {start: 2024-01-01, end: 2025-12-31}
"""


def walk(obj: Any) -> list[M.Number]:
    """Every Number inside a view."""
    if isinstance(obj, M.Number):
        return [obj]
    if isinstance(obj, M.Model):
        return [n for k in type(obj).model_fields for n in walk(getattr(obj, k))]
    if isinstance(obj, tuple):
        return [n for v in obj for n in walk(v)]
    return []


class World:
    def __init__(self, root: Path, *, health: bool = True):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.store = EvidenceStore(root / "store")
        Kernel(self.store).approve_adapter("ml", approver="fixture-human")
        if health:
            health_stand_in(self.store)
        self.fake = FakeDocker()
        self.n = 0
        self.x = Experience(self.store, docker=docker(self.fake), sandbox=lambda _root: self.factory)
        self.data = root / "private.csv"
        self.data.write_bytes(rings_csv())
        self.request = root / "promote.yaml"
        self.request.write_text(REQUEST, encoding="utf-8")

    def factory(self, key: str) -> LocalSandbox:
        self.n += 1
        return LocalSandbox(self.root / "sb" / f"{self.n}-{key}", i_understand_no_isolation=True)

    def seal(self, project: str = "cohort-1", parts: int = 2) -> M.Sealed:
        return self.x.seal(M.SealIn(project=project, data_file=str(self.data), label_column="outcome", rows=400,
                                    parts=parts, beacon_round="r1", beacon_value=BEACON))

    def lab_work(self, sid: str = "s-exp") -> None:
        """What a scripted agent leaves in its workspace: the code and two reported ideas, one dropped."""
        ws = S.workspace_of(sid)
        (ws / "method.py").write_bytes((ARMS / "knn_radius.py").read_bytes())
        (ws / "baseline.py").write_bytes((ARMS / "majority.py").read_bytes())
        (ws / "requirements.lock").write_bytes(b"x==1\n")
        (ws / ".lab").mkdir(exist_ok=True)
        lines = [{"kind": "idea", "client_id": "1",
                  "payload": {"op": "propose", "idea_id": "radius", "title": "Radius"}},
                 {"kind": "idea", "client_id": "2", "payload": {"op": "propose", "idea_id": "angle", "title": "Angle"}},
                 {"kind": "idea", "client_id": "3", "payload": {"op": "abandon", "idea_id": "angle",
                                                                "reason": "the angle carries no signal"}}]
        (ws / ".lab" / "trace.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    def session(self, sid: str = "s-exp", project: str | None = "cohort-1") -> M.Session:
        out = self.x.start(M.StartIn(agent="opencode", project=project, session_id=sid, model="m",
                                     local_model_server=True))
        self.lab_work(sid)
        return out

    def freeze(self, sid: str = "s-exp") -> str:
        return self.x.freeze(M.FreezeIn(session_id=sid, request_file=str(self.request))).promotion_id

    def approve_role(self, target: str) -> None:
        Kernel(self.store).approve_role(target, approver="fixture-human", note="fixture")

    def clean(self, view: M.Model) -> None:
        """The two rules of a main view: no internal term, and every number resolves to the same value."""
        assert jargon_problems(view.model_dump(mode="json")) == []
        for n in walk(view):
            assert self.x.resolve(n.link).value == n.value, n.link

    def close(self) -> None:
        self.store.close()
