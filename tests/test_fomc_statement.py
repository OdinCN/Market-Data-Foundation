"""Synthetic official-shaped HTML; these fixtures are not live-source evidence."""

from hashlib import sha256

import pytest

from market_data_foundation.historical.fomc_statement import (
    DocumentError,
    parse_calendar,
    parse_statement,
    validate_statement_url,
)
from market_data_foundation.historical.fomc_statement_contract import (
    EXTRACTION_REVISION,
    OFFICIAL_PUBLISHED_AT,
)

URL = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm"
ROW = b"""<div class="fomc-meeting"><div class="fomc-meeting__month">September</div>
<div class="fomc-meeting__date">15-16*</div>
<div class="col-xs-12 col-md-4 col-lg-2"><strong>Statement:</strong>
<a href="/statement.pdf">PDF</a> |
<a href="/newsevents/pressreleases/monetary20260916a.htm">HTML</a>
<a href="/newsevents/pressreleases/monetary20260916a1.htm">Implementation Note</a>
</div>
<p>Minutes: <a href="/minutes.htm">HTML</a></p></div>"""
CALENDAR = b'<div class="panel"><h4>2026 FOMC Meetings</h4>' + ROW + b"</div>"
STATEMENT = b"""<div id="article"><p class="article__time">September 16, 2026</p>
<h3>Federal Reserve issues FOMC statement</h3><p>For release at 2:00 p.m. EDT</p>
<div class="col-xs-12 col-sm-8 col-md-8"><p>Economic activity has &nbsp; expanded.</p>
<p>The Committee seeks <em>maximum employment</em> &amp; stable prices.</p>
<p>Voting for the FOMC monetary policy action were A and B.</p>
<p>For media inquiries, call somebody.</p><p>Implementation Note: excluded</p></div>
<nav>Navigation excluded</nav></div>
<div id="lastUpdate">Last Update: September 16, 2026</div>"""


def test_discovery_and_exact_document():
    discovery = parse_calendar(CALENDAR)
    assert discovery.statement_url == URL
    assert discovery.calendar_raw_sha256 == sha256(CALENDAR).hexdigest()
    assert discovery.calendar_raw_size == len(CALENDAR)
    document = parse_statement(STATEMENT, URL)
    assert document.text == (
        "Economic activity has expanded.\n\n"
        "The Committee seeks maximum employment & stable prices.\n\n"
        "Voting for the FOMC monetary policy action were A and B."
    )
    assert document.official_published_at == OFFICIAL_PUBLISHED_AT
    assert document.extraction_revision == EXTRACTION_REVISION
    assert document.raw_sha256 == sha256(STATEMENT).hexdigest()
    assert document.text_sha256 == sha256(document.text.encode()).hexdigest()
    assert document.raw_sha256 != document.text_sha256
    assert document.last_update == "Last Update: September 16, 2026"
    assert not hasattr(document, "provider_revision_number")


def test_replay_and_wrapper_change():
    a = parse_statement(STATEMENT, URL)
    b = parse_statement(STATEMENT + b"<!-- wrapper change -->", URL)
    assert a == parse_statement(STATEMENT, URL)
    assert a.raw_sha256 != b.raw_sha256
    assert a.text_sha256 == b.text_sha256
    assert (
        parse_statement(STATEMENT.split(b'<div id="lastUpdate">')[0], URL).last_update
        is None
    )


@pytest.mark.parametrize(
    "old,new",
    [
        (b"2026 FOMC", b"2025 FOMC"),
        (b"September", b"July"),
        (b"15-16*", b"16-17"),
        (b"<strong>Statement:</strong>", b"<strong>Minutes:</strong>"),
        (b">HTML<", b">PDF<"),
        (
            b"/newsevents/pressreleases/monetary20260916a.htm",
            b"https://evil.example/a.htm",
        ),
        (b"monetary20260916a.htm", b"monetary20260916a.pdf"),
        (b"monetary20260916a.htm", b"monetary20260916a1.htm"),
    ],
)
def test_calendar_rejects_wrong_scope(old, new):
    with pytest.raises(DocumentError):
        parse_calendar(CALENDAR.replace(old, new))


