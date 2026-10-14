# kerneld boundary: what is behind it and what is not (GOAL §16.5.2)

Status as of 2026-10-08, branch w2-wiring. Ratchet: `tests/test_kerneld_boundary.py` (AST, by file and count).

## Behind kerneld
- `research/claims.py` makes no private vault read in the host process (commit 063019a):
  - `confirm`: each held-out score comes from kerneld through one single-use `evaluate` token per (claim, arm, seed);
  - `audit` I4 re-score: one `kernel` token bound to the certificate's claim hash, `rescore` per evidence record;
  - `audit` I5 leak scan: one `leakscan` token; kerneld builds the fingerprints from the hidden labels and returns
    location strings only. The vault canary is added inside kerneld; the run's own canary is passed in.
  - Trace outputs go over the wire as strict JSON: a non-finite float becomes `None` (`kerneld/client._wire_safe`);
    the scan reads only strings, and the `_redacted_private_values` flag is kept truthy.
  - Tests: `tests/test_research_kerneld.py` (end to end: the host process records no private `Vault.read`),
    `tests/test_kerneld.py::test_leak_scan_of_a_trace_with_non_finite_numbers`.
- `research/claims.py` is in the ratchet's COVERED list: no private read and no store open there.

## Not behind kerneld (listed in the ratchet, old path)
- `cli.py` opens the EvidenceStore once (`main`). It makes no private vault read. The store open stays because the
  Engine, Kernel and Ledger run inside the CLI process for every command; moving it means running the engine against a
  store it does not hold. Not done.
- Private ingest (`register_dataset(..., private=...)`): the host holds the bytes it registers. Sites:
  `cli.py` (`chh return-data`), `research/claims.py` (task registration reads the task dir's private labels),
  `audit/data.py` (the claim audit's EXTERNAL split). kerneld has no ingest op.
- Vault canary read: `research/claims.py` registration checks that the task's canary equals the registered one (it
  already holds that canary from the task dir).
- Exempt: `research/preflight.py` builds a scratch store of simulated labels (reason in the test).

## Ratchet checks (tests/test_kerneld_boundary.py)
Private reads (`.read(purpose=...)` or `.read(**kw)`), store opens, private ingest (non-empty `private=`, positional
or `**kw`), canary reads, and `getattr(x, "read" | "canary" | "register_dataset")` (must be none). A new site anywhere
fails; moving one behind kerneld shrinks the list. Mutation check (2026-10-08): planting one of each in
`evolve/driver.py` fails 5 of the 6 tests; the previous 3-test ratchet passed on the same mutated source.
Limits: AST only. A private blob fetched with `store.blobs.get(<private blob hash>)` is not detected statically; the
end-to-end test in `tests/test_research_kerneld.py` covers the research loop's reads dynamically.
