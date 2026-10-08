"""Yield cost of the release rules (docs/taint.md): run the existing SCRIPTED research runs (the mock-LLM runs of
tests/test_research.py and tests/test_research_yield.py; no model call, no network) and count the releases each one
records, by channel / alphabet / note, plus the certificate labels and the K4 transcript bound of each claim.

    python scripts/taint_yield.py OUT.json

Run it on two commits (each with PYTHONPATH=<that checkout>/src) and diff the JSON files. Every run uses a fresh
temporary store and data directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def _scenarios():
    from custom_harness.gateway import MockBackend
    from test_research import scripted_llm
    from test_research_yield import packed_llm

    def one_family(fn):
        llm = MockBackend(fn)
        return {"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, True

    def two_families():
        author, other = MockBackend(scripted_llm), MockBackend(scripted_llm)
        author.family, other.family = "family-a", "family-b"
        return {"proposer": author, "executor": author, "judge": other, "replicator": other}, False

    return {"v1_scripted": ("research_v1.yaml", lambda: one_family(scripted_llm)),
            "v1_two_family_replication": ("research_v1.yaml", two_families),
            "v2_packed": ("research_v2.yaml", lambda: one_family(packed_llm))}


def _run(name: str, spec_file: str, make, tmp: Path) -> dict:
    import yaml

    import custom_harness.research  # noqa: F401  registers modules
    from custom_harness.kernel import Kernel
    from custom_harness.kernel.checks import related_targets
    from custom_harness.kerneld.releases import released
    from custom_harness.runtime import Engine, HarnessSpec
    from custom_harness.sandbox import LocalSandbox
    from custom_harness.store import EvidenceStore

    task = tmp / "rings"
    subprocess.run([sys.executable, str(ROOT / "examples/tasks/make_rings_task.py"), str(task), "--n-train", "400",
                    "--n-test", "300"], check=True, capture_output=True)
    store = EvidenceStore(tmp / "store")
    kernel = Kernel(store)
    kernel.approve_adapter("ml", approver="taint-yield-script")
    backends, same_family = make()
    if same_family:
        os.environ["CHH_ALLOW_SAME_FAMILY"] = "1"
    else:
        os.environ.pop("CHH_ALLOW_SAME_FAMILY", None)
    spec = HarnessSpec.model_validate(yaml.safe_load((ROOT / "examples" / spec_file).read_text(encoding="utf-8")))
    res = Engine(spec, backends=backends, store=store,
                 sandbox=LocalSandbox(tmp / "ws", i_understand_no_isolation=True)).run("rings",
                                                                                      {"task_dir": str(task)})
    rel = released(store)
    by = Counter(f"{r['channel']}/{r['alphabet']}" for r in rel)
    notes = Counter(f"{r['channel']}/{r['alphabet']}/{r['note']}" for r in rel)
    s = res.state
    claims = {}
    for key in ("certificate", "replica_certificate"):
        cert = s.get(key)
        if isinstance(cert, dict):
            claims[key] = {"label": cert.get("label"), "failed": cert.get("failed")}
    targets = sorted({r["dataset_id"] for r in rel})
    bound = {}
    for t in targets:
        fam = related_targets(kernel.vault, t)
        bound[t] = {"releases_in_family": len(kernel.releases(t)), "family_size": len(fam)}
    store.close()
    return {"status": res.status.value, "releases_total": len(rel), "by_channel_alphabet": dict(sorted(by.items())),
            "by_note": dict(sorted(notes.items())), "claims": claims, "targets": bound}


def main(out: str) -> None:
    import custom_harness
    result: dict = {"custom_harness": custom_harness.__file__, "runs": {}}
    os.environ["CHH_ALLOW_UNISOLATED"] = "1"  # trusted, stdlib-only scripted solution code (as in the tests)
    for name, (spec_file, make) in _scenarios().items():
        with tempfile.TemporaryDirectory(prefix=f"taint-yield-{name}-") as d:
            os.environ["CHH_DATA_DIR"] = str(Path(d) / "data")
            result["runs"][name] = _run(name, spec_file, make, Path(d))
            print(name, json.dumps(result["runs"][name]["by_channel_alphabet"]), flush=True)
    Path(out).write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
