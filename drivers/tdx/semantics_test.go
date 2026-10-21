package main

import (
	"encoding/binary"
	"encoding/json"
	"math"
	"os"
	"testing"
	"time"

	"github.com/injoyai/tdx"
	"github.com/injoyai/tdx/protocol"
)

// These are invented wire records. The legacy UTC defect is asserted as a
// rollback boundary, never relabelled as corrected historical data.
func TestPinnedTradeTimezoneAndCrossDayMinutes(t *testing.T) {
	legacy := os.Getenv("MDF_TDX_TEST_SDK_VERSION") == "v0.0.87"
	shanghai, err := time.LoadLocation("Asia/Shanghai")
	if err != nil {
		t.Fatal(err)
	}
	original := time.Local
	defer func() { time.Local = original }()
	for _, hostZone := range []string{"UTC", "America/Los_Angeles", "Asia/Shanghai"} {
		t.Run(hostZone, func(t *testing.T) {
			time.Local, err = time.LoadLocation(hostZone)
			if err != nil {
				t.Fatal(err)
			}
			for _, historical := range []bool{false, true} {
				var trades protocol.Trades
				for _, date := range []string{"20261012", "20261009"} {
					wire := []byte{1, 0, 0x6e, 2, 1, 2, 1, 0, 0}
					cache := protocol.TradeCache{Date: date, Code: "sh600519"}
					var resp *protocol.TradeResp
					if historical {
						wire = []byte{1, 0, 0, 0, 0, 0, 0x6e, 2, 1, 2, 0, 0}
						resp, err = protocol.MHistoryTrade.Decode(wire, cache)
					} else {
						resp, err = protocol.MTrade.Decode(wire, cache)
					}
					if err != nil || len(resp.List) != 1 {
						t.Fatalf("decode historical=%v: %v", historical, err)
					}
					trade := resp.List[0]
					loc := shanghai
					if legacy {
						loc = time.UTC
					}
					want, _ := time.ParseInLocation("2006010215:04", date+"10:22", loc)
					encoded, _ := json.Marshal(trade.Time)
					if !trade.Time.Equal(want) || string(encoded) != `"`+want.Format(time.RFC3339)+`"` || trade.Price != 10 || trade.Volume != 2 || trade.Amount() != 2000 {
						t.Fatalf("timestamp/units historical=%v: %s %+v", historical, encoded, trade)
					}
					trades = append(trades, trade)
				}
				bars := trades.Klines()
				if len(bars) != 482 {
					t.Fatalf("cross-day count: %d", len(bars))
				}
				for dayIndex, day := range []int{9, 12} {
					loc := shanghai
					barDate := time.Date(2026, 10, day, 0, 0, 0, 0, loc)
					if legacy {
						loc = time.Local
						barDate = time.Date(2026, 10, day, 0, 0, 0, 0, time.UTC).In(loc)
					}
					for j, bar := range bars[dayIndex*241 : (dayIndex+1)*241] {
						_, wantOffset := barDate.Zone()
						_, gotOffset := bar.Time.Zone()
						if bar.Time.Day() != barDate.Day() || gotOffset != wantOffset {
							t.Fatalf("day/offset drift: %s", bar.Time)
						}
						if j == 0 && !bar.Time.Equal(time.Date(2026, 10, barDate.Day(), 9, 30, 0, 0, loc)) || j == 240 && !bar.Time.Equal(time.Date(2026, 10, barDate.Day(), 15, 0, 0, 0, loc)) {
							t.Fatalf("session boundary: %s", bar.Time)
						}
					}
					bar := bars[dayIndex*241+53]
					if bar.Time.Hour() != 10 || bar.Time.Minute() != 23 || bar.Volume != 2 || bar.Close != 10 || bar.Amount != 2000 {
						t.Fatalf("10:23 aggregation lost: %+v", bar)
					}
				}
			}
		})
	}
}

func TestPinnedETFIndexMetadataAndUnits(t *testing.T) {
	for _, tc := range []struct {
		code    string
		kind    string
		decimal int8
		price   protocol.Price
	}{
		{"sh510300", "", 3, 1}, {"sz159915", "", 3, 1},
		{"sh000001", protocol.KindIndex, 2, 10}, {"sz399001", protocol.KindIndex, 2, 10},
	} {
		t.Run(tc.code, func(t *testing.T) {
			wire := make([]byte, 31)
			wire[0] = 1
			copy(wire[2:8], tc.code[2:])
			binary.LittleEndian.PutUint16(wire[8:10], 100)
			copy(wire[10:18], "MOCK")
			wire[22] = byte(tc.decimal)
			binary.LittleEndian.PutUint32(wire[23:27], math.Float32bits(1.25))
			metadata, err := protocol.MCode.Decode(wire)
			if err != nil || len(metadata.List) != 1 {
				t.Fatalf("metadata: %v", err)
			}
			m := metadata.List[0]
			if m.Code != tc.code[2:] || m.Multiple != 100 || m.Decimal != tc.decimal || m.LastPrice != 1.25 || m.Name != "MOCK" {
				t.Fatalf("metadata drift: %+v", m)
			}
			if protocol.IsETF(tc.code) != (tc.kind == "") || protocol.IsIndex(tc.code) != (tc.kind == protocol.KindIndex) {
				t.Fatal("security kind drift")
			}
			trade, err := protocol.MTrade.Decode([]byte{1, 0, 0x6e, 2, 1, 2, 1, 0, 0}, protocol.TradeCache{Date: "20261009", Code: tc.code})
			if err != nil || trade.List[0].Price != tc.price || trade.List[0].Volume != 2 {
				t.Fatalf("trade price/volume drift: %v", err)
			}
			model := &tdx.CodeModel{Decimal: tc.decimal}
			if got := model.Price(1000); got != tc.price*100 {
				t.Fatalf("metadata decimal conversion: %d", got)
			}
			for _, minute := range []bool{false, true} {
				body := make([]byte, 18)
				body[0] = 1
				kind := uint8(protocol.TypeKlineDay)
				binary.LittleEndian.PutUint32(body[2:6], 20261009)
				wantVolume := int64(200)
				if minute {
					kind = protocol.TypeKlineMinute
					binary.LittleEndian.PutUint16(body[2:4], uint16((2026-2004)<<11|1009))
					binary.LittleEndian.PutUint16(body[4:6], 622)
					wantVolume /= 100
				}
				body[6] = 1
				binary.LittleEndian.PutUint32(body[10:14], math.Float32bits(200))
				binary.LittleEndian.PutUint32(body[14:18], math.Float32bits(1.25))
				if tc.kind == protocol.KindIndex {
					body = append(body, 3, 0, 4, 0)
					wantVolume *= 100
				}
				resp, err := protocol.MKline.Decode(body, protocol.KlineCache{Type: kind, Kind: tc.kind, Code: tc.code})
				if err != nil || len(resp.List) != 1 {
					t.Fatalf("Kline: %v", err)
				}
				k := resp.List[0]
				if k.Volume != wantVolume || k.Amount != 1250 || k.Close != 1 || tc.kind == protocol.KindIndex && (k.UpCount != 3 || k.DownCount != 4) {
					t.Fatalf("Kline minute=%v: %+v", minute, k)
				}
			}
		})
	}
}
