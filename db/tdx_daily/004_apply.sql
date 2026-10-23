WITH inserted AS (
    INSERT INTO mdf_tdx_daily.bars_daily_current
    SELECT market,instrument,period,trading_day,basis,
        open,high,low,close,volume,amount,units,value_digest,
        %(source)s,%(priority)s,received_at,%(admitted)s,%(batch)s
    FROM pg_temp.tdx_daily_delta WHERE disposition='NEW'
    RETURNING 1
), changed AS (
    UPDATE mdf_tdx_daily.bars_daily_current c SET
        open=d.open, high=d.high, low=d.low, close=d.close,
        volume=d.volume, amount=d.amount, value_digest=d.value_digest,
        value_source=%(source)s, source_priority=%(priority)s,
        source_observed_at=d.received_at, value_changed_at=%(admitted)s,
        batch_id=%(batch)s
    FROM pg_temp.tdx_daily_delta d
    WHERE d.disposition='CHANGED' AND c.market=d.market
        AND c.instrument=d.instrument AND c.period=d.period
        AND c.trading_day=d.trading_day AND c.basis=d.basis
    RETURNING d.*
), logged AS (
    INSERT INTO mdf_tdx_daily.bars_daily_corrections
    SELECT market,instrument,period,trading_day,basis,%(source)s,%(batch)s,
        ARRAY[old_open,old_high,old_low,old_close,old_volume,old_amount],
        ARRAY[open,high,low,close,volume,amount],old_digest,value_digest,
        old_source,old_batch_id,old_source_observed_at,old_value_changed_at,
        received_at,%(admitted)s,'vendor_correction_or_unknown'
    FROM changed RETURNING 1
)
SELECT (SELECT count(*) FROM inserted), (SELECT count(*) FROM changed),
    (SELECT count(*) FROM logged);
