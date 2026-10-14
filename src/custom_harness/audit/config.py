"""Claim Audit parameters the owner has not set (GOAL §13.8). ONE place; each one is marked.

None of these changes what the Kernel certifies (K0-K9 and KernelConfig are untouched). They fix how the audit
prepares fresh external data and how a non-certificate is worded.
"""

from __future__ import annotations

# Share of each fresh external dataset's rows held out as the audit's test split. The split is a function of the
# row position and the pinned file hash only (never of the labels), fixed before any label is read.
TEST_FRACTION = 0.5  # UNCALIBRATED (owner decision, GOAL §13.8)

# Smallest held-out split the audit will run at all; below it the audit ends COULD NOT RUN (too few rows to say
# anything) instead of spending the claim's one look.
MIN_TEST_ROWS = 30  # UNCALIBRATED (owner decision, GOAL §13.8)

# Largest fresh dataset (rows) the laptop-scale audit ingests.
MAX_ROWS = 200_000  # UNCALIBRATED (owner decision, GOAL §13.8)

# Days an author is given to reply before a "no reply" right-of-reply record may be written.
MIN_REPLY_DAYS = 14  # UNCALIBRATED (owner decision, GOAL §13.8)
