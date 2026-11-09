# Code review instructions (shared by all code-analysis agents)

## Goal
We are building **custom-harness**, a self-improving autonomous research harness meant to beat every system in
`C:\SERS\<USER>\OneDrive\Desktop\META\autoresearch_survey\00_SYNTHESIS.md`. Read these before you start:
- `C:\SERS\<USER>\OneDrive\Desktop\META\custom-harness\PLAN.md` (design; especially §3 data model, §4 layers, §8 novel ideas, §9 feature matrix)
- `C:\SERS\<USER>\OneDrive\Desktop\META\custom-harness\STATUS.md` (what is built)
- The relevant code in `C:\SERS\<USER>\OneDrive\Desktop\META\custom-harness\src\custom_harness\` when you compare.
- The paper/source summary for your system in `C:\SERS\<USER>\OneDrive\Desktop\META\autoresearch_survey\` (given in your task).

Your job: analyze the ACTUAL CODE of one system and report what custom-harness should learn, adopt, or avoid.

## Rules (strict)
1. **Clone, read, never execute** third-party code. Do not `pip install`, do not run their scripts or tests.
   Clone with: `git clone --depth 1 <url> C:/Users/<user>/custom-harness-data/repos/<name>` (use Bash tool).
   Record the commit hash (`git -C <dir> rev-parse HEAD`) and the LICENSE.
2. **Be extremely honest and precise.** Every claim about the code must cite `path/to/file.py:LINE` (or a function name)
   that you actually opened. If you did not read something, say so. Never invent file names, functions, or behavior.
   Mark inferences explicitly as `[inference]`.
3. Large repos: map the structure first (directory tree, entry points, README), then read the core loop files
   deeply. Prefer depth on the core mechanism over shallow coverage of everything.
4. Note **paper-vs-code discrepancies**: things the paper/summary claims that the code does not implement, or does differently.
5. Licensing: say whether we may copy code (MIT/Apache/BSD: yes with attribution; GPL: only if we accept GPL — say so;
   no license: ideas only, no copying).
6. Do NOT modify anything inside `custom-harness/src` or `tests`. Only write your report file.

## Report format
Write to `C:\SERS\<USER>\OneDrive\Desktop\META\custom-harness\code_review\<name>.md`:

```
# <System> — code review
Repo: <url> @ <commit> · License: <license> · Official: yes/no (by whom) · Size: <files / approx LoC of core>
Read depth: <which files you read fully, which you skimmed, which you skipped>

## 1. What the code actually does (architecture map)
## 2. Core loop, step by step (with file:line citations)
## 3. Paper/summary vs code discrepancies
## 4. Mechanisms worth adopting into custom-harness (ranked by expected value)
   For each: what it is (cite), why it is better than what custom-harness has now (cite our file),
   where it would go in our layer model (L0–L9), effort (S/M/L), and copy-vs-reimplement (license).
## 5. Things custom-harness already does better (with evidence)
## 6. Pitfalls, bugs, or anti-patterns seen in their code (cite) that we must avoid
## 7. Verifier / evaluation / safety handling (how they check results, handle exploits, sandbox code)
## 8. Bottom line (5 lines max)
```

Finish by replying with a 10-line summary of the report and the report path.
