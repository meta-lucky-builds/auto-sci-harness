# Third-party notices

## OpenResearch (alphaXiv)

Parts of this repository are Python ports of logic from OpenResearch, read at commit
`fe36f7424855d7218e64812c5fec6f7db955586f` of https://github.com/alphaXiv/OpenResearch. Nothing from it was copied
verbatim, built or run; the ported modules re-implement protocol knowledge (how to drive each coding-agent CLI) and
name the source file and commit in their docstrings:

- `src/custom_harness/lab/agents.py` (Claude Code, Codex, OpenCode, ACP and the deferred Cursor / Antigravity
  adapters): from `src/local/claude.rs`, `src/local/harness/claude.rs`, `src/local/codex.rs`,
  `src/local/harness/codex.rs`, `src/local/opencode.rs`, `src/local/opencode_runtime.rs`, `src/local/local_models.rs`,
  `src/local/acp.rs`, `src/local/harness/cursor.rs`, `src/local/harness/antigravity.rs`;
- `src/custom_harness/lab/acp_client.py` (Agent Client Protocol v1 client): from `src/local/acp.rs`,
  `src/local/acp/host.rs` and `src/local/harness/fixtures/acp-peer.py`.

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
