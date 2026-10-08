"""Every salt parameter the owner has not set (GOAL §13.8, §16.9). The ONE place they live.

Defaults are conservative. Changing any of them changes what a salt campaign may claim, so each is an owner decision.
"""

from __future__ import annotations

# ---------------------------------------------------------------- detectability exam (GOAL §16.9)
# A campaign is refused unless the exact one-sided upper confidence bound on d = TPR - FPR is at most this.
DETECTABILITY_MAX_UPPER = 0.2  # UNCALIBRATED (owner decision, GOAL §13.8)
# Presentations per arm (salt, real) the exam needs at least. 40 + 40 is the board's figure; <= 0.1 needs ~135.
EXAM_MIN_PER_ARM = 40  # UNCALIBRATED (owner decision, GOAL §13.8)
# One-sided error rate of the exam's upper bound (a 95% upper confidence bound).
EXAM_ALPHA = 0.05  # UNCALIBRATED (owner decision, GOAL §13.8)

# ---------------------------------------------------------------- salt generation
# A positive salt plants an effect of this multiple of the claim's minimum effect delta (GOAL: "2 delta").
POSITIVE_EFFECT_MULTIPLIER = 2.0  # UNCALIBRATED (owner decision, GOAL §13.8)
# A positive salt whose realised planted effect misses the target by more than this fraction of it is refused.
POSITIVE_EFFECT_TOLERANCE = 0.25  # UNCALIBRATED (owner decision, GOAL §13.8)
# A null salt is refused when the largest true effect any predictor could have on it (exact, in expectation) exceeds
# this fraction of delta: then a certification on it would not be clearly false.
NULL_MAX_RESIDUAL_FRACTION = 0.25  # UNCALIBRATED (owner decision, GOAL §13.8)

# ---------------------------------------------------------------- audit (after reveal)
# Two-sided confidence of the exact (Clopper-Pearson) intervals on campaign rates, and of a positive salt's effect
# interval when the audit computes it (paired_accuracy_effect).
AUDIT_CONFIDENCE = 0.95  # UNCALIBRATED (owner decision, GOAL §13.8)
# One-sided error rate of the pooled inflation test over all positive salts of a campaign.
POOLED_INFLATION_ALPHA = 0.05  # UNCALIBRATED (owner decision, GOAL §13.8)

# ---------------------------------------------------------------- budget
# Model calls a salt campaign or a model-backed spotter may spend. 0: nothing is spent until the owner sets a budget
# ("needs owner: salt budget"). Scripted subjects and spotters cost 0 calls.
SALT_MODEL_CALL_BUDGET = 0  # UNCALIBRATED (owner decision, GOAL §13.8)

# ---------------------------------------------------------------- isolation
# Splits whose tasks may be replaced by salts. gate and sealed are never allowed (salts stay out of the L6 gate);
# "research" names campaign tasks outside a suite.
SALTABLE_SPLITS = frozenset({"evolve", "research"})  # UNCALIBRATED (owner decision, GOAL §13.8)
