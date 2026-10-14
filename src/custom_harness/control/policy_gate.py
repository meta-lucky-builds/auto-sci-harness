"""Acceptance rule for a change of the L7 orchestrator policy (GOAL §5 L7: "the orchestrator policy is itself
versioned and gated on a slower cadence"). A challenger policy replaces the incumbent only on a measured gain over a
declared window, judged by paired statistics. Pure functions and one trial object; the Orchestrator
(control/islands.py) owns the trial, writes its records and refuses a change without a passing decision.

Design (chosen to be valid under the verdict embargo, invariant 18, and without any extra look at the gate):
  - a trial is DECLARED before its first round: trial id, incumbent and challenger (version + parameter hash), the
    window W (number of round PAIRS), alpha, min_nonzero, the order seed, the declaring round and the test name;
  - the window is W round-pairs. In pair k the two rounds run under the incumbent and the challenger in an order
    fixed by the coin c_k (0: incumbent first, 1: challenger first; an AB/BA crossover). Under the Orchestrator the
    coin of pair k is DRAWN when pair k starts, from fresh entropy (`secrets`) the declarer neither chooses nor
    sees in advance: c_k = (seeded coin of k) XOR int(sha256(f"{nonce_k}|{trial_id}|{k}"), 16) & 1, ledgered (record
    `policy_trial_coin`) before the pair runs (red-team finding: coins fixed by a declared seed could be searched
    for, and with any period effect a no-effect challenger then passed). `declare(...)` alone (no draws) keeps the
    seeded coins c_k = int(sha256(f"{order_seed}|{trial_id}|{k}"), 16) & 1 for simulations. Within a round EVERY
    island runs under the same policy, so the per-round total budget is identical across arms;
  - round outcome y = number of islands whose generation in that round ACCEPTED an edit (accept bits only: the bit
    is already a ledgered release to the proposer; no gate mean, CI, per-task value or score is read);
  - pair difference d_k = y_challenger - y_incumbent;
  - test: exact one-sided sign test. n+ = #{d_k > 0}, n- = #{d_k < 0}, n0 = #{d_k = 0};
    p = P(Bin(n+ + n-, 1/2) >= n+) = sum_{i=n+}^{n} C(n, i) / 2^n with n = n+ + n- (p = 1 when n = 0).
    The challenger is accepted iff n >= min_nonzero AND p <= alpha. p and alpha are exact fractions (no float enters
    the comparison).

Validity: under the sharp null (the policy has no effect on any round outcome) the outcomes of a pair do not depend
on which arm ran first, so each non-zero d_k has the sign of a fair, independent coin (the order coins). Conditionally
on the non-zero pairs, n+ ~ Binomial(n, 1/2) exactly: a randomization test, valid whatever the carry-over or period
effects are. Under the alternative, carry-over (the orchestrator state the previous round left) can bias the size of
the effect; the test does not estimate an effect size. alpha holds PER TRIAL, not across trials (the number of trials
is reported). The coins come from a declared seed, not a public beacon: a declarer could in principle search seeds;
the declaration is ledgered before the first round, which is the only guard.

Parameters (config.py): POLICY_TRIAL_ALPHA, POLICY_TRIAL_MIN_NONZERO, POLICY_TRIAL_PAIRS, all UNCALIBRATED.
"""

from __future__ import annotations

import hashlib
import math
import secrets
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Any

from .. import config

TRIAL_KIND = "policy_trial"
ROUND_KIND = "policy_trial_round"
DECISION_KIND = "policy_trial_decision"
COIN_KIND = "policy_trial_coin"
ARMS = ("incumbent", "challenger")
TEST_NAME = ("exact one-sided sign test: p = P(Bin(n_plus + n_minus, 1/2) >= n_plus) over the non-zero round-pair "
             "differences y_challenger - y_incumbent; accept iff n_plus + n_minus >= min_nonzero and p <= alpha")
_DECISION_FIELDS = ("trial_id", "incumbent_version", "incumbent_hash", "challenger_version", "challenger_hash",
                    "n_pairs", "alpha", "min_nonzero", "complete", "outcomes", "coins", "diffs", "n_plus", "n_minus",
                    "n_zero",
                    "p", "accepted", "reason")


