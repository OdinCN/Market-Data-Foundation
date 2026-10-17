"""Deterministic injected transport tests; no external services are accessed."""

import http.client
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_fomc_statement import CALENDAR, STATEMENT, URL

from market_data_foundation.historical import fomc_statement_live as live
from market_data_foundation.historical.fomc_statement_contract import (
    CALENDAR_URL,
    MAX_BODY_BYTES,
    OFFICIAL_PUBLISHED_AT,
)


class Store:
    def __init__(self):
        self.saved = []

    def retain(self, raw, artifact):
        self.saved.append((raw, artifact))
        return live.ArtifactPaths("artifacts/raw.html", "artifacts/request.json")


def run(source, store=None, **kwargs):
    return live.qualify_statement(
        request_source=source,
        artifact_store=store or Store(),
        live_opt_in=True,
        sleep=kwargs.pop("sleep", lambda _: None),
        **kwargs,
    )


def source_for(*results):
    source = Mock(side_effect=results)
    return source


def test_default_off():
    source, store = Mock(), Store()
    with pytest.raises(live.QualificationError, match="live_opt_in_required"):
        live.qualify_statement(request_source=source, artifact_store=store)
    with pytest.raises(live.QualificationError, match="live_opt_in_required"):
        live.execute_live_candidate()
    source.assert_not_called()
    assert store.saved == []


def test_two_reads_order_retention_and_publication_time():
    store = Store()
    now = datetime(2026, 10, 17, tzinfo=UTC)
    times = iter(now + timedelta(seconds=i) for i in range(4))
    source = source_for(live.Response(200, CALENDAR), live.Response(200, STATEMENT))
    candidate = run(source, store, clock=lambda: next(times))
    assert [c.args[0].url for c in source.call_args_list] == [CALENDAR_URL, URL]
    assert len(candidate.reads) == len(store.saved) == 2
    assert candidate.attempt_counts == (1, 1)
    assert candidate.discovery.statement_url == candidate.document.source_url
    assert candidate.document.official_published_at == OFFICIAL_PUBLISHED_AT
    assert candidate.reads[1].artifact.obtained_at > OFFICIAL_PUBLISHED_AT
    assert candidate.reads[0].raw_sha256 == candidate.discovery.calendar_raw_sha256
    assert candidate.reads[1].raw_sha256 == candidate.document.raw_sha256
    assert candidate.reads[1].raw_size == len(STATEMENT)


def test_each_logical_read_uses_shared_three_attempt_policy():
    source = source_for(
        TimeoutError("private"),
        live.Response(503, b""),
        live.Response(200, CALENDAR),
        OSError("private"),
        live.Response(429, b""),
        live.Response(200, STATEMENT),
    )
    sleeps = []
    candidate = run(source, sleep=sleeps.append)
    assert candidate.attempt_counts == (3, 3)
    assert sleeps == [1.0, 2.0, 1.0, 2.0]
    assert [c.args[0].url for c in source.call_args_list] == [CALENDAR_URL] * 3 + [
        URL
    ] * 3


@pytest.mark.parametrize(
    "error,count,classification",
    [
        (TimeoutError("private"), 3, "timeout"),
        (OSError("private"), 3, "transport_failure"),
        (http.client.HTTPException("private"), 1, "protocol_failure"),
        (http.client.InvalidURL("private"), 1, "request_invalid"),
        (RuntimeError("private"), 1, "unexpected_exception"),
        (live.Response(403, b"private"), 1, "http_failure"),
        (live.Response(302, b"private"), 1, "http_failure"),
        (live.Response(503, b"private"), 3, "http_failure"),
    ],
)
def test_transport_terminal_errors(error, count, classification):
    source = source_for(*([error] * count))
    with pytest.raises(live.QualificationError) as caught:
        run(source)
    assert source.call_count == count
    assert caught.value.classification == classification
    assert "private" not in repr(caught.value.observations)
    assert caught.value.retained == ()


@pytest.mark.parametrize("logical", [1, 2])
def test_retention_before_parse_and_no_requery(logical):
    store = Store()
    results = [live.Response(200, b"invalid")]
    if logical == 2:
        results.insert(0, live.Response(200, CALENDAR))
    source = source_for(*results)
    with pytest.raises(live.QualificationError) as caught:
        run(source, store)
    assert source.call_count == len(store.saved) == logical
    assert len(caught.value.retained) == logical
    assert caught.value.classification == (
        "calendar_parser_error" if logical == 1 else "statement_parser_error"
    )


