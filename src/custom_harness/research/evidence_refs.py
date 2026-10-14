"""What an agent may cite or recall from the run's evidence store: one set of deterministic, fail-closed rules shared
by the critic-answer check (research/critic_answers.py, GOAL L4.8) and paged recall (research/recall.py, GOAL L4
"Context management"). Design note: docs/critic_answers_and_recall.md.

Sources of truth are the ENGINE's records, never run state (state is writable by evolved nodes):
  - the run's trace (`store.trace(run_id)`): which steps ran, what they wrote, which prompt each LLM step saw
    (`output.messages`) and which values were archived as handles in that prompt (`output.archived`);
  - the Kernel vault (private-file blobs, canaries) and the ledger (`data_access` rows of private reads);
  - the engine's per-run archive provenance (`ModuleContext.archive_taint`: handle -> held-out targets of the prompt
    that archived it). A handle missing from it has UNKNOWN provenance and is refused (e.g. after a resume).

Nothing here calls a model. Every function answers with a closed reason code (Code) or a resolved object.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..records import StepKind, TraceStep
from ..verify.leak import CANARY_PREFIX, secret_patterns

# Closed set of reason codes (a refusal never carries a value back to the agent).
MALFORMED = "malformed"  # not the typed shape (invariant 7)
UNCITED = "uncited"  # an answer with no evidence id
NOT_VISIBLE = "not_visible"  # the id was never rendered to the answering/requesting agent in this run
UNRESOLVABLE = "unresolvable"  # no such step / blob / evidence in this run, or ambiguous
NOT_EVIDENCE = "not_evidence"  # resolves, but is model output (LLM step, module that called a model) or a failed step
HIDDEN = "hidden"  # touches private state: `_private*` output, redacted values, a private read during the step
PRIVATE = "private"  # a private vault blob, a canary, a run `_private*` value, or content carrying one
TARGET_DERIVED = "target_derived"  # derived from held-out targets (invariant 18): refused, never released here
PROVENANCE_UNKNOWN = "provenance_unknown"  # the engine has no provenance record for this handle
CODES = (MALFORMED, UNCITED, NOT_VISIBLE, UNRESOLVABLE, NOT_EVIDENCE, HIDDEN, PRIVATE, TARGET_DERIVED,
         PROVENANCE_UNKNOWN)

# Citation grammar. A blob handle is what the prompt marker shows (16 hex characters) or more of the digest; hex
# only, so a handle can never be a path. `ev:` names a Kernel Evidence row.
STEP_RE = re.compile(r"^step:(\d{1,6})$")
BLOB_RE = re.compile(r"^blob:([0-9a-f]{16,64})$")
HANDLE_RE = re.compile(r"^[0-9a-f]{16,64}$")
EV_RE = re.compile(r"^ev:([A-Za-z0-9_\-]{1,64})$")


@dataclass(frozen=True)
class Vetted:
    ok: bool
    code: str | None = None  # one of CODES when not ok
    digest: str | None = None  # resolved full blob digest (blob refs)


def step_module(step: TraceStep) -> str | None:
    node = (step.input or {}).get("node") or {}
    return str(node.get("module")) if node.get("kind") == "module" else None


def step_params(step: TraceStep) -> dict[str, Any]:
    params = ((step.input or {}).get("node") or {}).get("params")
    return params if isinstance(params, dict) else {}


def rendered_prompt(step: TraceStep) -> str:
    """The prompt the model of an LLM step was SHOWN: its system message and first user message only. Later messages
    are retry turns, which quote the model's own output (an id it invented) or a validator message, never shown data.
    """
    out: list[str] = []
    for m in step.output.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if not isinstance(content, str):
            continue
        if role == "system":
            out.append(content)
        elif role == "user":
            out.append(content)
            break
        else:
            break
    return "\n".join(out)


def private_read_windows(store: Any) -> list[float]:
    """Ledger times of every PRIVATE vault read (any run: conservative, a concurrent read elsewhere over-hides)."""
    times = []
    for _seq, _kind, body, _prev, _h, at in store.ledger_rows("data_access"):
        try:
            if json.loads(body).get("private"):
                times.append(float(at))
        except (ValueError, TypeError, AttributeError):
            times.append(float(at))  # unreadable row: count it as private (fail closed)
    return times


def run_private_reads(store: Any, trace: Sequence[TraceStep], run_id: str) -> list[float]:
    """Ledger times of PRIVATE vault reads that may have fed THIS run: reads naming this run, or naming no run (Kernel
    re-check reads carry none), logged at or after the run's first step. Engine taint does not follow a value that a
    module copies into a fresh key, and is empty after a resume (red-team findings), so anything produced after such a
    read is treated as possibly target-derived (conservative: over-refuses, never under-refuses)."""
    if not trace:
        return []
    start = min(s.started_at for s in trace) - 1e-6
    out = []
    for _seq, _kind, body, _prev, _h, at in store.ledger_rows("data_access"):
        try:
            b = json.loads(body)
            private, rid = bool(b.get("private")), b.get("run_id")
        except (ValueError, TypeError, AttributeError):
            private, rid = True, None  # unreadable row: count it (fail closed)
        if private and rid in (run_id, None) and float(at) >= start:
            out.append(float(at))
    return out


# Modules whose outputs only RELAY model-written text or other steps' outputs (critic questions, the catalog, answer
# checks, recalled pages, memory entries, queued hypotheses / unpacked scripts): resolvable, but not evidence. A deny
# list (stated limit): a new relay module must be added here.
RELAY_MODULES = frozenset({"research.socratic_filter", "research.critic_evidence", "research.check_critic_answers",
                           "research.recall", "research.recall_memory", "research.next_hypothesis",
                           "research.unpack_solutions", "research.tournament"})


def _has_private_keys(obj: Any) -> bool:
    if isinstance(obj, dict):
        return any(str(k).startswith("_private") or _has_private_keys(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_has_private_keys(v) for v in obj)
    return False


def vet_step(step: TraceStep | None, *, before_index: int, private_reads: Sequence[float],
             run_reads: Sequence[float] = ()) -> Vetted:
    """A trace step is citable evidence only if it is a successful MODULE step (code, not model output) that ran
    before `before_index`, is not a relay module, made no model call, wrote no `_private*` key, had no private value
    redacted from its output, no private vault read (any run) was logged while it ran, and no private read of THIS run
    (`run_reads`, see run_private_reads) was logged before it ended."""
    if step is None or step.index >= before_index:
        return Vetted(False, UNRESOLVABLE)
    if (step.kind != StepKind.MODULE or step.error is not None or step.output.get("_module_llm_calls")
            or step_module(step) in RELAY_MODULES):
        return Vetted(False, NOT_EVIDENCE)
    if _has_private_keys(step.output) or "_redacted_private_values" in step.output:
        return Vetted(False, HIDDEN)
    if any(step.started_at - 1e-6 <= t <= step.ended_at + 1e-6 for t in private_reads):
        return Vetted(False, HIDDEN)
    if any(t <= step.ended_at + 1e-6 for t in run_reads):
        return Vetted(False, HIDDEN)
    return Vetted(True)


@dataclass(frozen=True)
class PrivateScreen:
    """What may never be cited or recalled: vault private-file blobs and canary blobs (by digest), canary tokens and
    the run's `_private*` values with their encodings (by content), and a digest that IS a `_private*` value (e.g.
    the dev-labels blob ref)."""

    private_digests: frozenset[str]
    patterns: tuple[str, ...]

    @classmethod
    def build(cls, vault: Any, secrets: Iterable[str]) -> PrivateScreen:
        from ..kerneld.scope import screening_canaries  # canary blobs are private: read under a screen-only grant
        digests: set[str] = set()
        pats: set[str] = {CANARY_PREFIX}
        canaries = screening_canaries(vault)
        for ds in vault.datasets().values():
            digests |= {ref.blob for ref in ds.files.values() if ref.private}
            if ds.canary_ref:
                digests.add(ds.canary_ref)
                canary = canaries.get(ds.dataset_id, "")
                pats |= set(secret_patterns(canary, min_verbatim=1)) if canary else set()
        secrets = [s for s in secrets if s]
        digests |= {s for s in secrets if HANDLE_RE.fullmatch(s) and len(s) == 64}
        pats |= {s for s in secrets if len(s) >= 6}
        return cls(frozenset(digests), tuple(sorted(pats, key=lambda p: (-len(p), p))))

    def digest_private(self, digest: str) -> bool:
        return digest in self.private_digests

    def content_private(self, text: str) -> bool:
        low = text.lower()
        return any(p in text or p.lower() in low for p in self.patterns)


def resolve_handle(handle: str, visible: Iterable[str]) -> Vetted:
    """A handle (>= 16 lowercase hex) resolves only against digests VISIBLE to the agent, and only if exactly one
    matches. Nothing is looked up by the agent's string itself, so a forged or traversal-shaped handle cannot reach
    the blob store."""
    if not isinstance(handle, str) or not HANDLE_RE.fullmatch(handle):
        return Vetted(False, MALFORMED)
    hits = sorted({d for d in visible if isinstance(d, str) and len(d) == 64 and d.startswith(handle)})
    if not hits:
        return Vetted(False, NOT_VISIBLE)
    if len(hits) > 1:
        return Vetted(False, UNRESOLVABLE)
    return Vetted(True, digest=hits[0])


def vet_blob(digest: str, store: Any, screen: PrivateScreen,
             archive_taint: Mapping[str, Iterable[str]], *, after_run_read: bool = False) -> tuple[Vetted, str | None]:
    """Private / target-derived / provenance checks on a VISIBLE digest; returns the decoded text when allowed.
    `after_run_read`: the prompt that archived it was rendered after a private read of this run (run_private_reads)."""
    if screen.digest_private(digest):
        return Vetted(False, PRIVATE), None
    if digest not in archive_taint:
        return Vetted(False, PROVENANCE_UNKNOWN), None
    if set(archive_taint[digest]) or after_run_read:
        return Vetted(False, TARGET_DERIVED), None
    try:
        text = store.blobs.get(digest).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return Vetted(False, UNRESOLVABLE), None
    if screen.content_private(text):
        return Vetted(False, PRIVATE), None
    return Vetted(True, digest=digest), text


def archived_by(trace: Sequence[TraceStep], node_ids: Iterable[str] | None = None,
                before_index: int | None = None) -> set[str]:
    """Digests archived (rendered as a handle) in the prompts of LLM steps of this trace, optionally only of the given
    nodes and before an index."""
    nodes = set(node_ids) if node_ids is not None else None
    out: set[str] = set()
    for st in trace:
        if st.kind != StepKind.LLM or (nodes is not None and st.node_id not in nodes):
            continue
        if before_index is not None and st.index >= before_index:
            continue
        for d in st.output.get("archived") or []:
            if isinstance(d, str):
                out.add(d)
    return out


__all__ = ["BLOB_RE", "CODES", "EV_RE", "HANDLE_RE", "RELAY_MODULES", "STEP_RE", "PrivateScreen", "Vetted",
           "archived_by", "private_read_windows", "rendered_prompt", "resolve_handle", "run_private_reads",
           "step_module", "step_params", "vet_blob", "vet_step"]
