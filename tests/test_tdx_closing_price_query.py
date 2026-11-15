from datetime import date, datetime

import pytest

from market_data_foundation.web.tdx_daily_query import DailyQuery


@pytest.mark.parametrize(
    ("clock", "unfinished"),
    [("15:00", True), ("15:05", True), ("15:29:59", True), ("15:30", False)],
)
def test_trade_date_bar_completeness_in_closing_price_session(clock, unfinished):
    observed = datetime.fromisoformat(f"2026-11-16T{clock}+08:00")
    row = DailyQuery.present({"trading_day": observed.date()}, observed, temporary=True)
    assert row["is_complete"] is not unfinished
    assert row["persisted"] is False


def test_dated_suspension_blocks_source_only_inside_its_interval():
    class Store:
        def query(self, **scope):
            return {"rows": [], "total": 0}

        def universe(self, **scope):
            return {
                "members": [{"market": 1, "instrument": "688001"}],
                "details": {
                    "suspensions": [
                        {
                            "market": 1,
                            "instrument": "688001",
                            "start": "2026-11-13",
                            "end": "2026-11-16",
                            "available_at": "2026-11-13T09:00:00+08:00",
                        }
                    ]
                },
            }

    def forbidden():
        pytest.fail("a confirmed suspension requested live bars")

    query = DailyQuery(
        Store(),
        forbidden,
        allow_source=True,
        clock=lambda: datetime.fromisoformat("2026-11-16T15:20:00+08:00"),
    )
    day = date(2026, 11, 16)
    result = query.query(start=day, end=day, market=1, instrument="688001")
    assert result["rows"] == []
    assert result["provider_requests"] == 0
