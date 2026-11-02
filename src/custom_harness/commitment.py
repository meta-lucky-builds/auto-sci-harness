"""One commitment primitive: `draw(purpose, ...)` (GOAL §16.3), and the keyed random streams built on it (§16.1).

Every choice made after pre-registration comes from `draw`: held-out subsample, seeds, target, gate tasks, minting,
salt assignment and check parameters. A per-purpose table (PURPOSES) fixes the inputs:

- purposes whose object is frozen before the draw use a public beacon only. Anyone can re-run the draw at once from
  (purpose, prereg_hash, beacon), so a secret is REFUSED for them (it would make the draw unverifiable);
- salt assignment uses an offline human secret plus the beacon. The secret is REQUIRED, and it must appear nowhere
  an agent can read, scratchpads included: `secret_leak_scan` is the grep check (scripts/check_secret.py).

The beacon is an explicit value passed in by the caller. Nothing here fetches anything: whether the beacon is a
drand round, dice rolls or a newspaper is an owner decision (GOAL §13.8), and the caller must fix WHICH beacon value
it will use (e.g. the drand round number) before the object is frozen, or it could shop for a favourable one.

`KeyedStream` is a `random.Random` whose bits come from HMAC-SHA256 in counter mode under a key, so its state cannot
be reconstructed from its outputs (unlike Mersenne Twister, where 624 outputs give the whole state) and it cannot be
reseeded or have its state read or set. `stream(master, label)` derives an independent stream per purpose label:
features, labels and the split of one task never share a stream (the COUPLED-PRNG fix).

Restatement of commit-reveal; stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

DOMAIN = b"chh-draw/1"
STREAM_DOMAIN = b"chh-stream/1"
MIN_SECRET_BYTES = 16  # a shorter secret is guessable and its encodings are too short to grep for without noise
_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class Purpose:
    inputs: str  # "beacon" (public, re-runnable at once) or "secret+beacon" (offline human secret plus the beacon)
    frozen_before_draw: bool  # the object the draw is about is hashed and anchored before the draw
    what: str


# The per-purpose input table (GOAL §16.3). Adding a purpose is a policy change: keep this table the only place.
PURPOSES: dict[str, Purpose] = {
    "heldout_subsample": Purpose("beacon", True, "which held-out rows a claim is scored on"),
    "seed": Purpose("beacon", True, "run seeds of a pre-registered evaluation"),
    "target": Purpose("beacon", True, "which target a pre-registered claim is tested on"),
    "gate_tasks": Purpose("beacon", True, "which tasks form the gate after a candidate is frozen"),
    "heldout_mint": Purpose("beacon", True, "the key of held-out truth minted after the freeze (GOAL §16.2)"),
    "seal_split": Purpose("beacon", True, "which rows of a project's dataset are sealed at its start (GOAL 18.6)"),
    "check_params": Purpose("beacon", True, "parameters of a check, drawn after the checked object is frozen"),
    "salt_assignment": Purpose("secret+beacon", False, "which tasks carry null salts (GOAL §16.9); must stay "
                                                      "unpredictable to whoever is measured"),
}


class DrawError(ValueError):
    pass


def _refuse(msg: str) -> NoReturn:
    raise DrawError(msg)


def draw(purpose: str, *, prereg_hash: str, beacon: str | None, secret: bytes | None) -> int:
    """A 256-bit integer for `purpose`, bound to the frozen object's hash and the beacon (and, for secret purposes,
    the human secret). Deterministic: the same inputs always give the same value. Fails closed (DrawError) on an
    unknown purpose, a malformed hash, a missing beacon, a missing secret where the table requires one, or a secret
    where the table forbids one."""
    spec = PURPOSES.get(purpose)
    if spec is None:
        _refuse(f"unknown draw purpose {purpose!r} (known: {sorted(PURPOSES)})")
    if not isinstance(prereg_hash, str) or not _HEX64.fullmatch(prereg_hash):
        _refuse("prereg_hash must be a lowercase hex SHA-256 (64 characters) of the frozen object")
    if not isinstance(beacon, str) or not beacon.strip():
        _refuse(f"purpose {purpose!r} needs an explicit public beacon value (none is fetched here)")
    if spec.inputs == "beacon":
        if secret is not None:
            _refuse(f"purpose {purpose!r} is beacon-only: anyone must be able to re-run it, so no secret is allowed")
        key = DOMAIN
    else:
        if not isinstance(secret, bytes) or len(secret) < MIN_SECRET_BYTES:
            _refuse(f"purpose {purpose!r} needs the offline human secret ({MIN_SECRET_BYTES}+ bytes)")
        key = secret
    msg = json.dumps([DOMAIN.decode(), purpose, prereg_hash, beacon], separators=(",", ":")).encode()
    return int.from_bytes(hmac.new(key, msg, hashlib.sha256).digest(), "big")


def as_key(value: int) -> bytes:
    """A draw result as a 32-byte stream master key."""
    return value.to_bytes(32, "big")


def subkey(master: bytes, label: str) -> bytes:
    """An independent key for `label` under `master` (HMAC: knowing one subkey says nothing about another)."""
    if not isinstance(master, bytes) or len(master) < MIN_SECRET_BYTES:
        raise DrawError(f"a stream master key must be {MIN_SECRET_BYTES}+ bytes")
    return hmac.new(master, STREAM_DOMAIN + b"\x00" + label.encode(), hashlib.sha256).digest()


class KeyedStream(random.Random):
    """random.Random driven by HMAC-SHA256(key, counter). gauss/uniform/shuffle/randrange/choice all work (they are
    built on random() and getrandbits()). No seed(), getstate() or setstate(): the stream is its key."""

    def __init__(self, key: bytes):
        if not isinstance(key, bytes) or len(key) < MIN_SECRET_BYTES:
            raise DrawError(f"a stream key must be {MIN_SECRET_BYTES}+ bytes")
        self._key = key
        self._ctr = 0
        self._buf = b""
        super().__init__()  # calls seed(None) below, which is a no-op until the stream is ready
        self._ready = True

    def seed(self, a: Any = None, version: int = 2) -> None:
        if getattr(self, "_ready", False):
            raise TypeError("a KeyedStream cannot be reseeded")

    def getstate(self) -> NoReturn:
        raise TypeError("a KeyedStream has no readable state")

    def setstate(self, state: Any) -> NoReturn:
        raise TypeError("a KeyedStream has no settable state")

    def _take(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._buf += hmac.new(self._key, self._ctr.to_bytes(8, "big"), hashlib.sha256).digest()
            self._ctr += 1
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def getrandbits(self, k: int) -> int:
        if k < 0:
            raise ValueError("number of bits must be non-negative")
        if k == 0:
            return 0
        nbytes = (k + 7) // 8
        return int.from_bytes(self._take(nbytes), "big") >> (nbytes * 8 - k)

    def random(self) -> float:
        return self.getrandbits(53) * (2.0 ** -53)


def stream(master: bytes, label: str) -> KeyedStream:
    """The independent keyed stream for `label` under `master`."""
    return KeyedStream(subkey(master, label))


# ---------------------------------------------------------------- the human secret and the grep check
SECRET_FILE_ENV = "CHH_DRAW_SECRET_FILE"  # path of the offline human secret (raw bytes); never inside a scanned root
AGENT_READABLE_ENV = "CHH_AGENT_READABLE"  # os.pathsep-separated roots agents can read (overrides the defaults)


def load_secret(path: Path | None = None) -> bytes:
    """The configured secret (file named by $CHH_DRAW_SECRET_FILE unless `path` is given). Fails closed."""
    p = path if path is not None else (Path(os.environ[SECRET_FILE_ENV]) if os.environ.get(SECRET_FILE_ENV) else None)
    if p is None:
        raise DrawError(f"no secret configured (set {SECRET_FILE_ENV})")
    raw = Path(p).read_bytes().strip()
    if len(raw) < MIN_SECRET_BYTES:
        raise DrawError(f"the configured secret is shorter than {MIN_SECRET_BYTES} bytes")
    return raw


# "lab": the Lab session workspaces (<data>/lab, lab/walls.lab_root; GOAL §17): agents run there with full tools, so
# every secret and truth scan walks them too (docs/lab.md D15). <data>/lab_court (promotion intake) is NOT listed.
AGENT_READABLE_DATA_SUBDIRS = ("sandboxes", "repos", "suites", "tasks", "lab")


def default_agent_readable_roots(repo_root: Path | None = None, *, existing_only: bool = True) -> list[Path]:
    """Every place an agent can read, by default: the repo checkout, the data dir's sandboxes/repos/suites/tasks/lab
    (agents read public task files and their own workspaces) and the agent scratchpads under the system temp dir.
    $CHH_AGENT_READABLE replaces this list. Held-out truth is NOT under any of them: it lives under
    truth_layout.truth_root() (`<data>/private/...`), and truth_layout.truth_layout_problems() checks that statically.
    `existing_only=False` also lists roots that do not exist yet (a containment check must not pass merely because a
    root has not been created)."""
    env = os.environ.get(AGENT_READABLE_ENV)
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p]
    from .config import data_dir

    root = repo_root or Path(__file__).resolve().parents[2]
    d = data_dir()
    roots = [root, *(d / s for s in AGENT_READABLE_DATA_SUBDIRS), Path(tempfile.gettempdir()) / "claude"]
    return [r for r in roots if r.exists()] if existing_only else roots


def agent_readable_root_of(path: Path | str, roots: Iterable[Path | str] | None = None) -> Path | None:
    """The agent-readable root that `path` is or lies under (alias-safe: pathsafe.is_within), else None. Default
    roots: default_agent_readable_roots(existing_only=False)."""
    from .pathsafe import root_containing

    return root_containing(path, default_agent_readable_roots(existing_only=False) if roots is None else roots)


def secret_forms(secret: bytes) -> list[bytes]:
    """The encodings of `secret` the grep looks for: raw, hex (both cases), base64 and urlsafe base64 (padded and
    not), and the text decoding when the secret is text. A transformed copy (split, xored, hashed) is NOT found."""
    hx = secret.hex()
    forms = {secret, hx.encode(), hx.upper().encode()}
    for enc in (base64.b64encode(secret), base64.urlsafe_b64encode(secret)):
        forms |= {enc, enc.rstrip(b"=")}
    return sorted(forms)


def _files(roots: Iterable[Path]) -> Iterable[Path]:
    for r in roots:
        r = Path(r)
        if r.is_file():
            yield r
        elif r.is_dir():
            for dirpath, _dirs, names in os.walk(r):
                for n in names:
                    yield Path(dirpath) / n


def secret_leak_scan(secret: bytes, roots: Iterable[Path], *, secret_file: Path | None = None) -> list[str]:
    """Problems (empty = clean): every file under `roots` holding the secret in any `secret_forms` encoding, the
    secret file itself sitting under a root, or a file that cannot be read (absence cannot be shown: fail closed).
    Messages name paths only, never the secret."""
    if len(secret) < MIN_SECRET_BYTES:
        return [f"secret shorter than {MIN_SECRET_BYTES} bytes: too short to scan for without false matches"]
    from .pathsafe import is_within

    roots = [Path(r).resolve() for r in roots]
    problems: list[str] = []
    if secret_file is not None:
        sf = Path(secret_file).resolve()
        problems += [f"the secret file {sf} is inside agent-readable root {r}" for r in roots
                     if is_within(secret_file, r)]
    forms = secret_forms(secret)
    for f in _files(roots):
        try:
            data = f.read_bytes()
        except OSError as e:
            problems.append(f"{f}: unreadable ({type(e).__name__}); cannot show the secret is absent")
            continue
        if any(form in data for form in forms):
            problems.append(f"{f}: contains the draw secret")
    return problems
