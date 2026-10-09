"""`chh evolve branches` / `chh evolve route` (GOAL §5 L6.9): read-only views of the branch records and of the
router over a trunk. Nothing here runs a harness, reads a task directory or touches the sealed split:

- `branches SPEC`: every branch record made against the trunk SPEC, whitelisted fields only (id, spec hash, cluster
  key, won clusters, whether its LOFO/LODO bit passed, whether the router routes it). No gate value exists in a
  branch record (branches.check_record), and none is printed.
- `route SPEC --suite SUITE`: the route table (router.build_router: ledger-verified verdict bits only) and, per task
  of the suite, the spec hash its PUBLIC descriptor (family / domain) routes to. The suite file is read for ids and
  descriptors only (no split-isolation check: this is a view, not a campaign; `chh evolve` still runs load_suite).
A refused router is printed with its reason and exits 2 (fail closed: nothing is routed).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .branches import BRANCH_KIND, CLUSTER_KEYS, BranchRefused, check_record
from .evaluate import SuiteTask
from .router import RouteRefused, build_router

SUBCOMMANDS = ("branches", "route")


def _trunk(path: str) -> HarnessSpec:
    return HarnessSpec.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def _descriptors(path: str) -> list[SuiteTask]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [SuiteTask(str(t["id"]), Path(str(t.get("dir", "."))), t.get("split", "sealed"),
                      family=str(t.get("family", "")), domain=str(t.get("domain", "ml"))) for t in doc["tasks"]]


def cmd_branches(a: argparse.Namespace, store: EvidenceStore) -> int:
    trunk = _trunk(a.spec)
    h = trunk.spec_hash()
    try:
        routes = dict(build_router(store, trunk, cluster_key=a.cluster_key).routes)
        refused = None
    except RouteRefused as e:
        routes, refused = {}, str(e)
    out: list[dict[str, Any]] = []
    for rec in store.records(BRANCH_KIND):
        if not isinstance(rec, dict) or rec.get("trunk") != h:
            continue
        try:
            check_record(rec)
            ok = True
        except BranchRefused:
            ok = False
        out.append({"branch_id": str(rec.get("branch_id")), "spec_hash": str(rec.get("spec_hash")),
                    "cluster_key": str(rec.get("cluster_key")),
                    "won": [str(c) for c in rec.get("won", [])] if ok else [],
                    "lofo_passed": ok and rec["lofo"]["passed"] is True,
                    "routable": ok and rec.get("spec_hash") in routes.values()})
    print(json.dumps({"trunk": h, "branches": out, **({"router_refused": refused} if refused else {})},
                     indent=1, sort_keys=True))
    return 0


def cmd_route(a: argparse.Namespace, store: EvidenceStore) -> int:
    trunk = _trunk(a.spec)
    try:
        router = build_router(store, trunk, cluster_key=a.cluster_key)
    except RouteRefused as e:
        print(json.dumps({"trunk": trunk.spec_hash(), "router_refused": str(e)}, indent=1))
        return 2
    tasks: dict[str, str] = {}
    if a.suite:
        for t in _descriptors(a.suite):
            try:
                tasks[t.task_id] = router.select(t)
            except RouteRefused as e:
                tasks[t.task_id] = f"refused: {e}"
    print(json.dumps({"trunk": router.trunk, "cluster_key": router.cluster_key, "routes": dict(router.routes),
                      "table_hash": router.table_hash(), "tasks": tasks}, indent=1, sort_keys=True))
    return 0


def main(argv: list[str], store: EvidenceStore) -> int:
    p = argparse.ArgumentParser(prog="chh evolve")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("branches", help="branch records made against a trunk spec (read-only)")
    b.add_argument("spec")
    b.add_argument("--cluster-key", default="family", choices=CLUSTER_KEYS)
    r = sub.add_parser("route", help="the router over a trunk spec, and the route of each task of a suite")
    r.add_argument("spec")
    r.add_argument("--suite", help="suite YAML (ids and family/domain are read; nothing else)")
    r.add_argument("--cluster-key", default="family", choices=CLUSTER_KEYS)
    a = p.parse_args(argv)
    return {"branches": cmd_branches, "route": cmd_route}[a.cmd](a, store)
