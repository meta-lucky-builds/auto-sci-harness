# Role exams for a second model family (GOAL §16.8, invariant 5)

Module: `src/custom_harness/gateway/role_exam.py`. Tests: `tests/test_role_exam.py` (fake backends only).

## Status (honest)
- **Code path built, nothing admitted.** No second-family backend is installed. `second_family_status()` returns
  "needs owner: install a local model (e.g. via Ollama) or provide an allowlisted API; no second-family backend is
  configured" for today's configuration (Codex plus mocks). Invariant 5 stays NOT MET until the owner installs a
  model (or allowlists an API) and it passes the exams below.
- **Replicator exam: NOT BUILT.** It needs real finished claims re-run through `replica_dev`. Only the item type
  (`ReplicatorItem`) and grading hook (`grade_replicator_item`) exist. Both raise `NotImplementedError`, and
  `admit("replicator", ...)` always refuses.
- **Not wired into `policy.py`.** The workstream lead decides how a binding requires an admission.
- **Ledger.** Decisions go to `EvidenceStore.append_record("role_exam", ...)`. A dedicated Kernel ledger kind has been
  requested from kstats. Until then these records are provenance, not Kernel evidence.

## Exams
Builders take a TaskSpec-like dict, a `research.data.profile()` dict and a `source`. The source must have
`dataset_role` set to `dev` or `public` and a non-empty `ref` that does not point into a
`private/held_out/sealed/gate` path. If the task or the profile carries any nested `dataset_role` other than
dev/public, the builder raises `ExamSourceError`. Every item records its provenance: role, ref, task id, profile
sha256 and builder. The exam hash is the sha256 of the canonical JSON of its items.

- **Judge (tournament), `build_judge_exam`.** The builder makes 3 sound kNN plans. Each one is paired with a copy that
  has one planted defect: `test_label_peek`, `unscaled_distance`, `wrong_metric_direction` or
  `target_leak_feature`. Each pair is shown in both orders, giving 12 pairs (24 items). The exam is graded on
  accuracy and on order consistency, meaning the same plan is chosen in both orders.
- **Critic, `build_critic_exam`.** The exam has 12 flawed plans and 6 sound plans. The answer is typed tags from the
  closed `FLAW_TAGS` enum. An unknown tag makes the whole answer unparseable. The exam is graded on tag recall over
  flawed plans and on the false-flag rate over sound plans. An unparseable answer on a sound plan counts as a false
  flag.

## Running and admission
`run_exam(backend, exam)` makes one call per item with a JSON schema at temperature 0. Parsing is strict: the reply
must be bare JSON. Prose, code fences, invalid values and `BackendError` all count as misses.
`admit(role, cand, prop, cand_family, prop_family, thresholds)` admits only when every one of these holds:
- the role matches the exam;
- the candidate and the proposer took the same exam hash;
- both families are known and not mock, and they differ;
- the score bars pass;
- `miss_phi(cand.misses, prop.misses) < max_phi`.

Phi is undefined when either miss vector is constant, and an undefined phi is a refusal ("diversity not shown").
**Consequence:** a candidate (or a proposer) with zero misses cannot be admitted on that exam. A harder or larger
exam is needed. This is an owner decision.

`record_admission(store, backend, admission)` ledgers every decision, admitted or refused.
`admitted_roles(store, backend, exam_hashes=...)` returns roles only for the exact name, provider, model and family,
and only under the current exam hash per role. The latest decision wins.

## Bars (`ExamThresholds`, one place)
| bar | default | status |
|---|---|---|
| judge score | 0.80 | UNCALIBRATED (owner, §13.8) |
| order consistency | 0.85 | UNCALIBRATED |
| critic tag recall | 0.6 | UNCALIBRATED |
| critic false-flag max | 0.3 | UNCALIBRATED |
| max miss phi | 0.8 | stated in GOAL §16.8 |

The exams are small: 24 and 18 items from one task. Real admission should pool several dev/public tasks (inferred).
Pooling builders is not built.
