"""Evidence Kernel (GOAL §4): a small, deterministic, LLM-free checker for scientific claims."""

from .adapters import AdapterError
from .certificate import CHECK_IDS, CHECK_NAMES, Certificate, CheckResult, Label
from .checks import KernelConfig
from .claim import (
    AnalysisPlan,
    ArtifactRef,
    Claim,
    ClaimKind,
    Effect,
    EvidenceClass,
    MetricRef,
    Risk,
    Scope,
    canonical_json,
)
from .execution import DEFAULT_ARGV, ExecRecord, InputRef, SandboxReplayer, run_program
from .kernel import Kernel, KernelError
from .ledger import Ledger
from .vault import DatasetRole, Vault, VaultError

__all__ = ["CHECK_IDS", "CHECK_NAMES", "DEFAULT_ARGV", "AdapterError", "AnalysisPlan", "ArtifactRef", "Certificate",
           "CheckResult", "Claim", "ClaimKind", "DatasetRole", "Effect", "EvidenceClass", "ExecRecord", "InputRef",
           "Kernel", "KernelConfig", "KernelError", "Label", "Ledger", "MetricRef", "Risk", "SandboxReplayer", "Scope",
           "Vault", "VaultError", "canonical_json", "run_program"]
