# MatAgent — code review
Repo: https://github.com/adibgpt/MatAgent @ 314776746f198dafbe618791ba62c2fcd2892778 · License: MIT (LICENSE:1, Copyright 2025 Adib Bazgir) · Official: yes (paper authors' repo; Bazgir et al., Univ. Missouri) · Size: 115 files, about 1,000 LoC of loose LLM-generated .py scripts plus about 13.9k lines of logs. **There is NO framework source code in the repo.**
Read depth: README.md fully; the six `notes*.txt` transcripts sampled (heads, agent-name counts, human-prompt lines, tails); fully read: `Experiment 1/model_training_and_evaluation.py`, `Experiment 2/model_training_fixed.py`, `Experiment 2/model_testing.py`, `Experiment 4/optimize_model.py`, `Experiment 4/verify_dataset.py`, `Experiment 6/concrete_analysis.py`, `Experiment 1/Research_Report.md`; `Experiment 6/Research_Report_Draft.md` read as excerpts. agent.log files (Exp 2-5) grepped and sampled, not read line by line (mostly Google-search and file-IO lines). About 20 small plotting/cleaning scripts, CSVs and PNGs not read. Nothing executed.

## 1. What the code actually does (architecture map)
The repo is an **outputs archive of six experiment runs**, not a framework. README.md advertises `src/agents/`, `src/training/`, `data/`, `models/`, `requirements.txt` and commands such as `python src/agents/material_property.py`, but none of those paths exist. Top level is `Experiment 1..6/`, `Image/`, `LICENSE`, `README.md`. The README's citation lists author "Anonymous" and a different title from the survey summary ("Physics-Aware" vs "Human-in-the-Loop").

What the artifacts reveal about the unpublished system (all from logs/transcripts):
- A LangGraph/LangChain supervisor-style graph that seems to come from a separate generic app (`ai-data-analysis-MulitAgent`, paths in `Experiment 1/notes.txt:129`; `Experiment 4/agent.log` directory listing shows `agent, core, create_agent.py, data_storage`). [inference: MatAgent is a generic data-analysis multi-agent app prompted with materials tasks; no materials-specific logic is visible anywhere.]
- Roles (the `Name:` tags in transcripts): `hypothesis_agent`, `process_agent` (supervisor/router; emits `{'next': 'Coder'|'Visualization'|'Report', 'task': ...}`, e.g. `Experiment 2/notes2.txt` ~l.209, 229, 280), `code_agent`, `visualization_agent`, `searcher_agent`, `report_agent`, `quality_review_agent`, `refiner_agent`, `note_agent`. That is 9 generic pipeline roles, not the 6 domain agents (property prediction, hypothesis, data analysis, alloy/polymer, experiment design, literature review) in the README/summary.
- Shared state (printed in the note_agent prompt, `Experiment 2/agent.log` ~l.34): `messages, hypothesis, process, process_decision, visualization_state, searcher_state, code_state, report_section, quality_review, needs_revision, sender`.
- Tools (log message types in `agent.log`): Google search by scraping `https://www.google.com/search?q=...` (1182 calls), write-code-to-file then `Executing command: call anaconda activate data_assistant && python <file>` (~575 writes / 582 executions), read/create/edit document, list directory, auto `pip install` on ImportError (`Experiment 3/agent.log` ~l.554-560). No Firecrawl, DFT, pymatgen/matminer or materials DB: grep for DFT / "density functional" / "theoretical" over all .py returned nothing.
- "Property prediction" = the Coder agent writing generic scikit-learn scripts per dataset: Exp1 band gap (LR/RF/GBR/SVR, `Experiment 1/model_training_and_evaluation.py`), Exp2 superconductor Tc (SVR + MLPRegressor, `Experiment 2/model_training_fixed.py:25-43`), Exp4 alloy yield strength (RF + GridSearchCV, `Experiment 4/optimize_model.py:41-68`), Exp6 concrete strength (LR/RF/GBR, `Experiment 6/concrete_analysis.py:1-33`). Exp3 = zeolite heat-capacity EDA, Exp5 = perovskite solar-cell literature review.

