"""Zero-call checks of a (rebuilt) task suite, per task. No model call, no program run, no network.

    python scripts/suite_checks.py SUITE_DIR --record-dir DIR --beacon-file FILE [--old-suite DIR] [--out JSON]
                                   [--skip-scan] [--sims N]

TRUSTED audit code: it reads private labels, but only to SCORE attacks and scans; it writes no label anywhere (the
JSON report holds counts, hashes and paths only). Checks:
1. split isolation (research.suite.check_split_isolation);
2. host truth scan: every held-out row WITH its label, as a CSV line (the format of every train.csv), and every
   canary, searched line by line in every file under the agent-readable roots (commitment.default_agent_readable_roots,
   minus the by-design truth location (truth_layout.truth_dir; legacy <task>/private) and .git object stores);
   the same rows
   format-agnostically (numbers normalised to 6 significant digits, any delimiter, JSON-escaped newlines, an index
   column: see TokenIndex); plus the sealed beacon in every encoding (commitment.secret_leak_scan). The report and
   the record directory are refused inside an agent-readable root (the report scores attacks against the truth);
3. COUPLED-PRNG regression: the pre-fix seeded generators from the repo history (tests/fixtures/
   suite_prefix_401c00a.py) regenerate each synthetic task; plus the current generators keyed with every public
   value in the suite (manifest hash, key commitments, task ids). Positive control: the same attack on --old-suite;
4. certifiability preflight (research.preflight.suite_audit, as `chh preflight-suite`), and the minted test size
   that reaches P(certify) >= 0.8 at the 0.02 floor (normal approximation, labelled; spot-checked with the real
   preflight);
5. the salts detectability exam (salts.exam.run_exam) at the suite's own sizes, with a scripted spotter that reads
   public files only (0 calls), and the distribution of its statistic in each arm. Salts are the 2026-10-14 design
   (docs/salts_redesign.md): positive salts of the suite's reference-rule families; a null arm only when the suite
   has a genuinely null family.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import os
import re
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
from custom_harness.truth_layout import legacy_truth_dir, truth_dir, truth_file

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
    ys = _lines(truth_file(task_dir, "test_y.csv"))[1:]
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


# Format-agnostic form of the same search. A labelled copy need not be a CSV line in the suite's own format: other
# float formatting (8 decimals, 1e-05), other delimiters (tab, ';', spaces), an index column in front (pandas), or
# rows embedded in JSON/JSONL text whose newlines are the two characters `\n` (agent transcripts) are all found.
# Every number is normalised to 6 significant digits; text is cut into segments at newlines (real or escaped),
# brackets, braces, parentheses and double quotes; a segment is a hit when its numbers CONTAIN a held-out row's
# features in order with the label immediately after them ("label last", the train.csv order of every suite task)
# or immediately before them ("label first": reported apart, because an index column equal to the label also
# matches it). Not found: copies with fewer than 6 significant digits than the suite's own values carry
# (e.g. 3 decimals), binary formats (.npy, parquet, pickles: files with a NUL byte in their first 8 KiB are not
# token-scanned), and transformed or split copies.
# one pass over the bytes: group 1 is a number; a separator (real newline, escaped \n or \r, bracket, brace,
# parenthesis, double quote) matches with an empty group
_TOK = re.compile(rb'(?=[-+.0-9\r\n\[\]{}()"\\])'
                  rb'(?:([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)|[\r\n\[\]{}()"]|\\[rn])')


@functools.lru_cache(maxsize=1 << 20)
def norm_number(tok: bytes) -> str:
    """One number, normalised to 6 significant digits (`-0` is `0`)."""
    s = f"{float(tok):.6g}"
    return "0" if s == "-0" else s


def heldout_tuples(task_dir: Path) -> dict[str, set[tuple[str, ...]]]:
    """Every held-out row as normalised numbers: features then label, and label then features."""
    xs = _lines(task_dir / "public" / "test_X.csv")[1:]
    ys = _lines(truth_file(task_dir, "test_y.csv"))[1:]
    if len(xs) != len(ys):
        raise ValueError(f"{task_dir}: test_X and test_y differ in length")
    last, first = set(), set()
    for x, y in zip(xs, ys, strict=True):
        f = tuple(norm_number(v.encode()) for v in x.split(","))
        lab = (norm_number(y.encode()),)
        last.add(f + lab)
        first.add(lab + f)
    return {"label_last": last, "label_first": first}


class TokenIndex:
    """Normalised needles of every task, looked up by window length."""

    def __init__(self) -> None:
        self.by_len: dict[int, dict[tuple[str, ...], list[tuple[str, str]]]] = {}
        self.starts: dict[str, set[int]] = {}  # first number of a needle -> the needle lengths that start with it

    def add(self, needle: tuple[str, ...], tid: str, form: str) -> None:
        self.by_len.setdefault(len(needle), {}).setdefault(needle, []).append((tid, form))
        self.starts.setdefault(needle[0], set()).add(len(needle))

    def scan(self, raw: bytes) -> list[tuple[tuple[str, ...], str, str]]:
        """(needle, task id, form) for every window of a segment of `raw` that equals a needle."""
        out: list[tuple[tuple[str, ...], str, str]] = []
        shortest = min(self.by_len)
        segs: list[list[bytes]] = [[]]
        for tok in _TOK.findall(raw):  # one pass: a number, or b"" for a segment separator
            if tok:
                segs[-1].append(tok)
            elif segs[-1]:
                segs.append([])
        for toks in segs:
            if len(toks) >= shortest:
                self._scan_segment([norm_number(t) for t in toks], out)
        return out

    def _scan_segment(self, vals: list[str], out: list[tuple[tuple[str, ...], str, str]]) -> None:
        starts = self.starts
        for i, v in enumerate(vals):
            lens = starts.get(v)
            if not lens:
                continue
            for n in lens:
                if i + n <= len(vals):
                    w = tuple(vals[i:i + n])
                    hit = self.by_len[n].get(w)
                    if hit:
                        out += [(w, tid, form) for tid, form in hit]


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
    # a needle may belong to several tasks (a repair task shares its real task's split, rows and canary)
    needles: dict[bytes, list[str]] = {}
    canaries: dict[bytes, list[str]] = {}
    n_heldout: dict[str, int] = {}
    index = TokenIndex()
    for t in tasks:
        d = suite / t["id"]
        lines = heldout_lines(d)
        n_heldout[t["id"]] = len(lines)
        for ln in lines:
            needles.setdefault(ln, []).append(t["id"])
        for form, tuples in heldout_tuples(d).items():
            for tup in tuples:
                index.add(tup, t["id"], form)
        canaries.setdefault(truth_file(d, "canary.txt").read_text(encoding="utf-8").strip().encode(),
                            []).append(t["id"])
    # the by-design truth locations (truth_layout: outside the roots for a suite under <data>/suites; the legacy
    # in-task private/ for an archived or scratch suite)
    skip = [p.resolve() for t in tasks for p in {truth_dir(suite / t["id"]), legacy_truth_dir(suite / t["id"])}]
    hits: dict[str, dict[str, int]] = {t["id"]: {} for t in tasks}
    found: dict[str, set[bytes]] = {t["id"]: set() for t in tasks}
    agn_files: dict[str, dict[str, dict[str, int]]] = {t["id"]: {"label_last": {}, "label_first": {}} for t in tasks}
    agn_found: dict[str, dict[str, set[tuple[str, ...]]]] = {t["id"]: {"label_last": set(), "label_first": set()}
                                                             for t in tasks}
    canary_hits: dict[str, list[str]] = {t["id"]: [] for t in tasks}
    n_files = skipped_big = unreadable = not_token_scanned = 0
    for f in _walk(roots, skip):
        n_files += 1
        if n_files % 5000 == 0:
            log(f"truth scan: {n_files} files, now in {f.parent}")
        try:
            if f.stat().st_size > MAX_READ:
                skipped_big += 1
                continue
            raw = f.read_bytes()
        except OSError:
            unreadable += 1
            continue
        for c, ctids in canaries.items():
            if c in raw:
                for ctid in ctids:
                    canary_hits[ctid].append(str(f))
        if b"\x00" in raw[:8192]:
            not_token_scanned += 1
        else:
            for tup, tid, form in index.scan(raw):
                fh = agn_files[tid][form]
                fh[str(f)] = fh.get(str(f), 0) + 1
                agn_found[tid][form].add(tup)
        if b"," not in raw:
            continue
        for ln in raw.splitlines():
            key = ln.rstrip(b"\r")
            for tid in needles.get(key, ()):
                hits[tid][str(f)] = hits[tid].get(str(f), 0) + 1
                found[tid].add(key)
    beacon_leaks = secret_leak_scan(beacon.encode(), roots)
    per_task = {}
    for t in tasks:
        tid = t["id"]
        rows = sum(hits[tid].values())
        agn = {form: {"distinct_heldout_rows_found": len(agn_found[tid][form]), "n_files": len(fh),
                      "files": sorted(fh, key=fh.__getitem__, reverse=True)[:5]}
               for form, fh in agn_files[tid].items()}
        per_task[tid] = {"labelled_heldout_lines_found": rows, "distinct_heldout_rows_found": len(found[tid]),
                         "heldout_rows": n_heldout[tid],
                         "files": sorted(hits[tid], key=lambda k: -hits[tid][k])[:5],
                         "n_files": len(hits[tid]), "canary_found_in": canary_hits[tid][:5],
                         "format_agnostic": agn}
    return {"roots": [str(r) for r in roots], "files_scanned": n_files, "skipped_over_64MB": skipped_big,
            "unreadable": unreadable, "not_token_scanned_binary": not_token_scanned, "beacon_leaks": beacon_leaks,
            "per_task": per_task}


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
        truth = _lines(truth_file(suite / tid, "test_y.csv"))[1:]
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
    truth = _lines(truth_file(suite / tid, "test_y.csv"))[1:]
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


def _task_sizes(t: dict) -> tuple[int, int | None]:
    """A suite task's generator sizes (n_rows, heldout_rows): suite.yaml provenance, else the build plan's rows."""
    prov = t.get("provenance") or {}
    n = prov.get("rows")
    if n is None:
        plan = {gen(seed, 1, key=b"\x00" * 32).task_id: rows
                for gen, seed, rows in S.synthetic_plan(tuple(sorted(S.NULL_FAMILIES)))}
        n = plan[t["id"]]
    return int(n), prov.get("heldout_rows")


def _exam_arm(suite: Path, suite_tasks: list[dict], kind: str, work: Path, per_arm: int, pool: set[str],
              mc_rows: int) -> dict[str, Any]:
    """One exam arm (salts.exam.run_exam, unchanged): `per_arm` salts of `kind` against the suite's own tasks of the
    salted families topped up with fresh draws of the SAME generators at the SAME sizes (n_rows, heldout_rows). Salts
    cycle over the same task list, so both arms have the same family and size mix."""
    from custom_harness.salts import config as C
    from custom_harness.salts import generate as G
    from custom_harness.salts.exam import presentation, run_exam

    reals: list[tuple[Path, str]] = [(suite / t["id"], t["family"]) for t in suite_tasks]
    i = 0
    while len(reals) < per_arm:  # more draws of "a real task": same generator, same sizes, fresh keys
        t = suite_tasks[i % len(suite_tasks)]
        n, ho = _task_sizes(t)
        spec = S.synthetic_task(t["family"], 100 + i, n, secrets.token_bytes(32), heldout_rows=ho)
        reals.append((S.write_task(spec, work / f"real-{kind}"), t["family"]))
        i += 1
    salts: list[tuple[Path, str]] = []
    planted: dict[str, list[float]] = {}
    for k in range(per_arm):
        t = suite_tasks[k % len(suite_tasks)]
        n, ho = _task_sizes(t)
        if kind == "positive":
            g = G.positive_salt(t["family"], n, key=secrets.token_bytes(32), task_id=f"exam-pos-{k}",
                                out=work / "salt-pos", pool_families=pool, heldout_rows=ho, mc_rows=mc_rows)
            planted.setdefault(t["family"], []).append(round(g.planted_effect, 4))
        else:
            g = G.null_salt(t["family"], n, key=secrets.token_bytes(32), task_id=f"exam-null-{k}",
                            out=work / "salt-null", pool_families=pool, heldout_rows=ho)
        salts.append((g.task_dir, t["family"]))
    key = secrets.token_bytes(32)
    real_dirs = [d for d, _f in reals]
    blind = run_exam(salts, real_dirs, lambda pres: False, key=key)
    capable = run_exam(salts, real_dirs, lambda pres: tell_statistic(pres) < 0.85, key=key)
    stat_salt = sorted(tell_statistic(presentation(d)) for d, _f in salts)
    stat_real = sorted(tell_statistic(presentation(d)) for d in real_dirs)
    # best threshold spotter (in-sample: an upper bound on what this one statistic can do)
    best = max(((sum(s < th for s in stat_salt) / len(stat_salt) - sum(r < th for r in stat_real) / len(stat_real),
                 th) for th in sorted(set(stat_salt + stat_real)) + [1.01]), key=lambda x: x[0])
    by_fam: dict[str, dict[str, list[float]]] = {}
    for arm, items in (("salt", salts), ("real", reals)):
        for d, f in items:  # family from the exam's own records (noise and xor share a header)
            by_fam.setdefault(f, {"salt": [], "real": []})[arm].append(round(tell_statistic(presentation(d)), 4))

    def b(r: Any) -> dict[str, Any]:
        return {"tp": r.bound.tp, "n_salt": r.bound.n_salt, "fp": r.bound.fp, "n_real": r.bound.n_real,
                "d_hat": round(r.bound.d_hat, 4), "upper_95_one_sided": round(r.bound.upper, 4), "passed": r.passed}

    out: dict[str, Any] = {
        "salt_kind": kind, "per_arm": per_arm, "threshold": C.DETECTABILITY_MAX_UPPER,
        "families": sorted({t["family"] for t in suite_tasks}),
        "suite_tasks_in_real_arm": [t["id"] for t in suite_tasks], "blind_spotter": b(blind),
        "capable_spotter_agree_lt_0.85": b(capable),
        "best_threshold_on_statistic": {"d": round(best[0], 4), "threshold": round(best[1], 4)},
        "statistic_by_family": {f: {k: {"min": min(v), "median": sorted(v)[len(v) // 2], "max": max(v)}
                                    for k, v in arms.items() if v} for f, arms in sorted(by_fam.items())}}
    if kind == "positive":
        out["planted_effect_mc"] = {"mc_rows": mc_rows, "by_family": {
            f: {"min": min(v), "median": sorted(v)[len(v) // 2], "max": max(v)} for f, v in sorted(planted.items())}}
    return out


def salts_exam(suite: Path, doc: dict, work: Path, per_arm: int, mc_rows: int = 2_000) -> dict[str, Any]:
    """The detectability exam on the redesigned salts (decision 2026-10-14, docs/salts_redesign.md): a positive arm
    (positive salts of the suite's reference-rule families vs real tasks of those families) and, only when the suite
    has a genuinely null family, a null arm (null salts of it vs its real tasks). Salts are fresh draws of the same
    generators, so a pass is expected by construction; this run MEASURES it. `mc_rows` only sets the Monte Carlo
    behind the reported planted effects (it does not change any file the spotter sees)."""
    from custom_harness.salts import generate as G

    pool = {t["family"] for t in doc["tasks"]}
    report: dict[str, Any] = {"design": "fresh generator draws (decision 2026-10-14)", "per_arm": per_arm}
    pos_tasks = [t for t in doc["tasks"] if t["family"] in G.POSITIVE_FAMILIES]
    if pos_tasks:
        report["positive_arm"] = _exam_arm(suite, pos_tasks, "positive", work, per_arm, pool, mc_rows)
    else:
        report["positive_arm"] = {"not_run": "the suite has no task of a reference-rule family"}
    null_tasks = [t for t in doc["tasks"] if t["family"] in G.NULL_FAMILIES]
    if null_tasks:
        report["null_arm"] = _exam_arm(suite, null_tasks, "null", work, per_arm, pool, mc_rows)
    else:
        report["null_arm"] = {"not_run": "the suite has no genuinely null family (build with --null-family noise: "
                                         "owner decision, GOAL §13.8); no null salt may be drawn for it"}
    return report


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
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
    a = ap.parse_args(argv)
    # the report scores attacks against held-out truth (aggregate counts): it belongs with the private record
    readable = default_agent_readable_roots(existing_only=False)
    for what, p in (("report", Path(a.out)), ("record directory", Path(a.record_dir))):
        inside = RB._inside(p, readable)
        if inside is not None:
            print(f"refused: the {what} {p} is inside agent-readable root {inside}; write it outside every "
                  f"agent-readable root (e.g. <record-dir>/reports)", file=sys.stderr)
            return 2
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
        log("salts exam:", {arm: report["salts_exam"][arm].get("capable_spotter_agree_lt_0.85",
                                                                 report["salts_exam"][arm].get("not_run"))
                            for arm in ("positive_arm", "null_arm")})
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
