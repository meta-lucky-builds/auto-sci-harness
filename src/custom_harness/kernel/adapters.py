"""Domain adapters: the human-approved, fixed verifier layer (GOAL §4.7, §5 "Domain adapters", invariant 13).

An adapter is a named, versioned set of source modules that supply a domain's verifiers (metrics, statistics, leak
fingerprints, the program-execution contract, later Lean / solvers / simulators) for some evidence classes. Its
identity is a content hash over those modules' source bytes AND over every project module they import, transitively
(`covered_modules`; stdlib and installed packages excluded), so an edit to a helper the adapter imports also needs a
new approval. It is USABLE only while the ledger holds a human
approval for exactly that hash, recorded after the adapter's own fault-injection validation passed; changing any of
its files (a new version) needs a new approval. The registry below is code: adding an adapter is a code change AND
an approval. Adapters are not an edit surface of the harness evolver (they are not part of any HarnessSpec).

Trust boundary (stated, not hidden): `approve` is called by the human-operated CLI (`chh approve-adapter`). Trusted
host code could call it too; the ledger entry names the approver and is hash-chained, which is accountability, not
enforcement. No model output and no harness edit can reach it.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import hashlib
import importlib
import importlib.util
import json
import site
import sys
import sysconfig
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..adapters.circuit import validation as circuit_validation
from ..verify import lean_validation
from ..verify import metrics as M
from ..verify.lp_validation import LP_CHECKS
from ..verify.ml_validation import ML_CHECKS
from . import sim
from .claim import EvidenceClass
from .ledger import Entry, Ledger, inside


@dataclass(frozen=True)
class AdapterSpec:
    name: str
    version: str
    classes: frozenset[EvidenceClass]
    modules: tuple[str, ...]  # importable modules whose source bytes define the adapter
    metrics: frozenset[str]
    # computational class: the module (one of `modules`) exporting the exact certificate checker the Kernel calls:
    # check(problem, certificate, value) -> reasons (K3), bounds(...) -> reasons (K8), HIGHER_IS_BETTER
    exact_module: str | None = None
    # data class: the module of the domain procedures the Kernel core calls in K1 and K3-K8 (kernel/ml.py documents
    # the interface: metric_reasons, recompute, replay_failure, is_regression, statistics, conformance, risk,
    # record_bounds). It must be one of `modules`, so it is part of the adapter's identity.
    procedures: str | None = None


@functools.cache
def _external_roots() -> tuple[Path, ...]:
    """Directories whose modules are NOT project code: the standard library and installed packages."""
    paths = sysconfig.get_paths()
    roots = {paths[k] for k in ("stdlib", "platstdlib", "purelib", "platlib") if k in paths}
    roots |= set(site.getsitepackages()) | {site.getusersitepackages()}
    return tuple(Path(r).resolve() for r in sorted(roots))


def _source_file(name: str) -> Path | None:
    """The source file of module `name` if it is PROJECT code (not stdlib, not site-packages, not built in)."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):  # unresolvable (e.g. a parent that is a plain module): nothing to hash
        return None
    if spec is None or not spec.has_location or spec.origin is None:
        return None  # built-in / frozen / namespace package: no source of ours
    path = Path(spec.origin).resolve()
    return None if _is_external(path) else path


@functools.lru_cache(maxsize=4096)
def _is_external(path: Path) -> bool:
    r"""By identity, not spelling (\?\, 8.3, case), cached per process (the identity walk is slow on synced drives);
    an OSError counts as project code, which is hashed (fail closed)."""
    return any(inside(path, r, on_error=False) for r in _external_roots())


_IMPORTS: dict[str, frozenset[str]] = {}  # sha256 of a module's source -> absolute names it imports (parse cache)


def _imports(name: str, path: Path, src: bytes) -> frozenset[str]:
    """Absolute names of every module `name` imports explicitly (anywhere in the file, incl. lazy imports); for
    `from X import y`, X.y when that is a module, else X. Relative imports are resolved against the package."""
    key = f"{name}|{hashlib.sha256(src).hexdigest()}"
    if key not in _IMPORTS:
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        found: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                found |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                base = importlib.util.resolve_name("." * node.level + (node.module or ""), package) \
                    if node.level else str(node.module)
                found.add(base)
                found |= {f"{base}.{a.name}" for a in node.names if a.name != "*"}  # kept only if a module
        _IMPORTS[key] = frozenset(found)
    return _IMPORTS[key]


