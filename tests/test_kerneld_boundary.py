"""kerneld boundary ratchet (GOAL §16.5.2): no host code reads, logs or ingests private vault data, or opens the store.

Host code = every module under src/custom_harness except the kernel package and the PRIVILEGED kerneld modules
(server.py: the kerneld process; ingest.py: private ingest, run by that process; hoststore.py: the host store facade
and its grants; scope.py: the Kernel's per-claim grants). Since 2026-10-14 (s4-kerneld) every old-path list is EMPTY:

- private vault reads: `.read(..., purpose=...)` or `.read(**kw)` (purpose hidden);
- EvidenceStore constructions (the CLI opens the store through kerneld.hoststore.open_host_store);
- private ingest: `register_dataset` with a non-empty `private=` (or `**kw`);
- vault canary reads: `.canary(...)`, and blob reads of a `canary_ref` (`blobs.get(x.canary_ref)`);
- `getattr(x, "read" | "canary" | "register_dataset")` (a call the checks above cannot see);
- a bare `Kernel(...)` construction or a class deriving from `Kernel` (host code holds a `ScopedKernel`, whose
  private reads happen only inside kerneld's per-claim grants), and direct `build_bundle(...)` calls (bundles go
  through `ScopedKernel.bundle`);
- use of the grant machinery or the facade's private attributes (`_grant`, `_GRANT`, `_HostStore__*`,
  `_HostBlobs__*`, `_PrivateIndex__*`, any import of a `_`-name from kerneld.hoststore / kerneld.scope);
- `BlobStore(...)` constructions or unbound `BlobStore.get(...)` / `EvidenceStore.__init__` calls outside store/.

The one exempt site keeps its stated reason (SCRATCH_STORE_SITES). Limits (stated): AST only, by name. Reading a blob
file with `open()` / `Path.read_bytes()` is not detected statically; the HostStore refuses private blobs dynamically
(tests/test_kerneld_hoststore.py), and the boundary is a code boundary, not an OS one (docs/kerneld_boundary.md).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness"

PRIVILEGED = {"kerneld/server.py", "kerneld/ingest.py", "kerneld/hoststore.py", "kerneld/scope.py",
              "kerneld/truth.py"}
# every old-path list is empty since 2026-10-14 (s4-kerneld): a non-empty one is a regression
OLD_PATH_PRIVATE_READS: dict[str, int] = {}
OLD_PATH_STORE_OPENS: dict[str, int] = {}
OLD_PATH_PRIVATE_INGEST: dict[str, int] = {}
OLD_PATH_CANARY_READS: dict[str, int] = {}
COVERED = ["evolve", "runtime", "kerneld/client.py", "kerneld/releases.py", "kerneld/campaign.py", "research",
           "audit", "cli.py", "memory", "studio", "scorecard", "experience"]
# NOT old-path sites, exempt with a stated reason (2026-10-08): research/preflight.py (GOAL §16.8 zero-call preflight)
# builds a SCRATCH store in a temporary directory holding only SIMULATED labels it generated itself, registers them,
# and makes one logged read of them so the real K4 sees logged = reported evaluations. It never opens the real store
# and never reads real held-out data; routing simulated labels through kerneld would add a process and protect nothing.
SCRATCH_STORE_SITES = {"research/preflight.py": 1}
# Store opens only (int17 merge, 2026-10-15; owner to confirm): assure/corpus.py (s3-assure, GOAL §16.6 honest corpus)
# opens a SCRATCH COPY of a committed corpus item's store, unpacked into a temporary directory, to re-derive the
# Kernel's verdict on it (through a ScopedKernel); it never opens the real store and makes no private read or ingest
# of its own. Not in SCRATCH_STORE_SITES, because that list is also the expected private-read and ingest sites.
SCRATCH_STORE_OPEN_ONLY = {"assure/corpus.py": 1, "court/health.py": 1, "court/rehearsal.py": 1}
# Each site above needs its reason here (test_every_scratch_store_open_states_why), and a court site needs its test in
# tests/test_court_rehearsal_health.py that fails if it is pointed at the real store.
SCRATCH_STORE_REASONS = {
    "assure/corpus.py": "a scratch copy of a committed corpus item's store, unpacked into a temporary directory",
    "court/health.py": "run_plant opens one throwaway store per plant; its one caller, run_health, makes the root with "
                       "mkdtemp; nothing from it enters the real ledger, only a court_health record",
    "court/rehearsal.py": "rehearse opens one throwaway store under its own mkdtemp, deleted afterwards; it runs the "
                          "route on Lab data; the real store gets only a court_rehearsal record",
}

_PRIVATE_ATTRS = ("_HostStore__", "_HostBlobs__", "_PrivateIndex__")
_PRIVATE_NAMES = {"_grant", "_GRANT", "_Grant"}


def _rel(p: Path) -> str:
    return p.relative_to(SRC).as_posix()


def _files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if p.relative_to(SRC).parts[0] != "kernel" and _rel(p) not in PRIVILEGED]


def _sites(pred, files: list[Path] | None = None) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in _files() if files is None else files:
        n = sum(1 for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))) if pred(node))
        if n:
            out[_rel(p)] = n
    return out


def _call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _private_read(node: ast.AST) -> bool:
    """`x.read(..., purpose=...)` (Vault.read's purpose is keyword-only), or `x.read(**kw)` (purpose hidden)."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read"
            and any(k.arg in ("purpose", None) for k in node.keywords))


