ALTER TABLE {s}.batches DROP CONSTRAINT batches_origin_check;
ALTER TABLE {s}.batches ADD CONSTRAINT batches_origin_check CHECK(origin IN ('fred','controlled_fixture','federal_reserve_board','new_york_fed','us_treasury','mdf_derived'));