def covered_modules(spec: AdapterSpec) -> dict[str, Path]:
    """The adapter's listed modules plus the transitive closure of the PROJECT modules they import (R1-F5): an
    adapter's behaviour depends on them, so they are part of its identity. Stdlib and installed packages are pinned
    by the environment, not by the approval (limit: docs/kernel.md)."""
    out: dict[str, Path] = {}
    todo = list(spec.modules)
    while todo:
        name = todo.pop()
        if name in out:
            continue
        path = _source_file(name)
        if path is None:
            if name in spec.modules:  # a listed module must resolve to project source (fail closed)
                raise AdapterError(f"adapter module {name!r} has no project source file")
            continue
        out[name] = path
        todo += sorted(_imports(name, path, path.read_bytes()))
    return dict(sorted(out.items()))


Stamps = tuple[tuple[Path, int, int, int], ...]
_CLOSURES: dict[tuple[AdapterSpec, tuple[str, ...]], tuple[Stamps, dict[str, Path]]] = {}


def _stamps(paths: Iterable[Path]) -> Stamps:
    return tuple((p, (s := p.stat()).st_mtime_ns, s.st_size, s.st_ino) for p in paths)


def adapter_hash(spec: AdapterSpec) -> str:
    """The import closure (the slow part) is cached per process by (spec, sys.path) and walked again when a covered
    file's (mtime_ns, size, inode) changed or an edit raced the walk; the file BYTES are hashed on every call (RT-S4-8:
    an edit that restores size and mtime is still seen). Residual and measurement: docs/kernel_s4.md, item 9."""
    key = (spec, tuple(sys.path))
    hit = _CLOSURES.get(key)
    with contextlib.suppress(OSError):  # a covered file gone: walk again (fails closed there)
        if hit is not None and _stamps(p for p, *_ in hit[0]) == hit[0]:
            return _adapter_hash(spec, hit[1])
    covered = covered_modules(spec)
    stamps = _stamps(covered.values())
    if covered_modules(spec) == covered and _stamps(covered.values()) == stamps:
        _CLOSURES[key] = (stamps, covered)
    return _adapter_hash(spec, covered)


def _adapter_hash(spec: AdapterSpec, covered: dict[str, Path]) -> str:
    parts: dict[str, Any] = {"name": spec.name, "version": spec.version, "classes": sorted(spec.classes),
                             "metrics": sorted(spec.metrics), "modules": {}}
    if spec.exact_module is not None:  # only when set: adapters without one keep their approved hashes
        parts["exact_module"] = spec.exact_module
    if spec.procedures is not None:
        parts["procedures"] = spec.procedures
    for mod, path in covered.items():
        parts["modules"][mod] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


ML = AdapterSpec(
    name="ml", version="1", classes=frozenset({EvidenceClass.DATA}),
    modules=("custom_harness.verify.metrics", "custom_harness.verify.stats", "custom_harness.verify.leak",
             "custom_harness.kernel.execution", "custom_harness.verify.transductive", "custom_harness.kernel.ml",
             "custom_harness.verify.ml_validation"),  # s4: its fixtures (moved from this file) are now hashed
    metrics=frozenset(M.METRICS), procedures="custom_harness.kernel.ml")


LP = AdapterSpec(
    name="lp", version="1", classes=frozenset({EvidenceClass.COMPUTATIONAL}),
    modules=("custom_harness.kernel.lp", "custom_harness.verify.lp_validation"),
    metrics=frozenset({"lp_optimum"}), exact_module="custom_harness.kernel.lp")

# simulated class: exact_module names the pinned simulator module (SIMULATOR, VERSION, scope_phrase, check, bounds)
SIM = AdapterSpec(
    name="sim", version="1", classes=frozenset({EvidenceClass.SIMULATED}), modules=("custom_harness.kernel.sim",),
    metrics=frozenset({"sim_value"}), exact_module="custom_harness.kernel.sim")