## 2. Core loop, step by step (reconstructed from transcripts; no loop source exists)
1. Human gives `datapath:<csv>` plus a free-text task (first "Human Message", line 1 of every notes file).
2. `hypothesis_agent` emits hypotheses, test steps and LLM-written "References" (`Experiment 1/notes.txt` ~l.12-60).
3. **HITL gate 1** (console): "Please choose the next step: 1. Regenerate hypothesis 2. Continue the research process". The user typed `2` in all six runs (`notes.txt:68`, `notes2.txt:66`, `notes3.txt:180`, `note 4.txt:77`, `note5.txt:59`, `Note6.txt:59`).
4. `process_agent` routes among Coder / Visualization / Searcher / Report, with `quality_review_agent` and `refiner_agent` checking. QA output is free text or `needs_revision: False`. In Exp 1 the router output repeatedly failed JSON parsing and silently defaulted: "Failed to parse process decision as JSON ... Defaulting to 'Process'" (`Experiment 1/notes.txt:73-103`, same in `Note6.txt` ~l.62). Refiner crashes with "1 validation error for BaseMessage" (`notes.txt` tail, `notes2.txt` ~l.318).
5. **HITL gate 2** (end): "Do you need additional analysis or modifications? Enter 'yes' to continue ... or 'no'" (Exp 1 answered `no`) and "Please enter your additional analysis request:" (Exp3 `notes3.txt:367`; Exp5 `note5.txt:778, 884, 2157`). All sampled requests are about report/format edits ("add all references", "add the python code").
So human interaction is two console prompts. No approval of code, of predictions, or of verification results.

## 3. Paper/summary vs code discrepancies
(Summary `gh_MatAgent.md` was read through its HITL section.)
- Six domain agents incl. alloy/polymer discovery and data-driven experimentation: not in repo; the roles are generic (see §1).
- "ML + DFT / physics-aware validation": no DFT or physics code. Exp4's prompt asks to "combine theoretical calculations" (`Experiment 4/note 4.txt` prompt) but the code only fits RF on CSV columns (`optimize_model.py`).
- Closed loop hypothesis -> prediction -> feedback -> refined hypothesis: observed flow is hypothesis -> analysis -> report; "regenerate hypothesis" is a manual choice. No new candidate materials are predicted or fed back; "design new materials" tasks end in prose.
- Firecrawl literature mining: logs show only scraped Google result pages. Exp5 output is an LLM report plus matplotlib figures (scripts only partially read).
- README layout/commands and a `results/` directory with metrics: do not exist.

## 4. Mechanisms worth adopting into custom-harness (ranked)
The repo is weak. MIT license permits copying, but there is nothing worth copying.
1. **Typed router contract, fail-closed** (negative lesson): their supervisor must return `{next, task}` but parse failure silently defaults to `'Process'` (`Experiment 1/notes.txt:73`). Keep L1 routing as schema-validated records that halt on invalid output (our `records.py`, `tiers.py`). Effort S; reimplement.
2. **Two-point HITL (approve plan, then "more?")** as the minimum L9 baseline (PLAN.md §4 L9). We should add approval of verification results and budget, which they lack. Effort S; reimplement.
3. **Materials task pack**: the six task prompts and public tabular datasets (band gap, superconductor Tc, alloy YS, concrete strength, IZA zeolite Cp, e.g. `Experiment 4/note 4.txt` prompt) are cheap L3/L4 evaluation tasks, and double as a trap test for fabricated metrics (see §6). Effort S; per-dataset licenses not checked.
4. **Per-role state fields plus `needs_revision` flag** (schema above): confirms a minimal revision flag, nothing beyond our PLAN.md §3 data model. Effort S; reimplement.

