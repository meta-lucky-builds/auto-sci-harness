"""The delegation driver (GOAL §15, last paragraph): OR's delegation rules enforced by code, not by the prompt.

Sessions are identified by secret tokens the driver issues (only their sha256 is stored). A *lead* is registered
by the owner/orchestrator; a *helper* exists only because a lead spawned it with a valid brief. The driver:

  - refuses spawning by anything that is not a registered lead (helpers cannot spawn helpers; unknown tokens are
    nobody),
  - refuses a brief that is invalid (missing field, unsafe path, context-dependent text: brief.py),
  - caps helpers in flight (all leads of this root) and helpers spawned per lead (params.py, UNCALIBRATED),
  - never hands out a branch or worktree another session holds: lock files created with O_EXCL, honoured even
    when no ledger entry explains them (fail closed), plus an optional probe of `git worktree list`,
  - refuses every compute / model-quota spend not covered by the session's grants, and every spend that would go
    over a grant's limit.

Every action and every refusal is appended to a hash-chained JSONL ledger; a broken chain is an IntegrityError on
the next call (no silent recovery). State (sessions, spends, in-flight) is re-derived from the ledger on every call,
so separate processes (CLI invocations) share it; a driver-wide O_EXCL mutex serialises mutations.

Not built here: the driver does not run helpers or call models. Callers (the §15 expansion, the gateway) must call
`spend()` BEFORE making the spend; nothing yet forces the gateway to do so (stated in the report)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import secrets
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import params
from .brief import _BRANCH, SpendGrant, brief_problems, check_grants, parse_brief

Reason = Literal[
    "unknown_session", "nested_spawn", "brief_invalid", "in_flight_cap", "total_cap", "branch_held",
    "worktree_held", "spend_unauthorized", "spend_over_budget", "spend_invalid", "session_finished",
    "helper_cannot_claim", "not_holder", "not_a_helper", "not_parent", "duplicate_session", "invalid_request",
]

GENESIS = "0" * 64


class Refused(Exception):
    def __init__(self, action: str, reason: Reason, detail: Mapping[str, Any]):
        super().__init__(f"{action} refused: {reason} {dict(detail)}")
        self.action, self.reason, self.detail = action, reason, dict(detail)


class IntegrityError(RuntimeError):
    pass


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


# ---------------------------------------------------------------- ledger
class Ledger:
    """Append-only, hash-chained JSONL. entry = {seq, prev, at, kind, data, hash}; hash covers the rest."""

    def __init__(self, path: Path, clock: Callable[[], datetime]):
        self.path, self.clock = path, clock

    @staticmethod
    def _hash(e: Mapping[str, Any]) -> str:
        return _sha(_canon({k: e[k] for k in ("seq", "prev", "at", "kind", "data")}))

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        prev = GENESIS
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                raise IntegrityError(f"orchestration ledger: blank line {n + 1}")
            try:
                e = json.loads(line)
                ok = (isinstance(e, dict) and e.get("seq") == len(out) and e.get("prev") == prev
                      and e.get("hash") == self._hash(e))
            except (ValueError, KeyError, TypeError) as err:
                raise IntegrityError(f"orchestration ledger: unreadable line {n + 1}: {err}") from err
            if not ok:
                raise IntegrityError(f"orchestration ledger: line {n + 1} breaks the hash chain (edited, deleted "
                                     f"or reordered history)")
            out.append(e)
            prev = e["hash"]
        return out

    def append(self, kind: str, data: Mapping[str, Any]) -> dict[str, Any]:
        es = self.entries()
        e: dict[str, Any] = {"seq": len(es), "prev": es[-1]["hash"] if es else GENESIS,
                             "at": self.clock().isoformat(), "kind": kind, "data": dict(data)}
        e["hash"] = self._hash(e)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="") as f:
            f.write(_canon(e) + "\n")
        return e


# ---------------------------------------------------------------- locks
LockKind = Literal["branch", "worktree"]


class Locks:
    """One file per held branch / worktree, created with O_CREAT|O_EXCL (atomic on one filesystem). The file name
    is a hash of the exact name; the content repeats the name, which is checked on read."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, kind: LockKind, name: str) -> Path:
        return self.root / f"{kind}-{_sha(kind + chr(0) + name)[:40]}.lock"

    def holder(self, kind: LockKind, name: str) -> str | None:
        p = self._path(kind, name)
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise IntegrityError(f"lock {p.name} unreadable: {e}") from e
        if not isinstance(d, dict) or d.get("kind") != kind or d.get("name") != name or not d.get("holder"):
            raise IntegrityError(f"lock {p.name} does not describe {kind} {name!r}")
        return str(d["holder"])

    def acquire(self, kind: LockKind, name: str, holder: str, extra: Mapping[str, Any] | None = None) -> bool:
        p = self._path(kind, name)
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(_canon({"kind": kind, "name": name, "holder": holder, **(extra or {})}))
        return True

    def release(self, kind: LockKind, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path(kind, name).unlink()

    def write_foreign(self, kind: LockKind, name: str, holder: str) -> None:
        """Record a hold by a session outside this driver (another tool, a human). Refuses to overwrite."""
        if not self.acquire(kind, name, holder):
            raise FileExistsError(f"{kind} {name!r} is already held by {self.holder(kind, name)}")

    def all(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {"branches": {}, "worktrees": {}}
        for p in sorted(self.root.glob("*.lock")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise IntegrityError(f"lock {p.name} unreadable: {e}") from e
            if not (isinstance(d, dict) and d.get("kind") in ("branch", "worktree")
                    and p == self._path(d["kind"], str(d.get("name")))):
                raise IntegrityError(f"lock {p.name} does not match its content")
            out["branches" if d["kind"] == "branch" else "worktrees"][str(d["name"])] = str(d["holder"])
        return out


def git_checked_out(repo: Path) -> dict[str, str]:
    """branch -> worktree path for every worktree of `repo` (`git worktree list --porcelain`)."""
    res = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"], capture_output=True,
                         text=True, check=True)
    out: dict[str, str] = {}
    path = None
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/") and path is not None:
            out[line[len("branch refs/heads/"):]] = path
    return out


# ---------------------------------------------------------------- driver
@dataclass(frozen=True)
class HelperHandle:
    session: str
    token: str  # give this to the helper (environment / file), never in argv or the brief


@dataclass
class _Session:
    name: str
    role: Literal["lead", "helper"]
    grants: list[SpendGrant]
    parent: str | None = None
    finished: bool = False


class Driver:
    def __init__(self, root: Path, *, max_in_flight: int | None = None, max_total_per_lead: int | None = None,
                 checked_out: Callable[[], Mapping[str, str]] | None = None, worktree_base: Path | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_in_flight = params.MAX_IN_FLIGHT if max_in_flight is None else max_in_flight
        self.max_total_per_lead = params.MAX_TOTAL_PER_LEAD if max_total_per_lead is None else max_total_per_lead
        if self.max_in_flight < 1 or self.max_total_per_lead < 1:
            raise ValueError("caps must be >= 1")
        self.ledger = Ledger(self.root / "ledger.jsonl", clock)
        self.locks = Locks(self.root / "locks")
        self.checked_out = checked_out
        self.worktree_base = Path(worktree_base) if worktree_base else self.root.parent
        self._mutex_path = self.root / "driver.mutex"

    # -- plumbing
    @contextlib.contextmanager
    def _mutex(self) -> Iterator[None]:
        deadline = time.monotonic() + params.MUTEX_TIMEOUT_S
        while True:
            try:
                fd = os.open(self._mutex_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"driver mutex {self._mutex_path} held for > {params.MUTEX_TIMEOUT_S}s "
                                       f"(a crashed process leaves it behind: remove it by hand)") from None
                time.sleep(0.02)
        os.close(fd)
        try:
            yield
        finally:
            with contextlib.suppress(FileNotFoundError):
                self._mutex_path.unlink()

    def _state(self) -> tuple[dict[str, _Session], dict[str, str], list[dict[str, Any]]]:
        """sessions by name, token_hash -> name, entries (all verified)."""
        es = self.ledger.entries()
        sessions: dict[str, _Session] = {}
        by_token: dict[str, str] = {}
        for e in es:
            d = e["data"]
            if e["kind"] == "lead_registered":
                sessions[d["session"]] = _Session(d["session"], "lead",
                                                  [SpendGrant.model_validate(g) for g in d["allowed_spends"]])
                by_token[d["token_hash"]] = d["session"]
            elif e["kind"] == "spawned":
                sessions[d["session"]] = _Session(d["session"], "helper",
                                                  [SpendGrant.model_validate(g) for g in d["brief"]["allowed_spends"]],
                                                  parent=d["parent"])
                by_token[d["token_hash"]] = d["session"]
            elif e["kind"] in ("completed", "aborted"):
                sessions[d["session"]].finished = True
        return sessions, by_token, es

    def _refuse(self, action: str, reason: Reason, requester: str | None, detail: Mapping[str, Any]) -> Refused:
        self.ledger.append("refused", {"action": action, "reason": reason, "requester": requester,
                                       "detail": dict(detail)})
        return Refused(action, reason, detail)

    def _who(self, token: str, action: str, by_token: Mapping[str, str]) -> str:
        name = by_token.get(_sha(str(token)))
        if name is None:
            raise self._refuse(action, "unknown_session", None, {})
        return name

    def _norm_wt(self, p: str) -> str:
        q = Path(p)
        if not q.is_absolute():
            q = self.worktree_base / q
        return os.path.normcase(str(q.resolve()))

    def _blocker(self, branch: str, worktree: str) -> tuple[Reason, dict[str, str]] | None:
        h = self.locks.holder("branch", branch)
        if h is not None:
            return "branch_held", {"branch": branch, "holder": h}
        h = self.locks.holder("worktree", worktree)
        if h is not None:
            return "worktree_held", {"worktree": worktree, "holder": h}
        if self.checked_out is not None:
            for b, path in self.checked_out().items():
                other = self._norm_wt(path)
                if b == branch and other != worktree:
                    return "branch_held", {"branch": branch, "holder": f"git-worktree:{path}"}
                if other == worktree and b != branch:
                    return "worktree_held", {"worktree": worktree, "holder": f"git-worktree:{path} ({b})"}
        return None

    def _take(self, branch: str, worktree: str, holder: str) -> tuple[Reason, dict[str, str]] | None:
        blocked = self._blocker(branch, worktree)
        if blocked:
            return blocked
        if not self.locks.acquire("branch", branch, holder, {"worktree": worktree}):
            return "branch_held", {"branch": branch, "holder": self.locks.holder("branch", branch) or "?"}
        if not self.locks.acquire("worktree", worktree, holder, {"branch": branch}):
            self.locks.release("branch", branch)
            return "worktree_held", {"worktree": worktree, "holder": self.locks.holder("worktree", worktree) or "?"}
        return None

    def _drop_all(self, holder: str) -> None:
        held = self.locks.all()
        for b, h in held["branches"].items():
            if h == holder:
                self.locks.release("branch", b)
        for w, h in held["worktrees"].items():
            if h == holder:
                self.locks.release("worktree", w)

    # -- sessions
    def register_lead(self, name: str, *, allowed_spends: list[Any]) -> str:
        """Owner/orchestrator action: register a top-level session; returns its secret token."""
        grants = check_grants([SpendGrant.model_validate(g) for g in allowed_spends])
        with self._mutex():
            sessions, _, _ = self._state()
            if name in sessions:
                raise self._refuse("register", "duplicate_session", None, {"session": name})
            if not name or name.startswith("helper-") or len(name) > 64:
                raise self._refuse("register", "invalid_request", None, {"session": name})
            token = secrets.token_urlsafe(32)
            self.ledger.append("lead_registered", {"session": name, "token_hash": _sha(token),
                                                   "allowed_spends": [g.model_dump() for g in grants]})
            return token

    def spawn(self, token: str, raw_brief: Any) -> HelperHandle:
        with self._mutex():
            sessions, by_token, es = self._state()
            bid = raw_brief.get("brief_id") if isinstance(raw_brief, dict) else None
            base = {"brief_id": bid if isinstance(bid, str) else None}
            who = self._who(token, "spawn", by_token)
            s = sessions[who]
            if s.role != "lead":
                raise self._refuse("spawn", "nested_spawn", who, {**base, "parent": s.parent})
            problems = brief_problems(raw_brief)
            if problems:
                raise self._refuse("spawn", "brief_invalid", who, {**base, "problems": problems})
            b = parse_brief(raw_brief)
            flying = [x for x in sessions.values() if x.role == "helper" and not x.finished]
            if len(flying) >= self.max_in_flight:
                raise self._refuse("spawn", "in_flight_cap", who,
                                   {**base, "in_flight": len(flying), "cap": self.max_in_flight})
            mine = sum(1 for x in sessions.values() if x.role == "helper" and x.parent == who)
            if mine >= self.max_total_per_lead:
                raise self._refuse("spawn", "total_cap", who, {**base, "spawned": mine, "cap": self.max_total_per_lead})
            helper = f"helper-{len(es):04d}-{b.brief_id}"
            wt = self._norm_wt(b.worktree)
            blocked = self._take(b.branch, wt, helper)
            if blocked:
                reason, d = blocked
                raise self._refuse("spawn", reason, who, {**base, **d})
            token_h = secrets.token_urlsafe(32)
            self.ledger.append("spawned", {"session": helper, "parent": who, "token_hash": _sha(token_h),
                                           "worktree_resolved": wt, "brief": b.model_dump(),
                                           "brief_hash": _sha(_canon(b.model_dump()))})
            return HelperHandle(helper, token_h)

    def complete(self, token: str, *, output: str) -> None:
        with self._mutex():
            sessions, by_token, _ = self._state()
            who = self._who(token, "complete", by_token)
            s = sessions[who]
            if s.role != "helper":
                raise self._refuse("complete", "not_a_helper", who, {})
            if s.finished:
                raise self._refuse("complete", "session_finished", who, {})
            self._drop_all(who)
            self.ledger.append("completed", {"session": who, "output": str(output)})

    def abort(self, token: str, helper: str, *, reason: str) -> None:
        """The parent lead stops its helper: the slot and the helper's locks are freed."""
        with self._mutex():
            sessions, by_token, _ = self._state()
            who = self._who(token, "abort", by_token)
            h = sessions.get(helper)
            if h is None or h.role != "helper" or h.parent != who:
                raise self._refuse("abort", "not_parent", who, {"helper": helper})
            if h.finished:
                raise self._refuse("abort", "session_finished", who, {"helper": helper})
            self._drop_all(helper)
            self.ledger.append("aborted", {"session": helper, "by": who, "reason": str(reason)})

    # -- branches
    def claim(self, token: str, *, branch: str, worktree: str) -> None:
        """A lead holds its own branch + worktree, so no helper is ever handed them."""
        with self._mutex():
            sessions, by_token, _ = self._state()
            who = self._who(token, "claim", by_token)
            if sessions[who].role != "lead":
                raise self._refuse("claim", "helper_cannot_claim", who, {"branch": branch})
            if not _BRANCH.match(branch or ""):
                raise self._refuse("claim", "invalid_request", who, {"branch": branch})
            wt = self._norm_wt(worktree)
            blocked = self._take(branch, wt, who)
            if blocked:
                reason, d = blocked
                raise self._refuse("claim", reason, who, d)
            self.ledger.append("claimed", {"session": who, "branch": branch, "worktree": wt})

    def release(self, token: str, *, branch: str) -> None:
        with self._mutex():
            _, by_token, _ = self._state()
            who = self._who(token, "release", by_token)
            if self.locks.holder("branch", branch) != who:
                raise self._refuse("release", "not_holder", who, {"branch": branch,
                                                                  "holder": self.locks.holder("branch", branch)})
            self._release_branch(branch, who)
            self.ledger.append("released", {"session": who, "branch": branch})

    def _release_branch(self, branch: str, holder: str) -> None:
        held = self.locks.all()
        self.locks.release("branch", branch)
        for w, h in held["worktrees"].items():
            if h == holder:
                self.locks.release("worktree", w)

    def force_release(self, branch: str, *, by: str, reason: str) -> None:
        """HUMAN action (a crashed session left its lock): ledgered with who and why."""
        with self._mutex():
            self._state()  # integrity first
            holder = self.locks.holder("branch", branch)
            if holder is not None:
                self._release_branch(branch, holder)
            self.ledger.append("force_released", {"branch": branch, "holder": holder, "by": str(by),
                                                  "reason": str(reason)})

    # -- spends
    def spend(self, token: str, kind: str, resource: str, unit: str, amount: Any) -> None:
        """Authorise (and record) a spend BEFORE it is made. Raises Refused if it is outside the grants."""
        with self._mutex():
            sessions, by_token, es = self._state()
            who = self._who(token, "spend", by_token)
            s = sessions[who]
            key = (str(kind), str(resource), str(unit))
            d: dict[str, Any] = {"kind": key[0], "resource": key[1], "unit": key[2]}
            if s.finished:
                raise self._refuse("spend", "session_finished", who, d)
            if (isinstance(amount, bool) or not isinstance(amount, int | float) or not math.isfinite(amount)
                    or amount <= 0):
                raise self._refuse("spend", "spend_invalid", who, {**d, "amount": repr(amount)})
            amt = float(amount)
            grant = next((g for g in s.grants if g.key() == key), None)
            if grant is None:
                raise self._refuse("spend", "spend_unauthorized", who, {**d, "amount": amt})
            used = self._used(es, who).get(key, 0.0)
            if used + amt > grant.limit:
                raise self._refuse("spend", "spend_over_budget", who,
                                   {**d, "amount": amt, "used": used, "limit": grant.limit})
            self.ledger.append("spend", {"session": who, **d, "amount": amt})

    @staticmethod
    def _used(es: list[dict[str, Any]], who: str) -> dict[tuple[str, str, str], float]:
        out: dict[tuple[str, str, str], float] = {}
        for e in es:
            d = e["data"]
            if e["kind"] == "spend" and d["session"] == who:
                k = (d["kind"], d["resource"], d["unit"])
                out[k] = out.get(k, 0.0) + float(d["amount"])
        return out

    def used(self, session: str) -> dict[tuple[str, str, str], float]:
        return self._used(self.ledger.entries(), session)

    # -- reading
    def status(self) -> dict[str, Any]:
        sessions, _, es = self._state()
        return {
            "in_flight": [s.name for s in sessions.values() if s.role == "helper" and not s.finished],
            "sessions": {s.name: {"role": s.role, "parent": s.parent, "finished": s.finished,
                                  "used": {"/".join(k): v for k, v in self._used(es, s.name).items()}}
                         for s in sessions.values()},
            "locks": self.locks.all(),
            "refusals": sum(1 for e in es if e["kind"] == "refused"),
            "caps": {"in_flight": self.max_in_flight, "total_per_lead": self.max_total_per_lead},
        }
