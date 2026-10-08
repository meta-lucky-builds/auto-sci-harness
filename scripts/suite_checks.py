"""Zero-call checks of a (rebuilt) task suite, per task. No model call, no program run, no network.

    python scripts/suite_checks.py SUITE_DIR --record-dir DIR --beacon-file FILE [--old-suite DIR] [--out JSON]
                                   [--skip-scan] [--sims N]

TRUSTED audit code: it reads private labels, but only to SCORE attacks and scans; it writes no label anywhere (the
JSON report holds counts, hashes and paths only). Checks:
1. split isolation (research.suite.check_split_isolation);
2. host truth scan: every held-out row WITH its label, as a CSV line (the format of every train.csv), and every
   canary, searched line by line in every file under the agent-readable roots (commitment.default_agent_readable_roots,
   minus the by-design truth location <suites>/<suite>/<task>/private and .git object stores); plus the sealed
   beacon in every encoding (commitment.secret_leak_scan);
3. COUPLED-PRNG regression: the pre-fix seeded generators from the repo history (tests/fixtures/
   suite_prefix_401c00a.py) regenerate each synthetic task; plus the current generators keyed with every public
   value in the suite (manifest hash, key commitments, task ids). Positive control: the same attack on --old-suite;
4. certifiability preflight (research.preflight.suite_audit, as `chh preflight-suite`), and the minted test size
   that reaches P(certify) >= 0.8 at the 0.02 floor (normal approximation, labelled; spot-checked with the real
   preflight);
5. the salts detectability exam (salts.exam.run_exam) at the suite's own sizes, with a scripted spotter that reads
   public files only (0 calls), and the distribution of its statistic in each arm.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from statistics import NormalDist
from typing import Any

import yaml

from custom_harness.commitment import default_agent_readable_roots, secret_leak_scan
from custom_harness.research import rebuild as RB
from custom_harness.research import suite as S

ROOT = Path(__file__).resolve().parents[1]
PREFIX = ROOT / "tests" / "fixtures" / "suite_prefix_401c00a.py"
MAX_READ = 64 << 20
_N = NormalDist()


def log(*a: Any) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def _lines(p: Path) -> list[str]:
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln]


# ---------------------------------------------------------------- 2. host truth scan
def heldout_lines(task_dir: Path) -> set[bytes]:
    """Every held-out row as `features...,label` (the column order of train.csv), as bytes."""
    xs = _lines(task_dir / "public" / "test_X.csv")[1:]
    ys = _lines(task_dir / "private" / "test_y.csv")[1:]
    if len(xs) != len(ys):
        raise ValueError(f"{task_dir}: test_X and test_y differ in length")
    head = _lines(task_dir / "public" / "train.csv")[0].split(",")
    target = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))["target_column"]
    ti = head.index(target)
    out = set()
    for x, y in zip(xs, ys, strict=True):
        v = x.split(",")
        v.insert(ti, y)
        out.add(",".join(v).encode())
    return out


def _walk(roots: Iterable[Path], skip: list[Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    for r in roots:
        r = Path(r).resolve()
        if r in seen:
            continue
        seen.add(r)
        for dirpath, dirs, names in os.walk(r):
            dp = Path(dirpath)
            dirs[:] = [d for d in dirs if d != ".git" and not any((dp / d).resolve() == s for s in skip)]
            for n in names:
                yield dp / n


def host_truth_scan(suite: Path, tasks: list[dict], beacon: str, roots: list[Path]) -> dict[str, Any]:
    needles: dict[bytes, str] = {}
    canaries: dict[bytes, str] = {}
    for t in tasks:
        d = suite / t["id"]
        for ln in heldout_lines(d):
            needles.setdefault(ln, t["id"])
        canaries[(d / "private" / "canary.txt").read_text(encoding="utf-8").strip().encode()] = t["id"]
    skip = [(suite / t["id"] / "private").resolve() for t in tasks]
    hits: dict[str, dict[str, int]] = {t["id"]: {} for t in tasks}
    canary_hits: dict[str, list[str]] = {t["id"]: [] for t in tasks}
    n_files = skipped_big = unreadable = 0
    for f in _walk(roots, skip):
        n_files += 1
        try:
            if f.stat().st_size > MAX_READ:
                skipped_big += 1
                continue
            raw = f.read_bytes()
        except OSError:
            unreadable += 1
            continue
        for c, ctid in canaries.items():
            if c in raw:
                canary_hits[ctid].append(str(f))
        if b"," not in raw:
            continue
        for ln in raw.splitlines():
            tid = needles.get(ln.rstrip(b"\r"))
            if tid is not None:
                hits[tid][str(f)] = hits[tid].get(str(f), 0) + 1
    beacon_leaks = secret_leak_scan(beacon.encode(), roots)
    per_task = {}
    for t in tasks:
        rows = sum(hits[t["id"]].values())
        per_task[t["id"]] = {"labelled_heldout_lines_found": rows,
                             "files": sorted(hits[t["id"]], key=lambda k: -hits[t["id"]][k])[:5],
                             "n_files": len(hits[t["id"]]), "canary_found_in": canary_hits[t["id"]][:5]}
    return {"roots": [str(r) for r in roots], "files_scanned": n_files, "skipped_over_64MB": skipped_big,
            "unreadable": unreadable, "beacon_leaks": beacon_leaks, "per_task": per_task}


# ---------------------------------------------------------------- 3. COUPLED-PRNG regression
def keyed_attack(suite: Path, doc: dict, manifest: dict | None) -> dict[str, dict[str, Any]]:
    """The CURRENT generators keyed with every public value an agent could try."""
    pub: list[bytes] = []
    if manifest is not None:
        h = RB.manifest_hash(manifest)
        pub += [bytes.fromhex(h), h.encode(), bytes.fromhex(manifest["beacon_commitment"])]
    pub += [bytes.fromhex(doc["generator"]["key_commitment"])]
    out = {}
    for gen, seed, n in S.SYNTHETIC:
        tid = gen(seed, 1, key=b"\x00" * 32).task_id
        entry = next(t for t in doc["tasks"] if t["id"] == tid)
        cands = [*pub, bytes.fromhex(entry["provenance"]["key_commitment"]), hashlib.sha256(tid.encode()).digest()]
        truth = _lines(suite / tid / "private" / "test_y.csv")[1:]
        train = set(_lines(suite / tid / "public" / "train.csv")[1:])
        best = {"exact": False, "agree": 0, "train_rows_matched": 0}
        for master in cands:
            for key in (master, S.task_key(master, tid)):
                spec = gen(seed, n, key=key)
                guess = RB._test_labels(spec)
                agree = sum(a == b for a, b in zip(guess, truth, strict=False))
                tm = sum(",".join(r) in train for r in spec.train)
                if agree > best["agree"] or tm > best["train_rows_matched"]:
                    best = {"exact": guess == truth, "agree": agree, "train_rows_matched": tm}
        out[tid] = {**best, "n": len(truth), "keys_tried": 2 * len(cands)}
    return out


def chance_agreement(suite: Path, tid: str) -> float:
    """Expected agreement of a guess drawn from the training label distribution (classification only)."""
    ys = [r.rsplit(",", 1)[1] for r in _lines(suite / tid / "public" / "train.csv")[1:]]
    freq = {v: ys.count(v) / len(ys) for v in set(ys)}
    truth = _lines(suite / tid / "private" / "test_y.csv")[1:]
    return sum(freq.get(v, 0.0) for v in truth) / len(truth)


# ---------------------------------------------------------------- 4. preflight and minted sizes
Z95, Z80 = _N.inv_cdf(0.95), _N.inv_cdf(0.8)


def p_certify_normal(n: int, effect: float, delta: float, discordant: float) -> float:
    """Normal approximation of P(one-sided 95% lower bound of the paired accuracy difference >= delta)."""
    var = discordant - effect ** 2
    se = math.sqrt(var / n)
    return 1 - _N.cdf(Z95 - (effect - delta) / se)


def n_needed(effect: float, delta: float, discordant: float, power: float = 0.8) -> int:
    var = discordant - effect ** 2
    return math.ceil((Z95 + _N.inv_cdf(power)) ** 2 * var / (effect - delta) ** 2)


# ---------------------------------------------------------------- 5. salts exam
def tell_statistic(pres: Mapping[str, bytes]) -> float:
    """Agreement of the train labels with the family's reference rule (the capable spotter's statistic)."""
    from custom_harness.salts import generate as G

    rows = [r.split(",") for r in pres["public/train.csv"].decode().split("\n") if r]
    head, body = rows[0], rows[1:]
    fam = {"x1": "rings", "f1": "xor", "x": "spirals"}[head[0]]
    return sum(G.ORACLES[fam]([float(v) for v in r[:-1]]) == r[-1] for r in body) / len(body)


def salts_exam(suite: Path, doc: dict, work: Path, per_arm: int) -> dict[str, Any]:
    from custom_harness.salts import config as C
    from custom_harness.salts import generate as G
    from custom_harness.salts.exam import presentation, run_exam

    fams = sorted(G.POSITIVE_FAMILIES)
    sizes = {gen(seed, 1, key=b"\x00" * 32).task_id: n for gen, seed, n in S.SYNTHETIC}
    suite_tasks = [t for t in doc["tasks"] if t["family"] in fams]
    reals: list[Path] = [suite / t["id"] for t in suite_tasks]
    i = 0
    while len(reals) < per_arm:  # same generators, same sizes, fresh keys: more draws of "a real task"
        t = suite_tasks[i % len(suite_tasks)]
        spec = S.synthetic_task(t["family"], 100 + i, sizes[t["id"]], secrets.token_bytes(32))
        reals.append(S.write_task(spec, work / "real"))
        i += 1
    salts = []
    delta = 0.2  # as tests/test_salts.py's exam; a null salt (rho = 1/2) does not depend on delta for its labels' tell
    for k in range(per_arm):
        t = suite_tasks[k % len(suite_tasks)]
        g = G.null_salt(t["family"], sizes[t["id"]], key=secrets.token_bytes(32), task_id=f"exam-null-{k}",
                        delta=delta, out=work / "salt")
        salts.append((g.task_dir, t["family"]))
    key = secrets.token_bytes(32)
    blind = run_exam(salts, reals, lambda pres: False, key=key)
    capable = run_exam(salts, reals, lambda pres: tell_statistic(pres) < 0.85, key=key)
    stat_salt = sorted(tell_statistic(presentation(d)) for d, _f in salts)
    stat_real = sorted(tell_statistic(presentation(d)) for d in reals)
    # best threshold spotter (in-sample: an upper bound on what this one statistic can do)
    best = max(((sum(s < th for s in stat_salt) / len(stat_salt) - sum(r < th for r in stat_real) / len(stat_real),
                 th) for th in sorted(set(stat_salt + stat_real)) + [1.01]), key=lambda x: x[0])
    by_fam: dict[str, dict[str, list[float]]] = {}
    for sd, f in salts:
        by_fam.setdefault(f, {"salt": [], "real": []})["salt"].append(round(tell_statistic(presentation(sd)), 4))
    for d in reals:
        f = {"x1": "rings", "f1": "xor", "x": "spirals"}[_lines(d / "public" / "train.csv")[0].split(",")[0]]
        by_fam.setdefault(f, {"salt": [], "real": []})["real"].append(round(tell_statistic(presentation(d)), 4))

    def b(r: Any) -> dict[str, Any]:
        return {"tp": r.bound.tp, "n_salt": r.bound.n_salt, "fp": r.bound.fp, "n_real": r.bound.n_real,
                "d_hat": round(r.bound.d_hat, 4), "upper_95_one_sided": round(r.bound.upper, 4), "passed": r.passed}

    return {"per_arm": per_arm, "salt_kind": "null (rho = 1/2)", "delta": delta, "threshold": C.DETECTABILITY_MAX_UPPER,
            "suite_tasks_in_real_arm": [t["id"] for t in suite_tasks], "blind_spotter": b(blind),
            "capable_spotter_agree_lt_0.85": b(capable),
            "best_threshold_on_statistic": {"d": round(best[0], 4), "threshold": round(best[1], 4)},
            "statistic_by_family": {f: {k: {"min": min(v), "median": sorted(v)[len(v) // 2], "max": max(v)}
                                        for k, v in arms.items() if v} for f, arms in sorted(by_fam.items())}}


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("suite")
    ap.add_argument("--record-dir", required=True)
    ap.add_argument("--beacon-file", required=True)
    ap.add_argument("--old-suite", help="positive control for the PRNG regression (the archived pre-fix suite)")
    ap.add_argument("--out", required=True, help="JSON report (counts and paths only, no labels)")
    ap.add_argument("--skip-scan", action="store_true")
    ap.add_argument("--sims", type=int, default=100)
    ap.add_argument("--exam-per-arm", type=int, default=40)
    ap.add_argument("--only", default="isolation,scan,prng,preflight,minted,salts")
    ap.add_argument("--extra-root", action="append", default=[], help="another agent-readable root to scan")
    a = ap.parse_args()
    import custom_harness

    log("custom_harness from", custom_harness.__file__)
    suite = Path(a.suite)
    doc = yaml.safe_load((suite / "suite.yaml").read_text(encoding="utf-8"))
    tasks = doc["tasks"]
    manifest = json.loads((suite / "manifest.json").read_text(encoding="utf-8")) \
        if (suite / "manifest.json").is_file() else None
    beacon = RB.read_beacon(Path(a.beacon_file))
    only = set(a.only.split(","))
    report: dict[str, Any] = {"suite": str(suite), "tasks": len(tasks),
                              "manifest_sha256": RB.manifest_hash(manifest) if manifest else None}
    outp = Path(a.out)

    def save() -> None:
        outp.write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")

    if "isolation" in only:
        problems = S.check_split_isolation(tasks)
        report["isolation"] = {"problems": problems,
                               "per_task": {t["id"]: [p for p in problems if t["id"] in p] for t in tasks}}
        log("isolation problems:", len(problems))
        save()
    if "prng" in only:
        old = RB.load_generator_module(PREFIX)
        res = RB.prng_regression(suite, old)
        for tid, r in res.items():
            r["chance_agreement"] = round(chance_agreement(suite, tid), 4) if \
                next(t for t in tasks if t["id"] == tid)["family"] not in ("friedman", "interactions") else None
        report["prng_old_seeded"] = res
        report["prng_keyed_public_values"] = keyed_attack(suite, doc, manifest)
        if a.old_suite:
            report["prng_positive_control_old_suite"] = RB.prng_regression(Path(a.old_suite), old)
        log("prng exact (old seeded):", sum(r["exact"] for r in res.values()), "/", len(res))
        save()
    if "preflight" in only:
        from custom_harness.research.preflight import PreflightConfig, suite_audit

        dirs = [suite / t["id"] for t in tasks]
        rows = suite_audit(dirs, baseline_acc=0.8, family_size=1, config=PreflightConfig(n_sims=a.sims))
        report["preflight_suite_audit"] = {"assumption": "candidate perfect on dev vs a baseline of dev accuracy "
                                                         "0.8; family 1; as `chh preflight-suite`", "n_sims": a.sims,
                                           "rows": rows}
        log("preflight done")
        save()
    if "minted" in only:
        grid = []
        n_tests = {t["id"]: len(_lines(suite / t["id"] / "public" / "test_X.csv")) - 1 for t in tasks}
        for effect in (0.03, 0.04, 0.05, 0.10):
            for disc_extra in (0.05,):
                disc = effect + 2 * disc_extra
                grid.append({"true_effect": effect, "discordant_rate": round(disc, 4), "delta": S.DELTA_FLOOR,
                             "n_test_for_P0.8": n_needed(effect, S.DELTA_FLOOR, disc),
                             "p_certify_at_suite_n": {tid: round(p_certify_normal(n, effect, S.DELTA_FLOOR, disc), 3)
                                                      for tid, n in n_tests.items()},
                             "p_certify_at_MINT_TEST_ROWS": round(p_certify_normal(S.MINT_TEST_ROWS, effect,
                                                                                    S.DELTA_FLOOR, disc), 3)})
        report["minted_sizes_normal_approx"] = {"n_test": n_tests, "grid": grid}
        save()
    if "salts" in only:
        with tempfile.TemporaryDirectory(dir=a.record_dir) as tmp:
            report["salts_exam"] = salts_exam(suite, doc, Path(tmp), a.exam_per_arm)
        log("salts exam:", report["salts_exam"]["capable_spotter_agree_lt_0.85"])
        save()
    if "scan" in only and not a.skip_scan:
        roots = [*default_agent_readable_roots(), *(Path(r) for r in a.extra_root)]
        log("truth scan over", [str(r) for r in roots])
        report["truth_scan"] = host_truth_scan(suite, tasks, beacon, roots)
        log("truth scan done:", report["truth_scan"]["files_scanned"], "files")
        save()
    log("report:", outp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
