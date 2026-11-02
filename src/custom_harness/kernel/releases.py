"""Release ledger (GOAL §16.5.3, invariant 18): every transfer of target-derived information to an agent-visible
surface is a typed `release` entry, and K4 counts the transcripts the releases allow.

Guarantee (union bound / description length, Dwork et al. 2015): if the t-th claim on a target is any function of
randomness independent of the held-out sample, public data and the released transcript, and each fixed claim is
tested at alpha / N with N = sum_t |T_{t-1}| (T_{t-1} = the transcripts reachable from the releases logged before
claim t was registered), then P(any false certification on the target) <= alpha. It holds only if the release log
is COMPLETE (an assumption listed on every certificate). A `continuous` release makes N unbounded: the target is
ADAPTIVELY EXPOSED and K6 fails for every claim registered on it afterwards.
"""

from __future__ import annotations

import re
from enum import StrEnum


class Channel(StrEnum):
    """The agent-visible surface the information reached (closed set)."""

    PROMPT = "prompt"
    EXPORT = "export"
    MEMORY = "memory"
    EVOLUTION_SCORE = "evolution_score"
    REPORT = "report"


class Alphabet(StrEnum):
    """What one release can tell (closed set)."""

    VERDICT_BIT = "verdict_bit"  # certified yes/no: 2 transcripts
    COUNT = "count"  # a small integer: COUNT_ALPHABET transcripts (KernelConfig)
    CONTINUOUS = "continuous"  # a real number: unbounded, the target becomes adaptively exposed


NOTE = re.compile(r"[a-z][a-z0-9_.:-]{0,79}")  # a release note is a code, never free text or a value


def exposed(claim_seq: int, releases: list[tuple[int, Alphabet]]) -> bool:
    """A claim registered after a continuous release on its target family could have been chosen from real-valued
    held-out feedback: it is never certified (K6), so it also adds nothing to the family's error budget."""
    return any(at < claim_seq and a == Alphabet.CONTINUOUS for at, a in releases)


def transcript_bound(claim_seqs: list[int], releases: list[tuple[int, Alphabet]], count_alphabet: int) -> int:
    """N = sum over the family's non-exposed claims (by registration seq) of the number of transcripts reachable from
    the releases logged before that claim. With no releases (verdict embargo) N = k, today's claim count."""
    total = 0
    for seq in claim_seqs:
        if exposed(seq, releases):
            continue
        reachable = 1
        for at, alphabet in releases:
            if at < seq:
                reachable *= 2 if alphabet == Alphabet.VERDICT_BIT else count_alphabet
        total += reachable
    return total