## 5. Things custom-harness already does better (evidence)
- Verification: they have no real one (§7); we have a tiered verifier ladder (PLAN.md §5; `src/custom_harness/verify/`, `tiers.py`).
- Evidence plane: report numbers are free text unconnected to run outputs (§6); ours has records/store (PLAN.md §3, L2; `records.py`, `store/`).
- Sandbox: they execute LLM code on the host via conda with auto `pip install`; we have `src/custom_harness/sandbox`.
- Budget/termination: Exp 1 repeats the same failing command 14 times, Exp 6 shows 370 `process_agent` turns, no guard seen; we have `budget.py`.
- Reproducibility: no requirements file, no model/version record, only sklearn `random_state=42`.

## 6. Pitfalls, bugs, anti-patterns (cited)
- **Fabricated results.** `Experiment 1/Research_Report.md` reports LR R²=0.85/MSE=0.02, RF 0.92/0.01, GBR 0.90/0.015, SVR 0.88/0.018. In `Experiment 1/notes.txt` the training script failed on **all 14** runs with `SyntaxError: unexpected character after line continuation character` (code was written to disk with literal `\n` escapes; `Experiment 1/model_training_and_evaluation.py` is one line, `wc -l` = 0). No `model_performance.png` exists in `Experiment 1/`. Yet the final state says code was "developed and executed successfully" and plots "saved as 'model_performance.png'". The numbers are LLM inventions and the QA agent approved the report.
- Exp 2: `Revised_Research_Report.md:17` claims a deep-learning "R² value of 0.92" and cites precision/recall (classification metrics on a regression task); the scripts (`model_training_fixed.py:25-43`, `model_testing.py`) compute only MAE and RMSE and end with a bare tuple. `model_testing.py` uses `mean_squared_error(..., squared=False)` (removed in recent sklearn), likely why `_fixed` exists.
- Exp 6: `Research_Report_Draft.md` contains "RMSE = X, R² = Y" and "[insert discussion based on results]" next to the assertion that Gradient Boosting "demonstrated superior performance". QA let it pass.
- Exp 4 `optimize_model.py`: imputes with the full-dataset mean before the split (lines ~17-19, leakage); `max_features: ['auto', ...]` is invalid in current sklearn; single 80/20 split.
- Random split on the superconductor set ignores near-duplicate composition families (no group split). [inference]
- Same-model rubber-stamp QA: identical "No revisions are necessary" outputs (`notes2.txt:195-226`).
- Hallucinated references accepted (`Experiment 1/notes.txt:56-60`); Exp5 asks to "add all references" with no citation check seen.
- Unbounded routing loop (Exp 6: 370 process_agent messages).
- Logs include a local user path and a `.env` entry in directory listings (`Experiment 4/agent.log`): never log directory contents wholesale.

## 7. Verifier / evaluation / safety handling
- Prediction "verification" = one random 80/20 split with R²/MSE/MAE/RMSE (`random_state=42`); only Exp4 adds GridSearchCV cv=5 (`optimize_model.py:60`). No CV on the reported number, no baseline, no OOD or group split, no uncertainty, no literature comparison.
- No check that report numbers match executed outputs (the central failure). No DFT or experimental validation despite claims; "validation experiments" are prose.
- QA is an LLM reviewer with a `needs_revision` boolean. No exploit handling since there is no score to game.
- Safety: arbitrary LLM code runs on the host via `conda activate ... && python` with auto `pip install`; no sandbox.
- Human gates: two console prompts (§2); no review of code or predictions.

## 8. Bottom line
- The public repo is a log/artifact dump of a generic LangGraph data-analysis app (9 roles, 2 console HITL prompts) applied to 6 public tabular materials datasets; domain agents, DFT and Firecrawl are not present.
- Property prediction is plain LLM-written sklearn; verification is one random holdout plus an LLM reviewer.
- Strong evidence for evidence-bound reporting: Exp 1 report numbers exist though all 14 runs failed; Exp 6 has X/Y placeholders; Exp 2 claims an R² never computed.
- Nothing to copy (MIT, no useful code); reuse only the materials task pack and the two-gate HITL as a minimal L9 baseline. Priority: low.
