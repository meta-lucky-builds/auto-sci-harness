"""L8 Weights, data side only (GOAL §5 L8). There is no GPU here and training is out of scope: this package builds
the training DATA and its guards, nothing that changes weights.

- trajectory.py  trajectories, declared verifier sets A and B (disjoint by id AND adapter hash), verdicts, certification
- licence.py     training-licence filter (invariant 11), owner flags default to false (fail closed)
- export.py      double-certified, licence-filtered, outcome-filtered SFT rows (JSONL + manifest)
- goodhart.py    coupled-Goodhart indicator: slope of (A-score - sealed score) over time, alarm when it widens
- selection.py   task selection by reward variance
- config.py      the one config place (owner flags and UNCALIBRATED parameters)

Not wired into any loop yet: nothing in the runtime calls this package, and verdicts are inputs, not yet derived from
Kernel certificates.
"""
