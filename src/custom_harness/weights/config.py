"""The ONE config place of the L8 data side (GOAL §5 L8; brief rule 6). Nothing here trains anything.

`PROVIDER_TRAINING_PERMITTED` is OWNER-SET: whether a provider's terms allow training on its outputs (invariant 11).
It is keyed "provider/mode" (e.g. "local/ow", "openai/api"), since one provider's terms differ by access mode. A key
that is absent is False: nothing is permitted until the owner says so (fail closed). An owner may instead keep the
flags in a JSON file and load it with `weights.licence.LicencePolicy.load(path)`; no agent may edit either.
"""

from __future__ import annotations

from ..tiers import Tier

# Owner decision (GOAL §13.8, invariant 11). Empty on purpose: every provider defaults to "training NOT permitted".
PROVIDER_TRAINING_PERMITTED: dict[str, bool] = {}

# Which trajectory outcomes become SFT rows. "negative" (an honest NEGATIVE claim) is a defensible addition, but it
# changes what the weights learn, so it is left to the owner.
SFT_OUTCOMES = frozenset({"certified"})  # UNCALIBRATED (owner decision, GOAL §13.8)

# Every verifier of a certifying set must be at least this strong (lower Tier value = stronger; invariant 4: no
# persistent write, weights included, without evidence at the tier floor). T2 matches PUBLISH_VERIFIED in tiers.py.
EXPORT_TIER_FLOOR = Tier.T2_STATISTICAL  # UNCALIBRATED (owner decision, GOAL §13.8)

# Coupled-Goodhart indicator: Theil-Sen slope of (A-score - sealed score) per time unit over the last WINDOW points.
GOODHART_WINDOW = 6  # UNCALIBRATED (owner decision, GOAL §13.8): points the slope is computed over
GOODHART_MIN_POINTS = 4  # UNCALIBRATED (owner decision, GOAL §13.8): fewer points -> "insufficient_data", not "ok"
GOODHART_SLOPE_ALARM = 0.01  # UNCALIBRATED (owner decision, GOAL §13.8): gap growth per time unit that alarms

# Task selection by reward variance (AWorld idea).
VARIANCE_MIN_ROLLOUTS = 4  # UNCALIBRATED (owner decision, GOAL §13.8): rollouts needed to estimate a variance
VARIANCE_MIN = 0.05  # UNCALIBRATED (owner decision, GOAL §13.8): population variance below this is not selected
