"""Clean-room differential test at scale (GOAL §16.6: the reference model vs the Kernel on 1e5 generated histories).

    python scripts/diffscale.py --state DIR --total 100000 --chunk 250 --budget-s 1500   # one slice (resumable)
    python scripts/diffscale.py --state DIR --report                                      # totals so far

The histories come from the same Hypothesis strategy as the gate's differential test (`tests/spec/gen.py`,
`gen.scripts()`), replayed on both sides by `gen.run` (the triaged clean-room model `tests/spec/kernel_model.py` and
the real Kernel in a fresh store). The gate test runs 25 derandomized examples; this driver runs many CHUNKS, each
with its own fixed seed (base seed + chunk index, generate phase only, no shrinking, no example database), so:
- seeded: chunk i always generates the same histories for the same code (Hypothesis is deterministic per seed);
- chunked and resumable: every finished chunk is one line in `DIR/chunks.jsonl`; a slice skips finished chunks;
- time-budgeted: a slice starts no new chunk after `--budget-s` seconds (run it through the machine limiter);
- honest counts: `n_run` counts the histories actually replayed; every history's digest (sha256 of its repr) is
  kept, so the report states how many were DISTINCT, not only how many ran;
- fast store mode (CHH_SPEC_FAST, tests/spec/real_kernel.py): each world starts from a byte copy of one store in which
  the ML adapter was approved, with sqlite fsync off. Durability only; no Kernel rule reads either. CHH_DIFFSCALE_SLOW=1
  runs the gate's exact setup instead (see docs/assure.md for the measured speeds);
- code-pinned: a state directory belongs to one code hash (Kernel source + the model and generator files); resuming
  with other code is refused, so results of two Kernel versions are never added up.

Every disagreement is written in full (the script's repr and the differing check) to `DIR/disagreements.jsonl` and
must become a finding in redteam/findings.yaml. Nothing here edits the Kernel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "tests" / "spec"
PINNED = ("tests/spec/gen.py", "tests/spec/history.py", "tests/spec/kernel_model.py", "tests/spec/model_triage.py",
          "tests/spec/real_kernel.py")


def code_hash(root: Path = ROOT) -> str:
    h = hashlib.sha256()
    files = sorted((root / "src" / "custom_harness" / "kernel").glob("*.py"))
    for f in [*files, *(root / p for p in PINNED)]:
        h.update(f.relative_to(root).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def _lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def run_chunk(index: int, seed: int, size: int) -> tuple[int, int, list[dict], list[str]]:
    """Replay `size` generated histories with Hypothesis seed `seed`; (n_run, n_cert, disagreements, digests)."""
    if str(SPEC) not in sys.path:
        sys.path.insert(0, str(SPEC))
    os.environ.setdefault("CHH_ALLOW_UNISOLATED", "1")
    os.environ["CHH_SPEC_FAST"] = "0" if os.environ.get("CHH_DIFFSCALE_SLOW") == "1" else "1"
    import gen
    from hypothesis import HealthCheck, Phase, given, settings
    from hypothesis import seed as hseed

    n_run = n_cert = 0
    dis: list[dict] = []
    digests: list[str] = []

    @hseed(seed)
    @settings(max_examples=size, deadline=None, database=None, phases=(Phase.generate,),
              suppress_health_check=list(HealthCheck))
    @given(gen.scripts())
    def one(script: list) -> None:
        nonlocal n_run, n_cert
        text = repr(script)
        try:
            _hist, out, nc = gen.run(script)
            found = [repr(d) for d in out]
        except Exception as e:  # noqa: BLE001  a crash on either side is a disagreement, never a skipped history
            nc, found = 0, [f"crash: {type(e).__name__}: {str(e)[:300]}"]
        n_run += 1
        n_cert += nc
        digests.append(hashlib.sha256(text.encode()).hexdigest()[:20])
        if found:
            dis.append({"chunk": index, "seed": seed, "script": text, "disagreements": found})

    one()
    return n_run, n_cert, dis, digests


def report(state: Path) -> dict:
    chunks = _lines(state / "chunks.jsonl")
    digests: set[str] = set()
    for c in chunks:
        digests |= set((state / "digests" / f"{c['chunk']}.txt").read_text(encoding="utf-8").split())
    finished = {c["chunk"] for c in chunks}  # a chunk interrupted before its line was written is re-run: dedupe
    dis = list({(d["chunk"], d["script"]): d for d in _lines(state / "disagreements.jsonl")
                if d["chunk"] in finished}.values())
    secs = sum(c["secs"] for c in chunks)
    n = sum(c["n_run"] for c in chunks)
    out = {"chunks": len(chunks), "histories_run": n, "distinct_histories": len(digests),
           "certifications": sum(c["n_cert"] for c in chunks), "disagreeing_histories": len(dis),
           "secs": round(secs, 1), "histories_per_s": round(n / secs, 3) if secs else None,
           "code_hash": chunks[0]["code_hash"] if chunks else None}
    print(json.dumps(out), flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True)
    ap.add_argument("--total", type=int, default=100_000)
    ap.add_argument("--chunk", type=int, default=250)
    ap.add_argument("--base-seed", type=int, default=20261009)
    ap.add_argument("--budget-s", type=float, default=1500.0)
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args(argv)
    state = Path(a.state)
    state.mkdir(parents=True, exist_ok=True)
    (state / "digests").mkdir(exist_ok=True)
    if a.report:
        report(state)
        return 0
    ch = code_hash()
    cfg = {"total": a.total, "chunk": a.chunk, "base_seed": a.base_seed, "code_hash": ch,
           "fast": os.environ.get("CHH_DIFFSCALE_SLOW") != "1"}
    cfg_path = state / "config.json"
    if cfg_path.exists():
        old = json.loads(cfg_path.read_text(encoding="utf-8"))
        if old != cfg:
            print(f"diffscale: state {state} belongs to {old}, not {cfg}: refusing to mix runs", flush=True)
            return 2
    else:
        cfg_path.write_text(json.dumps(cfg, sort_keys=True), encoding="utf-8")
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                          check=False).stdout.strip()
    done = {c["chunk"] for c in _lines(state / "chunks.jsonl")}
    n_chunks = -(-a.total // a.chunk)
    t0 = time.time()
    ran = 0
    for i in range(n_chunks):
        if i in done:
            continue
        if time.time() - t0 > a.budget_s:
            break
        size = min(a.chunk, a.total - i * a.chunk)
        t = time.time()
        n, nc, dis, digests = run_chunk(i, a.base_seed + i, size)
        secs = time.time() - t
        (state / "digests" / f"{i}.txt").write_text("\n".join(digests) + "\n", encoding="utf-8")
        with (state / "disagreements.jsonl").open("a", encoding="utf-8") as f:
            for d in dis:
                f.write(json.dumps(d) + "\n")
        with (state / "chunks.jsonl").open("a", encoding="utf-8") as f:  # last: a chunk counts once fully written
            f.write(json.dumps({"chunk": i, "seed": a.base_seed + i, "n_run": n, "n_cert": nc,
                                "n_disagree": len(dis), "secs": round(secs, 2), "head": head, "code_hash": ch}) + "\n")
        ran += 1
        print(f"chunk {i}: {n} histories, {nc} certifications, {len(dis)} disagreeing, {secs:.0f}s", flush=True)
    print(f"diffscale: slice ran {ran} chunk(s) in {time.time() - t0:.0f}s", flush=True)
    rep = report(state)
    return 1 if rep["disagreeing_histories"] else 0


if __name__ == "__main__":
    sys.exit(main())
