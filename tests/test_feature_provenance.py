"""GOAL §16.5 item 6: feature provenance (tags, refusal, dominance screen, human ruling, gate).

Synthetic tasks only (generated here); conftest points CHH_DATA_DIR at a per-test tmp dir and the ``store`` fixture
is a per-test Kernel evidence store, so rulings (Kernel ledger kind ``feature_ruling``) are isolated. Attack tests
first assert that the planted leak really leaks (alone it predicts the target with accuracy >= 0.9 on train) before
asserting the screen routes it.
"""

from __future__ import annotations

import io
import json
import math
import random
from collections import Counter
from pathlib import Path

import pytest
import yaml

from custom_harness import cli
from custom_harness.config import data_dir
from custom_harness.kernel import Ledger
from custom_harness.kernel.ledger import write_anchor
from custom_harness.store import EvidenceStore
from custom_harness.verify import provenance as prov

N_TRAIN, N_TEST = 400, 200


def _write_task(d: Path, header: list[str], rows: list[list[float | int | str]], target: str = "y",
                tags: dict[str, dict[str, str]] | None = None, metric: str = "accuracy") -> Path:
    (d / "public").mkdir(parents=True)
    (d / "private").mkdir()
    (d / "task.yaml").write_text(yaml.safe_dump({
        "id": d.name, "title": "t", "description": "d", "metric": metric, "target_column": target}), "utf-8")
    ti = header.index(target)
    feats = [h for h in header if h != target]

    def line(vals: list[float | int | str]) -> str:
        return ",".join(f"{v:.6f}" if isinstance(v, float) else str(v) for v in vals)

    train, test = rows[:N_TRAIN], rows[N_TRAIN:]
    (d / "public" / "train.csv").write_text("\n".join([",".join(header)] + [line(r) for r in train]) + "\n", "utf-8")
    (d / "public" / "test_X.csv").write_text(
        "\n".join([",".join(feats)] + [line([v for i, v in enumerate(r) if i != ti]) for r in test]) + "\n", "utf-8")
    (d / "private" / "test_y.csv").write_text(
        "\n".join(["y"] + [str(r[ti]) for r in test]) + "\n", "utf-8")
    if tags is not None:
        (d / "features.yaml").write_text(yaml.safe_dump({"features": tags}), "utf-8")
    return d


def _honest_rows(seed: int, n_inf: int = 5, n_noise: int = 3) -> tuple[list[str], list[list[float | int | str]]]:
    """Several informative features of similar strength, a few noise features, none dominant; random row order."""
    rng = random.Random(seed)
    w = [rng.uniform(0.6, 1.0) for _ in range(n_inf)]
    header = [f"f{i}" for i in range(n_inf + n_noise)] + ["y"]
    rows: list[list[float | int | str]] = []
    for _ in range(N_TRAIN + N_TEST):
        x = [rng.gauss(0, 1) for _ in range(n_inf + n_noise)]
        logit = sum(wi * xi for wi, xi in zip(w, x[:n_inf], strict=True))
        rows.append([*x, int(rng.random() < 1 / (1 + math.exp(-logit)))])
    return header, rows


def _pre_tags(header: list[str]) -> dict[str, dict[str, str]]:
    return {h: {"availability": "pre_outcome", "role": "measurement", "source": "sensor"} for h in header if h != "y"}