@pytest.mark.parametrize(
    "raw",
    [
        CALENDAR.replace(ROW, ROW + ROW),
        CALENDAR + CALENDAR,
        CALENDAR.replace(b">HTML</a>", b'>HTML</a><a href="/x.htm">HTML</a>'),
        b"",
        b"\xff",
        b"<div>",
    ],
)
def test_calendar_rejects_ambiguity_and_invalid_html(raw):
    with pytest.raises(DocumentError):
        parse_calendar(raw)


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("https:", "http:"),
        URL.replace("www.", ""),
        URL.replace("www.federalreserve.gov", "www.federalreserve.gov.evil.example"),
        URL.replace("www.federalreserve.gov", "user@www.federalreserve.gov"),
        URL.replace("www.federalreserve.gov", "www.federalreserve.gov:443"),
        URL + "?download=1",
        URL + "#x",
        URL.replace("a.htm", "a.pdf"),
        URL.replace("monetary", "fomcminutes"),
        URL.replace("a.htm", "a1.htm"),
        URL.replace("monetary", "implementation"),
        URL.replace("monetary", "sep"),
    ],
)
def test_exact_url(url):
    with pytest.raises(DocumentError):
        validate_statement_url(url)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw.replace(
            b"<strong>Statement:</strong>",
            b"<strong hidden>Statement:</strong>",
        ),
        lambda raw: raw.replace(
            b'<div class="col-xs-12 col-md-4 col-lg-2">',
            b'<div class="col-xs-12 col-md-4 col-lg-2" hidden>',
            1,
        ),
        lambda raw: raw.replace(
            b">HTML</a>",
            b"><span hidden>HTML</span></a>",
            1,
        ),
        lambda raw: raw.replace(
            b"<strong>Statement:</strong>",
            b"<strong>Minutes:</strong>"
            b"<div hidden><strong>Statement:</strong>"
            b'<a href="/newsevents/pressreleases/monetary20260916a.htm">'
            b"HTML</a></div>",
        ),
        lambda raw: raw.replace(
            b"<strong>Statement:</strong>",
            b"<strong>Statement:</strong><strong hidden>Statement:</strong>",
        ),
    ],
)
def test_calendar_hidden_or_nested_discovery_evidence_rejected(mutation):
    with pytest.raises(DocumentError):
        parse_calendar(mutation(CALENDAR))


@pytest.mark.parametrize(
    "old,new",
    [
        (b"issues FOMC statement", b"issues FOMC minutes"),
        (b"September 16, 2026", b"September 15, 2026"),
        (b"2:00 p.m. EDT", b"2:00 p.m. EST"),
        (b"2:00 p.m. EDT", b"3:00 p.m. EDT"),
        (b"Economic activity", b"Implementation Note"),
        (b"Voting for the FOMC monetary policy action", b"Other material"),
        (b"For media inquiries", b"Unrecognized footer"),
        (b'id="article"', b'id="other"'),
        (b"col-md-8", b"col-md-9"),
    ],
)
def test_statement_rejects_metadata_or_anchor_changes(old, new):
    with pytest.raises(DocumentError):
        parse_statement(STATEMENT.replace(old, new), URL)


@pytest.mark.parametrize("tag", ["nav", "footer", "aside", "header", "template"])
def test_statement_rejects_paragraph_with_noncontent_ancestor(tag):
    injected = f"<{tag}><div><p>Untrusted content</p></div></{tag}>".encode()
    raw = STATEMENT.replace(
        b"<p>For media inquiries", injected + b"<p>For media inquiries"
    )
    with pytest.raises(DocumentError, match="statement_body_contaminated"):
        parse_statement(raw, URL)


POLICY = b"<p>The Committee seeks <em>maximum employment</em> &amp; stable prices.</p>"
METADATA = (
    b"<h3>Federal Reserve issues FOMC statement</h3>"
    b'<p class="article__time">September 16, 2026</p>'
    b"<p>For release at 2:00 p.m. EDT</p>"
)
POLLUTED_STATEMENTS = [
    STATEMENT.replace(POLICY, b"<h4>Implementation Note</h4>" + POLICY),
    STATEMENT.replace(
        POLICY, POLICY.replace(b"</p>", b"<template>hidden prose</template></p>")
    ),
    STATEMENT.replace(
        POLICY, POLICY.replace(b"<p>", b"<div>").replace(b"</p>", b"</div>")
    ),
    STATEMENT.replace(POLICY, b"Unwrapped policy prose" + POLICY),
    STATEMENT.replace(POLICY, b"<section>" + POLICY + b"</section>"),
    STATEMENT.replace(b"FOMC statement</h3>", b"Minutes</h3>")
    .replace(b"September 16, 2026</p>", b"July 29, 2026</p>")
    .replace(b"2:00 p.m.", b"3:00 p.m.")
    .replace(
        b'<div id="article">',
        b'<div id="article"><template>' + METADATA + b"</template>",
    ),
    STATEMENT.replace(b"<h3>", b'<p class="article__time">July 29, 2026</p><h3>'),
]


