"""kerneld: the Kernel process boundary (GOAL §16.5.2) and the host side of the release ledger (§16.5.3, §16.5.5).

- `releases`  typed releases of target-derived information to agent-visible surfaces, with record/replay pinning
- `client` / `server` / `protocol` / `tokens`  a separate process that alone serves the vault's private-read path
  (held-out scoring, Kernel re-scoring, leak scanning) to host code holding narrow per-run/claim/purpose tokens
- `campaign`  campaign-scope cassette replay with fake labels and pinned releases
"""
