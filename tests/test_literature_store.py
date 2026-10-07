"""Literature module: identifiers, publication dates, the HTTP layer (pacing, retries, error classes) and the
content-addressed dated snapshot store (GOAL §5 L4.3, §6.4). Offline and deterministic: no live requests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import httpx
import pytest

from custom_harness.errors import infra_cause, is_transient
from custom_harness.literature.http import (
    USER_AGENT,
    HttpResponse,
    HttpxTransport,
    LiteratureError,
    PacedTransport,
    Pacer,
    ScriptedTransport,
    fetch,
)
from custom_harness.literature.records import PaperRecord, latest_date, parse_identifier, text_as_of, visible_as_of
from custom_harness.literature.snapshot import OfflineTransport, RecordingTransport, SnapshotStore, request_key

URL = "https://api.crossref.org/works/10.5555/lit.2021.001"


def ok(text="{}", status=200, headers=None):
    return HttpResponse(status=status, text=text, headers=headers or {})


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


# ------------------------------------------------------------------ identifiers
@pytest.mark.parametrize("raw,kind,value", [
    ("10.5555/LIT.2021.001", "doi", "10.5555/lit.2021.001"),
    ("doi:10.5555/lit.2021.001", "doi", "10.5555/lit.2021.001"),
    ("https://doi.org/10.5555/lit.2021.001", "doi", "10.5555/lit.2021.001"),
    ("arXiv:2403.01234v2", "arxiv", "2403.01234"),
    ("https://arxiv.org/abs/2403.01234", "arxiv", "2403.01234"),
    ("2403.01234", "arxiv", "2403.01234"),
    ("hep-th/9901001v1", "arxiv", "hep-th/9901001"),
    ("math.GT/0309136", "arxiv", "math.GT/0309136"),
])
def test_identifiers_normalise(raw, kind, value):
    ident = parse_identifier(raw)
    assert (ident.kind, ident.value) == (kind, value)


@pytest.mark.parametrize("raw", ["", "not an id", "10.55/x", "10.5555/", "10.5555/litа2021", "2403.123",
                                 "10.5555/a b", "arXiv:", "doi:"])
def test_malformed_identifiers_are_refused(raw):
    with pytest.raises(ValueError):
        parse_identifier(raw)


# ------------------------------------------------------------------ dates (fail closed: latest day allowed)
def test_latest_date_precision():
    assert latest_date("2024-03-02") == date(2024, 3, 2)
    assert latest_date("2024-03-02T17:59:59Z") == date(2024, 3, 2)
    assert latest_date("2024-02") == date(2024, 2, 29)
    assert latest_date("2024") == date(2024, 12, 31)
    assert latest_date(None) is None
    assert latest_date("soon") is None
    assert latest_date("2024-13") is None


def _rec(**kw):
    base = {"source": "test", "source_id": "x", "title": "T"}
    return PaperRecord(**{**base, **kw})


def test_visibility_as_of_T():
    t = date(2024, 6, 30)
    assert visible_as_of(_rec(published="2024-06-30"), t)
    assert not visible_as_of(_rec(published="2024-07-01"), t)
    assert not visible_as_of(_rec(published="2024"), t)        # could be December: not provably before T
    assert visible_as_of(_rec(published="2023"), t)
    assert not visible_as_of(_rec(published=None), t)          # unknown date: never visible as of T
    # text revised after T may contain post-T content
    assert text_as_of(_rec(published="2024-03-02", updated="2024-06-10"), t)
    assert not text_as_of(_rec(published="2024-03-02", updated="2024-08-10"), t)
    assert not text_as_of(_rec(published="2024-08-02"), t)
    assert text_as_of(_rec(published="2024-03-02"), t)


# ------------------------------------------------------------------ fetch: retries only on transient errors
def test_transient_5xx_is_retried_with_backoff():
    tr = ScriptedTransport({URL: [ok(status=500), ok(status=503), ok('{"ok": 1}')]})
    clock = FakeClock()
    r = fetch(tr, URL, {}, sleep=clock.sleep)
    assert r.status == 200 and len(tr.calls) == 3
    assert clock.slept == [1.0, 2.0]


def test_retry_after_is_honoured_and_a_huge_one_gives_up():
    clock = FakeClock()
    tr = ScriptedTransport({URL: [ok(status=429, headers={"retry-after": "7"}), ok()]})
    assert fetch(tr, URL, {}, sleep=clock.sleep).status == 200
    assert clock.slept == [7.0]
    clock = FakeClock()
    tr = ScriptedTransport({URL: [ok(status=429, headers={"retry-after": "3600"}), ok()]})
    with pytest.raises(LiteratureError) as e:
        fetch(tr, URL, {}, sleep=clock.sleep)
    assert e.value.transient and e.value.infra and clock.slept == [] and len(tr.calls) == 1


def test_retries_are_bounded_and_end_as_transient_infra():
    tr = ScriptedTransport({URL: [ok(status=502)] * 10})
    clock = FakeClock()
    with pytest.raises(LiteratureError) as e:
        fetch(tr, URL, {}, sleep=clock.sleep, max_attempts=4)
    assert len(tr.calls) == 4 and is_transient(e.value) and infra_cause(e.value) is e.value


def test_network_errors_are_transient_and_retried():
    tr = ScriptedTransport({URL: [LiteratureError("connect failed", transient=True), ok()]})
    assert fetch(tr, URL, {}, sleep=FakeClock().sleep).status == 200 and len(tr.calls) == 2


@pytest.mark.parametrize("status,infra", [(400, False), (422, False), (401, True), (403, True)])
def test_non_transient_errors_are_not_retried(status, infra):
    tr = ScriptedTransport({URL: [ok(status=status), ok()]})
    with pytest.raises(LiteratureError) as e:
        fetch(tr, URL, {}, sleep=FakeClock().sleep)
    assert len(tr.calls) == 1 and not e.value.transient and e.value.infra is infra
    assert (infra_cause(e.value) is not None) is infra


def test_not_found_is_an_answer_not_an_error():
    tr = ScriptedTransport({URL: [ok("Resource not found.", status=404), ok()]})
    assert fetch(tr, URL, {}, sleep=FakeClock().sleep).status == 404 and len(tr.calls) == 1


def test_unscripted_request_fails_closed():
    with pytest.raises(LiteratureError):
        ScriptedTransport({}).get(URL, {})


# ------------------------------------------------------------------ pacing (per host)
def test_pacing_is_per_host():
    clock = FakeClock()
    inner = ScriptedTransport({"https://export.arxiv.org/api/query": [ok(), ok()],
                               "https://api.crossref.org/works": [ok()]})
    tr = PacedTransport(inner, Pacer({"export.arxiv.org": 3.0, "api.crossref.org": 1.0},
                                     clock=clock.now, sleep=clock.sleep))
    tr.get("https://export.arxiv.org/api/query", {"id_list": "a"})
    tr.get("https://api.crossref.org/works", {})          # other host: no wait
    assert clock.slept == []
    clock.t += 1.0
    tr.get("https://export.arxiv.org/api/query", {"id_list": "b"})
    assert clock.slept == [pytest.approx(2.0)]


def test_unknown_host_gets_a_conservative_default_interval():
    clock = FakeClock()
    p = Pacer({}, clock=clock.now, sleep=clock.sleep)
    p.wait("example.org")
    p.wait("example.org")
    assert clock.slept and clock.slept[0] >= 1.0


# ------------------------------------------------------------------ no secrets or user data on the wire
def test_live_transport_sends_no_identity_or_secrets(monkeypatch):
    for var in ("S2_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "OPENALEX_API_KEY", "CROSSREF_MAILTO", "OPENAI_API_KEY"):
        monkeypatch.setenv(var, "secret-value-123")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="{}")

    tr = HttpxTransport(mock=httpx.MockTransport(handler))
    tr.get("https://api.openalex.org/works", {"search": "sparse routing", "per-page": "5"})
    req = seen[0]
    assert dict(req.url.params) == {"search": "sparse routing", "per-page": "5"}
    sent = {k.lower(): v for k, v in req.headers.items()}
    assert sent["user-agent"] == USER_AGENT
    assert "@" not in USER_AGENT and "mailto" not in USER_AGENT.lower()
    assert not {"authorization", "x-api-key", "cookie", "api_key"} & set(sent)
    assert "secret-value-123" not in str(req.url) + json.dumps(sent)


def test_live_transport_maps_network_errors_to_transient_infra():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(LiteratureError) as e:
        HttpxTransport(mock=httpx.MockTransport(handler)).get(URL, {})
    assert e.value.transient and e.value.infra


# ------------------------------------------------------------------ snapshots: content-addressed, dated, frozen
def _store(tmp_path, when=datetime(2026, 10, 7, 12, 0, tzinfo=UTC)):
    return SnapshotStore(tmp_path / "lit", clock=lambda: when)


def test_responses_are_recorded_with_retrieval_date_and_served_from_cache(tmp_path):
    store = _store(tmp_path)
    inner = ScriptedTransport({URL: [ok('{"v": 1}')]})
    tr = RecordingTransport(inner, store)
    first = tr.get(URL, {})
    second = tr.get(URL, {})
    assert len(inner.calls) == 1                     # second answer came from the cache
    assert first.text == second.text == '{"v": 1}'
    assert second.retrieved_at == "2026-10-07T12:00:00+00:00" and second.envelope == first.envelope
    env = store.blobs.get_json(first.envelope)
    assert env["key"] == request_key(URL, {}) and env["status"] == 200


def test_transient_failures_are_never_recorded(tmp_path):
    store = _store(tmp_path)
    tr = RecordingTransport(ScriptedTransport({URL: [ok(status=503), ok()]}), store)
    tr.get(URL, {})
    assert store.latest(request_key(URL, {})) is None
    tr.get(URL, {})
    assert store.latest(request_key(URL, {})) is not None


def test_not_found_is_recorded_so_it_replays(tmp_path):
    store = _store(tmp_path)
    RecordingTransport(ScriptedTransport({URL: [ok("Resource not found.", status=404)]}), store).get(URL, {})
    snap = store.freeze(date(2026, 1, 1))
    assert OfflineTransport(store, snap).get(URL, {}).status == 404


def test_frozen_snapshot_replays_offline_and_refuses_unknown_requests(tmp_path):
    store = _store(tmp_path)
    RecordingTransport(ScriptedTransport({URL: [ok('{"v": 1}')]}), store).get(URL, {})
    snap = store.freeze(date(2024, 6, 30), label="t0")
    off = OfflineTransport(store, snap)
    assert off.offline and off.as_of == date(2024, 6, 30)
    assert off.get(URL, {}).text == '{"v": 1}'
    with pytest.raises(LiteratureError) as e:
        off.get(URL, {"rows": "5"})
    assert "not in the frozen snapshot" in str(e.value) and not e.value.transient
    # later recordings do not change an existing frozen snapshot
    RecordingTransport(ScriptedTransport({URL + "x": [ok()]}), store).get(URL + "x", {})
    with pytest.raises(LiteratureError):
        OfflineTransport(store, snap).get(URL + "x", {})


def test_snapshot_id_is_content_addressed(tmp_path):
    store = _store(tmp_path)
    RecordingTransport(ScriptedTransport({URL: [ok()]}), store).get(URL, {})
    a = store.freeze(date(2024, 6, 30))
    b = store.freeze(date(2024, 6, 30))
    assert a == b
    assert store.freeze(date(2024, 7, 1)) != a
    with pytest.raises(LiteratureError):
        OfflineTransport(store, "0" * 64)


def test_cache_poisoning_by_mismatched_key_is_detected(tmp_path):
    store = _store(tmp_path)
    other = URL.replace("001", "999")
    RecordingTransport(ScriptedTransport({other: [ok('{"poison": 1}')]}), store).get(other, {})
    poison = store.latest(request_key(other, {}))
    # point the index entry for URL at the envelope recorded for a different request
    with open(store.index_path, "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps({"key": request_key(URL, {}), "digest": poison, "retrieved_at": "x"}) + "\n")
    inner = ScriptedTransport({URL: [ok('{"real": 1}')]})
    with pytest.raises(LiteratureError) as e:
        RecordingTransport(inner, store).get(URL, {})
    assert "integrity" in str(e.value) and not e.value.transient


def test_poisoned_manifest_entry_is_detected(tmp_path):
    store = _store(tmp_path)
    other = URL.replace("001", "999")
    RecordingTransport(ScriptedTransport({other: [ok('{"poison": 1}')]}), store).get(other, {})
    manifest = {"v": 1, "kind": "literature-snapshot", "as_of": "2024-06-30", "frozen_at": "x", "label": "",
                "entries": {request_key(URL, {}): store.latest(request_key(other, {}))}}
    snap = store.blobs.put_json(manifest)
    with pytest.raises(LiteratureError) as e:
        OfflineTransport(store, snap).get(URL, {})
    assert "integrity" in str(e.value)


def test_corrupted_blob_is_detected(tmp_path):
    store = _store(tmp_path)
    RecordingTransport(ScriptedTransport({URL: [ok('{"v": 1}')]}), store).get(URL, {})
    digest = store.latest(request_key(URL, {}))
    path = store.blobs._path(digest)
    path.write_bytes(path.read_bytes().replace(b'\\"v\\": 1', b'\\"v\\": 2'))
    with pytest.raises(LiteratureError) as e:
        RecordingTransport(ScriptedTransport({}), store).get(URL, {})
    assert "integrity" in str(e.value)


def test_a_manifest_that_is_not_a_snapshot_is_refused(tmp_path):
    store = _store(tmp_path)
    bogus = store.blobs.put_json({"kind": "something-else"})
    with pytest.raises(LiteratureError):
        OfflineTransport(store, bogus)
