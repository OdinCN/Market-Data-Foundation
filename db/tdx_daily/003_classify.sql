-- History is read once for the staged scope/date SET, never per-bar ORDER/LIMIT.
CREATE TEMP TABLE tdx_daily_delta ON COMMIT DROP AS
WITH unique_rows AS MATERIALIZED (
    SELECT DISTINCT * FROM pg_temp.tdx_daily_stage
), staged_scopes AS MATERIALIZED (
    SELECT market,instrument,period,basis,
        array_agg(trading_day ORDER BY trading_day) AS trading_days
    FROM unique_rows GROUP BY market,instrument,period,basis
), scoped_refresh AS MATERIALIZED (
    SELECT r.* FROM mdf_tdx_daily.source_refresh_batches r
    JOIN staged_scopes s USING (market,instrument,period,basis)
    WHERE r.confirmed_days && s.trading_days
), scoped_current AS MATERIALIZED (
    SELECT c.* FROM mdf_tdx_daily.bars_daily_current c
    JOIN unique_rows s USING (market,instrument,period,trading_day,basis)
), authority_candidates AS (
    -- Expand each matching receipt once (at most 800 days per receipt).
    SELECT r.market,r.instrument,r.period,d.trading_day,r.basis,
        r.source,r.source_priority,r.last_confirmed_at
    FROM scoped_refresh r CROSS JOIN unnest(r.confirmed_days) AS d(trading_day)
    JOIN unique_rows k ON k.market=r.market AND k.instrument=r.instrument
        AND k.period=r.period AND k.basis=r.basis AND k.trading_day=d.trading_day
    UNION ALL
    SELECT market,instrument,period,trading_day,basis,
        value_source,source_priority,source_observed_at
    FROM scoped_current
), source_clocks AS (
    SELECT market,instrument,period,trading_day,basis,source,source_priority,
        max(last_confirmed_at) AS last_confirmed_at
    FROM authority_candidates
    GROUP BY market,instrument,period,trading_day,basis,source,source_priority
), authority AS MATERIALIZED (
    -- Exactly one winner per key: no history join multiplicity at UPDATE time.
    SELECT DISTINCT ON (market,instrument,period,trading_day,basis) *
    FROM source_clocks
    ORDER BY market,instrument,period,trading_day,basis,
        source_priority DESC,last_confirmed_at DESC,source COLLATE "C"
)
SELECT s.*, c.open AS old_open, c.high AS old_high, c.low AS old_low,
    c.close AS old_close, c.volume AS old_volume, c.amount AS old_amount,
    c.value_digest AS old_digest, c.value_source AS old_source,
    c.batch_id AS old_batch_id, c.source_observed_at AS old_source_observed_at,
    c.value_changed_at AS old_value_changed_at,
    CASE WHEN c.instrument IS NULL THEN 'NEW'
         WHEN ROW(c.open,c.high,c.low,c.close,c.volume,c.amount,c.units)
            = ROW(s.open,s.high,s.low,s.close,s.volume,s.amount,s.units)
            THEN 'UNCHANGED'
         WHEN %(priority)s > a.source_priority
            OR (%(priority)s = a.source_priority AND %(source)s = a.source
                AND s.received_at > a.last_confirmed_at)
            THEN 'CHANGED'
         ELSE 'BLOCKED' END AS disposition
FROM unique_rows s
LEFT JOIN scoped_current c USING (market,instrument,period,trading_day,basis)
LEFT JOIN authority a USING (market,instrument,period,trading_day,basis);