def _alone_accuracy(col: list[float], y: list[int], bins: int = 10) -> float:
    """Train accuracy of predicting y from ONE column by majority class per quantile bin."""
    order = sorted(range(len(col)), key=lambda i: col[i])
    bin_of = {i: (rank * bins) // len(col) for rank, i in enumerate(order)}
    maj = {b: Counter(y[i] for i in bin_of if bin_of[i] == b).most_common(1)[0][0] for b in set(bin_of.values())}
    return sum(maj[bin_of[i]] == y[i] for i in range(len(y))) / len(y)


def _plant(kind: str, variant: int, tmp: Path) -> tuple[Path, str, float]:
    header, rows = _honest_rows(1000 + variant + (0 if kind == "id" else 50))
    rng = random.Random(7 + variant)
    if kind == "id":
        # rows sorted by class, then numbered: the ID encodes the class. Variants 0-2 shuffle the rows afterwards
        # (a unique integer column), 3-4 keep the file in ID order (monotone with row order). Gaps/offsets vary.
        col = "record_id"
        rows.sort(key=lambda r: (r[-1], rng.random()))
        start, step = [(1, 1), (1000, 3), (50000, 7), (1, 1), (200, 2)][variant]
        rows = [[start + step * k, *r] for k, r in enumerate(rows)]
        if variant < 3:
            rng.shuffle(rows)
    else:
        # a post-outcome variable (measured after y is known), mis-tagged as a pre-outcome measurement
        col = "followup_score"
        noise, flip = [(0.25, 0.0), (0.3, 0.03), (0.2, 0.05), (0.3, 0.02), (0.15, 0.06)][variant]
        rows = [[(1 - int(r[-1]) if rng.random() < flip else int(r[-1])) + rng.gauss(0, noise), *r] for r in rows]
    header = [col, *header]
    d = _write_task(tmp / f"{kind}{variant}", header, rows, tags=_pre_tags(header))
    acc = _alone_accuracy([float(r[0]) for r in rows[:N_TRAIN]], [int(r[-1]) for r in rows[:N_TRAIN]])
    return d, col, acc


# ---------------------------------------------------------------------------------------------------- attacks

def test_planted_raw_data_leaks_are_routed_to_a_human(tmp_path, store):
    routed = 0
    for kind in ("id", "post"):
        for v in range(5):
            d, col, acc = _plant(kind, v, tmp_path)
            assert acc >= 0.9, (kind, v, acc)  # the attack happened: the column alone predicts y
            report = prov.screen(d)
            assert report["features"][col]["tags"]["availability"] == "pre_outcome"  # mis-tagged, not refused
            assert not report["refused"]
            routed += col in report["routed"]
            assert prov.gate(report, prov.load_rulings(store)) != []
    assert routed == 10  # 10/10 routed (spec floor: >= 8/10)


def test_correctly_tagged_post_outcome_and_identifier_columns_are_refused(tmp_path, store):
    for kind, tag, code in (("id", {"availability": "pre_outcome", "role": "identifier"}, prov.REFUSE_IDENTIFIER),
                            ("post", {"availability": "post_outcome", "role": "measurement"},
                             prov.REFUSE_POST_OUTCOME)):
        d, col, acc = _plant(kind, 0, tmp_path)
        assert acc >= 0.9
        tags = yaml.safe_load((d / "features.yaml").read_text("utf-8"))
        tags["features"][col] = {**tag, "source": "registry"}
        (d / "features.yaml").write_text(yaml.safe_dump(tags), "utf-8")
        report = prov.screen(d)
        assert report["refused"] == {col: [code]}
        assert col not in report["routed"]
        assert prov.GATE_REFUSED in prov.gate(report, prov.load_rulings(store))
        with pytest.raises(prov.ProvenanceError):  # a refused feature cannot be ruled legitimate
            prov.record_ruling(d, col, "legitimate", "alice", store=store)


def test_honest_tasks_are_rarely_routed(tmp_path):
    false_routes = 0
    for s in range(20):
        header, rows = _honest_rows(s)
        report = prov.screen(_write_task(tmp_path / f"h{s}", header, rows, tags=_pre_tags(header)))
        assert not report["refused"]
        false_routes += bool(report["routed"])
    assert false_routes <= 2, false_routes
    # measured 2026-10-07: 1 of 20 (seed 8: honest f1 routed as dominant_feature; routing is not a verdict)
    assert false_routes == 1


def test_untagged_features_are_routed(tmp_path):
    header, rows = _honest_rows(3)
    report = prov.screen(_write_task(tmp_path / "u", header, rows))  # no features.yaml
    assert set(report["routed"]) == {h for h in header if h != "y"}
    assert all(prov.ROUTE_UNTAGGED in c for c in report["routed"].values())


def test_train_test_duplicates_are_routed(tmp_path):
    header, rows = _honest_rows(4)
    rows[N_TRAIN:N_TRAIN + 20] = [list(r) for r in rows[:20]]  # 10% of test rows copied from train
    report = prov.screen(_write_task(tmp_path / "dup", header, rows, tags=_pre_tags(header)))
    assert report["duplicate_fraction"] == pytest.approx(0.1)
    assert report["routed"] == {prov.ROWS_ITEM: [prov.ROUTE_DUPLICATES]}


def test_screen_never_reads_test_labels(tmp_path):
    d, _, _ = _plant("post", 0, tmp_path)
    before = prov.screen(d)
    (d / "private" / "test_y.csv").write_text("y\n" + "1\n" * N_TEST, "utf-8")
    assert prov.screen(d) == before
    for f in (d / "private").iterdir():
        f.unlink()
    (d / "private").rmdir()
    assert prov.screen(d) == before


def test_reason_codes_are_a_closed_set(tmp_path, store):
    d, _, _ = _plant("id", 3, tmp_path)
    report = prov.screen(d)
    codes = {c for v in report["routed"].values() for c in v} | {c for v in report["refused"].values() for c in v}
    assert codes and codes <= prov.REASON_CODES
    assert set(prov.gate(report, prov.load_rulings(store))) <= prov.REASON_CODES


# ---------------------------------------------------------------------------------------------------- tags

def test_tag_for_nonexistent_column_is_an_error(tmp_path):
    header, rows = _honest_rows(5)
    tags = {**_pre_tags(header), "no_such_column": {"availability": "pre_outcome", "role": "measurement"}}
    with pytest.raises(prov.ProvenanceError, match="not a feature"):
        prov.screen(_write_task(tmp_path / "bad", header, rows, tags=tags))


@pytest.mark.parametrize("tag", [{"y": {"availability": "pre_outcome"}},          # the target is not a feature
                                 {"f0": {"availability": "before"}},              # outside the closed enum
                                 {"f0": {"role": "measurement", "extra": "x"}}])  # unknown key
def test_malformed_tags_fail_closed(tmp_path, tag):
    header, rows = _honest_rows(5)
    with pytest.raises(prov.ProvenanceError):
        prov.screen(_write_task(tmp_path / "bad", header, rows, tags=tag))


def test_tags_hash_is_stable_and_sensitive(tmp_path):
    header, rows = _honest_rows(6)
    tags = _pre_tags(header)
    r1 = prov.screen(_write_task(tmp_path / "a", header, rows, tags=tags))
    r2 = prov.screen(_write_task(tmp_path / "b", header, rows, tags=dict(reversed(list(tags.items())))))
    assert r1["tags_hash"] == r2["tags_hash"]
    tags["f0"] = {**tags["f0"], "source": "other"}
    r3 = prov.screen(_write_task(tmp_path / "c", header, rows, tags=tags))
    assert r3["tags_hash"] != r1["tags_hash"]


# ---------------------------------------------------------------------------------------------------- gate

def _routed_task(tmp_path: Path) -> tuple[Path, str]:
    d, col, acc = _plant("post", 1, tmp_path)
    assert acc >= 0.9 and col in prov.screen(d)["routed"]
    return d, col


def test_gate_refuses_without_ruling_and_passes_with_matching_legitimate(tmp_path, store):
    d, col = _routed_task(tmp_path)
    assert prov.gate(prov.screen(d), prov.load_rulings(store)) == [prov.GATE_MISSING_RULING]
    prov.record_ruling(d, col, "legitimate", "alice", "outcome-independent assay", store=store)
    assert prov.gate(prov.screen(d), prov.load_rulings(store)) == []


def test_gate_refuses_a_leak_ruling(tmp_path, store):
    d, col = _routed_task(tmp_path)
    prov.record_ruling(d, col, "legitimate", "alice", store=store)
    prov.record_ruling(d, col, "leak", "bob", "measured after diagnosis", store=store)
    assert prov.GATE_LEAK_RULING in prov.gate(prov.screen(d), prov.load_rulings(store))


def test_gate_refuses_stale_rulings(tmp_path, store):
    d, col = _routed_task(tmp_path)
    prov.record_ruling(d, col, "legitimate", "alice", store=store)
    assert prov.gate(prov.screen(d), prov.load_rulings(store)) == []
    # different tags (same data): the ruling no longer counts
    tags = yaml.safe_load((d / "features.yaml").read_text("utf-8"))
    tags["features"][col]["source"] = "re-described"
    (d / "features.yaml").write_text(yaml.safe_dump(tags), "utf-8")
    assert prov.gate(prov.screen(d), prov.load_rulings(store)) == [prov.GATE_MISSING_RULING]
    # different data (one train row changed): stale too
    d2, col2 = _routed_task(tmp_path / "second")
    prov.record_ruling(d2, col2, "legitimate", "alice", store=store)
    train = d2 / "public" / "train.csv"
    lines = train.read_text("utf-8").splitlines()
    lines[1] = lines[1].replace(lines[1].split(",")[0], "0.123456", 1)
    train.write_text("\n".join(lines) + "\n", "utf-8")
    assert prov.gate(prov.screen(d2), prov.load_rulings(store)) == [prov.GATE_MISSING_RULING]


def test_gate_refuses_an_edited_report(tmp_path, store):
    d, col = _routed_task(tmp_path)
    prov.record_ruling(d, col, "legitimate", "alice", store=store)
    report = prov.screen(d)
    report["refused"] = {}
    report["routed"] = {}
    assert prov.GATE_REPORT_MISMATCH in prov.gate(report, prov.load_rulings(store))


# ---------------------------------------------------------------------------------------------------- ledger store

def _ruling_entries(store: EvidenceStore) -> list[dict]:
    return [e.body for e in Ledger(store).entries(prov.RULING_KIND)]


def _sql(store: EvidenceStore, statement: str, *args: object) -> None:
    with store._db:
        store._db.execute(statement, args)


def test_rulings_land_in_the_kernel_ledger_not_the_interim_file(tmp_path, store):
    d, col = _routed_task(tmp_path)
    out = prov.record_ruling(d, col, "legitimate", "  alice  ", "outcome-independent assay", store=store)
    report = prov.screen(d)
    [body] = _ruling_entries(store)
    assert {k: body[k] for k in ("data_digest", "feature", "tags_hash", "report_hash", "ruling", "approver",
                                 "note")} == {
        "data_digest": report["data_digest"], "feature": col, "tags_hash": report["tags_hash"],
        "report_hash": report["report_hash"], "ruling": "legitimate", "approver": "alice",
        "note": "outcome-independent assay"}
    assert body["timestamp"] and "migrated_from" not in body
    assert out["hash"] == Ledger(store).head()
    assert not prov.rulings_path().exists()
    assert Ledger(store).verify() == []


@pytest.mark.parametrize("tamper", ["edit", "delete_middle", "garbage"])
def test_gate_refuses_a_tampered_chain(tmp_path, store, tamper):
    d, col = _routed_task(tmp_path)
    for who in ("alice", "bob", "carol"):
        prov.record_ruling(d, col, "legitimate", who, store=store)
    rs = prov.load_rulings(store)
    assert rs.chain_ok and prov.gate(prov.screen(d), rs) == []
    seqs = [e.seq for e in Ledger(store).entries(prov.RULING_KIND)]
    if tamper == "edit":
        body = dict(_ruling_entries(store)[0], note="edited")
        _sql(store, "UPDATE ledger SET body = ? WHERE seq = ?", json.dumps(body, sort_keys=True), seqs[0])
    elif tamper == "delete_middle":
        _sql(store, "DELETE FROM ledger WHERE seq = ?", seqs[1])
    else:
        _sql(store, "UPDATE ledger SET body = ? WHERE seq = ?", "{not json", seqs[1])
    assert not prov.load_rulings(store).chain_ok
    assert prov.GATE_CHAIN_TAMPERED in prov.gate(prov.screen(d), prov.load_rulings(store))
    with pytest.raises(prov.ProvenanceError, match="tampered"):
        prov.record_ruling(d, col, "legitimate", "dave", store=store)


def test_record_refuses_empty_approver(tmp_path, store):
    d, col = _routed_task(tmp_path)
    for who in ("", "   ", "\t\n"):
        with pytest.raises(prov.ProvenanceError, match="approver"):
            prov.record_ruling(d, col, "legitimate", who, store=store)
    assert not prov.rulings_path().exists()
    assert _ruling_entries(store) == []


@pytest.mark.parametrize("ruling", ["Legitimate", "ok", "", "leak "])
def test_record_refuses_rulings_outside_the_closed_set(tmp_path, store, ruling):
    d, col = _routed_task(tmp_path)
    with pytest.raises(prov.ProvenanceError, match="ruling must be"):
        prov.record_ruling(d, col, ruling, "alice", store=store)
    assert _ruling_entries(store) == []


@pytest.mark.parametrize("approver", [None, "", "   ", 7, ["alice"]])
def test_gate_ignores_a_ledger_ruling_without_a_human_approver(tmp_path, store, approver):
    """A feature_ruling entry not written through record_ruling (no recorded human) never lets a feature through."""
    d, col = _routed_task(tmp_path)
    report = prov.screen(d)
    body = {k: report[k] for k in ("data_digest", "tags_hash", "report_hash")}
    body.update(feature=col, ruling="legitimate", note="", timestamp="2026-10-09T00:00:00+00:00")
    if approver is not None:
        body["approver"] = approver
    Ledger(store).append(prov.RULING_KIND, body)
    rs = prov.load_rulings(store)
    assert rs.chain_ok
    assert prov.gate(report, rs) == [prov.GATE_MISSING_RULING]


def test_gate_ignores_a_ledger_ruling_outside_the_closed_set(tmp_path, store):
    d, col = _routed_task(tmp_path)
    report = prov.screen(d)
    body = {k: report[k] for k in ("data_digest", "tags_hash", "report_hash")}
    body.update(feature=col, ruling="Legitimate", approver="alice", note="", timestamp="t")
    Ledger(store).append(prov.RULING_KIND, body)
    assert prov.gate(report, prov.load_rulings(store)) == [prov.GATE_MISSING_RULING]


def test_a_leak_ruling_counts_even_without_an_approver(tmp_path, store):
    """Fail closed both ways: a 'leak' entry is never ignored (it can only block, never pass)."""
    d, col = _routed_task(tmp_path)
    prov.record_ruling(d, col, "legitimate", "alice", store=store)
    report = prov.screen(d)
    body = {k: report[k] for k in ("data_digest", "tags_hash", "report_hash")}
    body.update(feature=col, ruling="leak", note="", timestamp="t")
    Ledger(store).append(prov.RULING_KIND, body)
    assert prov.gate(report, prov.load_rulings(store)) == [prov.GATE_LEAK_RULING]


def _write_interim(path: Path, rows: list[dict]) -> list[dict]:
    """The pre-ledger interim store format (hash-chained JSONL), written the way the old record_ruling wrote it."""
    out, prev = [], prov.GENESIS
    for r in rows:
        e = {"prev": prev, **r}
        e["hash"] = prov._line_hash(e)
        out.append(e)
        prev = e["hash"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(json.dumps(e, sort_keys=True) + "\n" for e in out).encode("utf-8"))  # LF on every OS
    return out


def _interim_row(d: Path, col: str, ruling: str, approver: str, ts: str) -> dict:
    report = prov.screen(d)
    return {"data_digest": report["data_digest"], "feature": col, "tags_hash": report["tags_hash"],
            "report_hash": report["report_hash"], "ruling": ruling, "approver": approver, "note": "n", "timestamp": ts}


def test_removed_trailing_ruling_is_detected_through_the_ledger_anchor_not_the_jsonl(tmp_path, store):
    d, col = _routed_task(tmp_path)
    # the interim JSONL: dropping the trailing 'leak' line leaves a chain that still verifies (the old weakness)
    path = tmp_path / "interim" / "rulings.jsonl"
    _write_interim(path, [_interim_row(d, col, "legitimate", "alice", "t1"), _interim_row(d, col, "leak", "bob", "t2")])
    path.write_bytes(path.read_bytes().split(b"\n")[0] + b"\n")  # byte-exact: only the trailing line is dropped
    assert prov.load_interim_rulings(path).chain_ok  # undetectable in the JSONL
    # the ledger: anchor after the leak ruling, then remove it
    prov.record_ruling(d, col, "legitimate", "alice", store=store)
    prov.record_ruling(d, col, "leak", "bob", "measured after diagnosis", store=store)
    anchor = tmp_path / "anchors" / "ledger.anchor"
    write_anchor(Ledger(store), anchor, tmp_path / "store")
    last = Ledger(store).entries()[-1]
    assert last.kind == prov.RULING_KIND and last.body["ruling"] == "leak"
    _sql(store, "DELETE FROM ledger WHERE seq = ?", last.seq)
    assert prov.gate(prov.screen(d), prov.load_rulings(store)) == []  # without an anchor: the same blind spot
    rs = prov.load_rulings(store, anchors=[anchor])
    assert not rs.chain_ok
    assert prov.gate(prov.screen(d), rs) == [prov.GATE_CHAIN_TAMPERED]
    with pytest.raises(prov.ProvenanceError, match="tampered"):
        prov.record_ruling(d, col, "legitimate", "carol", store=store, anchors=[anchor])


def test_unreadable_anchor_fails_closed(tmp_path, store):
    d, col = _routed_task(tmp_path)
    prov.record_ruling(d, col, "legitimate", "alice", store=store)
    assert not prov.load_rulings(store, anchors=[tmp_path / "no_such.anchor"]).chain_ok


# ---------------------------------------------------------------------------------------------------- migration

def test_migration_imports_a_verified_interim_chain_and_leaves_the_file(tmp_path, store):
    d, col = _routed_task(tmp_path)
    path = prov.rulings_path()
    lines = _write_interim(path, [_interim_row(d, col, "legitimate", "alice", "2026-10-07T10:00:00+00:00"),
                                  _interim_row(d, "f0", "leak", "bob", "2026-10-07T11:00:00+00:00")])
    before = path.read_bytes()
    assert prov.gate(prov.screen(d), prov.load_rulings(store)) == [prov.GATE_MISSING_RULING]  # JSONL is not read
    out = prov.migrate_interim_rulings(store, "operator", path=path)
    assert out["imported"] == 2 and out["already_imported"] == 0
    bodies = _ruling_entries(store)
    assert [(b["approver"], b["timestamp"], b["ruling"]) for b in bodies] == [
        ("alice", "2026-10-07T10:00:00+00:00", "legitimate"), ("bob", "2026-10-07T11:00:00+00:00", "leak")]
    assert [b["migrated_from"] for b in bodies] == [
        {"path": str(path), "line": i + 1, "line_hash": e["hash"]} for i, e in enumerate(lines)]
    assert all(b["migrated_by"] == "operator" for b in bodies)
    assert prov.gate(prov.screen(d), prov.load_rulings(store)) == []
    assert path.read_bytes() == before  # never deleted or rewritten
    again = prov.migrate_interim_rulings(store, "operator", path=path)
    assert again["imported"] == 0 and again["already_imported"] == 2
    assert len(_ruling_entries(store)) == 2


@pytest.mark.parametrize("tamper", ["edit", "delete_middle", "garbage"])
def test_migration_refuses_a_tampered_interim_file(tmp_path, store, tamper):
    d, col = _routed_task(tmp_path)
    path = prov.rulings_path()
    _write_interim(path, [_interim_row(d, col, "legitimate", w, "t") for w in ("alice", "bob", "carol")])
    lines = path.read_text("utf-8").splitlines()
    if tamper == "edit":
        e = json.loads(lines[0])
        e["note"] = "edited"
        lines[0] = json.dumps(e, sort_keys=True)
    elif tamper == "delete_middle":
        del lines[1]
    else:
        lines.insert(1, "{not json")
    path.write_text("\n".join(lines) + "\n", "utf-8")
    before = path.read_bytes()
    with pytest.raises(prov.ProvenanceError, match="tampered"):
        prov.migrate_interim_rulings(store, "operator", path=path)
    assert _ruling_entries(store) == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("approver", ""), ("approver", "  "), ("ruling", "maybe"),
                                         ("feature", 3)])
