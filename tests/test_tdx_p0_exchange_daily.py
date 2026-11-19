import httpx
import pytest

from market_data_foundation.historical import tdx_p0_exchange_daily as exchange


@pytest.mark.parametrize(
    ("url", "location", "final_status", "requests"),
    [
        (exchange.BSE_CENSUS_URL, exchange.BSE_CENSUS_URL, 200, 2),
        (exchange.BSE_CENSUS_URL, exchange.BSE_CENSUS_URL, 307, 2),
        (exchange.BSE_CENSUS_URL, "https://example.org/", 200, 1),
        (exchange.SZ, exchange.SZ, 200, 1),
    ],
)
def test_only_follows_one_bse_post_redirect_to_same_url(
    tmp_path, monkeypatch, url, location, final_status, requests
):
    monkeypatch.setattr(exchange, "sleep", lambda _: None)
    payload = {"page": "0", "typejb": "T"}
    body = b'null([{"number": 0}])'
    calls = []

    def send(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                307,
                headers={
                    "Location": location,
                    "Set-Cookie": "test_session=present; Path=/; Secure",
                },
            )
        assert request.method == calls[0].method == "POST"
        assert request.url == calls[0].url
        assert request.content == calls[0].content
        assert request.headers["cookie"] == "test_session=present"
        return httpx.Response(
            final_status, content=body, headers={"Location": location}
        )

    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        fetcher = exchange.ExchangeOriginals(tmp_path, client)
        if requests == 2 and final_status == 200:
            result = fetcher.get("bj_current_page_0", url, payload, method="POST")
            assert result["redirect_status"] == 307
            assert exchange.read_proof(result) == body
        else:
            with pytest.raises(httpx.HTTPStatusError) as error:
                fetcher.get("bj_current_page_0", url, payload, method="POST")
            assert error.value.response.status_code == 307
            assert not (tmp_path / "bj_current_page_0").exists()

    assert len(calls) == requests
