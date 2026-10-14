"""Red team (s3-evolve__redteam, 2026-10-09) against L6.9 branch/route and L6.11 temporal reveal, plus the
proposer-visible gate stage of L6.8 (GOAL §5 L6, invariants 2, 6, 16, 18).

Each `test_attack_*` below states the attack. A test that FAILS on the code it was written against is a live
finding (the failure is the finding); a `test_killed_*` passes and carries its own re-introduction check (a mutant
that the same check must catch). No model calls: scripted outcomes (test_router.Script reads each gate target
through the Kernel vault, so the release ledger and the target attribution are the real ones)."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_harness.evolve import SuiteTask
from custom_harness.evolve.branches import BRANCH_KIND, BranchConfig, propose_branch
from custom_harness.evolve.reveal import LabelItem, RevealRefused, TemporalReveal, label_time, watermark_time
from custom_harness.evolve.router import RouteRefused, build_router
from custom_harness.evolve.selection import non_regression, paired_sample
from custom_harness.kernel import DatasetRole, Kernel
from custom_harness.kerneld.releases import Releases, released
from custom_harness.store import EvidenceStore
from test_reveal import NOW, _ids, _stream
from test_router import CA, CB, NAMES, SEEDS, TRUNK, Script, _branches, _gate_tasks, _spec


def _second_store(tmp_path: Path) -> EvidenceStore:
    s = EvidenceStore(tmp_path / "store-b")
    Kernel(s).approve_adapter("ml", approver="pytest")
    return s


# ================================================================================ L6.9 branch records and the router
def _lofo_suite(tmp_path):
    """test_branch_route_lofo's suite: family E evolved (domain ml), H a held-out family inside ml, B the held-out
    domain bio."""
    return ([SuiteTask(f"e{i}", tmp_path, "evolve", family="E", domain="ml") for i in range(2)]
            + [SuiteTask(f"gE{i}", tmp_path, "gate", family="E", domain="ml") for i in range(4)]
            + [SuiteTask(f"gH{i}", tmp_path, "gate", family="H", domain="ml") for i in range(2)]
            + [SuiteTask(f"gB{i}", tmp_path, "gate", family="B", domain="bio") for i in range(3)])


def _lofo_score(h_delta: float):
    def score(name, family, task, seed):
        j = 0.01 * ((sum(map(ord, task)) + 3 * seed) % 5)
        if name == "T":
            return 0.5 + j
        return 0.5 + {"E": 1.0, "H": h_delta, "B": -0.1}[family] + 0.01 * ((sum(map(ord, task)) + seed) % 3)
    return score


def _domain_branch(store, tasks, h_delta):
    sc = Script(store, tasks, score=_lofo_score(h_delta))
    return propose_branch(TRUNK, CA, tasks, seeds=SEEDS, outcome=sc.outcome, store=store,
                          releases=Releases.live(store), globally_accepted=False,
                          cfg=BranchConfig(cluster_key="domain"))


def test_attack_record_claiming_a_passing_lofo_bit_whose_released_bit_was_false_is_routed(store, tmp_path):
    """ATTACK (invariant 16 via a store record): the candidate wins domain ml while regressing on the held-out family
    H, so propose_branch releases its LOFO bit as False and records no branch. A `branch` record carrying the SAME
    branch id, notes and targets but `lofo.passed: True` (byte-identical to the record an honest world would have
    written) is appended to the store. The router's ledger re-check only asks whether a release with that NOTE
    exists on each target; it never learns the released VALUE, so it routes ml -> candidate: the held-out family H is
    served by a harness whose leave-one-family-out bit was 0."""
    tasks = _lofo_suite(tmp_path)
    honest = _second_store(tmp_path)  # the template: the same campaign in a world where H does not regress
    template = _domain_branch(honest, tasks, h_delta=0.1)
    honest.close()
    assert template is not None and template["lofo"]["passed"] is True

    assert _domain_branch(store, tasks, h_delta=-0.2) is None  # the real world: LOFO fails, no branch
    lofo_releases = [b for b in released(store) if b["note"] == template["lofo"]["note"]]
    assert {b["dataset_id"] for b in lofo_releases} == set(template["lofo"]["targets"])  # the bit WAS released (0)
    store.add_harness_version(CA.id, CA.model_dump(mode="json"), parent_hash=TRUNK.spec_hash())  # the loop does this
    store.append_record(BRANCH_KIND, template)

    try:
        routes = dict(build_router(store, TRUNK, cluster_key="domain").routes)
    except RouteRefused:
        return  # killed: the router refuses a record whose bit value the ledger does not back
    assert "ml" not in routes, (
        "a branch whose released leave-one-family-out bit was 0 is routed on the held-out family's domain: the router "
        f"checks that a release with the note exists, not its value (routes {routes})")


def test_attack_record_reuses_another_branchs_released_bits_for_an_untested_spec_on_another_trunk(store, tmp_path):
    """ATTACK: branch_id (whose notes the ledger re-check looks up) is not bound to (trunk, spec_hash) in
    check_record. A record copied from the real branch (TRUNK, CA) with `trunk` set to another trunk T2 and
    `spec_hash` set to a spec X that was never compared with anything keeps every note and target, so every
    ledger lookup succeeds and the router over T2 routes cluster A to X."""
    tasks = _gate_tasks(tmp_path)
    (ra, _), _ = _branches(store, tasks)
    t2, evil = _spec("trunk-two"), _spec("never-tested")
    store.add_harness_version(evil.id, evil.model_dump(mode="json"), parent_hash=t2.spec_hash())
    forged = json.loads(json.dumps(ra)) | {"trunk": t2.spec_hash(), "spec_hash": evil.spec_hash()}
    store.append_record(BRANCH_KIND, forged)
    try:
        routes = dict(build_router(store, t2).routes)
    except RouteRefused:
        return  # killed
    assert evil.spec_hash() not in routes.values(), (
        f"an untested spec is routed on another trunk with bits released for ({TRUNK.spec_hash()[:12]}, "
        f"{CA.spec_hash()[:12]}): {routes}")


def _spanning_suite(tmp_path):
    """Two domains; the held-out family H spans BOTH (4 tasks in ml, 4 in bio); bio is the held-out domain (G, H)."""
    return ([SuiteTask(f"e{i}", tmp_path, "evolve", family="E", domain="ml") for i in range(2)]
            + [SuiteTask(f"gE{i}", tmp_path, "gate", family="E", domain="ml") for i in range(4)]
            + [SuiteTask(f"gHm{i}", tmp_path, "gate", family="H", domain="ml") for i in range(4)]
            + [SuiteTask(f"gHb{i}", tmp_path, "gate", family="H", domain="bio") for i in range(4)]
            + [SuiteTask(f"gG{i}", tmp_path, "gate", family="G", domain="bio") for i in range(4)])


R, EPS = 0.11, 0.1


def _spanning_score(name, family, task, seed):
    """Trunk 0.5 + jitter. CA: +1.0 on E, -R on H inside ml, -0.1 on G, ~0 on H inside bio. CB: the mirror image
    (+1.0 on G, -R on H inside bio, -0.1 on E, ~0 on H inside ml). A tiny per-unit jitter keeps intervals proper."""
    k = sum(map(ord, task)) + 3 * seed
    base = 0.5 + 0.01 * (k % 5)
    if name == "T":
        return base
    j = 0.002 * ((k + 1) % 3)
    in_ml = task.startswith(("gE", "gHm", "e"))
    delta = {"CA": {"E": 1.0, "G": -0.1, "H": -R if in_ml else 0.0},
             "CB": {"E": -0.1, "G": 1.0, "H": 0.0 if in_ml else -R}}[name][family]
    return base + delta + j


def test_attack_two_routes_each_lofo_checked_alone_compose_into_an_unchecked_regression(store, tmp_path):
    """ATTACK (invariant 16, composition): each branch's LOFO/LODO bit is computed for the routed harness 'this
    candidate on its won clusters, the trunk elsewhere'. The router then COMPOSES branches: ml -> CA, bio -> CB. The
    held-out family H spans both domains; each branch regresses on its half of H by R = 0.11, which the other half's
    zero differences dilute below eps = 0.1, so both LOFO bits pass. The composed router regresses on ALL of H by R
    > eps and its LOFO was never computed. (Needs eps > 0; the default eps 0.0 does not let this through in the
    symmetric case, by the argument in the red-team report.)"""
    tasks = _spanning_suite(tmp_path)
    sc = Script(store, tasks, score=_spanning_score)
    cfg = BranchConfig(cluster_key="domain", non_regression_eps=EPS)
    recs = [propose_branch(TRUNK, c, tasks, seeds=SEEDS, outcome=sc.outcome, store=store,
                           releases=Releases.live(store), globally_accepted=False, cfg=cfg, lofo_eps=EPS)
            for c in (CA, CB)]
    assert recs[0] is not None and recs[0]["won"] == ["ml"] and recs[1] is not None and recs[1]["won"] == ["bio"]
    router = build_router(store, TRUNK, cluster_key="domain")
    by_id = {t.task_id: t for t in tasks}
    h_units = [(t.task_id, s) for t in tasks if t.split == "gate" and t.family == "H" for s in SEEDS]

    def routed(u):
        return sc.outcome(router.route(by_id[u[0]]), u).score

    composed = non_regression(paired_sample(h_units, routed, lambda u: sc.outcome(TRUNK, u).score),
                              non_regression_eps=EPS)
    assert composed["mean_diff"] < -EPS  # the attack is real: the composed router regresses H beyond eps
    assert composed["passed"], (
        f"the router serves ml -> CA and bio -> CB ({dict(router.routes)}); each branch's LOFO bit passed alone, "
        f"but the COMPOSED routed harness fails leave-one-family-out on H (mean diff {composed['mean_diff']:.3f}, "
        f"CI lo {composed['lo']:.3f} < -{EPS}) and that check was never run")


def test_attack_route_policy_reads_the_store_from_the_routers_frame_and_its_choice_is_not_released(store, tmp_path):
    """ATTACK (invariant 18): the route check only refuses routes NOT backed by a winning bit. When two branches win
    one cluster (or when a policy chooses to drop a winning cluster), WHICH winner is routed, or whether a won
    cluster is routed at all, is free. A policy holding nothing but the GateEvidenceView fetches the EvidenceStore
    from build_router's own frame (`sys._getframe(1).f_locals['store']`), reads a host-only per-task gate value and
    expresses it through that free choice. Two worlds whose released bits are identical yield different route
    tables: the table carries information no release accounts for."""
    tasks = _gate_tasks(tmp_path)
    ca2 = _spec("branch-a2")
    sc = Script(store, tasks, names=NAMES | {ca2.spec_hash(): "CA"})
    for c in (CA, ca2):
        propose_branch(TRUNK, c, tasks, seeds=SEEDS, outcome=sc.outcome, store=store,
                       releases=Releases.live(store), globally_accepted=False)
    stolen: list[float] = []

    def policy(view):
        st = sys._getframe(1).f_locals["store"]  # no closure over the store: taken from the router's frame
        v = st.records("harness_edit")[-1]["per_task"]["gA0"]["mean_diff"]  # host-only gate attribution
        stolen.append(v)
        winners = view.winners("A")
        return {"A": winners[0] if v > 0 else winners[1]}

    tables = []
    for secret in (0.31, -0.2):  # two worlds, the same released bits
        store.append_record("harness_edit", {"per_task": {"gA0": {"split": "gate", "n": 2, "mean_diff": secret}}})
        try:
            tables.append(dict(build_router(store, TRUNK, policy=policy).routes))
        except RouteRefused:
            tables.append(None)
    assert stolen == [0.31, -0.2]  # the attack really read the host-only value
    assert tables[0] == tables[1], (
        f"the route table depends on an unreleased per-task gate value: {tables[0]} vs {tables[1]}")


def _gate_stage_world(tmp_path, *, gate_regresses: bool):
    """One generation through the real loop (test_evolve_evidence's table evaluator). The candidate improves the
    evolve split and costs 3x the parent's tokens (max_cost_increase 0.5). World 'gate passes': the gate and every
    LOFO group pass, the cost term rejects. World 'gate regresses': the gate rejects. Both: accept bit 0."""
    from custom_harness.evolve import EvolveConfig
    from custom_harness.evolve.export import lineage_view, proposer_view
    from test_evolve import seed_spec
    from test_evolve_evidence import CostTable, _gain, _gen, _tasks, _tokens

    store = EvidenceStore(tmp_path / ("store-regress" if gate_regresses else "store-pass"))
    Kernel(store).approve_adapter("ml", approver="pytest")

    def score(arm, task, seed):
        if gate_regresses and arm == "cand" and not task.startswith("e"):
            return -0.5
        return _gain(arm, task, seed)

    def cost(arm, t, s):
        return _tokens(300 if arm == "cand" else 100)

    rel = Releases(None, mode="record")
    ev = CostTable(store, _tasks(tmp_path), seed_spec().spec_hash(), score, cost)
    ev.releases = rel
    res, _ = _gen(store, tmp_path, evaluator=ev, cfg=EvolveConfig(seeds=(0, 1), n_edits=1, race_rounds=1,
                                                                 max_cost_increase=0.5))
    evolve_ids = {t.task_id for t in _tasks(tmp_path) if t.split == "evolve"}
    views = ([proposer_view(r, evolve_ids)["rejected_on"] for r in store.records("harness_edit")],
             [lineage_view(r).get("rejected_on") for r in store.records("lineage")
              if r.get("status") == "rejected_gate"])
    released_values = [(x["key"].split("|")[-1], x["value"]) for x in rel.log]
    store.close()
    return res, views, released_values


def test_attack_the_proposer_visible_rejecting_stage_carries_a_gate_bit_no_release_counts(tmp_path):
    """ATTACK (invariant 18, L6.1/L6.8 export, consumed by L7's GenerationSignal): the only gate-derived release of a
    generation is the accept bit (`evolve.gate:decision`, alphabet verdict_bit). `rejected_on` is shown to the
    proposer (proposer_view, lineage_view) and to the L7 orchestrator. 'cost' is only reachable when the pooled gate
    AND every held-out family/domain passed; 'held_out_gate' when one failed. Two worlds with the same released
    value(s) give different proposer-visible stages: one unreleased, target-derived bit per rejected candidate."""
    res_a, views_a, rel_a = _gate_stage_world(tmp_path, gate_regresses=False)
    res_b, views_b, rel_b = _gate_stage_world(tmp_path, gate_regresses=True)
    assert not res_a.accepted and not res_b.accepted
    assert views_a[0] == ["cost"] and views_b[0] == ["held_out_gate"]  # the proposer-visible stages differ
    # KILLED (s3-evolve fix, loop.py GATE_STAGES): the stage is itself a ledgered `count` release, so the two worlds'
    # releases differ exactly where the views do; with equal released values the views would be equal
    from custom_harness.evolve.loop import GATE_STAGES
    assert [v for k, v in rel_a if k == "evolve.gate:decision"] == [v for k, v in rel_b if k == "evolve.gate:decision"]
    assert dict(rel_a)["evolve.gate:stage"] == GATE_STAGES.index("cost")
    assert dict(rel_b)["evolve.gate:stage"] == GATE_STAGES.index("held_out_gate")
    assert rel_a != rel_b, (
        f"the proposer-visible rejecting stage differs between worlds with identical releases: {views_a} vs {views_b} "
        "('cost' tells the proposer the held-out gate and every LOFO group passed)")


@pytest.mark.xfail(strict=True, reason="OPEN, needs owner decision: is the driver's out_dir / stdout agent-visible? "
                   "(pre-existing driver behaviour; fixing it edits the summary lines w2-wiring also changes)")
def test_attack_driver_writes_the_continuous_gate_mean_into_the_output_directory_and_log(store, tmp_path,
                                                                                        monkeypatch):
    """ATTACK (invariant 18; NEEDS OWNER DECISION on who may read the driver's output): `driver.evolve` writes each
    generation's free-text `decision` into out_dir/genNNN.json and prints it through `log`. For a gate rejection that
    text is selection.gate's reason, e.g. 'no improvement on gate split (mean=-0.5)': a continuous held-out value.
    export.py drops this exact field from the proposer's view, and driver.check_reveals itself treats out_dir as an
    evolver mount (it refuses a seal overlapping it), yet the gate mean is written there, unreleased."""
    import custom_harness.evolve.driver as driver
    from custom_harness.gateway import MockBackend
    from test_evolve import seed_spec
    from test_evolve_evidence import ONE_EDIT, CostTable, _gain, _tasks, _tokens

    tasks = _tasks(tmp_path)
    parent = seed_spec()

    def score(arm, task, seed):  # evolve improves; every gate task regresses by 0.5 (the value that leaks)
        return -0.5 if arm == "cand" and not task.startswith("e") else _gain(arm, task, seed)

    monkeypatch.setattr(driver, "Evaluator", lambda *a, **k: CostTable(store, tasks, parent.spec_hash(), score,
                                                                        lambda a_, t, s: _tokens(100)))
    monkeypatch.setattr(driver, "check_campaign", lambda *a, **k: 0)
    lines: list[str] = []
    out = tmp_path / "out"
    from custom_harness.evolve import EvolveConfig
    driver.evolve(parent, tasks, generations=1, backends={}, proposer=MockBackend([ONE_EDIT]), store=store,
                  sandbox_factory=lambda k: None, cfg=EvolveConfig(seeds=(0, 1), n_edits=1, race_rounds=1),
                  out_dir=out, log=lines.append)
    written = (out / "gen000.json").read_text(encoding="utf-8")
    assert "gate split" in written  # the gate-rejection reason reached the file (the attack is observed)
    for surface, text in (("out_dir/gen000.json", written), ("driver log", "\n".join(lines))):
        assert "mean=" not in text and "CI lo=" not in text, (
            f"a continuous gate value reached the {surface}: "
            f"{[ln for ln in text.splitlines() if 'mean=' in ln or 'CI lo=' in ln][:2]}")


# ================================================================================ L6.11 temporal reveal: the seal
def test_attack_seal_root_given_with_the_extended_length_prefix_lands_inside_a_declared_mount(store, tmp_path):
    """ATTACK (invariant 2, Windows): `\\\\?\\C:\\...` names the same directory as `C:\\...`, but Path.resolve keeps the
    prefix, so `_inside` compares two different-looking paths and the overlap check passes. Here the seal root IS a
    sub-directory of the declared export mount: the sealed labels file sits inside the evolver's mount."""
    if os.name != "nt":
        pytest.skip("Windows path aliasing")
    Kernel(store).vault.register_dataset("stream-target", DatasetRole.TEST, public={"x.csv": b"x\n1\n"},
                                         private={"y.csv": b"y\n1\n"})
    export = tmp_path / "export"
    export.mkdir()
    alias_root = "\\\\?\\" + str((export / "sealed").resolve())
    try:
        r = TemporalReveal(store, "s1", "stream-target", Path(alias_root), mounts=[export], clock=lambda: NOW)
        r.seal([LabelItem("late", "label-FUTURE", "2026-09-30")])
    except RevealRefused:
        return  # killed
    labels = export / "sealed" / "labels.jsonl"
    assert not labels.exists(), f"the sealed labels were written inside the evolver mount {export}"


def test_attack_mount_and_task_dir_aliases_of_the_seal_pass_the_overlap_checks(store, tmp_path):
    """ATTACK: the same alias in the other direction. A declared mount, and a task directory checked by the driver
    (`check_mounts`, called by `driver.check_reveals`), that ARE the seal directory written with the `\\\\?\\` prefix
    pass both checks."""
    if os.name != "nt":
        pytest.skip("Windows path aliasing")
    r = _stream(store, tmp_path)
    alias = Path("\\\\?\\" + str(r.root))
    assert os.path.samefile(alias, r.root)  # the alias really is the seal
    passed = []
    try:
        TemporalReveal(store, "s1-alias", "stream-target", r.root, mounts=[tmp_path / "export", alias],
                       clock=lambda: NOW)
        passed.append("declared mount")
    except RevealRefused:
        pass
    try:
        r.check_mounts([alias])
        passed.append("driver task dir / out dir")
    except RevealRefused:
        pass
    assert not passed, f"a path that IS the seal passed the overlap check as: {passed}"


def test_attack_declared_mount_spelled_as_the_seals_future_8dot3_short_name(store, tmp_path):
    """ATTACK (Windows, volumes with 8.3 names): the declared mounts are resolved ONCE, at construction, before the
    seal root exists. A mount spelled as the short name the root WILL get (`AVERYL~1`) resolves to itself then; the
    root is created, the short name now names the seal, and every later `_check_seal` reuses the stale resolution."""
    if os.name != "nt":
        pytest.skip("8.3 short names")
    Kernel(store).vault.register_dataset("stream-target", DatasetRole.TEST, public={"x.csv": b"x\n1\n"},
                                         private={"y.csv": b"y\n1\n"})
    host, export = tmp_path / "host", tmp_path / "export"
    host.mkdir()
    export.mkdir()
    root, short = host / "averylongsealname", host / "AVERYL~1"
    try:
        r = TemporalReveal(store, "s1", "stream-target", root, mounts=[export, short], clock=lambda: NOW)
        r.seal([LabelItem("late", "label-FUTURE", "2026-09-30")])
        r.check_destination(export / "labels.jsonl")  # the stale check passes again
    except RevealRefused:
        return  # killed
    if not (short.exists() and os.path.samefile(short, root)):
        pytest.skip("this volume does not generate 8.3 short names")
    assert not (short / "labels.jsonl").exists(), "a declared evolver mount names the seal through its 8.3 short name"


def test_attack_a_second_stream_opened_on_the_same_seal_ledgers_its_reveals_on_another_target(store, tmp_path):
    """ATTACK (invariant 18, K4 accounting): nothing binds a seal directory to the stream id and target dataset it
    was sealed under. A second TemporalReveal over the SAME root with another (registered) target reveals the same
    labels and records every release on the other target; K4's count on the labels' real target does not move."""
    r = _stream(store, tmp_path)
    Kernel(store).vault.register_dataset("other-target", DatasetRole.TEST, public={"x.csv": b"x\n2\n"},
                                         private={"y.csv": b"y\n2\n"})
    try:
        r2 = TemporalReveal(store, "s2", "other-target", r.root, mounts=r.mounts, clock=lambda: NOW)
    except RevealRefused:
        return  # killed
    r2.advance("2026-09-20")
    shown = _ids(r2.visible())
    assert shown == ["edge-day", "old", "tz"]  # the labels sealed under s1 / stream-target were revealed
    assert [b for b in released(store, "stream-target") if b["note"].startswith("reveal")] == [], \
        "control: the real target got nothing"
    pytest.fail(f"{len(shown)} labels sealed for 'stream-target' were revealed through stream s2 and ledgered on "
                "'other-target' only: the seal is not bound to its stream/target")


def test_attack_date_only_label_is_revealed_before_its_day_ended_west_of_utc(store, tmp_path):
    """ATTACK (convention; NEEDS OWNER DECISION): reveal.py says a date-only label counts as the END of its day
    because 'it may have been produced at any time that day', but it takes the day in UTC. The calendar day
    2026-09-21 ends at 2026-09-22T12:00Z in UTC-12, so at watermark 2026-09-22T06:00Z (18:00 on 09-21 in UTC-12) a
    label dated '2026-09-21' may still lie in the future. This contradicts test_reveal_wiring's day-boundary test
    (which pins the UTC reading), so the fix is a rule change, not a bug fix."""
    r = _stream(store, tmp_path)
    r.seal([LabelItem("west-day", 11, "2026-09-21")])
    r.advance("2026-09-22T06:00:00+00:00")
    assert "west-day" not in _ids(r.visible()), (
        "a date-only label was revealed 6 hours before its calendar day ended in UTC-12 (the earliest-ending "
        "reading is UTC+14, the latest UTC-12)")


# --------------------------------------------------------------------- killed: date formats Python's parser accepts
DAY = date(2026, 9, 30)
_QUIRKS = ("2026-09-30+00:00",  # date + offset: Python 3.12 parses it as a NAIVE midnight
           "20260930",  # basic format
           "2026-W40-3",  # ISO week date of 2026-09-30 (10 characters: taken as date-only)
           "2026-09-30T00:00",  # naive
           "2026-09-30 00:00:00",  # naive, space separator
           " 2026-09-30\t",  # whitespace
           "2026-09-30T00:00:00,0+00:00")  # comma fraction: explicit midnight UTC, the producer's own claim


def _early(fn) -> list[str]:
    """Quirk spellings of 2026-09-30 that `fn` makes visible at the START of that day (UTC); only an explicit
    tz-aware datetime may be (it states its own instant)."""
    start = datetime.combine(DAY, datetime.min.time(), tzinfo=UTC)
    out = []
    for s in _QUIRKS:
        t = fn(s)
        if t is not None and t <= start and s != "2026-09-30T00:00:00,0+00:00":
            out.append(s)
    return out


def _naive_as_utc(value):  # the re-introduction patch: "a naive datetime is UTC"
    v = value.strip()
    try:
        dt = datetime.fromisoformat(v) if len(v) != 10 else None
    except ValueError:
        return None
    if dt is None:
        return label_time(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def test_killed_date_spellings_python_accepts_never_reveal_a_label_early():
    assert date.fromisocalendar(2026, 40, 3) == DAY  # the week-date quirk really is 2026-09-30
    assert _early(label_time) == []
    for wm in ("20260930", "2026-W40", "2026-09-30+00:00", "2026-09-30T00:00", "2026-09-30T24:00+00:00",
               "2026-09-30T23:59:60+00:00"):
        with pytest.raises(Exception):  # noqa: B017 - RevealRefused or ValueError: never a watermark
            watermark_time(wm)
    # re-introduction check: the naive-is-UTC patch is caught by the same check
    assert _early(_naive_as_utc), "the check must catch the naive-as-UTC mutant"


def test_killed_a_watermark_is_never_past_the_clock_even_at_the_largest_offset(store, tmp_path):
    r = _stream(store, tmp_path)
    now = datetime.fromtimestamp(NOW, tz=UTC)
    with pytest.raises(RevealRefused, match="future"):
        r.advance((now + timedelta(minutes=1)).astimezone(timezone(timedelta(hours=-23, minutes=-59))).isoformat())
    r.advance((now - timedelta(seconds=1)).astimezone(timezone(timedelta(hours=23, minutes=59))).isoformat())
    assert r.watermark() <= now
