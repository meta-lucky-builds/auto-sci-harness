"""Durable campaign generations (crash/resume, GOAL §5 L1 "budgets persisted across crash"): proposals are persisted
BEFORE any candidate is evaluated, so a resumed generation never calls the proposer again.

With a campaign (`evolve(..., campaign=...)`), the driver gives `evolve_generation` a `ProposalLog` for (campaign,
campaign-global generation index). On the first attempt the loop calls the proposer, then appends ONE
`evolve_proposal` record holding everything the generation's decisions depend on that was fixed at proposal time:

- the edits (as validated `Edit` objects, dumped), the malformed-edit problems and the decline reason;
- `known`: the evidence ids the proposer was shown (what its citations may resolve to, claims.py). It is derived from
  the diagnosis, which reads the Evaluator's cache; after a resume that cache holds more outcomes (the units the
  crashed attempt finished), so recomputing it could change a claims verdict;
- `rejected_before`: the novelty gate's rejected signatures, read before this generation wrote any lineage.

On a resume (same campaign and index) the loop takes these from the record and skips the diagnosis and the proposer.
Together with the Evaluator's durable unit outcomes (`Evaluator(durable_key=...)`, keyed by the campaign), a resumed
generation re-runs and re-charges no finished unit and decides as the uninterrupted one would.

Fail closed (LedgerError): two records for one (campaign, index), or a record whose parent differs from the parent
being resumed. The record is host-only: the evidence export writes only its whitelisted kinds (export.py), so the
proposer never reads it back.

Limits (stated): a crash AFTER the generation's verdict-bit release and before its ledger record re-emits that
release on resume (a duplicate `release` entry: conservative, it can only tighten K4's transcript bound). A crash
between `harness_edit` and `lineage` records leaves the crashed attempt's `harness_edit` record in the store; the
generation ledger attributes only the resumed attempt's records (it snapshots counts at `begin`).
"""

from __future__ import annotations

from typing import Any, NamedTuple

from ..store import EvidenceStore
from .edits import Edit

PROPOSAL_KIND = "evolve_proposal"
FORMAT = "evolve-proposal/1"
_FIELDS = frozenset({"format", "campaign", "generation", "parent", "edits", "problems", "decline", "known",
                     "rejected_before"})


class ReplayedProposal(NamedTuple):
    """A persisted proposal, shaped like propose.Proposal for the loop (no raw model text, no agent transcript)."""

    edits: list[Edit]
    problems: list[str]
    raw: Any
    decline: str | None


def durable_key(campaign: str) -> str:
    """The Evaluator's durable unit key of a campaign (one per campaign: units are keyed by spec hash, task, seed)."""
    return f"evolve-campaign:{campaign}"


class ProposalLog:
    def __init__(self, store: EvidenceStore, campaign: str, generation: int):
        from .generations import LedgerError
        if not isinstance(campaign, str) or not campaign or type(generation) is not int or generation < 0:
            raise LedgerError("a proposal log needs a campaign and a generation index >= 0")
        self.store, self.campaign, self.generation = store, campaign, generation

    def _rows(self) -> list[dict[str, Any]]:
        return [r for r in self.store.records_where(PROPOSAL_KIND, "campaign", self.campaign)
                if r.get("generation") == self.generation]

    def load(self, parent_hash: str) -> tuple[ReplayedProposal, set[str], set[str]] | None:
        """The persisted proposal of this generation, or None when it was never proposed."""
        from .generations import LedgerError
        rows = self._rows()
        if not rows:
            return None
        if len(rows) > 1:
            raise LedgerError(f"campaign {self.campaign!r} generation {self.generation}: {len(rows)} persisted "
                              "proposals (need at most 1)")
        r = rows[0]
        if set(r) != _FIELDS or r.get("format") != FORMAT:
            raise LedgerError(f"campaign {self.campaign!r} generation {self.generation}: malformed persisted proposal")
        if r["parent"] != parent_hash:
            raise LedgerError(f"campaign {self.campaign!r} generation {self.generation} was proposed for parent "
                              f"{str(r['parent'])[:12]}, not {parent_hash[:12]}: refusing to resume it")
        try:
            edits = [Edit.model_validate(e) for e in r["edits"]]
        except ValueError as e:
            raise LedgerError(f"persisted proposal edits do not validate: {e}") from None
        decline = r["decline"]
        if not (decline is None or isinstance(decline, str)) or not all(
                isinstance(x, str) for x in (*r["problems"], *r["known"], *r["rejected_before"])):
            raise LedgerError("persisted proposal fields malformed")
        return (ReplayedProposal(edits, list(r["problems"]), None, decline), set(r["known"]),
                set(r["rejected_before"]))

    def save(self, parent_hash: str, proposal: Any, known: set[str], rejected_before: set[str]) -> None:
        """Persist the proposal BEFORE any candidate runs (append-only; a second save for the index is refused)."""
        from .generations import LedgerError
        if self._rows():
            raise LedgerError(f"campaign {self.campaign!r} generation {self.generation} already has a proposal")
        self.store.append_record(PROPOSAL_KIND, {
            "format": FORMAT, "campaign": self.campaign, "generation": self.generation, "parent": parent_hash,
            "edits": [e.model_dump(mode="json") for e in proposal.edits],
            "problems": [str(p) for p in proposal.problems],
            "decline": proposal.decline, "known": sorted(known), "rejected_before": sorted(rejected_before)})