# Lean 4 (formal class, T0): the record checker (Kernel side), its fault-injection fixtures, and the sandbox runner
# that produces the records are ONE adapter: changing any of them changes the hash and needs a new human approval.
LEAN = AdapterSpec(
    name="lean", version="1", classes=frozenset({EvidenceClass.FORMAL}),
    modules=("custom_harness.kernel.formal", "custom_harness.verify.lean_validation", "custom_harness.sandbox.lean"),
    metrics=frozenset({"lean_theorem"}), exact_module="custom_harness.kernel.formal")

# Linear DC circuits (computational class, T1; M5 engineering, docs/adapters_circuit.md): exact MNA certificates,
# the checker, its validation evidence and the solver that builds its golden witnesses are ONE adapter.
CIRCUIT = AdapterSpec(
    name="circuit", version="1", classes=frozenset({EvidenceClass.COMPUTATIONAL}),
    modules=("custom_harness.adapters", "custom_harness.adapters.circuit", "custom_harness.adapters.circuit.check",
             "custom_harness.adapters.circuit.validation", "custom_harness.adapters.circuit.solver"),
    metrics=frozenset({"circuit_value"}), exact_module="custom_harness.adapters.circuit.check")

REGISTRY: dict[str, tuple[AdapterSpec, tuple[tuple[str, Callable[[], bool]], ...]]] = {
    "ml": (ML, ML_CHECKS), "lp": (LP, LP_CHECKS), "sim": (SIM, sim.SIM_CHECKS),
    "lean": (LEAN, lean_validation.LEAN_CHECKS), "circuit": (CIRCUIT, circuit_validation.CIRCUIT_CHECKS)}


def procedures_for(evidence_class: EvidenceClass, metric: str) -> str:
    """The procedures module of the registered adapter serving (class, metric); if none serves it, the first adapter
    (by name) that has procedures: K1 rejects such a claim anyway (no approved adapter), and its other checks then run
    exactly as they did before the core/adapter split (docs/kernel_core_adapter_split.md)."""
    have = [s for _, (s, _) in sorted(REGISTRY.items()) if s.procedures is not None]
    serving = [s for s in have if evidence_class in s.classes and metric in s.metrics]
    return str((serving or have)[0].procedures)


def validate(checks: tuple[tuple[str, Callable[[], bool]], ...]) -> list[str]:
    """Names of failed checks (a crashing check fails)."""
    failed = []
    for name, check in checks:
        try:
            ok = check()
        except Exception:  # noqa: BLE001  a crashing validation check is a failed one
            ok = False
        if not ok:
            failed.append(name)
    return failed


class AdapterError(ValueError):
    pass


def approve(ledger: Ledger, name: str, approver: str) -> Entry:
    """Record a human approval of the adapter's CURRENT content, after its validation passes."""
    if name not in REGISTRY:
        raise AdapterError(f"unknown adapter {name!r}")
    if not approver.strip():
        raise AdapterError("an approval must name the human approver")
    spec, checks = REGISTRY[name]
    failed = validate(checks)
    if failed:
        raise AdapterError(f"adapter {name!r} failed its validation: {failed}")
    return ledger.append("adapter_approval", {
        "name": spec.name, "version": spec.version, "hash": adapter_hash(spec), "classes": sorted(spec.classes),
        "approver": approver, "validation": {"checks": len(checks), "failed": []}})


def resolve(ledger: Ledger, evidence_class: EvidenceClass, metric: str) -> tuple[dict[str, Any] | None, list[str]]:
    """The approved adapter that serves (class, metric) with its CURRENT content, or None and the reasons."""
    candidates = [(n, s) for n, (s, _) in sorted(REGISTRY.items())
                  if evidence_class in s.classes and metric in s.metrics]
    if not candidates:
        return None, [f"no registered adapter serves evidence class {evidence_class.value!r} with metric {metric!r}"]
    reasons = []
    approvals = ledger.entries("adapter_approval")
    for name, spec in candidates:
        h = adapter_hash(spec)
        ok = [e for e in approvals if e.body.get("name") == name and e.body.get("hash") == h]
        if ok:
            return {"name": name, "version": spec.version, "hash": h, "approval_seq": ok[-1].seq,
                    "approver": ok[-1].body.get("approver")}, []
        reasons.append(f"adapter {name!r} (hash {h[:12]}) has no human approval for its current content "
                       f"(changed since approval, or never approved): run `chh approve-adapter {name}`")
    return None, reasons
