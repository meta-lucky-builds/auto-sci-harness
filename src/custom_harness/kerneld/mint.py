"""Minted held-out data for a Court case (GOAL §16.2 "freeze, then mint"; §17.1 VALIDATED), built INSIDE kerneld.

The truth of a minted target is created only after the claim is pre-registered (frozen) and the ledger anchored, from
independent keyed streams whose key is `commitment.draw("heldout_mint", prereg_hash=<claim hash>, beacon=...)`. The
host never holds the labels: it names the case (claim, target, generator family, sizes, beacon) and kerneld builds the
bytes in memory and registers them (kerneld/ingest.minted_heldout), so the truth lives only in the vault (private
blobs, kerneld-only) and in the mint record kept with it.

This module is pure (no store, no Kernel): `minted_protocol` is the exact string a Court case freezes into its claim
(scope condition `protocol`), and `minted_files` the bytes kerneld registers. Both are shared by the host (to build
the claim) and kerneld (to check it), so a case cannot be minted with other parameters than the ones it froze.

Which claim families can be minted: exactly the synthetic generator families of research/suite.FAMILIES (rings, xor,
spirals, imbalanced, friedman, interactions; `noise` is the genuinely null family). Fixed public datasets (OpenML,
anything downloaded) cannot be minted: their truth existed before any freeze (a networked Lab could have it), so they
go through the retrospective Claim Audit route, capped below VALIDATED (court/levels.py). Simulated, physical and
formal claims have no generator here (physical claims get FUTURE data only through `chh return-data`).
Built, NOT test-verified.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass

MINTED_FORMAT = "chh-court-minted/1"
MIN_BEACON_CHARS = 16  # a short beacon value could be brute-forced by frozen code that embeds the generator
_ROUND = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,79}")


class MintSpecError(ValueError):
    pass


def mintable_families() -> tuple[str, ...]:
    from ..research.suite import FAMILIES
    return tuple(sorted(FAMILIES))


def minted_protocol(family: str, n_train: int, n_test: int, beacon_round: str) -> str:
    """The exact protocol string a minted Court case freezes into its claim (scope condition `protocol`)."""
    if family not in mintable_families():
        raise MintSpecError(f"family {family!r} has no generator: only {list(mintable_families())} can be minted; "
                            f"a fixed public dataset is a retrospective target (Claim Audit route)")
    if not (isinstance(n_train, int) and isinstance(n_test, int) and n_train >= 1 and n_test >= 2):
        raise MintSpecError("n_train >= 1 and n_test >= 2")
    if not _ROUND.fullmatch(beacon_round):
        raise MintSpecError("the beacon round must be a plain identifier committed BEFORE the freeze (e.g. a drand "
                            "round number)")
    return (f"{MINTED_FORMAT} family={family} n_train={n_train} n_test={n_test} beacon_round={beacon_round}: held-out "
            f"truth minted after the freeze from keyed streams (GOAL 16.2); both arms through one contract wrapper")


def beacon_value_problems(beacon_round: str, value: str) -> list[str]:
    if not isinstance(value, str) or len(value.strip()) < MIN_BEACON_CHARS:
        return [f"the beacon value must be at least {MIN_BEACON_CHARS} characters (the public randomness of round "
                f"{beacon_round})"]
    return []


def draw_beacon(beacon_round: str, value: str) -> str:
    """The `beacon` argument of commitment.draw: the committed round and its public value, both recorded."""
    return f"{beacon_round}:{value.strip()}"


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode()


@dataclass(frozen=True)
class MintedFiles:
    train: bytes  # public: features + "label"
    test_x: bytes  # public: features only
    test_y: bytes  # PRIVATE: the "label" column
    record: bytes  # PRIVATE: the mint record (truth-equivalent: with the public generator it regenerates the labels)
    n_train: int
    n_test: int
    task_id: str


def minted_files(family: str, claim_hash: str, beacon: str, n_train: int, n_test: int) -> MintedFiles:
    """The minted files of one case, deterministic in (family, claim hash, beacon, sizes). The target column is
    renamed `label` (the audit contract wrapper's column); test rows whose features also occur in the training file
    are dropped (as research/suite.write_task does: their label would be public). Called inside kerneld only."""
    from ..commitment import as_key, draw
    from ..research.suite import FAMILIES, GENERATOR_VERSION, _minted_spec
    master = as_key(draw("heldout_mint", prereg_hash=claim_hash, beacon=beacon, secret=None))
    task_id = f"{family}-mint-{hashlib.sha256(master).hexdigest()[:12]}"
    spec = _minted_spec(family, master, n_train, n_test, task_id)
    ti = spec.header.index(spec.target)
    header = ["label" if j == ti else c for j, c in enumerate(spec.header)]
    feat = [c for j, c in enumerate(header) if j != ti]

    def x(r: list[str]) -> list[str]:
        return [v for j, v in enumerate(r) if j != ti]

    seen = {tuple(x(r)) for r in spec.train}
    test = [r for r in spec.test if tuple(x(r)) not in seen]
    record = {"format": MINTED_FORMAT, "purpose": "heldout_mint", "frozen_hash": claim_hash, "beacon": beacon,
              "family": family, "n_train": n_train, "n_test_requested": n_test, "n_test": len(test),
              "generator_version": GENERATOR_VERSION, "task_id": task_id, "minted": True,
              "retrospective_target": False, "generator_family_metric": FAMILIES[family].metric}
    return MintedFiles(train=_csv([header, *spec.train]), test_x=_csv([feat, *[x(r) for r in test]]),
                       test_y=_csv([["label"], *[[r[ti]] for r in test]]),
                       record=(json.dumps(record, sort_keys=True, indent=1) + "\n").encode(),
                       n_train=len(spec.train), n_test=len(test), task_id=task_id)
