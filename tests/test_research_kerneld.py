"""research/claims.py makes its private reads through kerneld (GOAL §16.5.2), end to end.

The research loop's three private reads (confirm: `evaluate`; audit I5: `leakscan`; audit I4 re-check: `kernel`)
used to be `Vault.read` calls in the host process. They now go through `kerneld/client.py` with narrow per-claim
tokens, so no private byte enters the host process from research code. This test runs the whole research_v1 loop
and records every in-process `Vault.read` whose caller is `custom_harness.research.claims`: there must be none,
and the ledger must still hold the `evaluate` / `kernel` / `leakscan` reads (written by the kerneld process).
"""

from __future__ import annotations

import sys

import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Kernel
from custom_harness.kernel.vault import Vault
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from test_research import SPEC, rings_task, scripted_llm  # noqa: F401  (fixture re-export)


def _called_from_claims() -> bool:
    """The DIRECT caller of Vault.read is research/claims.py (the Kernel's own reads while certifying, called
    through claims.certify, are Kernel reads and stay in-process by design)."""
    return sys._getframe(2).f_globals.get("__name__") == "custom_harness.research.claims"


def test_research_loop_private_reads_go_through_kerneld(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")  # trusted test code only
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    real = Vault.read
    in_process: list[str] = []

    def guarded(self, dataset_id, name, *, purpose, **kw):
        if _called_from_claims():
            in_process.append(purpose)
        return real(self, dataset_id, name, purpose=purpose, **kw)

    monkeypatch.setattr(Vault, "read", guarded)
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    llm = MockBackend(scripted_llm)
    eng = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True))
    res = eng.run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL", s["certificate"]
    assert s["audit"]["passed"], s["audit"]
    assert in_process == [], f"research/claims.py read private data in the host process: {in_process}"
    # the reads still happened, ledgered (by the kerneld process) under the run id and the claim
    kernel = Kernel(store)
    reads = [e.body for e in kernel.vault.accesses(s["datasets"]["test"]) if e.body.get("private")]
    mine = [r for r in reads if r["run_id"] == res.run_id]
    purposes = {r["purpose"] for r in mine}
    assert {"evaluate", "kernel", "leakscan"} <= purposes, purposes
    evaluate = [r for r in mine if r["purpose"] == "evaluate"]
    assert len(evaluate) == len(s["evidence_ids"]) + len(s.get("replica_evidence_ids") or [])
    assert all(r["claim_hash"] in (s["claim_hash"], s.get("replica_claim_hash")) for r in evaluate)
    assert kernel.ledger.verify() == []
