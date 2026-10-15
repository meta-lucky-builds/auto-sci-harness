# Model gateway: pacing, cost records and registry verification, wired (s3-gateway, 2026-10-08)

GOAL §5 L0 "Model gateway" asks for per-host pacing, cost tables and "the provider registry verified each release";
§10 invariant 9 asks for a cost record for every run and invariant 11 for BYOS only through allowlisted, official
auth. `gateway/pacing.py` and `gateway/costs.py` existed (g-runtime) but nothing called them. This note says what is
now wired, what each check guarantees and what it does not.

## Per-host pacing
- `pacing.shared_pacer()` is one `HostPacer` per process. `CodexCLIBackend` and `OpenAICompatBackend` take a slot
  from it (or from the pacer passed to them) immediately before EVERY call: each `codex exec` start (the schema
  fallback re-call takes its own slot) and each HTTP POST. Engine retries call `complete` again, so they are paced too.
- Hosts: the OpenAI-compatible backend paces by its base URL's `host[:port]`. Codex paces under `chatgpt.com`
  (`CODEX_PACING_HOST`). That host is **inferred** from the ChatGPT sign-in flow, not observed: we never see Codex's
  traffic. A wrong key changes only which calls share a slot.
- Intervals are UNCALIBRATED (`DEFAULT_MIN_INTERVAL_S = 1.0`, `HOST_MIN_INTERVAL_S` empty) until the owner sets them
  from the providers' published limits. Waiting time is not part of a call's measured wall time.
- `tests/test_gateway_wiring.py::test_every_backend_subclass_is_classified_and_network_backends_pace` scans `src`
  (AST, no imports) for every `Backend` subclass. A new subclass fails until it is classified, and a network backend
  must contain a `_pacer.acquire(` call.
- Not covered: `literature/http.py` has its own `Pacer` (not a model backend). Test cost: tests that build real
  backends now really sleep the default 1 s between calls to one host (e.g. `test_gateway_faults.py`, about 4 s).

## Cost records (invariant 9)
`costs.cost_record(steps)` prices a run's trace through `COST_TABLE`:
- `usd` is a number only when every step with model spend was priced from a calibrated row (`placeholder=False`, with
  a `source`). A step with no row, or with model spend but no provenance, makes it `None` with
  `usd_status="unknown"` and the reasons listed in `missing_rates`. Placeholder rows make it `None` with
  `usd_status="UNCALIBRATED"`. A run with no model spend is `0.0` with `usd_status="no model spend"`.
- `backend_reported_usd` keeps what the backends said, apart. It is never presented as the cost (Codex reports 0;
  the OpenAI-compatible backend computes usd from constructor prices, which the CLI leaves at 0).
- Every rate in `COST_TABLE` is still an UNCALIBRATED placeholder, so every real run today reports usd UNCALIBRATED.

Wired into:
- the run Reproducibility Bundle (`manifest["cost_record"]`, next to the unchanged `manifest["cost"]`);
- `chh run`, `chh resume` and `chh rerun` output (`cost.usd` is the record's usd, with `cost.usd_status`, plus the
  full `cost_record`);
- scorecard facts (`runs[*].cost_record`) and axis G (`total_cost` and `cost_per_certified_claim`: usd from
  `combine_usd`, `None` unless every run is calibrated, with `usd_status`).
- `chh evolve --max-usd` is refused unless every bound backend has a calibrated usd row. A usd cap counts the usd
  each call carries, and with placeholder rows that is 0, so the cap could never trigger.

Changed existing test: `tests/test_scorecard.py::test_fixture_campaign_every_number_resolves_and_reproduces` asserted
`cost_per_certified_claim.usd == 0.5`. That 0.5 is a backend-reported usd on a step with no provenance, which no cost
table row can price. The test now asserts tokens 100 (the same division check) and usd `None` with status "unknown".

NOT wired (outside this branch's ownership):
- `kernel/checklist.py` prints the reporting checklist's cost as `${total.usd:.4f}` from backend-reported usd. That
  is a Kernel change and needs a governance record.
- `evolve/selection.py` offers a `"usd"` cost metric from `Cost.usd`, which is also 0 for Codex.
- The per-run `Budget` usd cap (`budget.py`) has the same 0-usd blind spot; only the CLI refusal above guards it.

## Registry verification (invariant 11)
- `gateway/registry.py` holds the human-owned `BYOS_ALLOWLIST` (today only `openai-codex-cli`: provider openai,
  auth kind `official_cli`) and `OFFICIAL_AUTH` per mode (byos: official_cli / official_oauth; api: api_key; ow:
  none / api_key). `providers.yaml` entries now carry `auth_kind`.
- Runtime gate (tightened): `require_usable` admits a byos entry only if it is "allowed" AND on the allowlist with
  the same provider and the official auth kind. Before this change, a local override file
  (`$CHH_DATA_DIR/providers.override.yaml`) could switch on any subscription entry by setting
  `byos_status: allowed`.
- Static check: `policy_problems` flags unknown providers (not in the family policy's vendors + `local`; `mock` is
  refused), a missing `training_on_outputs` licence flag, a missing or unofficial `auth_kind`, an allowed byos entry
  off the allowlist (or with the wrong provider or auth kind, or a non-https terms URL), an allowlisted key whose mode
  is not byos, and owner licence flags (`weights/config.py`) that permit training where the registry records other
  terms.
- `chh verify-registry` checks the effective registry (bundled + override) and exits 1 on any problem.
  `--release` adds `costs.release_problems` (a human `verified_by` and a `verified_on` within 90 days for every usable
  entry, and a cost row for each provider). **The bundled registry fails `--release` today**: no entry has a human
  verifier. That is the honest state, and only the owner can fix it.
- Known gap (not fixed here, and only an inferred risk): `test_review_loop.py` builds `CodexCLIBackend` with the
  registry `{"openai-codex-cli": {"mode": "api"}}`, so the runtime gate does not stop a registry that relabels the
  Codex entry as api. The static check flags it ("must have mode 'byos'"). Making the runtime gate refuse it would
  break that existing test.
- `scripts/check.py` does not run `verify-registry`. Adding `--release` to the gate would fail every gate until the
  owner signs the registry; that is the owner's decision.

## Runs execute in their own directory
`chh run` and `chh rerun` named their sandbox directory `<spec>-<task>-<int(time.time())>`, so two runs started in
the same second shared one directory. Both now use `compute.fresh_dir` (key plus a random suffix; an existing path is
never reused). Test: `test_two_cli_runs_in_the_same_second_never_share_a_workspace`.
