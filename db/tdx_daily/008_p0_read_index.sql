-- Narrow index for published key summaries without scanning wide native facts.
CREATE INDEX p0_visible_keys_idx ON mdf_tdx_daily.bars_daily_current
    (market,instrument,trading_day)
    INCLUDE (value_source,batch_id,source_observed_at);
