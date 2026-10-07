# Literature module and citation check (GOAL §5 L4.3, K0/I3, §6.4, M7)

Package: `src/custom_harness/literature/`. No model calls anywhere; `cite.py` imports no network code.

| File | Role |
|---|---|
| `normalize.py` | text normalisation; title / author / quote-span matching (pure) |
| `records.py` | `PaperRecord`, `LookupResult`, DOI / arXiv id parsing, partial-date handling, as-of-T visibility |
| `http.py` | transports (httpx, scripted), per-host `Pacer`, `fetch` with bounded retries on transient errors only |
| `snapshot.py` | content-addressed dated store, `freeze(as_of)`, `RecordingTransport`, `OfflineTransport` |
| `sources.py` | arXiv (Atom), Semantic Scholar, OpenAlex, Crossref parsers; doi.org handle API (DOI existence) |
| `client.py` | `LiteratureClient.lookup` (three-valued) and `.search` (fallback, query splitting, as-of filter) |
| `cite.py` | `check_citation(s)` -> verified / failed / unverified; `i3_verdict` for the audit |
| `cli.py` | `chh lit-search`, `chh cite-check`, `chh lit-freeze` |

## ID resolution (three-valued)
- **resolves**: a source returned a record carrying exactly the requested id (as of T: provably published by T).
- **does_not_resolve**: the *authority* says it does not exist (doi.org handle API `responseCode 100` for DOIs;
  arXiv's own API for arXiv ids), the id is malformed (non-ASCII, spaces, bad shape), or the paper was provably
  published after T.
- **lookup_failed**: everything else. An index's 404 alone is never "does not resolve" (Crossref does not hold
  DataCite DOIs such as arXiv's `10.48550/...`; OpenAlex returned an HTML 404 for one in a manual check).

## Citation check
Verified iff: id resolves; title matches (NFKC, case, markup, punctuation-insensitive; whole words; no
cross-script folding); claimed year is one the record gives; claimed authors (optional) are an in-order prefix of
the record's authors (family names, accents folded, `et al.` allowed); every quote occurs as a contiguous
whole-token span in the stored abstract/full text. Quotes keep punctuation as tokens (sentence boundaries, minus
signs, decimals), and are refused if elided (`...`), shorter than 5 words, mixed-script (homoglyphs), or carrying
invisible control characters (bidi overrides, tag characters). Zero-width spaces / soft hyphens are removed.
Failed if anything is positively wrong; unverified if anything could not be checked. I3 = True only if every
citation is verified; `"n/a"` with no citations.

## Snapshots (reproducibility, §6.4)
Every final answer (2xx, 404/410) is a write-once blob envelope `{key, request, retrieved_at, status, body}`;
`index.jsonl` maps request keys to envelopes. `freeze(as_of=T)` writes a content-addressed manifest; its digest is
the snapshot id. Offline replay answers only from the manifest; a missing request is `lookup_failed`. An envelope is
served only if its hash verifies and its stored key and request match the request (cache poisoning -> error).
As of T: records whose latest possible publication date is after T are hidden; a stored abstract revised after T
(arXiv `updated`) is not used for quote support.

## Pacing and privacy
Intervals: arXiv 3 s, Semantic Scholar 1 s, OpenAlex 0.2 s, Crossref 1 s, doi.org 1 s; retries on 408/425/429/5xx
and network errors (backoff 1, 2, 4 ... s, `Retry-After` honoured, > 30 s gives up as transient infra). 400/422 are
the harness's fault (infra=False); 401/403 are infra, not retried. Requests carry only a fixed User-Agent
(`custom-harness-literature/0.0.1`), no contact e-mail, no API key, no cookies.

## CLI
```
chh lit-search "query; another query" --live            # network, answers recorded under $CHH_DATA_DIR/literature
chh lit-freeze --as-of 2025-01-01 --label t0             # prints the snapshot id
chh lit-search "query" --snapshot <id> [--as-of DATE]    # offline replay
chh cite-check cites.json --snapshot <id>                # exit 0 iff I3 passes (or n/a)
```
Without `--live` there is no network access.
