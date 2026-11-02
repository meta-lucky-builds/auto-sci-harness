"""A minted Court case on a throwaway store, for tests: the real route (register, kerneld mint, run, Kernel) with the
two human steps played by the fixture on that store only (adapter approval and role approval, as every Kernel test
world does). Arms are stdlib programs from tests/fixtures/audit/arms."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from custom_harness.audit.spec import Budget, ClaimPart
from custom_harness.court import minted as C
from custom_harness.kernel import Kernel
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore

ARMS = Path(__file__).parent / "fixtures" / "audit" / "arms"
BEACON = "public-beacon-value-0001"


def part(metric: str = "accuracy", min_effect: float = 0.1, seeds: tuple[int, ...] = (0, 1, 2)) -> ClaimPart:
    return ClaimPart(metric=metric, min_effect=min_effect, probability=0.6, seeds=seeds, n_boot=1000,
                     resolve_by=date(2030, 1, 1))


class Case:
    def __init__(self, root: Path, *, method: bytes | None = None, baseline: bytes | None = None,
                 family: str = "rings", claim: ClaimPart | None = None, n_train: int = 300, n_test: int = 600,
                 approve: bool = True, mint: bool = True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = EvidenceStore(self.root / "store")
        Kernel(self.store).approve_adapter("ml", approver="fixture-human")
        self.body = C.register_case(
            self.store, role="promotion", promotion_id="p-fixture", session_id="s-fixture", title="fixture case",
            sentence="the fixture method beats the fixture baseline", part=claim or part(),
            budget=Budget(timeout_s=120),
            method=method if method is not None else (ARMS / "knn_radius.py").read_bytes(),
            baseline=baseline if baseline is not None else (ARMS / "majority.py").read_bytes(), lock=b"x==1\n",
            family=family, beacon_round="r1", sizing={"n_test": n_test}, n_train=n_train,
            anchor_file=self.root / "anchors" / "court.jsonl")
        self.case_id: str = self.body["case_id"]
        self.claim_hash: str = self.body["claim_hash"]
        self.target: str = self.body["target"]
        self.attempt = 0
        if mint:
            self.mint()
            if approve:
                Kernel(self.store).approve_role(self.target, approver="fixture-human", note="fixture")

    def mint(self) -> dict[str, Any]:
        return C.mint(self.store, self.case_id, BEACON)

    def factory(self, key: str) -> LocalSandbox:
        self.attempt += 1
        return LocalSandbox(self.root / "sb" / f"{self.attempt}-{key}", i_understand_no_isolation=True)

    def run(self) -> dict[str, Any]:
        return C.run(self.store, self.case_id, self.factory)

    def evaluate_reads(self) -> int:
        return len([a for a in Kernel(self.store).vault.accesses()
                    if a.body.get("claim_hash") == self.claim_hash and a.body.get("purpose") == "evaluate"])

    def close(self) -> None:
        self.store.close()
