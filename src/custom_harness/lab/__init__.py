"""The Lab (GOAL §17): full-capability exploration, never certification.

A Lab session is one coding agent (Claude Code, Codex, OpenCode, an ACP agent) running with ALL its native tools
inside a lab box: a Docker container with network on, package installs allowed, a persistent workspace volume and an
optional GPU. The box is the boundary; the agent's own permission prompts are not.

The hard walls (each has a static and a run-time check, `walls.py`):
- no held-out truth, sealed data, ledger, store or private file is ever mounted into the box or copied into its
  workspace;
- no owner secret enters the box: only one explicit per-session credential, scoped and spend-capped by the owner at
  the provider, passed in at `chh lab start` (`credentials.py`);
- nothing of the owner's host filesystem is visible beyond the session workspace (one bind mount, checked on the
  final `docker run` argv).

Everything a Lab session produces is labelled EXPLORATORY and is never a finding. The only way from the Lab to a
finding is `chh lab promote` (freeze code + environment) followed by the existing Court path (pre-registration,
offline re-execution in the network-free candidate sandbox, Kernel certificate): `promote.py`.

Built 2026-10-23 by lab-agents; built, NOT test-verified. Design note: docs/lab.md.
"""

from __future__ import annotations

EXPLORATORY = "EXPLORATORY"  # the label on every Lab record and output; never a finding (GOAL §17)
NOT_A_FINDING = "Lab output: EXPLORATORY, not a finding. Only a Court certificate is a finding (GOAL §17)."
