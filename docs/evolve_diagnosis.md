# Structured diagnosis and claim checks (GOAL §5 L6.3) — what is and is not enforced

Branch `b-evolve` (2026-10-08). Code: `src/custom_harness/evolve/diagnosis.py`, `evolve/claims.py`, the hooks in
`evolve/loop.py`, `evolve/propose.py`, `evolve/proposer_agent.py`, `evolve/edits.py`. Tests:
`tests/test_evolve_diagnosis.py`.

## Enforced (fails closed, each has a test)

| Check | Where | On failure |
|---|---|---|
| The diagnosis reads only evolve-split runs | `propose.diagnose(evolve_ids=...)`, always passed by the loop | the run is dropped before anything is read; counted in `n_excluded_split` |
| Every causal signature carries verifier evidence (both runs' `#verdict`, the failing run's failed Kernel checks) and every reference it carries resolves (the step exists by its own index; the check failed in a certificate summarised exactly as the export summarises it) | `diagnosis.pair_divergences` / `link` | the signature is dropped, counted in `unlinked` |
| For the agentic proposer, every signature in its prompt resolves in the export it mounts | `proposer_agent.propose_from_export` → `link_digest(digest, export_refs(export))` | the signature is dropped from the prompt |
| Every citation of an edit (`evidence`, `failure_clusters_fixed`) resolves to something the proposer was shown: a cluster/divergence id of the diagnosis, or `<run>#<step>`, `<run>#verdict`, `<run>#<step>/<check>` present in the diagnosis or (agentic) the export | `claims.check_citations`, called by the loop | edit rejected unevaluated, lineage status `rejected_claims` |
| Cluster ids (C<n>/D<n>) the diagnosis does not contain | same | same (this was "recorded, not enforced" before; now enforced) |
| The claimed change (`claimed_change`, required in the edit schema) equals the real diff of the spec | `claims.check_claimed_diff`, called by the loop after the edit is applied | `rejected_claims`; a missing claim is a rejection too |

Ordering in the loop: prediction screen → novelty gate → static checks (apply, one mechanism) → no-op/duplicate →
**claims (citations + claimed diff)** → critic → smoke → race. A claims rejection is not added to the novelty gate's
rejected signatures: the same ops, honestly cited and claimed, are a new proposal.

Real-diff granularity: dict keys are followed down to the changed value; a list is one value (its path is the list's
path, so a claimed `/edges/3/target` is read as `/edges`); an added or removed key is its own path. The claim must
equal the real diff as a set (no hidden change, no claimed change that did not happen).

## Content-level divergence

When node, route and error agree at every step, the first step whose OUTPUT CONTENT differs is the divergence
(mechanism `content`). A decision divergence also carries the first content difference at or before it (`content`).
Content is compared after the export's own reduction (`export._clean`, `export.embargo`: certificates reduced to their
label, report text withheld) and with run-specific ids normalised (hashes, `*_ref`, ledger sequence numbers, ids of the
form `prefix_<16 hex>`). Only the differing KEY NAMES reach the signature; the values stay in the audited export.

## Not enforced / limits (stated)

- A citation that resolves proves the evidence EXISTS, not that it supports the edit. Whether the cited step explains
  the failure is not checked by code.
- An edit that cites nothing passes the citation check (it claims nothing). Requiring at least one citation would be
  a further tightening; not done (owner decision).
- The claimed-diff check proves the proposer knows what its ops change, not that the change does what the rationale
  says.
- The digest is not passed through the export's leak audit (`export._audit`); its content is limited by construction
  (key names, normalised error signatures, verdict fields of evolve runs, check names). The cluster EXAMPLES in the
  digest still carry raw error excerpts of evolve runs, as before this change.
- A divergence is a correlate located in time, not a proven cause.
- Fixture changes made for the enforcement (honest fixtures, no assertion weakened): `tests/test_evolve.py` PROPOSALS
  gained `claimed_change` and the first edit no longer cites `C0` (in table-driven worlds there is no cluster C0, so the
  citation was false); `tests/test_evolve_evidence.py` `CostTable` gives a run that earned 0 a failure signature (so
  the fixtures that cite C0 cite a real cluster); `tests/test_evolve_mechanism_smoke.py` items gained `claimed_change`.
