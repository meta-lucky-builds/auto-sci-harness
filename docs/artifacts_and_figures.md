# Large artifacts and figures (GOAL §5 L2)

Built on branch `or-artifact` (2026-10-07/08). Both items come from the OpenResearch comparison
(`code_review/openresearch.md` §3 items 7 and 8).

## Large-artifact manifest (`store/run_bundle.py`)

Every per-run bundle's `manifest.json` has `artifacts`: one entry per artifact of the run.

| field | meaning |
|---|---|
| `path` | `task/<rel>` (task data), `workspace/<rel>` (a file the run produced) or the declared host path |
| `bytes`, `sha256` | measured by the harness when the bundle is built (streamed), never taken from the run |
| `kind` | `weights` / `optimizer_state` / `dataset` / `output` / `other` |
| `required` | task data and declared inputs: true by default; workspace outputs: false |
| `status` | `bundled` (bytes in the zip), `available_locally` (on the building machine), `referenced` (declared only) |

Sources: the task directory's files; `input.artifacts` of the run (`[{path, kind, required, bundle?}]`, e.g. the
pretrained weights a run starts from; a declaration without a local file needs `sha256` and `bytes`); and every file
of the final workspace. A workspace file over `config.LARGE_ARTIFACT_BYTES` (UNCALIBRATED) is listed, not bundled.

`chh rerun BUNDLE [--artifact-root DIR ...]` and `chh reproduce BUNDLE [--artifact-root DIR ...]` resolve every entry
(zip, local blob store, recorded path, then each root) and print a row per artifact. They refuse (fail closed) when a
required artifact is missing, when ANY artifact found has a different size or sha256, or when a `bundled` artifact is
absent from the zip. A missing optional artifact is reported, not fatal. `chh rerun` also refuses a `manifest.json`
whose sha256 differs from the one this store recorded when the bundle was written (a manifest that lies about a hash
is caught here; without a record it says "not anchored").

Claim bundles (`kernel/bundle.py`, owned by kstats) do not write `artifacts` yet: they still carry every blob. The CLI
checks the list if present. Request posted: `mailbox/orartifact-to-kstats-01-...`.

Reading is bounded: member count, manifest size, per-member size, total size and compression ratio
(`config.RUN_BUNDLE_*`, UNCALIBRATED). Over a cap -> `RunBundleError`, nothing decompressed.

## Figures (`figures/`)

A figure is a directory with `figure.py` (the script), `figure.svg` (its output) and `figure.json` (refs + hashes).
A ref is `{evidence: <Evidence id>, pointer: <JSON pointer into its payload>}`; a raw blob is not accepted. The
script runs only in a sandbox, with `inputs.json` (ref id -> resolved value) and the stdlib renderer `chh_svg.py`
(= `figures/svg.py`; bar and line charts). It passes ref ids, never numbers, to the renderer.

Renderer choice (verified 2026-10-07): neither the host venv nor `custom-harness-sandbox:py312` has matplotlib, and
installs are not allowed, so figures are stdlib SVG. One real Docker render + audit was run and passed.

The audit (`audit_figure`, and I6 in `research.audit`) fails a figure unless: F1 file hashes match `figure.json`
(and `figure.json` matches the hash recorded in the run state); F2 every ref resolves to a finite number in an
Evidence payload, and (in the research audit) only to the run's own certified evidence; F3 the stored SVG, checked
without trusting the script, has only whitelisted elements/attributes, every mark bound to a ref with the exact
value, geometry, axis and ticks recomputed by the trusted renderer, and no decimal/percent/exponent number in free
text; F4 the script re-run in a fresh sandbox produces byte-identical SVG.

Limits: free text may carry integers; category labels are not bound to data (a script that swaps them itself is not
caught); no research-loop node produces figures yet (the audit runs when `state["figures"]` is set by trusted code;
`figures` is reserved state).
