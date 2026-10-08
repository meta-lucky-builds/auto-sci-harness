"""Every scorecard parameter the owner has not set (GOAL §13.8), in one place. Conservative defaults."""

from __future__ import annotations

# Two-sided confidence level of every comparison interval (paired bootstrap, percentile).
CONFIDENCE = 0.95  # UNCALIBRATED (owner decision, GOAL §13.8)
# Paired-bootstrap resamples and the fixed seed that makes every interval reproducible byte for byte.
N_BOOT = 2000  # UNCALIBRATED (owner decision, GOAL §13.8)
BOOT_SEED = 0  # UNCALIBRATED (owner decision, GOAL §13.8)
# Axis H: the leak-audit pass rate must be exactly this (GOAL §2 H says 100%); not a tunable tolerance.
LEAK_AUDIT_REQUIRED_RATE = 1.0