class PolicyGateError(ValueError):
    pass


def exact_alpha(alpha: Any) -> Fraction:
    """alpha as an exact fraction: a float is read as its shortest decimal repr (0.05 is 1/20). 0 < alpha < 1."""
    if isinstance(alpha, bool):
        raise PolicyGateError("alpha must be a number, not a bool")
    if isinstance(alpha, float):
        if not math.isfinite(alpha):
            raise PolicyGateError("alpha must be finite")
        a = Fraction(repr(alpha))
    elif isinstance(alpha, (int, Fraction)):
        a = Fraction(alpha)
    elif isinstance(alpha, str):
        try:
            a = Fraction(alpha)
        except (ValueError, ZeroDivisionError) as e:
            raise PolicyGateError(f"alpha {alpha!r} is not a fraction") from e
    else:
        raise PolicyGateError("alpha must be a float, int, Fraction or fraction string")
    if not 0 < a < 1:
        raise PolicyGateError("alpha must lie in (0, 1)")
    return a


def _frac_str(f: Fraction) -> str:
    return f"{f.numerator}/{f.denominator}"


def order_coin(order_seed: str, trial_id: str, k: int) -> int:
    """The declared order coin of pair k: 0 = incumbent first, 1 = challenger first."""
    return int(hashlib.sha256(f"{order_seed}|{trial_id}|{k}".encode()).hexdigest(), 16) & 1


def sign_p_value(n_plus: int, n: int) -> Fraction:
    """P(Bin(n, 1/2) >= n_plus), exact. n = 0 gives 1."""
    if type(n_plus) is not int or type(n) is not int or not 0 <= n_plus <= n:
        raise PolicyGateError("sign test needs integers 0 <= n_plus <= n")
    return Fraction(sum(math.comb(n, i) for i in range(n_plus, n + 1)), 2 ** n)


def sign_test(diffs: Sequence[int], *, alpha: Any, min_nonzero: int) -> dict[str, Any]:
    """The decision of the declared test on the pair differences (integers). `p` is an exact fraction string."""
    if any(type(d) is not int for d in diffs):
        raise PolicyGateError("pair differences must be integers (counts of accepting islands)")
    if type(min_nonzero) is not int or min_nonzero < 1:
        raise PolicyGateError("min_nonzero must be an integer >= 1")
    a = exact_alpha(alpha)
    n_plus = sum(d > 0 for d in diffs)
    n_minus = sum(d < 0 for d in diffs)
    n = n_plus + n_minus
    p = sign_p_value(n_plus, n)
    if n < min_nonzero:
        accepted, reason = False, f"too few non-zero pairs: {n} < min_nonzero {min_nonzero}"
    elif p <= a:
        accepted, reason = True, f"p = {_frac_str(p)} <= alpha {_frac_str(a)} with {n} non-zero pairs"
    else:
        accepted, reason = False, f"p = {_frac_str(p)} > alpha {_frac_str(a)}"
    return {"n_plus": n_plus, "n_minus": n_minus, "n_zero": len(diffs) - n, "p": _frac_str(p),
            "accepted": accepted, "reason": reason}