@pytest.mark.parametrize("raw", POLLUTED_STATEMENTS)
def test_review_full_dom_and_metadata_counterexamples(raw):
    with pytest.raises(DocumentError):
        parse_statement(raw, URL)


@pytest.mark.parametrize(
    "tag", ["script", "style", "nav", "footer", "template", "noscript"]
)
def test_inert_content_inside_policy_rejected(tag):
    raw = STATEMENT.replace(
        POLICY, POLICY.replace(b"</p>", f"<{tag}>hidden prose</{tag}></p>".encode())
    )
    with pytest.raises(DocumentError):
        parse_statement(raw, URL)


@pytest.mark.parametrize(
    "attrs",
    [
        "hidden",
        'aria-hidden="true"',
        'style="display: none"',
        'style="visibility:hidden"',
    ],
)
def test_hidden_policy_rejected(attrs):
    with pytest.raises(DocumentError):
        parse_statement(
            STATEMENT.replace(POLICY, POLICY.replace(b"<p>", f"<p {attrs}>".encode())),
            URL,
        )


@pytest.mark.parametrize(
    "tag", ["template", "script", "style", "nav", "footer", "noscript"]
)
@pytest.mark.parametrize(
    "field",
    [
        b"<h3>Federal Reserve issues FOMC statement</h3>",
        b'<p class="article__time">September 16, 2026</p>',
        b"<p>For release at 2:00 p.m. EDT</p>",
    ],
)
def test_inert_metadata_cannot_supply_expected_value(tag, field):
    raw = STATEMENT.replace(field, f"<{tag}>".encode() + field + f"</{tag}>".encode())
    with pytest.raises(DocumentError):
        parse_statement(raw, URL)


@pytest.mark.parametrize(
    "field",
    [
        b"<h3>Minutes</h3>",
        b'<p class="article__time">July 29, 2026</p>',
        b"<p>For release at 3:00 p.m. EDT</p>",
        METADATA,
    ],
)
def test_visible_metadata_conflicts_and_duplicates_rejected(field):
    with pytest.raises(DocumentError):
        parse_statement(
            STATEMENT.replace(b'<div id="article">', b'<div id="article">' + field), URL
        )


def test_ordered_grid_wrapper_and_nonparagraph_media_terminator():
    raw = STATEMENT.replace(
        POLICY,
        b'<div class="row"><div class="col-xs-12 col-sm-12 col-md-12">'
        + POLICY
        + b"</div></div>",
    )
    raw = raw.replace(
        b"<p>For media inquiries, call somebody.</p>", b"<h4>Media Contacts</h4>"
    )
    assert parse_statement(raw, URL).text == parse_statement(STATEMENT, URL).text


@pytest.mark.parametrize(
    "attrs", ["hidden", 'aria-hidden="true"', 'style="display:none"']
)
@pytest.mark.parametrize(
    "field",
    [
        b"<h3>Federal Reserve issues FOMC statement</h3>",
        b'<p class="article__time">September 16, 2026</p>',
        b"<p>For release at 2:00 p.m. EDT</p>",
    ],
)
def test_hidden_metadata_cannot_supply_expected_value(attrs, field):
    raw = STATEMENT.replace(field, f"<div {attrs}>".encode() + field + b"</div>")
    with pytest.raises(DocumentError):
        parse_statement(raw, URL)


def test_css_comment_cannot_hide_policy_content():
    raw = STATEMENT.replace(
        POLICY,
        POLICY.replace(b"<p>", b'<p style="display:/**/none">'),
    )
    with pytest.raises(DocumentError, match="html_style_unsupported"):
        parse_statement(raw, URL)


def test_hidden_child_cannot_mask_visible_release_conflict():
    injected = b"<p>For release at 3:00 p.m. EDT<span hidden>For release</span></p>"
    raw = STATEMENT.replace(
        b'<div id="article">',
        b'<div id="article">' + injected,
    )
    with pytest.raises(DocumentError, match="statement_release_invalid"):
        parse_statement(raw, URL)
