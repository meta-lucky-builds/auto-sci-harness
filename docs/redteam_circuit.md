# Red team of the `circuit` adapter (GOAL §15 "adapter red team")

**Invariant 5 NOT MET for the red team:** the red team was an opus sub-agent of the s3-domain builder, and both are
Claude models (same family). Tests: `tests/test_circuit_redteam.py`. The red-team agent wrote the tests and probes
(2026-10-09, worktree `s3-domain-rt`) but stopped before committing or writing this note. The builder committed its
file unchanged as `bfa99d4`, merged it (`3525001`), fixed the adapter (`dec7562`) and wrote this note.

Evidence: run on the adapter BEFORE the fix (`fc24df8`, log `scratchpad/logs/s3dom_rt_test1.log`): 9 failed,
66 passed, 7 xfailed (strict). AFTER the fix (`dec7562`): **the test file has NOT been run** (owner instruction of 2026-10-14: no test runs this round). Only direct checker calls were made (< 10 s, verified): a certificate in UTF-8-BOM, UTF-16, UTF-16-LE, UTF-32 and one with a backslash-escaped canary in `comment` are all refused by `check` and `bounds` ("not plain UTF-8" / "byte-order mark or NUL" / "backslash escapes are refused"); the plain certificate still passes; `validate(CIRCUIT_CHECKS)` == [] with 39 checks. "Now" below is therefore verified at the checker level only for rows 1a-1c and 1g, and NOT RUN through the Kernel for every row.

| # | Attack | Outcome on fc24df8 | Now | Test(s) |
|---|---|---|---|---|
| 1a | Canary smuggled in the certificate `comment` as UTF-16 / UTF-32 / `\u`-escaped JSON (K9 scans raw bytes as UTF-8) | **SUCCESS**: CERTIFIED-EXACT | blocked (adapter reads plain UTF-8 only, refuses BOM, NUL and backslash escapes) | `test_a_canary_hidden_in_a_certificate_comment_is_not_certified[utf-16, utf-32, escaped]` |
| 1b | Same through the netlist `comment` | **SUCCESS** | blocked | `test_a_canary_hidden_in_the_netlist_comment_is_not_certified[utf-16, escaped]` |
| 1c | Certificates in UTF-8-with-BOM, UTF-16, UTF-16-LE, UTF-32 | **accepted** (enabler of 1a) | refused | `test_certificates_must_be_plain_utf8_without_bom[4]` |
| 1d | Generalisation: the `lp` reader (kernel/lp.py) accepts UTF-16 too | **open** in lp (not this adapter) | strict xfail, owner s3-kernel | `test_generalised_lp_reader_refuses_utf16` |
| 1e | Non-canonical number spellings (`02`, `+1`, ` 1`, Arabic/fullwidth digits, `6e0`, `0x6`, `6/0`, ...) and non-string numbers | refused | refused | `test_non_canonical_number_spellings_are_refused`, `test_non_string_numbers_are_refused` |
| 1f | JSON tricks: duplicate/case/fullwidth keys, Cyrillic or zero-width names, comments, nested comment, Infinity, 1e400 | refused | refused | `test_json_tricks_in_the_netlist_are_refused` |
| 1g | Escaped spelling of the same netlist (other bytes, same parse) | accepted under its own hash (harmless: same circuit) | refused (no escapes) | `test_a_byte_different_spelling_of_the_same_netlist_is_refused` |
| 1h | Forged witnesses (right voltages wrong current; bogus left-null; huge/odd null vectors of a truly singular network are correctly accepted) | refused / correct | same | `test_forged_witnesses_are_refused`, `test_huge_and_odd_null_vectors_of_a_truly_singular_network_are_fine` |
| 2 | Value claims on 6 singular networks (floating resistor island, voltage-source loop, current-source cutset, VCVS of gain 1 across its own control, ground touching no element, 0 V source from a node to itself); ground handling | refused | refused | `test_a_value_claim_on_a_singular_network_cannot_be_certified`, `test_ground_handling` |
| 3a | Prefixed / respelled units (kohm, mV, mA, MΩ, mΩ, Ω, Ohm, OHM, v, "ohm ") and unit/quantity mismatches | refused | refused | `test_prefixed_and_respelled_units_are_refused`, `test_unit_quantity_mismatches_are_refused_at_k8` |
| 3b | Dimensionless spellings `1` / `V/V` / `A/A` interchangeable | cosmetic gap (numerically harmless) | unchanged, pinned | `test_dimensionless_spellings_are_interchangeable` |
| 4a | Scope creep: a true circuit value with a PHYSICAL statement ("will work when built", "rated for 5 W") | **SUCCESS**: CERTIFIED-EXACT with that prose | **open**: needs Kernel K1 tightening (owner s3-kernel); strict xfail | `test_a_physical_statement_on_a_true_circuit_value_is_refused_at_k1[3]` |
| 4b | Physical `scope.conditions` on a circuit claim | **SUCCESS** | **open** (same owner); strict xfail | `test_physical_scope_conditions_on_a_circuit_claim_are_refused_at_k1[3]` |
| 4c | Generalisation: same for `lp` claims | **SUCCESS** | **open** (same owner); strict xfail | `test_generalised_lp_claim_with_physical_statement_and_conditions_is_refused_at_k1` |
| 4d | Physical query / evidence class / kind / metric / direction / comparator relabels | refused | refused | `test_the_circuit_query_vocabulary_cannot_express_a_physical_quantity`, `test_kind_metric_direction_and_comparator_relabels_are_refused`, `test_relabelling_the_metric_to_another_adapter_routes_the_netlist_to_a_checker_that_refuses_it` |
| 5 | DoS: the full registered bound (80 unknowns, 100-digit numbers, 1.3 MB certificate) | K3 takes ~12 s, K8 0.5 s (probe `rt_probe4`, not a test) | bounded, not a hang; owner may lower the bounds | none (timing recorded only) |
| 6 | K8 independence: MNA rows vs element laws on >1000 random vectors | agree on every vector | same | `test_mna_rows_and_element_laws_agree_on_every_vector` |
| 7 | Identity: `screen.py` outside the hash cannot affect certification; checker imports only the stdlib; an unapproved second adapter serving `circuit_value` does not hijack resolution | refused | refused | `test_screen_is_outside_the_adapter_hash_and_cannot_affect_certification`, `test_the_checker_module_imports_only_the_stdlib`, `test_an_unapproved_second_adapter_serving_circuit_value_does_not_hijack_resolution` |

Open items (not fixable in the adapter): 4a-4c (k1_computational checks neither statement nor scope conditions) and
1d (lp reader encodings; a K9 that scans parsed strings would close the whole class for every adapter). Both are
handed to s3-kernel; the strict xfails turn into XPASS failures when they are fixed.
