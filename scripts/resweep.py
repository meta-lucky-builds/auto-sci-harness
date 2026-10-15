"""Resweep command (GOAL §16.10): re-check archived certificates when the rules change. See
custom_harness/assure/resweep.py for the statuses, grades and the VDS split.

    # 1. screen the rule on the honest corpus's SEALED half (one ledgered look; 0 honest flips allowed)
    python scripts/resweep.py --rule seeds4 --overrides '{"min_seeds": 4}' --screen --by NAME --reason WHY
    # 2. apply it to a store (refused without a passing screen for this exact rule and Kernel code)
    python scripts/resweep.py --rule seeds4 --overrides '{"min_seeds": 4}' --apply --store PATH --docker
    # current grades and the provisional / seasoned VDS of a store
    python scripts/resweep.py --status --store PATH

Replayer for K3 re-execution: --docker (production), --dev-sandbox (trusted code only, needs CHH_ALLOW_UNISOLATED=1),
or --stored-predictions (returns the stored predictions: K3 becomes a formality; for tests and the spec worlds
only, and the report says so). Without one the command refuses.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # this checkout's code, never another checkout's editable install

from custom_harness.assure import corpus, resweep  # noqa: E402
from custom_harness.store import EvidenceStore  # noqa: E402


def replayer_factory(a: argparse.Namespace) -> resweep.ReplayerFactory:
    from custom_harness.compute import unique_workspaces
    from custom_harness.config import data_dir
    from custom_harness.kernel import SandboxReplayer
    root = data_dir() / "sandboxes" / f"resweep-{int(time.time())}"
    if a.docker:
        from custom_harness.sandbox.docker import DockerSandbox
        f = unique_workspaces(DockerSandbox, root)
        return lambda: SandboxReplayer(f)
    if a.dev_sandbox:
        from custom_harness.sandbox import LocalSandbox
        g = unique_workspaces(lambda ws: LocalSandbox(ws, i_understand_no_isolation=True), root)
        return lambda: SandboxReplayer(g)
    if a.stored_predictions:
        return corpus.StoredPredictionsReplayer
    raise SystemExit("resweep: pass --docker, --dev-sandbox (trusted code only) or --stored-predictions (tests)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rule", default="today", help="rule id (a short lowercase code)")
    ap.add_argument("--overrides", default="{}", help="KernelConfig overrides as JSON ({} = today's Kernel code)")
    ap.add_argument("--screen", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--store")
    ap.add_argument("--by", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--looks", default=str(corpus.DEFAULT_LOOKS))
    ap.add_argument("--corpus-root")
    ap.add_argument("--manifest", default=str(corpus.MANIFEST))
    ap.add_argument("--docker", action="store_true")
    ap.add_argument("--dev-sandbox", action="store_true")
    ap.add_argument("--stored-predictions", action="store_true")
    ap.add_argument("--no-release", action="store_true", help="do not emit releases (only for a store no agent sees)")
    ap.add_argument("--out", help="write the full JSON report here (host-only: it names every certificate)")
    a = ap.parse_args(argv)
    rule = resweep.Rule(a.rule, json.loads(a.overrides))
    if a.screen:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              check=False).stdout.strip()
        try:
            out = resweep.screen(rule, a.by, a.reason, ledger=Path(a.looks),
                                 root=Path(a.corpus_root) if a.corpus_root else None,
                                 man=corpus.manifest(Path(a.manifest)), head=head)
        except (corpus.CorpusError, resweep.ResweepError, corpus.looks.LookError) as e:
            print(f"resweep: screen refused: {e}", flush=True)
            return 1
        print(json.dumps(out, sort_keys=True), flush=True)
        return 0 if out["passed"] else 1
    if not a.store:
        ap.error("--apply and --status need --store")
    store = EvidenceStore(Path(a.store))
    try:
        if a.status:
            path = resweep.clawback_path(store)
            print(json.dumps({"grades": resweep.current_grades(path),
                              "vds": resweep.vds_split(store, path, time.time())}, sort_keys=True, indent=1))
            return 0
        if a.apply:
            try:
                rep = resweep.apply(store, rule, replayer_factory(a), ledger=Path(a.looks),
                                    release=not a.no_release)
            except resweep.ResweepError as e:
                print(f"resweep: refused: {e}", flush=True)
                return 1
            rep["replayer"] = ("stored-predictions (K3 re-execution NOT performed)" if a.stored_predictions
                               else "docker" if a.docker else "dev-sandbox")
            if a.out:
                Path(a.out).write_text(json.dumps(rep, sort_keys=True, indent=1), encoding="utf-8")
            print(json.dumps({k: v for k, v in rep.items() if k != "results"}, sort_keys=True, indent=1))
            return 0
    finally:
        store.close()
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
