"""Every time-split parameter the owner has not set, in one place (GOAL §6.4, §13.8).

All values are conservative placeholders. None has been calibrated on real data.
"""

from __future__ import annotations

# Prior-art weight threshold for GOAL §6.4's "novelty-weighted subset": a task is in the low-prior-art subset when
# w = 1 / (1 + hits) >= this value, i.e. at most one frozen literature entry mentions any of its key phrases.
LOW_PRIOR_ART_THRESHOLD = 0.5  # UNCALIBRATED (owner decision, GOAL §13.8)

# Bins of the reliability table passed to kernel.predictions.calibration.
CALIBRATION_BINS = 5  # UNCALIBRATED (owner decision, GOAL §13.8)

# Paired bootstrap used by compare(): fixed seed (determinism), number of resamples, two-sided CI level.
BOOTSTRAP_SEED = 20261008  # UNCALIBRATED (owner decision, GOAL §13.8)
BOOTSTRAP_N = 2000  # UNCALIBRATED (owner decision, GOAL §13.8)
CI_LEVEL = 0.95  # UNCALIBRATED (owner decision, GOAL §13.8)

# Fewer resolved paired tasks than this in the last window: the §6.4 verdict is "not shown", whatever the CIs say.
MIN_PAIRS_FOR_VERDICT = 10  # UNCALIBRATED (owner decision, GOAL §13.8)
