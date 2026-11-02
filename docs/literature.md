# Literature module and citation check (GOAL §5 L4.3, K0/I3, §6.4, M7)

Package: `src/custom_harness/literature/`. No model calls anywhere; `cite.py` imports no network code.

| File | Role |
|---|---|
| `normalize.py` | text normalisation; title / author / quote-span matching (pure) |
| `records.py` | `PaperRecord`, `LookupResult`, DOI / arXiv id parsing, partial-date handling, as-of-T visibility |
| `http.py` | transports (httpx, scripted), per-host `Pacer`, `fetch` with bounded retries on transient errors only |
| `snapshot.py` | content-addressed dated store, `freeze(as_of)`, `RecordingTransport`, `OfflineTransport` |
| `sources.py` | arXiv (Atom), Semantic Scholar, OpenAlex, Crossref, PubMed (E-utilities), bioRxiv parsers; doi.org handle API |
| `client.py` | `LiteratureClient.lookup` (three-valued) and `.search` (fallback, query splitting, as-of filter) |
| `retrieval.py` | `RetrievalLog` (per-session, content-addressed, re-derived on read) and `RetrievalLoop` |
| `cite.py` | `check_citation(s)` -> verified / failed / unverified; `i3_verdict` for the audit |
| `cli.py` | `chh lit-search`, `chh cite-check`, `chh lit-freeze` |

## ID resolution (three-valued)
- **resolves**: a source returned a record carrying exactly the requested id (as of T: provably published by T).
- **does_not_resolve**: the *authority* says it does not exist (doi.org handle API `responseCode 100` for DOIs;
  arXiv's own API for arXiv ids; PubMed efetch for PMIDs), the id is malformed (non-ASCII, spaces, bad
  shape), or the paper was provably published after T.
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

Identifiers: DOI, arXiv id, PMID (`pmid:123`, `PMID: 123`, a pubmed.ncbi.nlm.nih.gov URL). Digits must be ASCII
(Python's `\d` also matches e.g. Arabic-Indic digits; that was a bug, fixed and tested). bioRxiv DOIs (`10.1101/`)
are looked up on the bioRxiv details API first (v1 date = published, latest version = updated + text).

## Retrieval provenance (coordinator scope addition)
A citation is verified only if its paper appeared in a logged **search** result of the same session
(`RetrievalLog(store, session)`; `chh lit-search --session S`, `chh cite-check --session S`). Lookups never count
(looking up a model-supplied id would launder it). Each entry stores the response envelope it came from; reading
the log re-parses that envelope with the named source and the entry's window and must reproduce the logged ids
exactly, so edited entries, entries copied from another session, and lookup responses passed off as searches are
integrity errors. Results with no stored envelope cannot be logged. No session given -> nothing is verified.

## Audit I3 (research/claims.py: `citation_audit`)
When the run state has `citations` (a list of `Citation` dicts), the audit checks them with
`offline_client(SnapshotStore($CHH_DATA_DIR/literature), state["literature_snapshot"])` against
`RetrievalLog(store, session=run_id).retrieved_ids()`; I3 = `i3_verdict(...)["I3"]`, details in
`audit["citations"]`. No citations -> "n/a". No/unknown snapshot, malformed citations or a log integrity error ->
I3 False. `passed` requires I3 True or "n/a". Both keys are reserved state (evolve/edits.py). Not built yet: the
research.literature node that searches, cites and freezes the snapshot during a run.

## Retrieval loop
`RetrievalLoop(client, log, max_rounds=N, published_from=..., published_to=...)`: one window fixed at
construction (read-only; defaults to end at the client's T; `published_to` later than T is refused, also by
`client.search`), every `search` / `follow_up` uses it (they take only a query), a hard round cap (a failed or
incomplete round still counts), and dedup across rounds: source id -> DOI / arXiv id / PMID -> normalised title.
Window membership is fail closed: the record's earliest possible date must be >= start and its latest <= end.

## Snapshots (reproducibility, §6.4)
Every final answer (2xx, 404/410) is a write-once blob envelope `{key, request, retrieved_at, status, body}`;
`index.jsonl` maps request keys to envelopes. `freeze(as_of=T)` writes a content-addressed manifest; its digest is
the snapshot id. Offline replay answers only from the manifest; a missing request is `lookup_failed`. An envelope is
served only if its hash verifies and its stored key and request match the request (cache poisoning -> error).
As of T: records whose latest possible publication date is after T are hidden; a stored abstract revised after T
(arXiv `updated`) is not used for quote support.

## Pacing and privacy
Intervals: arXiv 3 s, Semantic Scholar 1 s, OpenAlex 0.2 s, Crossref 1 s, doi.org 1 s, NCBI 0.34 s, bioRxiv 1 s;
retries on 408/425/429/5xx and network errors (backoff 1, 2, 4 ... s, `Retry-After` honoured, > 30 s gives up as transient infra). 400/422 are
the harness's fault (infra=False); 401/403 are infra, not retried. Requests carry only a fixed User-Agent
(`custom-harness-literature/0.0.1`), no contact e-mail, no API key, no cookies.

## CLI
```
chh lit-search "query; another query" --live            # network, answers recorded under $CHH_DATA_DIR/literature
chh lit-freeze --as-of 2025-01-01 --label t0             # prints the snapshot id
chh lit-search "query" --snapshot <id> [--as-of DATE]    # offline replay
chh lit-search "q" --live --session run_1 --published-from 2020-01-01   # logs retrieval provenance
chh cite-check cites.json --snapshot <id> --session run_1   # exit 0 iff I3 passes (or n/a)
```
Without `--live` there is no network access.

## Full-text keyword retrieval (2026-10-07; `fulltext.py`)
`chh lit-search "q" --source europepmc --full-text ...` searches Europe PMC (open-access articles with full text),
then fetches each in-window hit's JATS full text (`/{PMCID}/fullTextXML`) and cuts match snippets from it locally
(Europe PMC returns none, so every snippet re-derives from a stored envelope). `chh cite-check --full-text` also
fetches the full text of each resolved citation, so quote spans are checked against the body, not only the abstract.
Rules, all enforced in code:
- the full text is fetched only for records inside the loop's fixed window, through the same paced, recorded
  transport (Europe PMC: 1 s per request), and replays identically from a frozen snapshot;
- the text is title + abstract + body; the reference list and back matter never support a quote;
- the text's date is the LATEST date the article gives (e.g. its PMC release). As of T, a text dated after T, or with
  no date, is never used. A search result then carries no text and no snippets. In a citation check, a quote that is
  not in the abstract becomes `unchecked` (unverified), not `unsupported`, because a full text exists that cannot
  be used;
- a full text is attached only if it states one of the record's ids and contradicts none (`same_paper`);
- each record keeps the article's licence (`full_text_license`): only permissively licensed texts may go into a
  shared bundle (not enforced: no bundle code reads it yet).
Terms (read 2026-10-07): **alphaXiv** (OR's full-text source): terms of service **unknown, treated as forbidden until the
owner checks** (a 2026-10-07 reading said they forbid automated access; PORTMAP records them as unread, and nobody
has confirmed either). The client stays OFF behind the owner's permission record. **Europe PMC**: EMBL-EBI allows automated use at a reasonable load and expects attribution; each
article's own licence governs reuse. The Europe PMC pages returned HTTP 403 to our reader, so endpoint shapes are
inferred from EBI material. Never exercised live in this build. **arXiv** (no full-text search API; PDFs for
research use, not redistribution) and **NCBI BioC** (retrieval by id) are possible fetch-by-id fallbacks, not built.
**CORE** needs a licence and API key (needs owner).
