"""Which tasks are salts, sealed until a human reveals them (GOAL §16.9, §16.3).

    spec = CampaignSpec(...)                                   # the campaign, frozen: its hash is the prereg hash
    plan = assign(spec, beacon=..., secret=load_secret())      # draw("salt_assignment"): REQUIRES the human secret
    salts = generate(plan, out, real_data=...)                 # write the salt tasks (same files as real tasks)
    seal(plan, salts, exam, sealed_path, vault)                # plant list -> human-held file; hash -> ledger
    ... the campaign runs; salt results must go to an audit store only (isolation_problems checks) ...
    revealed = reveal(sealed_path, vault, human="...", today=date.today())   # HUMAN command, on/after the date
    audit.campaign_audit(revealed, outcomes)

Commitment. The plant list (which tasks are salts, their kind and planted effect, and the hash of every file written)
is hashed WITH a 32-byte nonce derived from the draw, so the short list cannot be brute-forced from its hash. The
ledger gets only {campaign hash, commitment, reveal date} as a `register_artifact` entry of kind `salt_commitment`
(a dedicated ledger kind is requested from kstats: mailbox salts-to-kstats-01). The sealed file must sit outside
every agent-readable root (commitment.default_agent_readable_roots), like the secret.

Isolation. Slots of the gate or sealed split are refused (salts never enter the L6 gate). Salt runs must go to a
separate audit store (that wiring is not built here: audit.run_salts writes to no store). `isolation_problems`
checks, after reveal, that no salt task reached the main ledger (preregistrations, certificates, registrations:
the inputs of VDS and of the gate).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..commitment import agent_readable_root_of, as_key, draw, stream, subkey
from . import config as C
from .generate import (
    PLASMODE_DATASETS,
    POSITIVE_FAMILIES,
    GeneratedSalt,
    SaltError,
    null_salt,
    plasmode_null,
    positive_salt,
)

FORMAT = "chh-salts/1"
COMMITMENT_KIND = "salt_commitment"
REVEAL_KIND = "salt_reveal"


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass(frozen=True)
class Slot:
    """One task of a campaign that MAY become a salt. family: a synthetic family, or "real" with `dataset`."""

    task_id: str
    family: str
    split: str
    n_rows: int = 0
    dataset: str | None = None

    def positive_ok(self) -> bool:
        return self.family in POSITIVE_FAMILIES

    def null_ok(self) -> bool:
        return self.positive_ok() or (self.family == "real" and self.dataset in PLASMODE_DATASETS)


@dataclass(frozen=True)
class CampaignSpec:
    campaign_id: str
    slots: tuple[Slot, ...]
    n_positive: int
    n_null: int
    delta: float  # the campaign's claim minimum effect
    reveal_on: str  # ISO date: the reveal is refused before it
    beacon_source: str  # WHICH beacon value will be used (e.g. a drand round), fixed before the draw

    def campaign_hash(self) -> str:
        return hashlib.sha256(_canon({"format": FORMAT, **asdict(self)})).hexdigest()


@dataclass(frozen=True)
class Assigned:
    slot: Slot
    kind: str  # "positive" | "null"


@dataclass(frozen=True)
class AssignmentPlan:
    spec: CampaignSpec
    beacon: str
    salts: tuple[Assigned, ...]
    _master: bytes = field(repr=False)  # from the secret draw; never written anywhere

    def salt_key(self, task_id: str) -> bytes:
        return subkey(self._master, "salt-data:" + task_id)

    def nonce(self) -> str:
        return subkey(self._master, "salt-commitment-nonce").hex()


def assign(spec: CampaignSpec, *, beacon: str, secret: bytes | None) -> AssignmentPlan:
    """Pick which slots become positive and null salts. The draw is draw("salt_assignment", ...), which refuses to
    run without the human secret, so nobody without it can predict the assignment. Fails closed (SaltError) on a
    gate/sealed slot, duplicate slots, a bad reveal date, or too few eligible slots."""
    ids = [s.task_id for s in spec.slots]
    if len(set(ids)) != len(ids):
        raise SaltError("duplicate slot task ids")
    bad = sorted(s.task_id for s in spec.slots if s.split not in C.SALTABLE_SPLITS)
    if bad:
        raise SaltError(f"slots outside {sorted(C.SALTABLE_SPLITS)} cannot carry salts (salts stay out of the L6 "
                        f"gate): {bad}")
    if spec.n_positive < 0 or spec.n_null < 0 or spec.n_positive + spec.n_null == 0:
        raise SaltError("a campaign needs a non-negative number of salts, at least one")
    if not spec.delta > 0:
        raise SaltError("delta must be positive")
    dt.date.fromisoformat(spec.reveal_on)
    value = draw("salt_assignment", prereg_hash=spec.campaign_hash(), beacon=beacon, secret=secret)
    master = as_key(value)
    rng = stream(master, "salt-assignment")
    pos_pool = sorted((s for s in spec.slots if s.positive_ok()), key=lambda s: s.task_id)
    if len(pos_pool) < spec.n_positive:
        raise SaltError(f"{spec.n_positive} positive salts asked, {len(pos_pool)} eligible slots")
    positives = rng.sample(pos_pool, spec.n_positive)
    taken = {s.task_id for s in positives}
    null_pool = sorted((s for s in spec.slots if s.null_ok() and s.task_id not in taken), key=lambda s: s.task_id)
    if len(null_pool) < spec.n_null:
        raise SaltError(f"{spec.n_null} null salts asked, {len(null_pool)} eligible slots left")
    nulls = rng.sample(null_pool, spec.n_null)
    return AssignmentPlan(spec, beacon, tuple([Assigned(s, "positive") for s in positives] +
                                              [Assigned(s, "null") for s in nulls]), master)


def generate(plan: AssignmentPlan, out: Path, *, real_data: dict[str, dict] | None = None) -> list[GeneratedSalt]:
    """Write every salt task of `plan` under `out` (task dir named by its slot's task id)."""
    made = []
    for a in plan.salts:
        s, k = a.slot, plan.salt_key(a.slot.task_id)
        if a.kind == "positive":
            made.append(positive_salt(s.family, s.n_rows, key=k, task_id=s.task_id, delta=plan.spec.delta, out=out))
        elif s.family == "real":
            if real_data is None or s.dataset not in real_data:
                raise SaltError(f"slot {s.task_id}: plasmode needs the real data of {s.dataset!r}")
            made.append(plasmode_null(str(s.dataset), real_data[str(s.dataset)], key=k, task_id=s.task_id,
                                      delta=plan.spec.delta, out=out))
        else:
            made.append(null_salt(s.family, s.n_rows, key=k, task_id=s.task_id, delta=plan.spec.delta, out=out))
    return made


# ---------------------------------------------------------------- seal, commit, reveal
def _plant_list(salts: Sequence[GeneratedSalt]) -> list[dict[str, Any]]:
    return sorted(({"task_id": s.task_id, "kind": s.kind, "family": s.family, "source": s.source,
                    "metric": s.metric, "target_effect": s.target_effect, "planted_effect": s.planted_effect,
                    "residual_bound": s.residual_bound, "files": s.files} for s in salts),
                  key=lambda e: e["task_id"])


def commitment_of(plant_list: list[dict[str, Any]], nonce: str) -> str:
    return hashlib.sha256(_canon({"plant_list": plant_list, "nonce": nonce})).hexdigest()


def _record(campaign_hash: str, commitment: str, reveal_on: str, exam_hash: str) -> bytes:
    return _canon({"format": FORMAT, "campaign_hash": campaign_hash, "commitment": commitment,
                   "reveal_on": reveal_on, "exam_hash": exam_hash})


def _under_agent_readable(path: Path) -> Path | None:
    """Alias-safe (pathsafe via commitment.agent_readable_root_of): a `\\\\?\\`, admin-share, 8.3 or other-case
    spelling of a path inside an agent-readable root is inside it."""
    return agent_readable_root_of(path)


def seal(plan: AssignmentPlan, salts: Sequence[GeneratedSalt], exam: Any, sealed_path: Path, vault: Any) -> str:
    """Write the plant list to the human-held `sealed_path` and ledger its commitment. Refused unless `exam` (an
    exam.ExamResult) passed and examined every family salted here, and unless the sealed file is outside every
    agent-readable root. Returns the commitment."""
    from .exam import require_passed

    require_passed(exam, {s.family for s in salts})
    if sorted(s.task_id for s in salts) != sorted(a.slot.task_id for a in plan.salts):
        raise SaltError("the generated salts are not the plan's salts")
    root = _under_agent_readable(sealed_path)
    if root is not None:
        raise SaltError(f"the sealed plant list would sit inside agent-readable root {root}")
    if Path(sealed_path).exists():
        raise SaltError(f"{sealed_path} exists: a campaign is sealed once")
    plants = _plant_list(salts)
    commitment = commitment_of(plants, plan.nonce())
    sealed = {"format": FORMAT, "spec": asdict(plan.spec), "campaign_hash": plan.spec.campaign_hash(),
              "beacon": plan.beacon, "nonce": plan.nonce(), "exam_hash": exam.exam_hash, "plant_list": plants,
              "commitment": commitment}
    Path(sealed_path).parent.mkdir(parents=True, exist_ok=True)
    with open(sealed_path, "w", encoding="utf-8", newline="") as f:
        f.write(json.dumps(sealed, sort_keys=True, indent=1) + "\n")
    vault.register_artifact(f"salt-commitment/{plan.spec.campaign_hash()}",
                            _record(plan.spec.campaign_hash(), commitment, plan.spec.reveal_on, exam.exam_hash),
                            COMMITMENT_KIND)
    return commitment


_TOKEN = object()


@dataclass(frozen=True)
class RevealedPlan:
    """Only reveal() makes one: campaign_audit refuses anything else."""

    campaign_hash: str
    delta: float
    plant_list: tuple[dict[str, Any], ...]
    revealed_by: str
    revealed_on: str
    _token: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._token is not _TOKEN:
            raise SaltError("a RevealedPlan comes only from reveal() (a human command after the reveal date)")

    def task_ids(self) -> set[str]:
        return {e["task_id"] for e in self.plant_list}


def reveal(sealed_path: Path, vault: Any, *, human: str, today: dt.date) -> RevealedPlan:
    """HUMAN command (`python -m custom_harness.salts reveal`): open the sealed plant list on or after its reveal
    date, check it against the ledgered commitment, and ledger the reveal. Fails closed (SaltError)."""
    if not isinstance(human, str) or not human.strip():
        raise SaltError("the reveal must name the human taking it")
    sealed = json.loads(Path(sealed_path).read_text(encoding="utf-8"))
    if sealed.get("format") != FORMAT:
        raise SaltError(f"unsupported sealed format {sealed.get('format')!r}")
    spec = sealed["spec"]
    spec_obj = CampaignSpec(**{**spec, "slots": tuple(Slot(**s) for s in spec["slots"])})
    h = spec_obj.campaign_hash()
    if h != sealed["campaign_hash"]:
        raise SaltError("the sealed campaign spec does not hash to its campaign hash")
    reveal_on = dt.date.fromisoformat(spec["reveal_on"])
    if today < reveal_on:
        raise SaltError(f"the reveal date is {reveal_on.isoformat()}; refused on {today.isoformat()}")
    plants = sealed["plant_list"]
    commitment = commitment_of(plants, sealed["nonce"])
    if commitment != sealed["commitment"]:
        raise SaltError("the sealed plant list does not match its own commitment (edited after sealing)")
    expected = hashlib.sha256(_record(h, commitment, spec["reveal_on"], sealed["exam_hash"])).hexdigest()
    art = vault.artifacts().get(f"salt-commitment/{h}")
    if art is None or art.kind != COMMITMENT_KIND:
        raise SaltError("no salt commitment for this campaign in the ledger")
    if art.hash != expected:
        raise SaltError("the ledgered commitment does not match the sealed plant list")
    vault.register_artifact(f"salt-reveal/{h}", _canon({"campaign_hash": h, "plant_list": plants,
                                                        "by": human.strip(), "on": today.isoformat()}), REVEAL_KIND)
    return RevealedPlan(h, float(spec["delta"]), tuple(plants), human.strip(), today.isoformat(), _TOKEN)


# ---------------------------------------------------------------- isolation (salts stay out of L6 and VDS)
def _mentions(value: Any, needles: set[str]) -> str | None:
    if isinstance(value, str):
        for n in needles:
            if n in value:
                return n
        return None
    if isinstance(value, dict):
        for v in value.values():
            hit = _mentions(v, needles)
            if hit:
                return hit
        return None
    if isinstance(value, list):
        for v in value:
            hit = _mentions(v, needles)
            if hit:
                return hit
    return None


def isolation_problems(main_entries: Iterable[Any], revealed: RevealedPlan) -> list[str]:
    """Every main-ledger entry (the ledger VDS and the L6 gate read) naming a salt task id or holding the hash of a
    salt file. Empty = the salts stayed in the audit store. Exact substring match: an id or hash rewritten into
    another form is not found."""
    needles = revealed.task_ids() | {h for e in revealed.plant_list for h in e["files"].values()}
    out = []
    for e in main_entries:
        hit = _mentions(e.body, needles)
        if hit:
            out.append(f"main ledger entry {e.seq} ({e.kind}) names salt material {hit[:24]}")
    return out
