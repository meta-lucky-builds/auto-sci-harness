"""Build and re-check the honest corpus (GOAL §16.6, invariant 20). No model call; deterministic.

    python scripts/honest_corpus.py --build                       # writes custom-harness-data/assure/honest-corpus/v1
                                                                  # and the committed manifest redteam/honest_corpus/
    python scripts/honest_corpus.py --check --half dev            # the verifier must certify every honest item and
                                                                  # reject every planted fault (dev half)
    python scripts/honest_corpus.py --check --half sealed --by NAME --reason WHY   # ledgered look (sealed.py ledger)

Sources (each an honest item plus a planted-fault counterpart, a PAIR):
  spec      clean-room spec worlds (tests/spec/real_kernel.py): generated from a fixed seed per pair; the clean-room
            reference model (tests/spec/kernel_model.py + model_triage.py) must give the SAME label as the Kernel, or
            the build fails (a disagreement is a finding, never a corpus item)
  world     the Kernel test world (tests/kernel_world.py): real trusted programs re-executed in LocalSandbox
  research  the scripted research run (tests/test_research.py `scripted_llm`, examples/research_v1.yaml): the loop's
            own certificate is the honest item; its counterpart re-certifies the same claim with half its evidence

Planted faults (spec): peek before registration, a develop read of the target, a byte-identical twin of the target
under another id, a dropped record, a stray seed, an unregistered claim, no real effect, and a development dataset
sharing the target's held-out examples. World: peek, an inflated reported score, a dropped seed, a weak subject.

Halves are per pair (looks.half_of("honest-corpus", pair id)). Item and pair ids are opaque hashes, so an id never
tells honest from fault. The committed manifest carries dev items' expected records and only a commitment hash for
sealed items. Determinism: two builds give identical item digests (recipe, claim hash, label, check statuses); the
store bytes differ (wall-clock timestamps, random evidence ids) and are not part of the digest.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
for sub in ("tests/spec", "tests"):
    if str(ROOT / sub) not in sys.path:
        sys.path.append(str(ROOT / sub))

from custom_harness.assure import corpus, looks  # noqa: E402
from custom_harness.kernel import KernelConfig  # noqa: E402
from custom_harness.kernel.bundle import build_bundle  # noqa: E402

SPEC_FAULTS = ("peek_audit", "develop_on_target", "twin_before", "drop_record", "stray_seed", "unregistered",
               "no_effect", "dev_shares_heldout")
WORLD_FAULTS = ("peek_audit", "inflated_score", "drop_seed", "weak_subject")
FIXED_RESOLVE_BY = 4_102_444_800.0


def opaque(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in (corpus.VERSION, *parts)).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ spec worlds
def spec_recipe(i: int, fault: str) -> dict[str, Any]:
    r = random.Random(f"honest-corpus:{corpus.VERSION}:spec:{i}")
    n_t = r.choice((1, 1, 2))
    targets = [{"id": f"t{k}", "role": r.choice(("test", "test", "external")), "start": 100 + 100 * k,
                "token": f"T{k}"} for k in range(n_t)]
    dev = r.random() < 0.4
    prior = r.choice((0, 0, 1, 2))
    noise = r.random() < 0.5
    return {"targets": targets, "dev": dev, "prior": prior, "noise": noise, "n_comparisons": r.randint(1, 3),
            "fault": fault}


def spec_events(rec: dict[str, Any], honest: bool) -> tuple[list[Any], Any, tuple[Any, ...]]:
    import gen
    from history import ApproveRole, Certify, ClaimSpec, Dataset, Prereg, Read, Record

    fault = None if honest else rec["fault"]
    ex = {t["id"]: frozenset(range(t["start"], t["start"] + 40)) for t in rec["targets"]}
    events: list[Any] = [Dataset(t["id"], t["role"], "features", ex[t["id"]], t["token"]) for t in rec["targets"]]
    t0 = rec["targets"][0]["id"]
    dev_ids: tuple[str, ...] = ()
    if rec["dev"] or fault == "dev_shares_heldout":
        shared = fault == "dev_shares_heldout"
        events.append(Dataset("devset", "dev", "features", ex[t0] if shared else frozenset(range(900, 940)),
                              "DEVTOKEN"))
        dev_ids = ("devset",)
    events += list(gen.ARTIFACTS)
    events += [ApproveRole(t["id"]) for t in rec["targets"] if t["role"] == "external"]
    for j in range(rec["prior"]):  # earlier honest claims on unrelated targets (irrelevant history)
        u = Dataset(f"u{j}", "test", "features", frozenset(range(2000 + 100 * j, 2040 + 100 * j)), f"U{j}")
        pc = ClaimSpec(key=f"prior{j}", kind="improvement", subject="m2", comparator="b1", development=(),
                       targets=(u.id,), seeds=gen.SEEDS, alpha=0.05, n_comparisons=1, min_effect=0.1, strong=True)
        events += [u, Prereg(pc), *gen.eval_reads(pc), Certify(pc, gen.honest_records(pc))]
    if rec["noise"]:
        events += [Dataset("z", "test", "features", frozenset(range(5000, 5040)), "Z"), Read("z", "audit"),
                   Read("z", "develop")]
    c = ClaimSpec(key="c", kind="improvement", subject="m1", comparator="b1", development=dev_ids,
                  targets=tuple(t["id"] for t in rec["targets"]), seeds=gen.SEEDS, alpha=0.05,
                  n_comparisons=rec["n_comparisons"], min_effect=0.1, strong=fault != "no_effect")
    if fault == "peek_audit":
        events.append(Read(t0, "audit"))
    if fault == "develop_on_target":
        events.append(Read(t0, "develop"))
    if fault == "twin_before":
        events.append(Dataset("t_twin", "test", "features", ex[t0], rec["targets"][0]["token"]))
    if fault != "unregistered":
        events.append(Prereg(c))
    events += gen.eval_reads(c)
    records = list(gen.honest_records(c))
    if fault == "drop_record":
        records.pop(len(records) // 2)
    if fault == "stray_seed":
        records.append(Record("subject", 7, t0))
    return events, c, tuple(records)


def build_spec_item(rec: dict[str, Any], honest: bool, work: Path) -> tuple[dict[str, Any], Path]:
    import model_triage
    from history import Certify, Dataset
    from real_kernel import RealWorld

    events, c, records = spec_events(rec, honest)
    world = RealWorld(work / "world")
    history: list[Any] = []
    try:
        for ev in events:
            if isinstance(ev, Dataset) and ev.id in world.datasets:
                continue
            history += world.apply(ev)
        expect = model_triage.verdict(tuple(history), c, records)
        world.apply(Certify(c, records))
        got = world.last
        cert = world.last_cert
        if expect.label != got.label or expect.checks != got.checks:
            raise SystemExit(f"honest_corpus: the clean-room model and the Kernel DISAGREE on spec recipe {rec} "
                             f"(honest={honest}): model {expect.label} {expect.checks}, Kernel {got.label} "
                             f"{got.checks}. This is a finding, not a corpus item.")
        return _finish(world.kernel, world.store, cert, work, model_label=expect.label)
    finally:
        world.close()


# ------------------------------------------------------------------ Kernel test world
def build_world_item(seed: int, fault: str | None, work: Path) -> tuple[dict[str, Any], Path]:
    from kernel_world import World

    world = World(work / "world", seed=seed)
    world.kernel.clock = lambda: 1.0
    try:
        good = world.artifact("good")
        base = world.artifact("baseline", kind="baseline")  # K5: a registered BASELINE comparator
        subject = world.artifact("weak") if fault == "weak_subject" else good
        claim = world.claim(subject, base, delta=0.15).model_copy(update={"resolve_by": FIXED_RESOLVE_BY})
        if fault == "peek_audit":
            world.vault.read("test", "test_y.csv", purpose="audit")
        world.kernel.preregister(claim)
        ids = world.evaluate_all(claim, score_offset=0.05 if fault == "inflated_score" else 0.0)
        if fault == "drop_seed":
            ids = ids[:-2]
        cert = world.kernel.certify(claim, ids)
        trusted = sorted({claim.subject.hash, claim.comparator.hash})
        return _finish(world.kernel, world.store, cert, work, replayer="local-trusted", trusted=trusted)
    finally:
        world.close()


# ------------------------------------------------------------------ scripted research run
@contextlib.contextmanager
def _env(**kv: str) -> Iterator[None]:
    old = {k: os.environ.get(k) for k in kv}
    os.environ.update(kv)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def build_research_pair(work: Path) -> list[tuple[dict[str, Any], Path]]:
    import yaml

    import test_research as tr
    from custom_harness.gateway import MockBackend
    from custom_harness.kernel import Claim, Kernel
    from custom_harness.records import RunStatus
    from custom_harness.runtime import Engine, HarnessSpec
    from custom_harness.sandbox import LocalSandbox
    from custom_harness.store import EvidenceStore

    task = work / "rings"
    subprocess.run([sys.executable, str(ROOT / "examples/tasks/make_rings_task.py"), str(task), "--n-train", "400",
                    "--n-test", "300"], check=True, capture_output=True)
    with _env(CHH_DATA_DIR=str(work / "data"), CHH_ALLOW_SAME_FAMILY="1", CHH_ALLOW_UNISOLATED="1"):
        store = EvidenceStore(work / "store")
        try:
            kernel = Kernel(store)
            kernel.approve_adapter("ml", approver="honest-corpus-builder")
            llm = MockBackend(tr.scripted_llm)
            spec = HarnessSpec.model_validate(yaml.safe_load(tr.SPEC.read_text()))
            res = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm},
                         store=store, sandbox=LocalSandbox(work / "ws", i_understand_no_isolation=True)).run(
                "rings", {"task_dir": str(task)})
            if res.status != RunStatus.COMPLETED:
                raise SystemExit(f"honest_corpus: the scripted research run did not complete: {res.detail}")
            honest = kernel.certificate(res.state["certificate"]["hash"])
            claim = Claim.model_validate(honest.claim)
            from custom_harness.kernel.execution import SandboxReplayer
            kernel.replayer = SandboxReplayer(lambda key: LocalSandbox(work / "replay" / key,
                                                                       i_understand_no_isolation=True))
            ids = list(honest.evidence_ids)
            fault = kernel.certify(claim, ids[: len(ids) // 2])  # planted: half the evidence (dropped seeds)
            trusted = sorted({claim.subject.hash, claim.comparator.hash})
            out = []
            for cert in (honest, fault):
                d = work / f"item-{cert.cert_hash()[:8]}"
                d.mkdir()
                out.append(_finish(kernel, store, cert, d, replayer="local-trusted", trusted=trusted,
                                   close_store=False))
        finally:
            store.close()
    for _exp, item_dir in out:  # one store holds both certificates; each item re-derives as of its own
        corpus.pack_store(work / "store", item_dir / "store.zip")
    return out


# ------------------------------------------------------------------ common
def _finish(kernel: Any, store: Any, cert: Any, work: Path, *, replayer: str = "stored",
            trusted: list[str] | None = None, model_label: str | None = None,
            close_store: bool = True) -> tuple[dict[str, Any], Path]:
    out = work / "item"
    out.mkdir(parents=True, exist_ok=True)
    h = cert.cert_hash()
    entry = next(e for e in kernel.ledger.entries("certificate") if e.body.get("cert_hash") == h)
    (out / "ledger.jsonl").write_text(corpus.ledger_lines(kernel, entry.seq), encoding="utf-8", newline="")
    build_bundle(kernel, h, out / "bundle.zip")
    expected = {"cert_hash": h, "claim_hash": cert.claim_hash, "cert_seq": entry.seq,
                "evidence_ids": list(cert.evidence_ids), "label": cert.label.value,
                "checks": {k: v.status for k, v in cert.checks.items()}, "config": cert.config,
                "kernel_version": cert.kernel_version, "replayer": replayer, "trusted": trusted or [],
                "model_label": model_label}
    if close_store:
        store.close()
        corpus.pack_store(Path(store.root), out / "store.zip")
    return expected, out


def build(root: Path, n_spec: int, n_world: int, research: bool, manifest_path: Path) -> dict[str, Any]:
    os.environ["CHH_ALLOW_UNISOLATED"] = "1"  # trusted repository programs only (kernel_world, scripted research)
    version_dir = root / corpus.VERSION
    if version_dir.exists():
        archive = root / f"{corpus.VERSION}.replaced-{time.strftime('%Y%m%d-%H%M%S')}"
        version_dir.rename(archive)  # never delete data: the previous build is archived
        print(f"honest_corpus: archived the previous build to {archive}", flush=True)
    entries: list[dict[str, Any]] = []
    trusted: set[str] = set()

    def add(pair_key: str, source: str, recipe: dict[str, Any], role: str, exp: dict[str, Any], src: Path,
            fault: str | None) -> None:
        pair = "hp-" + opaque("pair", pair_key)
        item = "hc-" + opaque("item", pair_key, role)
        half = looks.half_of(corpus.SET_NAME, pair)
        want_ok = role == "honest"
        if exp["label"].startswith("CERTIFIED") != want_ok:
            raise SystemExit(f"honest_corpus: {source} {pair_key} {role} ({fault}) got {exp['label']} "
                             f"{exp['checks']}: the verifier does not {'certify' if want_ok else 'reject'} it. This "
                             f"is a finding (or a recipe bug), never silently dropped.")
        exp = {"id": item, "pair": pair, "role": role, "source": source, "recipe": recipe, "fault": fault, **exp}
        exp["digest"] = corpus.digest(exp)
        dest = version_dir / half / item
        shutil.copytree(src, dest)
        (dest / "expected.json").write_text(json.dumps(exp, sort_keys=True, indent=1), encoding="utf-8",
                                            newline="")
        trusted.update(exp["trusted"])
        e = {"id": item, "pair": pair, "half": half, "digest": exp["digest"]}
        e.update({"expected": exp} if half == "dev" else {"commitment": corpus.commitment(exp)})
        entries.append(e)
        shown = f"{role:6s} -> dev    {exp['label']}" if half == "dev" else "-> sealed (role and label not printed)"
        print(f"  {source:8s} {pair_key:12s} {shown}", flush=True)

    with tempfile.TemporaryDirectory(prefix="honest-corpus-") as tmp:
        work = Path(tmp)
        for i in range(n_spec):
            fault = SPEC_FAULTS[i % len(SPEC_FAULTS)]
            rec = spec_recipe(i, fault)
            for role in ("honest", "fault"):
                exp, src = build_spec_item(rec, role == "honest", work / f"spec{i}{role}")
                add(f"spec:{i}", "spec", rec, role, exp, src, None if role == "honest" else fault)
        for s in range(n_world):
            fault = WORLD_FAULTS[s % len(WORLD_FAULTS)]
            rec = {"world_seed": s, "subject": "good", "comparator": "baseline", "delta": 0.15, "fault": fault}
            for role in ("honest", "fault"):
                exp, src = build_world_item(s, None if role == "honest" else fault, work / f"world{s}{role}")
                add(f"world:{s}", "world", rec, role, exp, src, None if role == "honest" else fault)
        if research:
            (h_exp, h_dir), (f_exp, f_dir) = build_research_pair(work / "research")
            rec = {"spec": "examples/research_v1.yaml", "llm": "tests/test_research.py::scripted_llm",
                   "fault": "half_the_evidence"}
            add("research:0", "research", rec, "honest", h_exp, h_dir, None)
            add("research:0", "research", rec, "fault", f_exp, f_dir, "half_the_evidence")
    man = {"version": corpus.VERSION, "builder": "scripts/honest_corpus.py",
           "sources": {"spec": n_spec, "world": n_world, "research": int(research)},
           "split": "looks.half_of('honest-corpus', pair id)", "trusted_programs": sorted(trusted),
           "items": sorted(entries, key=lambda e: e["id"])}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(man, sort_keys=True, indent=1) + "\n", encoding="utf-8", newline="")
    counts = {h: sum(e["half"] == h for e in entries) for h in ("dev", "sealed")}
    print(f"honest_corpus: built {len(entries)} items ({len(entries) // 2} pairs) {counts} -> {version_dir}",
          flush=True)
    return man


def _git_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                          check=False).stdout.strip()


def check(half: str, root: Path, config: KernelConfig | None, by: str, reason: str, ledger: Path,
          manifest_path: Path = corpus.MANIFEST) -> int:
    man = corpus.manifest(manifest_path)
    look = None
    if half == "sealed":
        look = corpus.open_sealed(by, reason, ledger=ledger, man=man, head=_git_head(), purpose="corpus-check")
        items = corpus.sealed_items(look, root, man)
    else:
        items = corpus.dev_items(root, man)
    res = corpus.check_all(items, config, progress=lambda r: print(
        f"  {r.item} {r.role:6s} expected {r.expected_label:20s} got {r.label or 'UNCHECKABLE: ' + str(r.uncheckable)}",
        flush=True))
    s = corpus.summary(res)
    bad = bool(s["uncheckable"] or s["honest_flips"] or s["fault_escapes"] or s["label_changes"])
    if look is not None:  # the sealed result is ledgered as a COUNT only (which items flipped stays off the record)
        looks.record_result(look, ran=True, n=s["items"], honest_flips=len(s["honest_flips"]),
                            fault_escapes=len(s["fault_escapes"]), uncheckable=len(s["uncheckable"]),
                            passed=not bad)
        s = {k: (len(v) if isinstance(v, list) else v) for k, v in s.items()}
    print(f"honest_corpus: {half} half {'FAILED' if bad else 'OK'}: {json.dumps(s, sort_keys=True)}", flush=True)
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--half", choices=("dev", "sealed"), default="dev")
    ap.add_argument("--root", help="corpus directory (default custom-harness-data/assure/honest-corpus)")
    ap.add_argument("--manifest", default=str(corpus.MANIFEST))
    ap.add_argument("--spec-pairs", type=int, default=120)
    ap.add_argument("--world-pairs", type=int, default=4)
    ap.add_argument("--no-research", action="store_true")
    ap.add_argument("--config-json", help="KernelConfig overrides to re-check under (default: each item's own)")
    ap.add_argument("--by", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--looks", default=str(corpus.DEFAULT_LOOKS))
    a = ap.parse_args(argv)
    root = Path(a.root) if a.root else corpus.default_root()
    if a.build:
        build(root, a.spec_pairs, a.world_pairs, not a.no_research, Path(a.manifest))
        return 0
    if a.check:
        cfg = KernelConfig(**json.loads(a.config_json)) if a.config_json else None
        return check(a.half, root, cfg, a.by, a.reason, Path(a.looks), Path(a.manifest))
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