def test_migration_refuses_an_interim_ruling_a_human_could_not_have_recorded(tmp_path, store, field, value):
    d, col = _routed_task(tmp_path)
    path = prov.rulings_path()
    _write_interim(path, [_interim_row(d, col, "legitimate", "alice", "t"),
                          {**_interim_row(d, col, "legitimate", "bob", "t"), field: value}])
    with pytest.raises(prov.ProvenanceError):
        prov.migrate_interim_rulings(store, "operator", path=path)
    assert _ruling_entries(store) == []


def test_migration_needs_a_named_operator_and_tolerates_no_file(tmp_path, store):
    with pytest.raises(prov.ProvenanceError, match="operator"):
        prov.migrate_interim_rulings(store, "  ")
    assert prov.migrate_interim_rulings(store, "operator")["imported"] == 0
    assert _ruling_entries(store) == []


# ---------------------------------------------------------------------------------------------------- CLI

class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _rule(d: Path, col: str, *extra: str) -> list[str]:
    """`--no-anchor` unless other flags are given (s4-human: an anchor is required by default)."""
    return ["rule-feature", str(d), "--feature", col, "--ruling", "legitimate", "--approver", "alice",
            "--note", "checked the protocol", *(extra or ("--no-anchor",))]


def _cli_rulings() -> list[dict]:
    """Rulings in the store the CLI uses (data_dir()/store)."""
    s = EvidenceStore(data_dir() / "store")
    try:
        return _ruling_entries(s)
    finally:
        s.close()


