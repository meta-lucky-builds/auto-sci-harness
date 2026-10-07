"""GOAL §16.5 item 6: feature provenance (tags, refusal, dominance screen, human ruling, gate).

Synthetic tasks only (generated here); conftest points CHH_DATA_DIR at a per-test tmp dir, so the ruling store is
isolated. Attack tests first assert that the planted leak really leaks (alone it predicts the target with accuracy
>= 0.9 on train) before asserting the screen routes it.
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

def test_planted_raw_data_leaks_are_routed_to_a_human(tmp_path):
    routed = 0
    for kind in ("id", "post"):
        for v in range(5):
            d, col, acc = _plant(kind, v, tmp_path)
            assert acc >= 0.9, (kind, v, acc)  # the attack happened: the column alone predicts y
            report = prov.screen(d)
            assert report["features"][col]["tags"]["availability"] == "pre_outcome"  # mis-tagged, not refused
            assert not report["refused"]
            routed += col in report["routed"]
            assert prov.gate(report, prov.load_rulings()) != []
    assert routed == 10  # 10/10 routed (spec floor: >= 8/10)


def test_correctly_tagged_post_outcome_and_identifier_columns_are_refused(tmp_path):
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
        assert prov.GATE_REFUSED in prov.gate(report, prov.load_rulings())
        with pytest.raises(prov.ProvenanceError):  # a refused feature cannot be ruled legitimate
            prov.record_ruling(d, col, "legitimate", "alice")


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


def test_reason_codes_are_a_closed_set(tmp_path):
    d, _, _ = _plant("id", 3, tmp_path)
    report = prov.screen(d)
    codes = {c for v in report["routed"].values() for c in v} | {c for v in report["refused"].values() for c in v}
    assert codes and codes <= prov.REASON_CODES
    assert set(prov.gate(report, prov.load_rulings())) <= prov.REASON_CODES


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


def test_gate_refuses_without_ruling_and_passes_with_matching_legitimate(tmp_path):
    d, col = _routed_task(tmp_path)
    assert prov.gate(prov.screen(d), prov.load_rulings()) == [prov.GATE_MISSING_RULING]
    prov.record_ruling(d, col, "legitimate", "alice", "outcome-independent assay")
    assert prov.gate(prov.screen(d), prov.load_rulings()) == []


def test_gate_refuses_a_leak_ruling(tmp_path):
    d, col = _routed_task(tmp_path)
    prov.record_ruling(d, col, "legitimate", "alice")
    prov.record_ruling(d, col, "leak", "bob", "measured after diagnosis")
    assert prov.GATE_LEAK_RULING in prov.gate(prov.screen(d), prov.load_rulings())


def test_gate_refuses_stale_rulings(tmp_path):
    d, col = _routed_task(tmp_path)
    prov.record_ruling(d, col, "legitimate", "alice")
    assert prov.gate(prov.screen(d), prov.load_rulings()) == []
    # different tags (same data): the ruling no longer counts
    tags = yaml.safe_load((d / "features.yaml").read_text("utf-8"))
    tags["features"][col]["source"] = "re-described"
    (d / "features.yaml").write_text(yaml.safe_dump(tags), "utf-8")
    assert prov.gate(prov.screen(d), prov.load_rulings()) == [prov.GATE_MISSING_RULING]
    # different data (one train row changed): stale too
    d2, col2 = _routed_task(tmp_path / "second")
    prov.record_ruling(d2, col2, "legitimate", "alice")
    train = d2 / "public" / "train.csv"
    lines = train.read_text("utf-8").splitlines()
    lines[1] = lines[1].replace(lines[1].split(",")[0], "0.123456", 1)
    train.write_text("\n".join(lines) + "\n", "utf-8")
    assert prov.gate(prov.screen(d2), prov.load_rulings()) == [prov.GATE_MISSING_RULING]


def test_gate_refuses_an_edited_report(tmp_path):
    d, col = _routed_task(tmp_path)
    prov.record_ruling(d, col, "legitimate", "alice")
    report = prov.screen(d)
    report["refused"] = {}
    report["routed"] = {}
    assert prov.GATE_REPORT_MISMATCH in prov.gate(report, prov.load_rulings())


@pytest.mark.parametrize("tamper", ["edit", "delete_middle", "garbage"])
def test_gate_refuses_a_tampered_chain(tmp_path, tamper):
    d, col = _routed_task(tmp_path)
    for who in ("alice", "bob", "carol"):
        prov.record_ruling(d, col, "legitimate", who)
    assert prov.verify_chain() and prov.gate(prov.screen(d), prov.load_rulings()) == []
    path = prov.rulings_path()
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
    assert not prov.verify_chain()
    assert prov.GATE_CHAIN_TAMPERED in prov.gate(prov.screen(d), prov.load_rulings())
    with pytest.raises(prov.ProvenanceError, match="tampered"):
        prov.record_ruling(d, col, "legitimate", "dave")


def test_record_refuses_empty_approver(tmp_path):
    d, col = _routed_task(tmp_path)
    for who in ("", "   "):
        with pytest.raises(prov.ProvenanceError, match="approver"):
            prov.record_ruling(d, col, "legitimate", who)
    assert not prov.rulings_path().exists()


# ---------------------------------------------------------------------------------------------------- CLI

class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _rule(d: Path, col: str) -> list[str]:
    return ["rule-feature", str(d), "--feature", col, "--ruling", "legitimate", "--approver", "alice",
            "--note", "checked the protocol"]


def test_cli_rule_feature_refuses_without_tty(tmp_path, monkeypatch):
    d, col = _routed_task(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(col + "\n"))  # not a terminal, even with the right answer
    with pytest.raises(SystemExit, match="interactive terminal"):
        cli.main(_rule(d, col))
    assert not prov.rulings_path().exists()


def test_cli_rule_feature_refuses_wrong_confirmation(tmp_path, monkeypatch):
    d, col = _routed_task(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("f0\n"))
    with pytest.raises(SystemExit, match="confirmation"):
        cli.main(_rule(d, col))
    assert not prov.rulings_path().exists()


def test_cli_rule_feature_records_with_tty_and_confirmation(tmp_path, monkeypatch, capsys):
    d, col = _routed_task(tmp_path)
    assert cli.main(["feature-screen", str(d)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert col in out["report"]["routed"] and out["gate"] == [prov.GATE_MISSING_RULING]
    monkeypatch.setattr("sys.stdin", _Tty(col + "\n"))
    assert cli.main(_rule(d, col)) == 0
    entry = prov.load_rulings().rulings[-1]
    assert (entry["feature"], entry["ruling"], entry["approver"]) == (col, "legitimate", "alice")
    capsys.readouterr()
    assert cli.main(["feature-screen", str(d)]) == 0
    assert json.loads(capsys.readouterr().out)["gate"] == []