@dataclass(frozen=True)
class TrialDeclaration:
    trial_id: str
    incumbent_version: str
    incumbent_hash: str
    challenger_version: str
    challenger_hash: str
    n_pairs: int
    alpha: str  # exact fraction string
    min_nonzero: int
    order_seed: str
    declared_at_round: int
    test: str = TEST_NAME
    drawn: bool = False  # True: coins are drawn per pair at run time (Orchestrator), never derived from the seed

    def __post_init__(self) -> None:
        if not self.trial_id or not isinstance(self.trial_id, str) or not isinstance(self.order_seed, str):
            raise PolicyGateError("a trial needs a non-empty trial_id and a string order seed")
        if self.challenger_hash == self.incumbent_hash and self.challenger_version == self.incumbent_version:
            raise PolicyGateError("challenger and incumbent are the same policy")
        for name in ("n_pairs", "min_nonzero", "declared_at_round"):
            if type(getattr(self, name)) is not int:
                raise PolicyGateError(f"{name} must be an integer")
        if self.n_pairs < 1 or self.min_nonzero < 1 or self.declared_at_round < 0:
            raise PolicyGateError("n_pairs and min_nonzero must be >= 1, declared_at_round >= 0")
        if self.min_nonzero > self.n_pairs:
            raise PolicyGateError(f"min_nonzero {self.min_nonzero} > n_pairs {self.n_pairs}: the trial can never pass")
        a = exact_alpha(self.alpha)
        if _frac_str(a) != self.alpha:
            raise PolicyGateError("alpha must be stored in canonical fraction form")
        if Fraction(1, 2 ** self.n_pairs) > a:  # the smallest p the window can reach
            raise PolicyGateError(f"with {self.n_pairs} pairs the smallest reachable p is 1/{2 ** self.n_pairs} > "
                                  f"alpha {self.alpha}: the trial can never pass")
        if self.test != TEST_NAME:
            raise PolicyGateError("unknown test")

    def coins(self) -> list[int]:
        """The seeded coins (declare() alone). A drawn trial has no coins before its pairs start (fail closed)."""
        if self.drawn:
            raise PolicyGateError("the coins of a drawn trial exist only once each pair starts")
        return [order_coin(self.order_seed, self.trial_id, k) for k in range(self.n_pairs)]

    def schedule(self, coins: Sequence[int] | None = None) -> list[str]:
        """The arm of each trial round (2 x n_pairs rounds, starting at declared_at_round + 1)."""
        out: list[str] = []
        for c in (self.coins() if coins is None else coins):
            out += ["incumbent", "challenger"] if c == 0 else ["challenger", "incumbent"]
        return out

    def first_round(self) -> int:
        return self.declared_at_round + 1

    def last_round(self) -> int:
        return self.declared_at_round + 2 * self.n_pairs

    def as_record(self) -> dict[str, Any]:
        return asdict(self) | ({} if self.drawn else {"coins": self.coins()})


def drawn_coin(order_seed: str, trial_id: str, k: int, nonce: str) -> int:
    """The coin of pair k of a drawn trial, from the nonce drawn when the pair started."""
    return order_coin(f"{order_seed}", trial_id, k) ^ order_coin(nonce, trial_id, k)


def declare(*, trial_id: str, incumbent_version: str, incumbent_hash: str, challenger_version: str,
            challenger_hash: str, round_now: int, order_seed: str, n_pairs: int = config.POLICY_TRIAL_PAIRS,
            alpha: Any = config.POLICY_TRIAL_ALPHA, min_nonzero: int = config.POLICY_TRIAL_MIN_NONZERO,
            drawn: bool = False) -> TrialDeclaration:
    return TrialDeclaration(trial_id, incumbent_version, incumbent_hash, challenger_version, challenger_hash, n_pairs,
                            _frac_str(exact_alpha(alpha)), min_nonzero, str(order_seed), round_now, drawn=drawn)


def decision(decl: TrialDeclaration, outcomes: Sequence[int | None],
             coins: Sequence[int | None] | None = None) -> dict[str, Any]:
    """The trial decision from the per-round outcomes y (in trial-round order; None = round not run) and, for a drawn
    trial, the coins drawn so far (None = pair not started). A trial whose window is not complete is never
    accepted."""
    if decl.drawn:
        if coins is None or len(coins) != decl.n_pairs or any(c not in (0, 1, None) for c in coins):
            raise PolicyGateError("a drawn trial's decision needs its drawn coins")
        cs: list[int | None] = list(coins)
    else:
        cs = list(decl.coins())
    sched = decl.schedule([c if c is not None else 0 for c in cs])
    if len(outcomes) != len(sched):
        raise PolicyGateError("one outcome per trial round is needed")
    if any(y is not None and (type(y) is not int or y < 0) for y in outcomes):
        raise PolicyGateError("round outcomes are counts")
    complete = all(y is not None for y in outcomes) and all(c is not None for c in cs)
    diffs: list[int] = []
    if complete:
        for k in range(decl.n_pairs):
            pair = {sched[2 * k]: outcomes[2 * k], sched[2 * k + 1]: outcomes[2 * k + 1]}
            diffs.append(int(pair["challenger"] or 0) - int(pair["incumbent"] or 0))
        test = sign_test(diffs, alpha=decl.alpha, min_nonzero=decl.min_nonzero)
    else:
        test = {"n_plus": 0, "n_minus": 0, "n_zero": 0, "p": "1/1", "accepted": False,
                "reason": f"window not completed ({sum(y is not None for y in outcomes)}/{len(sched)} rounds)"}
    return {"trial_id": decl.trial_id, "incumbent_version": decl.incumbent_version,
            "incumbent_hash": decl.incumbent_hash, "challenger_version": decl.challenger_version,
            "challenger_hash": decl.challenger_hash, "n_pairs": decl.n_pairs, "alpha": decl.alpha,
            "min_nonzero": decl.min_nonzero, "complete": complete, "outcomes": list(outcomes), "coins": cs,
            "diffs": diffs, **test}