def _private_ingest(node: ast.AST) -> bool:
    """`x.register_dataset(..., private=<anything but an empty dict literal>)`, or with `**kw` (private hidden)."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register_dataset"):
        return False
    if any(k.arg is None for k in node.keywords):
        return True
    priv = [k.value for k in node.keywords if k.arg == "private"]
    if len(node.args) > 3:  # register_dataset(dataset_id, role, public, private, ...) positionally
        priv.append(node.args[3])
    if len(node.args) > 4 or any(k.arg == "canary" for k in node.keywords):  # a canary marks private files
        return True
    return any(not (isinstance(v, ast.Dict) and not v.keys) for v in priv)


def _canary_read(node: ast.AST) -> bool:
    """`x.canary(...)`, or a blob read whose argument is `<expr>.canary_ref`."""
    if _call_name(node) == "canary":
        return True
    return (_call_name(node) in ("get", "get_json") and isinstance(node, ast.Call)
            and any(isinstance(a, ast.Attribute) and a.attr == "canary_ref" for a in node.args))


def _dynamic_access(node: ast.AST) -> bool:
    """`getattr(x, "read" | "canary" | "register_dataset" | <a facade private name>)`."""
    return (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr" and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
            and (node.args[1].value in ("read", "canary", "register_dataset")
                 or node.args[1].value.startswith(_PRIVATE_ATTRS) or node.args[1].value in _PRIVATE_NAMES))


def _store_open(node: ast.AST) -> bool:
    return _call_name(node) == "EvidenceStore"


def _bare_kernel(node: ast.AST) -> bool:
    """`Kernel(...)` (any spelling ending in the name Kernel), or `class X(Kernel)`."""
    if _call_name(node) == "Kernel":
        return True
    return isinstance(node, ast.ClassDef) and any(
        (isinstance(b, ast.Name) and b.id == "Kernel") or (isinstance(b, ast.Attribute) and b.attr == "Kernel")
        for b in node.bases)


def _direct_bundle(node: ast.AST) -> bool:
    return _call_name(node) == "build_bundle"


def _grant_machinery(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and (node.attr.startswith(_PRIVATE_ATTRS) or node.attr in _PRIVATE_NAMES):
        return True
    if isinstance(node, ast.Name) and node.id in _PRIVATE_NAMES:
        return True
    return (isinstance(node, ast.ImportFrom) and (node.module or "").endswith(("hoststore", "scope"))
            and any(a.name.startswith("_") for a in node.names))


def _raw_blob_access(node: ast.AST) -> bool:
    """`BlobStore(...)`, `BlobStore.get(...)` / `BlobStore.get_json(...)` unbound, `EvidenceStore.<method>(...)`
    unbound (a HostStore passed as `self` to the unrestricted method)."""
    if _call_name(node) == "BlobStore":
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id in ("BlobStore", "EvidenceStore"))


_GUARDED_CLASSES = ("Kernel", "BlobStore", "EvidenceStore", "HostStore", "HostBlobs")
_INTROSPECTION_ATTRS = ("__self__", "__mro__", "__bases__", "__dict__", "__globals__", "__closure__")
# callers allowed to receive canary tokens for CONTENT SCREENS (kerneld/scope.screening_canaries; reviewer finding 6)
SCREENING_CALLERS = {"memory/screen.py": 1, "research/evidence_refs.py": 1}


def _alias_or_introspection(node: ast.AST) -> bool:
    """Bypasses of the name-based detectors (s4-kerneld-review finding 8): an aliased import of a guarded class
    (`from ..kernel import Kernel as K`), a two-argument `super(<guarded class>, x)` (skips HostBlobs.get), `vars(...)`,
    introspection attributes that reach a bound object or a class's bases, and `<x>.blobs._path(...)` (the raw blob
    file)."""
    if isinstance(node, ast.ImportFrom) and any(a.name in _GUARDED_CLASSES and a.asname for a in node.names):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "vars":
            return True
        if node.func.id == "super" and len(node.args) == 2:
            return True
    if isinstance(node, ast.Attribute) and node.attr in _INTROSPECTION_ATTRS:
        return True
    return (isinstance(node, ast.Attribute) and node.attr == "_path" and isinstance(node.value, ast.Attribute)
            and node.value.attr == "blobs")


def _screening_call(node: ast.AST) -> bool:
    return _call_name(node) == "screening_canaries"


# Host reads of TASK TRUTH from disk (truth_layout.truth_file / truth_files; s4-kerneld-review finding 3). Moved
# behind kerneld (kerneld/truth.py, op truth_query) on 2026-10-15: evolve/export.py (leak fingerprints),
# evolve/multiplicity.held_out_target_id, research/rebuild.py. NO truth read may appear under these paths:
TRUTH_READ_FREE = ["evolve", "research/rebuild.py"]
# Remaining host truth reads, each with its reason (old path, listed; shrink as they move):
OLD_PATH_TRUTH_READS = {
    "research/suite.py": 2,  # the suite BUILDER re-derives a minted task from its own mint record (verify_mint)
    "research/task.py": 1,  # load_task: an existence check of the label file, no bytes read
    "research/truth_copies.py": 1,  # read_truth_copies: called by kerneld's ingest (and by tests) only
    "salts/generate.py": 1,  # the salt generator hashes the truth it just wrote (builder)
    "store/run_bundle.py": 1,  # task_file_hashes: a run bundle records truth HASHES of the task it ran
    "kerneld/campaign.py": 1,  # fake_labels: the campaign-replay tool writes FAKE truth for a copy of a task
}


def _truth_read(node: ast.AST) -> bool:
    return _call_name(node) in ("truth_file", "truth_files")


def test_private_vault_reads_outside_kerneld_are_only_the_listed_old_path_sites():
    assert _sites(_private_read) == OLD_PATH_PRIVATE_READS | SCRATCH_STORE_SITES


def test_store_opens_outside_the_kernel_are_only_the_listed_old_path_sites():
    assert _sites(_store_open) == OLD_PATH_STORE_OPENS | SCRATCH_STORE_SITES | SCRATCH_STORE_OPEN_ONLY


def test_every_scratch_store_open_states_why():
    assert set(SCRATCH_STORE_REASONS) == set(SCRATCH_STORE_OPEN_ONLY)
    assert all(len(why) > 20 for why in SCRATCH_STORE_REASONS.values())


def test_private_ingest_outside_the_kernel_is_only_the_listed_old_path_sites():
    assert _sites(_private_ingest) == OLD_PATH_PRIVATE_INGEST | SCRATCH_STORE_SITES


def test_canary_reads_outside_kerneld_are_only_the_listed_old_path_sites():
    assert _sites(_canary_read) == OLD_PATH_CANARY_READS


def test_no_dynamic_access_to_private_vault_methods():
    assert _sites(_dynamic_access) == {}


def test_host_code_holds_only_a_scoped_kernel_and_bundles_through_it():
    assert _sites(_bare_kernel) == {}
    assert _sites(_direct_bundle) == {}


def test_host_code_never_touches_the_grant_machinery_or_facade_internals():
    assert _sites(_grant_machinery) == {}


def test_host_code_never_reaches_the_blob_store_around_the_facade():
    others = [p for p in _files() if p.relative_to(SRC).parts[0] != "store"]
    # literature/snapshot.py: the literature snapshot store's OWN blob directory (papers, never vault data)
    assert _sites(_raw_blob_access, others) == {"literature/snapshot.py": 1}


def test_no_aliased_guarded_class_and_no_introspection_bypass():
    assert _sites(_alias_or_introspection) == {}


def test_host_truth_reads_are_only_the_listed_sites_and_none_in_the_moved_paths():
    sites = _sites(_truth_read)
    assert not [f for f in sites if any(f == c or f.startswith(c + "/") for c in TRUTH_READ_FREE)], sites
    assert sites == OLD_PATH_TRUTH_READS


def test_canary_tokens_reach_only_the_listed_content_screens():
    assert _sites(_screening_call) == SCREENING_CALLERS


def test_covered_host_modules_have_no_private_read_or_store_open():
    sites = {**_sites(_private_read), **_sites(_store_open)}
    bad = [f for f in sites if f not in SCRATCH_STORE_SITES | SCRATCH_STORE_OPEN_ONLY
           and any(f == c or f.startswith(c + "/") for c in COVERED)]
    assert not bad, bad


def test_privileged_modules_exist_and_the_ratchet_is_not_vacuous():
    """The exemptions name real files, and each detector fires on the privileged code it exempts (so a detector that
    silently stopped matching would fail here, not pass everywhere)."""
    for rel in PRIVILEGED:
        assert (SRC / rel).is_file(), rel
    priv = [SRC / rel for rel in sorted(PRIVILEGED)]
    assert _sites(_private_ingest, priv).get("kerneld/ingest.py")
    assert _sites(_store_open, priv).get("kerneld/hoststore.py") == 1
    assert _sites(_canary_read, priv).get("kerneld/ingest.py")
    assert _sites(_bare_kernel, priv).get("kerneld/scope.py")
    assert _sites(_direct_bundle, priv).get("kerneld/scope.py") == 1
    assert _sites(_grant_machinery, priv).get("kerneld/scope.py")
    assert _sites(_truth_read, priv).get("kerneld/truth.py") and _sites(_truth_read, priv).get("kerneld/ingest.py")


def test_detectors_catch_planted_bypasses(tmp_path):
    """Mutation check, in-test: one planted line per bypass is caught by its detector (2026-10-14)."""
    planted = {
        _private_read: "v.read('d', 'f', purpose='evaluate')",
        _store_open: "EvidenceStore(root)",
        _private_ingest: "v.register_dataset('d', role, {}, {'y': b'1'})",
        _canary_read: "store.blobs.get(ds.canary_ref)",
        _dynamic_access: "getattr(v, 'canary')",
        _bare_kernel: "class K(Kernel): pass",
        _direct_bundle: "build_bundle(k, h, out)",
        _grant_machinery: "from custom_harness.kerneld.hoststore import _grant",
        _raw_blob_access: "BlobStore.get(store.blobs, digest)",
        _alias_or_introspection: "data = super(HostBlobs, store.blobs).get(digest)",
        _truth_read: "labels = truth_file(task_dir, 'test_y.csv').read_bytes()",
    }
    more = ["from custom_harness.kernel import Kernel as K", "inner = vars(hs)", "f = hs.close.__self__",
            "p = store.blobs._path(d)"]
    for line in more:
        assert sum(1 for n in ast.walk(ast.parse(line)) if _alias_or_introspection(n)) == 1, line
    for pred, line in planted.items():
        p = tmp_path / f"{pred.__name__}.py"
        p.write_text(line + "\n", encoding="utf-8")
        assert sum(1 for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))) if pred(n)) == 1, (pred, line)
