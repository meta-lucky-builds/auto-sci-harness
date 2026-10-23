# Third-party notices

## OpenResearch (alphaXiv)

Parts of this repository are Python ports of logic from OpenResearch, read at commit
`fe36f7424855d7218e64812c5fec6f7db955586f` of https://github.com/alphaXiv/OpenResearch. Apart from the four figure
assets listed below, nothing from it was copied verbatim, and nothing was built or run; the ported modules re-implement protocol knowledge (how to drive each coding-agent CLI) and
name the source file and commit in their docstrings:

- `src/custom_harness/lab/agents.py` (Claude Code, Codex, OpenCode, ACP and the deferred Cursor / Antigravity
  adapters): from `src/local/claude.rs`, `src/local/harness/claude.rs`, `src/local/codex.rs`,
  `src/local/harness/codex.rs`, `src/local/opencode.rs`, `src/local/opencode_runtime.rs`, `src/local/local_models.rs`,
  `src/local/acp.rs`, `src/local/harness/cursor.rs`, `src/local/harness/antigravity.rs`;
- `src/custom_harness/lab/acp_client.py` (Agent Client Protocol v1 client): from `src/local/acp.rs`,
  `src/local/acp/host.rs` and `src/local/harness/fixtures/acp-peer.py`.

Ported by lab-compute (same commit; each module names its sources in its docstring):

- `src/custom_harness/compute/jobs.py` (stage vocabulary, async lifecycle shape, log cursor, `log_lines`): from
  `src/jobs/mod.rs` and `src/compute.rs`;
- `src/custom_harness/compute/remote_docker.py` and `pinned_ssh.py` (run-directory layout, stdin-carried upload,
  status order, cancel window): from `src/jobs/ssh.rs` and `src/jobs/ssh/container.rs`;
- `src/custom_harness/compute/slurm.py` (`render_sbatch`, `parse_mem`, `resolve_gres`, `slurm_time`,
  `parse_job_id`, the inspect probe and its token mapping, cancel): from `src/jobs/slurm.rs`;
- `src/custom_harness/compute/k8s.py` (kubectl usage, Job defaults, status reading, resource deletion): from
  `src/jobs/kubernetes.rs`;
- `src/custom_harness/compute/modal_backend.py` (launcher pattern, `gated_script`): from `src/jobs/modal.rs` and
  `src/compute.rs`;
- `src/custom_harness/compute/ray_jobs.py`: from `src/jobs/ray.rs`; `src/custom_harness/compute/hf_jobs.py`: from
  `src/jobs/huggingface.rs`;
- `src/custom_harness/literature/alphaxiv.py` (endpoints, hit shape, `versionless_id`; the connector is OFF by
  default: alphaXiv's terms of service are unknown): from `src/client.rs` and `src/config.rs`;
- `src/custom_harness/outputs/latex.py` (engine/driver choice, hardened compile arguments, error detection): from
  `src/local/latex.rs`.

Copied verbatim (one provenance comment line added at the top of each; full licence also in
`src/custom_harness/outputs/assets/openresearch/NOTICE`): `orx_figstyle.py`, `orx_chart.py`, `orx-chart.html` and
`orx-tikz-preamble.tex` from `agent-skills/orx-figures/assets/`, in `src/custom_harness/outputs/assets/openresearch/`.
No pdf.js (Apache-2.0) asset is used, so no pdf.js notice is needed.

OpenResearch's telemetry, agent feedback, auto-update, host execution and secrets fan-out were not ported (GOAL §17).
"OpenResearch" and "alphaXiv" are named here for attribution only; no trademark use is intended.

Its licence:

```
MIT License

Copyright (c) 2026 alphaXiv

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