@pytest.mark.parametrize("logical", [1, 2])
def test_artifact_failure_never_retries_or_advances(logical):
    store = Store()
    original = store.retain

    def retain(raw, artifact):
        if artifact.logical_read == logical:
            raise OSError("private artifact detail")
        return original(raw, artifact)

    store.retain = retain
    source = source_for(live.Response(200, CALENDAR), live.Response(200, STATEMENT))
    with pytest.raises(live.QualificationError, match="artifact_error") as caught:
        run(source, store)
    assert source.call_count == logical
    assert len(caught.value.retained) == logical - 1
    assert len(caught.value.request_artifacts) == logical


def test_atomic_store_exact_bytes_and_metadata(tmp_path):
    root = tmp_path / "artifacts" / "t07"
    store = live.AtomicArtifactStore(root)
    source = source_for(live.Response(200, CALENDAR), live.Response(200, STATEMENT))
    candidate = run(source, store)
    for retained in candidate.reads:
        assert Path(retained.paths.raw).read_bytes() == retained.raw
        metadata = json.loads(Path(retained.paths.request).read_text())
        assert metadata["raw_sha256"] == retained.raw_sha256
        assert metadata["raw_size"] == retained.raw_size
        assert metadata["request"]["url"] == retained.artifact.request.url
    assert not list(root.glob(".staging-*"))


def test_atomic_store_failure_publishes_neither(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    monkeypatch.setattr(live.os, "replace", Mock(side_effect=OSError("private")))
    source = source_for(live.Response(200, CALENDAR))
    with pytest.raises(live.QualificationError, match="artifact_error"):
        run(source, live.AtomicArtifactStore(root))
    assert list(root.iterdir()) == []


def wire(monkeypatch, body=STATEMENT, headers=None, status=200):
    response = Mock(status=status)
    response.getheaders.return_value = (
        headers
        if headers is not None
        else [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ]
    )
    offset = 0

    def read(limit):
        nonlocal offset
        chunk = body[offset : offset + limit]
        offset += len(chunk)
        return chunk

    response.read.side_effect = read
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(live.http.client, "HTTPSConnection", factory)
    return factory, connection, response


def test_concrete_https(monkeypatch):
    factory, connection, _ = wire(monkeypatch)
    response = live.ExactFomcHttpsSource()(live.Request(URL))
    assert response.body == STATEMENT
    factory.assert_called_once_with("www.federalreserve.gov", timeout=20)
    connection.request.assert_called_once_with(
        "GET",
        "/newsevents/pressreleases/monetary20260916a.htm",
        headers={
            "User-Agent": live.USER_AGENT,
            "Accept": "text/html",
            "Accept-Encoding": "identity",
        },
    )
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    "headers,body",
    [
        ([("Content-Type", "application/pdf")], b"x"),
        ([("Content-Type", "text/html"), ("content-type", "text/html")], b"x"),
        ([("Content-Type", "text/html"), ("Content-Encoding", "gzip")], b"x"),
        (
            [
                ("Content-Type", "text/html"),
                ("Content-Length", "1"),
                ("Transfer-Encoding", "chunked"),
            ],
            b"x",
        ),
        ([("Content-Type", "text/html"), ("Content-Length", "-1")], b"x"),
        ([("Content-Type", "text/html"), ("Content-Length", "2")], b"x"),
        (
            [
                ("Content-Type", "text/html"),
                ("Content-Length", str(MAX_BODY_BYTES + 1)),
            ],
            b"x",
        ),
        ([("Content-Type", "text/html"), ("Transfer-Encoding", "gzip")], b"x"),
        ([("Content-Type", "text/html\r\nx: y")], b"x"),
        ([], b"x"),
        ([("Content-Type", "text/html")], b"x" * (MAX_BODY_BYTES + 1)),
    ],
)
def test_concrete_https_strict_protocol(monkeypatch, headers, body):
    _, connection, _ = wire(monkeypatch, body, headers)
    with pytest.raises(http.client.HTTPException):
        live.ExactFomcHttpsSource()(live.Request(URL))
    connection.close.assert_called_once()


def test_concrete_redirect_never_followed(monkeypatch):
    factory, connection, response = wire(
        monkeypatch, headers=[("Location", URL)], status=302
    )
    assert live.ExactFomcHttpsSource()(live.Request(CALENDAR_URL)).status_code == 302
    assert factory.call_count == connection.request.call_count == 1
    response.read.assert_not_called()


def test_concrete_external_url_never_connects(monkeypatch):
    factory, _, _ = wire(monkeypatch)
    with pytest.raises(http.client.InvalidURL):
        live.ExactFomcHttpsSource()(live.Request("https://evil.example/"))
    factory.assert_not_called()