def verify_decision(doc: Any, decl: TrialDeclaration) -> list[str]:
    """Recompute a decision from its declaration and the outcomes it carries. Problems (empty = consistent)."""
    if not isinstance(doc, dict) or set(doc) != set(_DECISION_FIELDS):
        return ["decision is malformed"]
    try:
        want = decision(decl, doc["outcomes"], doc["coins"] if decl.drawn else None)
    except (PolicyGateError, TypeError) as e:
        return [f"decision cannot be recomputed: {e}"]
    return [] if want == doc else ["decision differs from its recomputation"]


class PolicyTrial:
    """One declared trial: its outcomes per trial round, filled in round order."""

    def __init__(self, decl: TrialDeclaration, entropy: Callable[[], str] | None = None):
        self.decl = decl
        self.outcomes: list[int | None] = [None] * (2 * decl.n_pairs)
        self.coins: list[int | None] = [None] * decl.n_pairs if decl.drawn else list(decl.coins())
        self._entropy = entropy or (lambda: secrets.token_hex(16))

    def index(self, round_no: int) -> int | None:
        i = round_no - self.decl.first_round()
        return i if 0 <= i < len(self.outcomes) else None

    def start(self, round_no: int) -> dict[str, Any] | None:
        """Called when `round_no` starts: draws the coin of its pair if it is the pair's first round (a drawn trial).
        Returns the `policy_trial_coin` record to ledger, or None."""
        i = self.index(round_no)
        if i is None or not self.decl.drawn or self.coins[i // 2] is not None:
            return None
        if i % 2:
            raise PolicyGateError("a pair's coin is drawn at its first round")
        k = i // 2
        nonce = self._entropy()
        if not isinstance(nonce, str) or len(nonce) < 16:
            raise PolicyGateError("the coin entropy must be a string of at least 16 characters")
        self.coins[k] = drawn_coin(self.decl.order_seed, self.decl.trial_id, k, nonce)
        return {"trial_id": self.decl.trial_id, "pair": k, "round": round_no, "nonce": nonce, "coin": self.coins[k]}

    def arm(self, round_no: int) -> str | None:
        i = self.index(round_no)
        if i is None:
            return None
        c = self.coins[i // 2]
        if c is None:
            raise PolicyGateError(f"the coin of pair {i // 2} has not been drawn (round {round_no} not started)")
        return ("incumbent", "challenger")[(i % 2) ^ c]

    def record(self, round_no: int, y: int) -> dict[str, Any]:
        i = self.index(round_no)
        if i is None or self.outcomes[i] is not None:
            raise PolicyGateError(f"round {round_no} is not an open round of trial {self.decl.trial_id}")
        if any(v is None for v in self.outcomes[:i]):
            raise PolicyGateError("trial rounds must be recorded in order")
        if type(y) is not int or y < 0:
            raise PolicyGateError("a round outcome is a count")
        self.outcomes[i] = y
        return {"trial_id": self.decl.trial_id, "round": round_no, "pair": i // 2, "arm": self.arm(round_no), "y": y}

    @property
    def complete(self) -> bool:
        return all(v is not None for v in self.outcomes)

    def decision(self) -> dict[str, Any]:
        return decision(self.decl, self.outcomes, self.coins if self.decl.drawn else None)
