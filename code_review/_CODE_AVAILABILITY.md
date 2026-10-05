# Code availability (checked 2026-10-06; every URL verified via `git ls-remote` and/or fetch)

| source | arXiv id | official code? | verified URL | evidence | notes |
|---|---|---|---|---|---|
| ResearchBench | 2503.21248 | yes (moderate confidence) | https://github.com/ankitala/ResearchBench | README says "official code release for the ACL Findings 2026 paper" and links arXiv; HF paper page lists dataset ankilok/ResearchBench (same author handle family). arXiv abs/HTML do NOT link it | Eval toolkit + 12-sample tiny split; full data on HF https://huggingface.co/datasets/ankilok/ResearchBench. Owner handle not an obvious author name, hence moderate |
| SDE (Scientific Discovery Evaluation) | 2512.15567 | yes | https://github.com/HowieHwong/sde-harness | arXiv HTML introduces "sde-harness" and links this repo | Also eval code https://github.com/deepprinciple/lm-evaluation-harness (linked in paper) |
| SciDER | 2603.01421 | yes | https://github.com/leonardodalinky/SciDER | Footnote on arXiv HTML: "project is available at ..." | Active, has branches |
| EvoScientist | 2603.08127 | yes | https://github.com/EvoScientist/EvoScientist | Linked in arXiv HTML; README links the arXiv report | Active (pushed 2026-10-05) |
| Meta-Harness | 2603.28052 | yes | https://github.com/stanford-iris-lab/meta-harness | Project page https://yoonholee.com/meta-harness/ links it; README has arXiv badge | Framework + 2 reference experiments. Optimized TB2 harness only: https://github.com/stanford-iris-lab/meta-harness-tbench2-artifact (linked from arXiv HTML) |
| PARNESS | 2605.05258 | yes | https://github.com/gtrhythm/PARNESS | arXiv HTML: "Source code and pipeline configurations" | |
| Continual Harness | 2605.09998 | yes (unlinked from arXiv) | https://github.com/sethkarten/continual-harness | README: "Reference implementation for Continual Harness" linking arXiv; owner = first author | arXiv abs/HTML had no repo link. Repo renamed from pokeagent-speedrun; bundles PokeAgent benchmark infra |
| SIA | 2605.27276 | yes | https://github.com/hexo-ai/sia | README: "Official implementation of SIA..." with arXiv link; PDF header names Hexo Labs | arXiv abs/HTML had no link (only found via search + README) |
| Adaptive Auto-Harness | 2606.01770 | yes | https://github.com/A-EVO-Lab/a-evolve/tree/release/adaptive-auto-harness | Linked in arXiv abstract page and HTML; branch exists (ls-remote) | Lives on a release branch of the shared a-evolve repo |
| Self-Harness | 2606.09498 | yes (partial) | https://github.com/qzzqzzb/Self-Harness | Linked on arXiv abs page; README has arXiv badge | README says "More results are coming soon"; small repo (1.6MB) |
| HarnessX / AEGIS | 2606.14249 | yes | https://github.com/Darwin-Agent/HarnessX | Project page https://darwin-agent.github.io/HarnessX/ lists it; arXiv HTML links project page | Org Darwin-Agent |
| AHOIS (Socratic agents) | 2606.26722 | no | - | No link in arXiv abs/HTML; searches found nothing | Hardware (multimode fibre) system; none located |
| RHI | 2607.15524 | no | - | No link in arXiv abs/HTML; GitHub + web search found only awesome-lists | Not found |
| AutoOPT | 2608.07407 | yes | https://github.com/Shuvomoy/AutoOPT | arXiv HTML: "We open-source the portable agent skills and Lean verification projects ... at" | Skills + Lean projects only, not necessarily a full harness |
| MDA (Model Discovery Agent) | 2608.09696 | no (benchmarks only) | https://github.com/murphyk/neuronbench , https://github.com/murphyk/glucosebench | Paper links these benchmarks; glucosebench README states no MDA/LLM code | Agent code itself not found |
| ScienceFlow | 2608.14354 | yes | https://github.com/huawei-noah/noah-research/tree/master/ScienceFlow | Noah README names the paper; project page https://huawei-noah.github.io/noah-research/ScienceFlow/website/ (HTTP 200); PyPI `scienceflow` homepage points to https://github.com/science-learner/ScienceFlow (fork, "Noah Research fork") | arXiv HTML does not link code directly |
| AutoSaddler | 2608.23041 | yes | https://github.com/microsoft/AutoSaddler | README links arXiv 2608.23041 + project site; paper says "Project website and code will be available at https://aka.ms/AutoSaddler-website" | Project page repo AutoSaddler-ProjectPage is only a site template |
| Prime Agent | 2608.23552 | yes | https://github.com/PrimeIntellect-ai/prime-agent | arXiv HTML: "Code is available at" | Active |
| Auto-RecSys | 2609.10922 | no | - | No link in arXiv; searches found nothing | Industry paper; none located |
| SoL-Pi | 2609.20519 | yes | https://github.com/NVlabs/SoL-Pi | Linked on arXiv abs + HTML; project page https://nvlabs.github.io/SoL-Pi/ | Active |
| RRSI | 2609.24972 | yes | https://github.com/google-research/rrsi | Linked on arXiv HTML; README links paper + project page https://regularized-rsi.com/ | |
| Mixture of Branches | 2609.37834 | no | - | No link in arXiv HTML; search found nothing | Not found |
| MILO | 2609.38349 | no (project page only) | https://jprithwish.github.io/MILO/ | Project page linked from arXiv HTML; no code link on page; author GitHub has no MILO repo | Page only |
| ScholarEvolve | 2609.40169 | empty-placeholder | https://github.com/UCSB-NLP-Chang/ScholarEvolve | arXiv HTML: "Code is available at ..." | Repo exists but is EMPTY (size 0, git ls-remote returns no refs) |
| Robin (Nature s41586-026-10652-y) | arXiv 2505.13400 | yes | https://github.com/Future-House/robin | Nature/PMC "Code availability" references the Robin GitHub repo; README links arXiv + blog | Includes robin_full.ipynb with paper parameters |
| ScientistOne | 2605.26340 | no (artifacts only) | https://github.com/scientist-one/generated-artifacts | Linked from https://scientist-one.github.io/ | Contains generated-papers/ and solution-code/ outputs, NOT the framework. Third-party repos named ScientistOne exist and are unrelated |
