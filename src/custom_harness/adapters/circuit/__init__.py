"""The `circuit` adapter: exact linear DC circuit analysis (computational class, T1; GOAL §4.7, §15 engineering).

Modules: `check` (the trusted certificate checker the Kernel calls: never solves), `validation` (golden cases and
fault injection; the adapter is usable only after these pass AND a human approves its hash), `solver` (exact rational
modified nodal analysis that PRODUCES certificates; untrusted for soundness: the checker re-checks everything it
emits), `screen` (a float screen that may only prune). Design note: docs/adapters_circuit.md. This file is part of the
adapter's identity (hashed), so keep it free of behaviour.
"""
