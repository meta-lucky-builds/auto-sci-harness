"""custom-harness: a research harness whose claims are judged by a fixed evidence checker.

Design history: docs/archive/PLAN.md. Layer map (PLAN §4):
  L0 runtime/, sandbox/   L1 budget.py   L2 store/   L3 tiers.py (verifier ladder grows here)
  gateway/ = model access (three modes, PLAN §14)
"""

__version__ = "0.0.1"
