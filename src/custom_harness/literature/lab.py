"""Literature connectors for the Lab (GOAL §17: "literature ... allowed"; PORTMAP §5).

The Lab searches broadly: OpenAlex, PubMed, bioRxiv and Semantic Scholar (all already implemented in sources.py)
plus arXiv and Crossref, each asked SEPARATELY so a Lab session sees every source's answer rather than the first
one that responds (the Court's citation path keeps its own fallback order, client.SEARCH_ORDER). alphaXiv is added
only when the owner has recorded permission (alphaxiv.py; default OFF: "ToS unknown, needs owner permission").

Every rule of the existing module still applies, because the Lab goes through the same LiteratureClient:
- the publication window can be narrowed but never widened past the as-of date T (client.window);
- every answer is recorded into the snapshot store (RecordingTransport) and every search result set can be logged
  in a RetrievalLog whose entries are re-derived from the stored responses (retrieval.py, with the same source
  mapping the Lab searched with);
- nothing identifying is sent (http.py: fixed User-Agent, no contact address, no keys).
OR sends a hard-coded contact address to NCBI and OpenAlex (`orx@alphaxiv.org`, src/config.rs L59/L66); we send
none. Lab search results are EXPLORATORY material; a Court citation is checked by literature/cite.py on a frozen
snapshot as before. Built, NOT test-verified; no live request in this phase.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from ..compute.credentials import OwnerConfig
from .alphaxiv import AlphaXivPermission, AlphaXivSource, ConnectorDisabled
from .client import LiteratureClient, SearchResult
from .http import HttpxTransport, LiteratureError, PacedTransport, Pacer, Transport
from .snapshot import OfflineTransport, RecordingTransport, SnapshotStore
from .sources import ALL_SOURCES, PACING, Source

LAB_SOURCES = ("openalex", "semanticscholar", "pubmed", "biorxiv", "arxiv", "crossref")
ALPHAXIV_SOURCES = ("alphaxiv", "alphaxiv-embedding")


def lab_sources(config: OwnerConfig | None = None) -> dict[str, Source]:
    """The source parsers a Lab session may use: the public ones always, alphaXiv only with owner permission."""
    out = dict(ALL_SOURCES)
    cfg = config if config is not None else OwnerConfig.load()
    try:
        perm = AlphaXivPermission.from_owner_config(cfg.literature("alphaxiv"))
    except ConnectorDisabled:
        return out
    out["alphaxiv"] = AlphaXivSource(perm)
    out["alphaxiv-embedding"] = AlphaXivSource(perm, strategy="embedding")
    return out


def lab_client(store: SnapshotStore, *, as_of: date | None = None, config: OwnerConfig | None = None,
               snapshot_id: str | None = None, transport: Transport | None = None) -> LiteratureClient:
    """A Lab client. `snapshot_id` -> offline replay of a frozen snapshot; otherwise live (recorded) HTTP.
    `transport` lets tests inject a scripted one (it is still wrapped by the recorder for provenance)."""
    sources = lab_sources(config)
    if snapshot_id is not None:
        inner: Transport = OfflineTransport(store, snapshot_id)
    else:
        base = transport if transport is not None else HttpxTransport()
        inner = RecordingTransport(PacedTransport(base, Pacer(PACING)), store)
    return LiteratureClient(inner, as_of=as_of, sources=sources)


def lab_search(client: LiteratureClient, query: str, *, limit: int = 10, sources: Sequence[str] = LAB_SOURCES,
               published_from: date | None = None, published_to: date | None = None) -> list[SearchResult]:
    """Ask each source separately (one SearchResult per source; a failing source is reported, not fatal)."""
    unknown = [s for s in sources if s not in client.sources]
    if unknown:
        disabled = [s for s in unknown if s in ALPHAXIV_SOURCES]
        if disabled:
            raise ConnectorDisabled("alphaXiv is OFF: terms of service unknown; needs owner permission")
        raise LiteratureError(f"unknown sources {unknown}", infra=False)
    return [client.search(query, limit, [name], published_from=published_from, published_to=published_to)
            for name in sources]
