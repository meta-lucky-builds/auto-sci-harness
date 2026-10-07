"""Closed set of Kernel reason codes (GOAL §16.5.8).

Every reason a check gives carries one code from `Code`. The code is what may leave the Kernel towards agent-visible
surfaces (exports, prompts, evolution feedback); the human-readable text next to it stays with the certificate for
humans. Free-text exception messages never enter a reason (they could carry data out of the Kernel): an exception is
reported by its builtin type name only (`exception_name`).
"""

from __future__ import annotations

import builtins
from enum import StrEnum


class Code(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"  # the human-owned table says the check does not apply to the class
    NOT_EVALUATED_UNREGISTERED = "NOT_EVALUATED_UNREGISTERED"
    CHECK_RAISED = "CHECK_RAISED"
    CHECK_UNCODED = "CHECK_UNCODED"  # a check returned a reason without a code (fail closed)
    # K0 provenance
    K0_ARTIFACT_UNREGISTERED = "K0_ARTIFACT_UNREGISTERED"
    K0_ARTIFACT_BYTES = "K0_ARTIFACT_BYTES"
    K0_NO_EVIDENCE = "K0_NO_EVIDENCE"
    K0_EVIDENCE_UNRESOLVABLE = "K0_EVIDENCE_UNRESOLVABLE"
    K0_REPLAYED_EVIDENCE = "K0_REPLAYED_EVIDENCE"  # invariant 21: replayed model output is never evidence
    K0_CERTIFICATE_COUNT = "K0_CERTIFICATE_COUNT"
    # K1 well-formed
    K1_KIND = "K1_KIND"
    K1_MIN_EFFECT = "K1_MIN_EFFECT"
    K1_VALUE = "K1_VALUE"
    K1_METRIC = "K1_METRIC"
    K1_ADAPTER = "K1_ADAPTER"
    K1_SAME_ARTIFACT = "K1_SAME_ARTIFACT"
    K1_COMPARATOR = "K1_COMPARATOR"
    K1_TARGETS = "K1_TARGETS"
    K1_DATASETS = "K1_DATASETS"
    K1_PLAN = "K1_PLAN"
    K1_EFFECT = "K1_EFFECT"
    K1_PROBABILITY = "K1_PROBABILITY"
    K1_PROBABILITY_BELOW_P0 = "K1_PROBABILITY_BELOW_P0"  # §16.4: q < p0 is refused (F2)
    K1_RISK_PRIOR = "K1_RISK_PRIOR"
    K1_RELIABILITY = "K1_RELIABILITY"
    K1_STATEMENT = "K1_STATEMENT"
    K1_SCOPE = "K1_SCOPE"
    K1_UNREADABLE = "K1_UNREADABLE"
    # K2 pre-registered
    K2_CHAIN = "K2_CHAIN"
    K2_NOT_REGISTERED = "K2_NOT_REGISTERED"
    K2_PREREG_MISMATCH = "K2_PREREG_MISMATCH"
    K2_PEEK = "K2_PEEK"
    K2_ARTIFACT_AFTER = "K2_ARTIFACT_AFTER"
    K2_CERTIFICATE_BEFORE = "K2_CERTIFICATE_BEFORE"
    # K3 reproduced
    K3_NO_RECORDS = "K3_NO_RECORDS"
    K3_SCORE_MISMATCH = "K3_SCORE_MISMATCH"
    K3_EARLIER_FAILURE = "K3_EARLIER_FAILURE"
    K3_NO_REPLAYER = "K3_NO_REPLAYER"
    K3_NONE_SELECTED = "K3_NONE_SELECTED"
    K3_REPLAY_ERROR = "K3_REPLAY_ERROR"
    K3_REPLAY_NO_OUTPUT = "K3_REPLAY_NO_OUTPUT"
    K3_REPLAY_DIFFERS = "K3_REPLAY_DIFFERS"
    K3_ADAPTER_CHECK = "K3_ADAPTER_CHECK"  # an approved adapter's exact checker rejected the certificate
    K3_INPUTS = "K3_INPUTS"
    # K4 statistically valid procedure
    K4_NO_RECORDS = "K4_NO_RECORDS"
    K4_OUTSIDE_PLAN = "K4_OUTSIDE_PLAN"
    K4_DUPLICATE = "K4_DUPLICATE"
    K4_SEEDS = "K4_SEEDS"
    K4_SELECTIVE_REPORTING = "K4_SELECTIVE_REPORTING"
    K4_UNLOGGED = "K4_UNLOGGED"
    K4_NO_SLOT = "K4_NO_SLOT"  # an alpha-account exists on the target but the claim holds no slot in it
    K4_PLAN_CODE_DIVERGENCE = "K4_PLAN_CODE_DIVERGENCE"  # §16.6: the executed test differs from the registered one
    # K5 comparator integrity
    K5_WRONG_CODE = "K5_WRONG_CODE"
    K5_WRONG_METRIC = "K5_WRONG_METRIC"
    K5_NOT_BASELINE = "K5_NOT_BASELINE"
    K5_NOT_PAIRED = "K5_NOT_PAIRED"
    # K6 held-out confirmation
    K6_UNREGISTERED = "K6_UNREGISTERED"
    K6_ROLE = "K6_ROLE"
    K6_DEV_CONTENT = "K6_DEV_CONTENT"
    K6_TWIN = "K6_TWIN"
    K6_PUBLIC_LABELS = "K6_PUBLIC_LABELS"
    K6_DEVELOP_READ = "K6_DEVELOP_READ"
    K6_NO_STATS = "K6_NO_STATS"
    K6_CRITERION_NOT_MET = "K6_CRITERION_NOT_MET"
    K6_ADAPTIVELY_EXPOSED = "K6_ADAPTIVELY_EXPOSED"  # §16.5.3: a continuous release preceded the claim
    # K7 risk
    K7_NO_RISK_MODEL = "K7_NO_RISK_MODEL"
    K7_NO_STATS = "K7_NO_STATS"
    K7_NO_SPREAD = "K7_NO_SPREAD"
    K7_TRIVIALLY_SAFE = "K7_TRIVIALLY_SAFE"
    # K8 background consistency
    K8_RANGE = "K8_RANGE"
    K8_LENGTH = "K8_LENGTH"
    K8_EMPTY = "K8_EMPTY"
    K8_LABEL_FILE = "K8_LABEL_FILE"
    K8_PRIVATE_INPUT = "K8_PRIVATE_INPUT"
    K8_INPUT_BYTES = "K8_INPUT_BYTES"
    K8_ADAPTER_BOUNDS = "K8_ADAPTER_BOUNDS"
    # K9 leak & canary audit
    K9_PRIVATE_CODE = "K9_PRIVATE_CODE"
    K9_LEAK = "K9_LEAK"
    K9_TRACE = "K9_TRACE"
    K9_PATH = "K9_PATH"


class Reason(str):
    """A reason text tagged with its closed code."""

    code: Code

    def __new__(cls, code: Code, text: str) -> Reason:
        obj = super().__new__(cls, text)
        obj.code = Code(code)
        return obj


def coded(code: Code, texts: list[str] | tuple[str, ...]) -> list[str]:
    """Tag a list of texts (e.g. an approved adapter's checker output) with one code."""
    return [Reason(code, t) for t in texts]


def codes_of(reasons: list[str] | tuple[str, ...]) -> tuple[Code, ...]:
    """The code of every reason; a reason without one raises (a check must not emit uncoded text)."""
    out = []
    for r in reasons:
        if not isinstance(r, Reason):
            raise TypeError("uncoded Kernel reason")
        out.append(r.code)
    return tuple(out)


_KNOWN_ERRORS = frozenset({n for n in dir(builtins) if isinstance(getattr(builtins, n), type)
                           and issubclass(getattr(builtins, n), BaseException)}
                          | {"ValidationError", "JSONDecodeError", "VaultError", "ExecutionError", "AdapterError",
                             "KernelError", "LPError", "SimError", "FormalError", "WorkspaceEscape"})


def exception_name(e: BaseException) -> str:
    """The exception's type name if it is in a fixed set (builtins and the Kernel's own errors), else 'Exception':
    the output is a closed set, so not even a crafted class name leaves the Kernel."""
    name = type(e).__name__
    return name if name in _KNOWN_ERRORS else "Exception"
