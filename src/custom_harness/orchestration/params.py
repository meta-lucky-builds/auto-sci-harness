"""The one config place for the delegation driver's parameters the owner has not set (GOAL §13.8).

Each default is conservative: fewer helpers, a stricter brief. Changing one changes how much work runs at once,
not any claim policy."""

from __future__ import annotations

# At most this many helpers in flight at once, across all leads of one driver root (one laptop runs everything).
MAX_IN_FLIGHT = 2  # UNCALIBRATED (owner decision, GOAL §13.8)

# At most this many helpers spawned by one lead over its lifetime (finished ones included).
MAX_TOTAL_PER_LEAD = 4  # UNCALIBRATED (owner decision, GOAL §13.8)

# A brief's instructions shorter than this cannot be self-contained (a floor, not a proof of self-containedness).
MIN_INSTRUCTIONS_CHARS = 80  # UNCALIBRATED (owner decision, GOAL §13.8)

# Phrases that point at context the helper does not have; a brief containing one is refused (lower-cased match).
# A heuristic floor: passing it does not prove a brief is self-contained.
CONTEXT_DEPENDENT_PHRASES = (  # UNCALIBRATED (owner decision, GOAL §13.8)
    "as discussed", "see above", "mentioned above", "discussed above", "as before", "same as before",
    "like before", "as we said", "you already know", "earlier in this conversation", "the previous message",
    "same as last time", "as agreed",
)

# How long a driver call waits for the driver-wide mutex before giving up (seconds).
MUTEX_TIMEOUT_S = 10.0
