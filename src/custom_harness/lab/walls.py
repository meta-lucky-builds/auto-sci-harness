"""The Lab's hard walls (GOAL §17 "the one hard wall", §10 invariant 2): static and run-time checks.

What a lab box may see of the host is exactly ONE directory: its session workspace, a directory strictly inside the
lab root (`$CHH_LAB_ROOT`, default `<data>/lab`). Everything else is refused, by path, before `docker run`:

- the evidence store, bundles and the human-only config (`sandbox.base.protected_paths()`, the same list every
  sandbox mount is checked against);
- every held-out truth location: `truth_layout.truth_root()` and its mapped truth dirs when s4-integrity's
  `truth_layout` is merged, else `$CHH_TRUTH_ROOT` or `<data>/private` (the same default truth_layout uses);
- the offline draw secret (`$CHH_DRAW_SECRET_FILE`, GOAL §16.3);
- the owner's credential stores in the home directory (~/.ssh, ~/.aws, ~/.codex, ~/.claude, ...), the repo checkout
  and the Python environment running this code;
- the data dir itself, the home directory and a drive root (the workspace may not CONTAIN them).

Overlap is checked in both directions (the workspace may be neither inside nor around a forbidden path), on
canonical paths (resolved, case-folded on Windows), and no component of the workspace path may be a link or
junction (an alias would defeat the path comparison).

Run-time content checks on the workspace (`scan_workspace`), at `start` and again at `promote`:
- no legacy truth directory (`private/` holding test_y.csv / canary.txt / mint.json);
- no exact copy of any PRIVATE vault file (sha256 equal to a registered private blob; only files whose size equals
  some private blob's size are hashed). Metadata only: no private byte is read;
- no copy of the draw secret in any common encoding (`commitment.secret_leak_scan`), when a secret is configured.
A transformed copy (re-encoded, split, compressed) is NOT found by these scans: stated, not hidden. The real guarantee
is that the truth is never reachable from the box in the first place (mounts, network to kerneld: kerneld speaks over
a pipe only, no socket).

The final `docker run` argv is checked too (`docker_argv_problems`): one bind mount (the workspace), no privileged
mode, no host namespaces, no added capabilities or devices, no Docker socket, and only the allowed environment names.

Built, NOT test-verified.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from ..config import data_dir

LAB_ROOT_ENV = "CHH_LAB_ROOT"
TRUTH_ROOT_ENV = "CHH_TRUTH_ROOT"  # same name as truth_layout.TRUTH_ROOT_ENV (s4-integrity)
SECRET_FILE_ENV = "CHH_DRAW_SECRET_FILE"  # same name as commitment.SECRET_FILE_ENV
CONTAINER_WORKSPACE = "/workspace"
# owner credential stores under the home directory: never inside or around a lab workspace
HOME_SECRET_DIRS = (".ssh", ".aws", ".azure", ".config", ".codex", ".claude", ".claude.json", ".docker", ".gnupg",
                    ".kube", ".netrc", ".git-credentials", ".openresearch", ".npmrc", ".pypirc", ".gemini",
                    ".cursor", ".local/share/opencode")
# truth file names of the legacy task layout (truth_layout.TRUTH_FILE_NAMES on s4-integrity)
TRUTH_FILE_NAMES = ("test_y.csv", "canary.txt", "mint.json")
LEGACY_TRUTH_DIRNAME = "private"
SCAN_MAX_FILES = 500_000  # UNCALIBRATED (owner decision, GOAL §13.8): workspace entries one scan walks (fail closed)


class LabWallError(PermissionError):
    """A lab box would see something the Lab must never see (GOAL §17 hard wall). Fail closed."""


# ---------------------------------------------------------------- paths
# int18 (stage-c): containment is decided by pathsafe (s4-integrity), which also sees aliases (8.3 short names,
# junctions, extended-length prefixes, file identity) that a canonical-path prefix test misses; lab-agents was
# written before s4-integrity was merged (test_pathsafe::test_no_containment_check_bypasses_pathsafe flagged it).
def canon(p: Path | str) -> Path:
    """Resolved, case-folded (Windows) absolute path (pathsafe.canon)."""
    from ..pathsafe import canon as _canon
    return _canon(Path(p).expanduser())


def overlaps(a: Path | str, b: Path | str) -> bool:
    """True when a and b are the same path or one lies inside the other (pathsafe.overlaps, alias-safe)."""
    from ..pathsafe import overlaps as _overlaps
    return _overlaps(Path(a).expanduser(), Path(b).expanduser())


def contains(outer: Path | str, inner: Path | str) -> bool:
    """True when inner is outer or lies inside it (pathsafe.is_within, alias-safe)."""
    from ..pathsafe import is_within
    return is_within(Path(inner).expanduser(), Path(outer).expanduser())


def _link_like(p: Path) -> bool:
    from ..sandbox.base import is_link_like
    return is_link_like(p)


def lab_root() -> Path:
    """Where every Lab session workspace lives: $CHH_LAB_ROOT, else `<data>/lab`."""
    env = os.environ.get(LAB_ROOT_ENV)
    return Path(env) if env else data_dir() / "lab"


def truth_locations() -> list[Path]:
    """Every held-out truth location: s4-integrity's truth root and its mapped truth dirs (truth_layout, the one
    module that builds truth locations; int18 stage-c removed the pre-merge fallback that joined `<data>/private`)."""
    from .. import truth_layout as tl
    return [Path(tl.truth_root()), *(Path(p) for p in tl.protected_truth_dirs())]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def forbidden_overlaps() -> list[tuple[Path, str]]:
    """(path, why) pairs a lab workspace (and the lab root) may neither be inside nor contain."""
    from ..sandbox.base import protected_paths
    out: list[tuple[Path, str]] = [(p, "evidence store / bundles / human config / protected path")
                                   for p in protected_paths()]
    out += [(p, "held-out truth location") for p in truth_locations()]
    secret = os.environ.get(SECRET_FILE_ENV)
    if secret:
        out.append((Path(secret), "the offline draw secret (GOAL §16.3)"))
    home = Path.home()
    out += [(home / d, "an owner credential store") for d in HOME_SECRET_DIRS]
    out.append((_repo_root(), "the harness repo checkout (owner host filesystem)"))
    out.append((Path(sys.prefix), "the Python environment running the harness"))
    return out


def forbidden_ancestors() -> list[tuple[Path, str]]:
    """(path, why) pairs a lab workspace (and the lab root) may not equal or contain."""
    anchors = {Path(p.anchor) for p in (Path.home(), data_dir()) if p.anchor}
    return [(data_dir(), "the harness data dir"), (Path.home(), "the owner's home directory"),
            *((a, "a drive / filesystem root") for a in sorted(anchors))]


def _path_problems(p: Path, what: str) -> list[str]:
    problems = []
    for f, why in forbidden_overlaps():
        if overlaps(p, f):
            problems.append(f"{what} {p} overlaps {f} ({why})")
    for f, why in forbidden_ancestors():
        if contains(p, f):
            problems.append(f"{what} {p} is or contains {f} ({why})")
    return problems


def lab_root_problems(root: Path | None = None) -> list[str]:
    """Static check of the lab root (empty = clean)."""
    r = Path(root) if root is not None else lab_root()
    return _path_problems(r, "lab root")


def workspace_problems(workspace: Path | str, root: Path | None = None) -> list[str]:
    """Run-time path check of one session workspace (empty = clean): strictly inside the lab root, no link or junction
    on its path below the root, and no overlap with anything forbidden."""
    r = Path(root) if root is not None else lab_root()
    ws = Path(workspace)
    problems = lab_root_problems(r)
    if not contains(r, ws) or contains(ws, r):  # strictly inside (pathsafe, alias-safe)
        problems.append(f"workspace {ws} is not strictly inside the lab root {r}")
        return problems
    cur = Path(os.path.abspath(r))
    for part in Path(os.path.abspath(ws)).relative_to(os.path.abspath(r)).parts:
        cur = cur / part
        if _link_like(cur):
            problems.append(f"workspace path component {cur} is a link or junction (an alias defeats the wall)")
            break
    problems += _path_problems(ws, "workspace")
    return problems


def require_workspace(workspace: Path | str, root: Path | None = None) -> None:
    problems = workspace_problems(workspace, root)
    if problems:
        raise LabWallError("; ".join(problems))


# ---------------------------------------------------------------- content scan
def _walk_files(root: Path, limit: int) -> Iterable[Path] | None:
    out: list[Path] = []
    n = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                n += 1
                if n > limit:
                    return None
                p = Path(e.path)
                if _link_like(p):
                    continue  # never followed; a link inside the box cannot point at host files the box never had
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(p)
                    elif e.is_file(follow_symlinks=False):
                        out.append(p)
                except OSError:
                    continue
    return out


def private_blob_index(store: object | None) -> dict[int, set[str]]:
    """{size: {sha256}} of every PRIVATE vault file of `store` (metadata only: hashes from the dataset records, sizes
    from the blob files; no private byte is read). {} without a store."""
    if store is None:
        return {}
    from ..kerneld.scope import ScopedKernel
    kernel = ScopedKernel(store)  # host code: never a bare Kernel (kerneld ratchet)
    out: dict[int, set[str]] = {}
    for rec in kernel.vault.datasets().values():
        for ref in rec.files.values():
            if not ref.private:
                continue
            blob_path = kernel.store.blobs.root / ref.blob[:2] / ref.blob
            try:
                size = blob_path.stat().st_size
            except OSError:
                size = -1  # unknown size: hash every file (handled below)
            out.setdefault(size, set()).add(ref.blob)
    return out


def scan_workspace(workspace: Path | str, *, store: object | None = None, secret: bytes | None = None,
                   secret_file: Path | None = None, max_files: int = SCAN_MAX_FILES) -> list[str]:
    """Run-time content check of a lab workspace (empty = clean). See the module doc for what it finds and what it
    cannot find."""
    ws = Path(workspace)
    problems: list[str] = []
    files = _walk_files(ws, max_files)
    if files is None:
        return [f"workspace {ws} has more than {max_files} entries: cannot show it holds no truth (fail closed)"]
    index = private_blob_index(store)
    unknown_size = index.pop(-1, set())
    sizes = set(index)
    for f in files:
        rel = f.relative_to(ws)
        if rel.parent.name == LEGACY_TRUTH_DIRNAME and f.name in TRUTH_FILE_NAMES:
            problems.append(f"{rel.as_posix()}: a legacy held-out truth file (private/{f.name}) inside the workspace")
        if not (index or unknown_size):
            continue
        try:
            size = f.stat().st_size
        except OSError:
            problems.append(f"{rel.as_posix()}: unreadable; cannot show it is not a private file (fail closed)")
            continue
        if size not in sizes and not unknown_size:
            continue
        try:
            h = hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError:
            problems.append(f"{rel.as_posix()}: unreadable; cannot show it is not a private file (fail closed)")
            continue
        if h in index.get(size, set()) or h in unknown_size:
            problems.append(f"{rel.as_posix()}: an exact copy of a PRIVATE vault file (invariant 2)")
    if secret is not None:
        from ..commitment import secret_leak_scan
        problems += secret_leak_scan(secret, [ws], secret_file=secret_file)
    return problems


def configured_secret() -> tuple[bytes | None, Path | None]:
    """The draw secret when one is configured ($CHH_DRAW_SECRET_FILE), for the workspace scan; (None, None) if not.
    A configured but unreadable secret fails closed (the caller cannot show it is absent)."""
    p = os.environ.get(SECRET_FILE_ENV)
    if not p:
        return None, None
    from ..commitment import load_secret
    return load_secret(Path(p)), Path(p)


# ---------------------------------------------------------------- the docker run argv
_FORBIDDEN_FLAGS = ("--privileged", "--cap-add", "--device", "--device-cgroup-rule", "--pid", "--ipc", "--uts",
                    "--userns", "--cgroupns", "--volumes-from", "--security-opt=seccomp=unconfined", "--env-file",
                    "--mount")
_HOST_NETWORK = re.compile(r"^(--network|--net)(=|$)")
_SECRET_NAME = re.compile(r"(?i)(key|token|secret|passw|credential|auth|cookie|session)")


def docker_argv_problems(argv: Sequence[str], *, workspace: Path | str, credential_env: Iterable[str] = (),
                         allowed_env: Iterable[str] = ()) -> list[str]:
    """Static check of the final `docker run` argv for a lab box (empty = clean)."""
    a = list(argv)
    problems: list[str] = []
    if len(a) < 2 or a[1] != "run":
        return [f"not a docker run argv: {a[:2]}"]
    cred = set(credential_env)
    allowed = set(allowed_env) | cred
    mounts: list[str] = []
    i = 2
    while i < len(a):
        t = a[i]
        if not t.startswith("-"):
            break  # the image; the command follows
        nxt = a[i + 1] if i + 1 < len(a) else ""
        name = t.split("=", 1)[0]
        if name in _FORBIDDEN_FLAGS or t in _FORBIDDEN_FLAGS:
            problems.append(f"forbidden docker flag {t!r}")
        if _HOST_NETWORK.match(t):
            val = t.split("=", 1)[1] if "=" in t else nxt
            if val == "host" or val.startswith("container:"):
                problems.append(f"host / shared network namespace refused ({val!r})")
        if name in ("-v", "--volume"):
            mounts.append(t.split("=", 1)[1] if "=" in t else nxt)
        if name in ("-e", "--env"):
            val = t.split("=", 1)[1] if "=" in t else nxt
            env_name = val.split("=", 1)[0]
            if env_name not in allowed:
                problems.append(f"environment variable {env_name!r} is not on the lab allowlist")
            if env_name in cred and "=" in val:
                problems.append(f"credential {env_name!r} has its value in the argv (process listings show argv)")
            if env_name not in cred and _SECRET_NAME.search(env_name):
                problems.append(f"secret-like variable {env_name!r} is not the session credential")
        if "docker.sock" in t or "docker.sock" in nxt:
            problems.append("the Docker socket must never reach a lab box")
        if name in ("--security-opt",) and "unconfined" in nxt:
            problems.append(f"unconfined security profile refused ({nxt!r})")
        i += 2 if (name == t and t not in _BOOLEAN_FLAGS) else 1
    want = f"{Path(workspace)}:{CONTAINER_WORKSPACE}"
    if mounts != [want] and mounts != [want + ":rw"]:
        problems.append(f"a lab box has exactly one bind mount, the workspace ({want}); got {mounts}")
    return problems


_BOOLEAN_FLAGS = frozenset({"-d", "--detach", "--init", "--rm", "-i", "-t", "-it", "--interactive", "--tty",
                            "--privileged", "--read-only"})


# ---------------------------------------------------------------- static checks (recipe + layout)
_ARG_DEFAULT = re.compile(r"^\s*ARG\s+([A-Z0-9_]+)\s*=\s*(\S*)", re.M)
_SECRETISH_ENV = re.compile(r"^\s*(ENV|ARG)\s+\S*(?i:key|token|secret|passw|credential|cookie)\S*", re.M)


def recipe_problems(text: str) -> list[str]:
    """Static check of the lab image recipe (docker/lab.Dockerfile). Empty = clean. Comment lines are ignored."""
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    problems = []
    for m in re.finditer(r"^\s*(COPY|ADD)\b.*$", text, re.M):
        problems.append(f"the lab recipe copies files into the image ({m.group(0).strip()!r}): a lab image is built "
                        "from public packages only, never from host files (truth or secrets could ride along)")
    if re.search(r"^\s*VOLUME\b", text, re.M):
        problems.append("the lab recipe declares a VOLUME: the only volume is the session workspace, set at run time")
    for m in _SECRETISH_ENV.finditer(text):
        problems.append(f"secret-like ENV/ARG in the lab recipe: {m.group(0).strip()!r}")
    for m in _ARG_DEFAULT.finditer(text):
        if m.group(1).endswith("_VERSION") and m.group(2) in ("", "latest", "*"):
            problems.append(f"agent CLI version {m.group(1)} is not pinned ({m.group(2)!r})")
    if re.search(r"curl[^\n|]*\|\s*(ba)?sh", text):
        problems.append("the lab recipe pipes a download into a shell (unpinned installer)")
    if not re.search(r"^\s*USER\s+(?!root\b|0\b)\S+", text, re.M):
        problems.append("the lab recipe does not end as a non-root USER")
    return problems


def static_problems(recipe: Path | None = None) -> list[str]:
    """Every static Lab wall problem (empty = clean): the lab root, the recipe, no truth location overlapping the lab
    root, and s4-integrity's truth-layout check run with the lab root added to the agent-readable roots."""
    problems = lab_root_problems()
    path = recipe if recipe is not None else _repo_root() / "docker" / "lab.Dockerfile"
    try:
        problems += recipe_problems(path.read_text(encoding="utf-8"))
    except OSError as e:
        problems.append(f"lab recipe {path} unreadable: {e}")
    from .. import truth_layout as tl
    from ..commitment import default_agent_readable_roots
    for loc in truth_locations():
        if overlaps(loc, lab_root()):
            problems.append(f"truth location {loc} overlaps the lab root {lab_root()}")
    roots = [*default_agent_readable_roots(), lab_root()]
    problems += [str(x) for x in tl.truth_layout_problems(roots)]
    return problems
