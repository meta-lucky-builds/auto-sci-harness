"""Where held-out truth lives: OUTSIDE every agent-readable root (GOAL §10 invariant 2, §16.1; s4-integrity item 1).

Before (w2-suite red team, 2026-10-09): each task's `private/` (test labels, canary, mint record) sat inside the task
directory, under `<data>/suites`, which commitment.default_agent_readable_roots() lists. Run-time `protect_path`
kept it out of sandbox MOUNTS, but any agent that can read the data dir (a read-only Codex sandbox reads what the user
can) could read the labels. That was the main open route to held-out labels.

Now (orchestrator decision 2026-10-14, owner-delegated):

    <data>/suites/<suite>/<task>/            task.yaml, baseline.py, public/        agent-readable
    <data>/private/suites/<suite>/<task>/    test_y.csv, canary.txt, mint.json      truth: NOT agent-readable

i.e. a task directory under `<data>/<sub>` (sub in MAPPED_SUBDIRS: the agent-readable data dirs that hold tasks)
keeps its truth at `truth_root()/<sub>/<same relative path>`. truth_root() is `<data>/private` ($CHH_TRUTH_ROOT
overrides it). Only trusted code resolves it: the suite builder/exporter (research/suite.py, research/rebuild.py),
task registration into the Kernel vault (research/claims.py register_task), the Kernel and kerneld, and trusted audit
scripts. Agent code never receives the path: every sandbox mount overlapping the truth root is refused
(sandbox/base.protected_paths lists it).

A task directory anywhere else (a test's tmp dir, an exam's scratch dir) keeps the legacy `<task>/private/` layout,
but ONLY when that location is outside every agent-readable root: `truth_dir` refuses (TruthLayoutError) otherwise.
A task under a mapped dir whose truth still sits in the legacy place is refused with a pointer to the migration
script (scripts/migrate_truth.py): it is never read from there.

Checks:
- static: `truth_layout_problems()` (no truth root overlaps an agent-readable root; no agent-readable root holds a
  legacy truth file), used by tests and by the migration script's verify mode;
- run time: `truth_dir` / `truth_file` refuse a truth location inside an agent-readable root (alias-safe,
  pathsafe.is_within), on every read and write.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from .pathsafe import canon, is_within, overlaps

TRUTH_ROOT_ENV = "CHH_TRUTH_ROOT"
# agent-readable data subdirectories that hold task directories (a subset of commitment.AGENT_READABLE_DATA_SUBDIRS)
MAPPED_SUBDIRS = ("suites", "tasks")
LEGACY_DIRNAME = "private"
# the files a task's truth directory holds (research/suite.py write_task, mint_heldout); anything else in a legacy
# private/ dir is truth too (fail closed: the scan and the migration treat every file there as truth)
TRUTH_FILE_NAMES = ("test_y.csv", "canary.txt", "mint.json")


class TruthLayoutError(PermissionError):
    """Held-out truth would sit (or be read from) inside an agent-readable root, or has not been migrated."""


def truth_root() -> Path:
    """The root of all held-out truth: $CHH_TRUTH_ROOT, else `<data>/private`."""
    env = os.environ.get(TRUTH_ROOT_ENV)
    if env:
        return Path(env)
    from .config import data_dir

    return data_dir() / "private"


def protected_truth_dirs() -> list[Path]:
    """The directories that hold mapped truth (`truth_root()/<sub>`); no sandbox may mount them. The rest of
    `<data>/private` (beacons, suite records, the trusted exporter's workspace) is not covered by this list."""
    return [truth_root() / s for s in MAPPED_SUBDIRS]


def _mapped(task_dir: Path) -> Path | None:
    from .config import data_dir

    d = data_dir()
    td = canon(task_dir)
    for sub in MAPPED_SUBDIRS:
        base = canon(d / sub)
        if td == base or base in td.parents:
            return truth_root() / sub / td.relative_to(base)
        if is_within(td, base):  # an alias the canonical form did not unify: refuse rather than guess the path
            raise TruthLayoutError(f"task dir {task_dir} is an alias inside {d / sub}; use its canonical path")
    return None


def _agent_readable_root_of(p: Path) -> Path | None:
    from .commitment import agent_readable_root_of

    return agent_readable_root_of(p)


def require_outside_agent_readable(path: Path | str, what: str = "held-out truth") -> None:
    """Run-time check: refuse (TruthLayoutError) when `path` is, or lies under, an agent-readable root."""
    root = _agent_readable_root_of(Path(path))
    if root is not None:
        raise TruthLayoutError(f"{what} at {path} would be inside agent-readable root {root} (invariant 2)")


def legacy_truth_dir(task_dir: Path | str) -> Path:
    return Path(task_dir) / LEGACY_DIRNAME


def truth_dir(task_dir: Path | str) -> Path:
    """The directory holding `task_dir`'s held-out truth (see the module doc). Fails closed (TruthLayoutError)
    when that directory would be inside an agent-readable root."""
    mapped = _mapped(Path(task_dir))
    out = mapped if mapped is not None else legacy_truth_dir(task_dir)
    require_outside_agent_readable(out)
    return out


def truth_file(task_dir: Path | str, name: str) -> Path:
    """Path of one truth file of `task_dir` for READING. Refuses (TruthLayoutError) a task under a mapped
    agent-readable dir whose truth was never migrated (the file exists only in the legacy place), so labels are
    never read from an agent-readable location; scripts/migrate_truth.py moves them."""
    if not name or Path(name).name != name:
        raise TruthLayoutError(f"truth file name {name!r} is not a plain file name")
    d = truth_dir(task_dir)
    p = d / name
    if not p.exists():
        legacy = legacy_truth_dir(task_dir) / name
        if legacy != p and legacy.exists():
            raise TruthLayoutError(f"{task_dir}: truth file {name} is still in the agent-readable legacy place "
                                   f"{legacy.parent}; run scripts/migrate_truth.py (archive + move) first")
    return p


def truth_files(task_dir: Path | str) -> dict[str, Path]:
    """Every file of `task_dir`'s truth directory, by its path relative to it (posix). Empty if none."""
    d = truth_dir(task_dir)
    if not d.is_dir():
        legacy = legacy_truth_dir(task_dir)
        if legacy != d and legacy.is_dir() and any(legacy.iterdir()):
            raise TruthLayoutError(f"{task_dir}: truth is still in the agent-readable legacy place {legacy}; run "
                                   f"scripts/migrate_truth.py first")
        return {}
    return {p.relative_to(d).as_posix(): p for p in sorted(d.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------- static checks
def legacy_truth_paths(roots: Iterable[Path | str]) -> Iterator[Path]:
    """Every file under a directory named `private` that holds a TRUTH_FILE_NAMES file, below any of `roots` (the
    legacy layout; also finds old copies such as `tasks/rings-v1/private/test_y.csv` or a scratchpad's
    `salts-tmp/e/<task>/private/test_y.csv`). Yields every file of each such directory. Skips `.git`."""
    seen: set[str] = set()
    for r in roots:
        r = Path(r)
        if not r.is_dir():
            continue
        for dirpath, dirs, names in os.walk(r):
            dirs[:] = [x for x in dirs if x != ".git"]
            p = Path(dirpath)
            if p.name != LEGACY_DIRNAME or not any(n in TRUTH_FILE_NAMES for n in names):
                continue
            for n in sorted(names):
                f = p / n
                key = os.path.normcase(str(f))
                if key not in seen:
                    seen.add(key)
                    yield f


def truth_layout_problems(roots: Iterable[Path | str] | None = None, *, scan: bool = False) -> list[str]:
    """Static check (empty = clean):
    - the truth root, and every mapped truth dir, overlaps no agent-readable root (by path, alias-safe);
    - with `scan=True`, no legacy truth file sits under an agent-readable root (walks the roots: slow on big trees).
    `roots`: default commitment.default_agent_readable_roots(existing_only=False)."""
    from .commitment import default_agent_readable_roots

    rs = [Path(r) for r in (default_agent_readable_roots(existing_only=False) if roots is None else roots)]
    problems = []
    for t in [truth_root(), *protected_truth_dirs()]:
        for r in rs:
            if overlaps(t, r):
                problems.append(f"truth location {t} overlaps agent-readable root {r}")
    if scan:
        problems += [f"legacy truth file inside an agent-readable root: {f}" for f in legacy_truth_paths(rs)]
    return problems
