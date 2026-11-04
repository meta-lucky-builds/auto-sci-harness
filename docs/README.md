# Documentation map

Start with the [project README](../README.md). [STATUS.md](../STATUS.md) records what has really been run, and
[GOAL.md](../GOAL.md) is the full specification. Where a document here disagrees with STATUS.md, trust STATUS.md.

## Use it

- [A 10-minute start](tutorial_ml.md): no agent, Docker or key needed
- [User guide](user_guide.md): install, settings, backup, upgrades
- [First session checklist](first_session_checklist.md): every human step before a real run
- [Human-only actions](human_actions.md): what the software will never do for you
- [The UI](ui.md) and the [Experience API](experience_api.md)

## How a claim gets judged

- [Evidence Kernel](kernel.md): what is enforced, what is only accountable, what is not covered
- [kerneld boundary](kerneld_boundary.md) and [hermetic sandbox](hermetic_sandbox.md)
- [The Lab](lab.md) and [Lab compute](lab_compute.md)
- [Sealed projects](sealed_projects.md): "sealed" as a ledger fact, not a promise
- [Display, rehearsal, readiness and the health run](court_rehearsal.md)
- [Failures never spend truth](court_failures.md)
- [Claim Audit](audit.md), [alpha-accounts](alpha_accounts.md), [bounded nulls](bounded_null.md)
- [Taint labels](taint.md)
- [First-discovery plan](first_discovery_plan.md)

## Who watches the checker

- [Governance of the verifier](governance.md) and the [human-held check](human_check.md)
- [Assurance](assure.md): honest corpus, resweep, differential, mutation
- [Kernel differential test](kernel_differential.md) and the [core / adapter split](kernel_core_adapter_split.md)
- [Salts](salts.md) and their [redesign](salts_redesign.md)
- [Circuit adapter red team](redteam_circuit.md)
- Findings registry: [`redteam/findings.yaml`](../redteam/findings.yaml), with attack patches in
  [`redteam/museum/`](../redteam/museum)
- [Transcript bound](r2-2_transcript_bound.md) and [feature rulings](feature_rulings_ledger.md)

## Domain adapters

- [Lean](adapters_lean.md) (formal), [circuit](adapters_circuit.md), [simulator](adapters_sim.md)
- [Verifier additions](l3_verifiers.md)

## Self-improvement

No self-improvement has been shown yet; see STATUS.md.

- [Evidence embargo](evolve_embargo.md), [structured diagnosis](evolve_diagnosis.md)
- [Search over search](search_over_search.md), [time-split benchmark](timesplit.md)
- [Emergence rule](emergence.md), [ablation switches](ablation.md)
- [Role exams](role_exams.md), [L8 isolation](l8_isolation.md)

## The research loop

- [Loop wiring](research_loop_wiring.md), [hypothesis front](hypothesis_front.md), [multi-agent mode](multi_agent_mode.md)
- [Experiment tree extras](tree_extras.md), [branch, route and reveal](branch_route_reveal.md),
  [key graph and figures](key_graph_and_report_figure.md), [artifacts](artifacts_and_figures.md)
- [Memory](memory.md), [skill writer](skill_writer.md), [critic answers and recall](critic_answers_and_recall.md),
  [literature](literature.md)
- [Control allocation](control_allocation.md), [downgrade-only controllers](control_downgrade.md),
  [orchestration](orchestration.md), [gateway wiring](gateway_wiring.md), [wall-clock budget](campaign_wallclock.md),
  [SSH compute](compute_ssh.md)
- [Scorecard](scorecard.md), [local scorecard](scorecard_local.md), [suite rebuild](suite_rebuild.md)

## Engineering records

Written while the work happened, kept as they were. They describe the state at their date.

- [Kernel round 3](kernel_s3_tightenings.md), [Kernel round 4](kernel_s4.md), [integrity round 4](integrity_s4.md)
- [int17 test plan](int17_test_plan.md), [int18 test plan](int18_test_plan.md)
- [`domain_proposals/`](domain_proposals)
- [`archive/`](archive): the original design plan, handoff notes and the first live-run report
- [`../code_review/`](../code_review): notes from reading the public code of the systems this one learned from
