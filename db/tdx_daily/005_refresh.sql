INSERT INTO mdf_tdx_daily.source_refresh_batches
SELECT %(source)s,%(batch)s,market,instrument,period,basis,%(digest)s,
    %(priority)s,received_at,%(admitted)s,
    COALESCE(array_agg(trading_day ORDER BY trading_day)
        FILTER (WHERE disposition <> 'BLOCKED'), '{}'::date[]),
    %(evidence)s::jsonb -> (market::text || ':' || instrument),
    'complete_bounded_quality_checked',
    count(*) FILTER (WHERE disposition='NEW'),
    count(*) FILTER (WHERE disposition='UNCHANGED'),
    count(*) FILTER (WHERE disposition='CHANGED'),
    count(*) FILTER (WHERE disposition='BLOCKED')
FROM pg_temp.tdx_daily_delta GROUP BY market,instrument,period,basis,received_at;
