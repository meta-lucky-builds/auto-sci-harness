"""The ONE config place for the L3 additions in `verify/synthesis.py`, `verify/tolerance.py` and `verify/screen.py`
(GOAL §5 L3, §13.8). Every value the owner has not set is marked UNCALIBRATED; defaults are the conservative side.
Changing a value here is a human-only action (it changes what a verifier may certify)."""

from __future__ import annotations

from ..kernel.checks import KernelConfig
from ..kernel.claim import EvidenceClass
from ..tiers import Tier

# ---------------------------------------------------------------- verifier synthesis (synthesis.py)
# A fault suite (agent-authored or human-approved) is usable only with at least this many golden cases and planted
# faults in EACH half (public: shown to the authoring model; sealed: never shown). Fewer would let a verifier pass
# by luck.
SYNTH_MIN_GOLDEN_PER_HALF = 2  # UNCALIBRATED (owner decision, GOAL §13.8)
SYNTH_MIN_FAULTS_PER_HALF = 2  # UNCALIBRATED (owner decision, GOAL §13.8)
# A synthesized verifier is executable code (natural tier T1); until a human review is ledgered it is capped at T2
# (GOAL §5 L3; fixed by the GOAL, not a tunable).
SYNTH_REVIEWED_TIER = Tier.T1_EXECUTABLE
SYNTH_UNREVIEWED_TIER_CAP = Tier.T2_STATISTICAL

# ---------------------------------------------------------------- calibrated tolerances (tolerance.py)
# Classes whose checks compare re-run NUMBERS and may therefore use a measured tolerance. Exact classes (formal,
# computational) never take a tolerance; physical claims are not re-run by the system.
# UNCALIBRATED (owner decision, GOAL §13.8)
TOL_CALIBRATABLE_CLASSES = frozenset({EvidenceClass.DATA, EvidenceClass.SIMULATED})
# The loosest miss rate (share of honest re-runs allowed outside the tolerance) a caller may declare.
TOL_MAX_MISS_RATE = 0.10  # UNCALIBRATED (owner decision, GOAL §13.8)
# A calibrated tolerance never exceeds the Kernel's own ceiling on declared re-execution tolerances (K1): a noise
# floor above it means the claim cannot be checked by re-execution, and calibration refuses.
TOL_CEILING = KernelConfig().max_reproduce_tolerance
# The claim path (`tolerance.claim_tolerance`, used by research.claim): its declared miss rate and noise sources.
# The research loop's K3 replays run in the same sandbox environment as the confirm runs, so the rebuild source is not
# part of the purpose (fewer sources = a smaller, stricter tolerance). Never read from node params or run state.
TOL_CLAIM_MISS_RATE = 0.10  # UNCALIBRATED (owner decision, GOAL §13.8)
TOL_CLAIM_PURPOSE = "replay_same_env"  # UNCALIBRATED (owner decision, GOAL §13.8)

# ---------------------------------------------------------------- float screen -> exact certificate (screen.py)
# Slack of the FLOAT screen only. It can only change what is pruned, never what is certified (the exact checker
# decides), so a larger slack costs time, never soundness; too small a slack only loses results.
SCREEN_FLOAT_SLACK = 1e-9  # UNCALIBRATED (owner decision, GOAL §13.8)