def test_cli_rule_feature_refuses_without_tty(tmp_path, monkeypatch):
    d, col = _routed_task(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(col + "\n"))  # not a terminal, even with the right answer
    with pytest.raises(SystemExit, match="interactive terminal"):
        cli.main(_rule(d, col))
    assert not prov.rulings_path().exists()
    assert _cli_rulings() == []


def test_cli_rule_feature_refuses_wrong_confirmation(tmp_path, monkeypatch):
    d, col = _routed_task(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("f0\n"))
    with pytest.raises(SystemExit, match="confirmation"):
        cli.main(_rule(d, col))
    assert not prov.rulings_path().exists()
    assert _cli_rulings() == []


def test_cli_rule_feature_records_with_tty_and_confirmation(tmp_path, monkeypatch, capsys):
    d, col = _routed_task(tmp_path)
    assert cli.main(["feature-screen", str(d)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert col in out["report"]["routed"] and out["gate"] == [prov.GATE_MISSING_RULING]
    # s4-human (orchestrator decision 2026-10-14): an anchor is required by default
    monkeypatch.setattr("sys.stdin", _Tty(col + "\n"))
    with pytest.raises(SystemExit, match="--anchor"):
        cli.main(_rule(d, col, "--note", "no anchor flag"))
    assert _cli_rulings() == []
    first = tmp_path / "anchors" / "first.jsonl"
    assert cli.main(["ledger-anchor", str(first)]) == 0
    monkeypatch.setattr("sys.stdin", _Tty(col + "\n"))
    with pytest.raises(SystemExit, match="you reviewed"):  # the ruling binds the report hash the human reviewed
        cli.main(_rule(d, col, "--anchor", str(first), "--report-hash", "0" * 64))
    assert _cli_rulings() == []
    monkeypatch.setattr("sys.stdin", _Tty(col + "\n"))
    assert cli.main(_rule(d, col, "--anchor", str(first))) == 0
    entry = _cli_rulings()[-1]
    assert (entry["feature"], entry["ruling"], entry["approver"]) == (col, "legitimate", "alice")
    assert entry["anchors_checked"] == 1 and entry["report_hash"] == prov.screen(d)["report_hash"]
    capsys.readouterr()
    assert cli.main(["feature-screen", str(d)]) == 0
    assert json.loads(capsys.readouterr().out)["gate"] == []
    # with an anchor: a later removal of the ruling is caught by `chh feature-screen --anchor`
    anchor = tmp_path / "anchors" / "a.jsonl"
    assert cli.main(["ledger-anchor", str(anchor)]) == 0
    s = EvidenceStore(data_dir() / "store")
    try:
        _sql(s, "DELETE FROM ledger WHERE seq = (SELECT MAX(seq) FROM ledger)")
    finally:
        s.close()
    capsys.readouterr()
    assert cli.main(["feature-screen", str(d), "--anchor", str(anchor)]) == 1
    assert prov.GATE_CHAIN_TAMPERED in json.loads(capsys.readouterr().out)["gate"]


def test_cli_migrate_is_a_human_action(tmp_path, monkeypatch, capsys):
    d, col = _routed_task(tmp_path)
    path = prov.rulings_path()
    _write_interim(path, [_interim_row(d, col, "legitimate", "alice", "t")])
    monkeypatch.setattr("sys.stdin", io.StringIO("1\n"))
    with pytest.raises(SystemExit, match="interactive terminal"):
        cli.main(["feature-rulings-migrate", "--by", "operator"])
    monkeypatch.setattr("sys.stdin", _Tty("2\n"))
    with pytest.raises(SystemExit, match="confirmation"):
        cli.main(["feature-rulings-migrate", "--by", "operator"])
    assert _cli_rulings() == []
    monkeypatch.setattr("sys.stdin", _Tty("1\n"))
    assert cli.main(["feature-rulings-migrate", "--by", "operator"]) == 0
    assert [b["approver"] for b in _cli_rulings()] == ["alice"]
    assert path.exists()


