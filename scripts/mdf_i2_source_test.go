package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"io"
	"net"
	"testing"
	"time"

	"github.com/injoyai/tdx/protocol"
)

func testPlan() plan {
	return plan{Endpoint: "124.71.187.122:7709", Requests: []request{{Operation: "GetCount", Market: 1}}, MaxRequestBytes: 64, MaxResponseBytes: 1000, WallSeconds: 1, Deadline: time.Now().Add(time.Minute).UTC().Format(time.RFC3339Nano)}
}
func TestBudgets(t *testing.T) {
	p := testPlan()
	if !valid(p) {
		t.Fatal("valid rejected")
	}
	for _, mutate := range []func(*plan){
		func(p *plan) { p.Requests[0].Market = 2 }, func(p *plan) { p.WallSeconds = 121 },
		func(p *plan) { p.MaxRequestBytes = 30 }, func(p *plan) { p.Requests[0].Operation = "GetCodeAll" },
		func(p *plan) { p.Endpoint = "example.org:7709" },
		func(p *plan) { p.Endpoint = "203.0.113.1:7709" },
	} {
		p = testPlan()
		mutate(&p)
		if valid(p) {
			t.Fatal("unsafe accepted")
		}
	}
}
func reply(c net.Conn, payload []byte) {
	h := make([]byte, 12)
	io.ReadFull(c, h)
	n := int(binary.LittleEndian.Uint16(h[6:8])) - 2
	io.CopyN(io.Discard, c, int64(n))
	r := make([]byte, 16+len(payload))
	copy(r[:4], []byte{0xb1, 0xcb, 0x74, 0x00})
	r[4] = 0x1c
	copy(r[5:9], h[1:5])
	copy(r[10:12], h[10:12])
	binary.LittleEndian.PutUint16(r[12:14], uint16(len(payload)))
	binary.LittleEndian.PutUint16(r[14:16], uint16(len(payload)))
	copy(r[16:], payload)
	c.Write(r)
}
func TestFiniteRetained(t *testing.T) {
	a, b := net.Pipe()
	defer a.Close()
	go func() { defer b.Close(); reply(b, []byte{0}); reply(b, []byte{3, 0}) }()
	var out bytes.Buffer
	status := run(testPlan(), func(context.Context, string, string) (net.Conn, error) { return a, nil }, &out)
	if status != 0 {
		t.Fatal(out.String())
	}
	d := json.NewDecoder(&out)
	var rows []result
	for {
		var row result
		if d.Decode(&row) != nil {
			break
		}
		rows = append(rows, row)
	}
	if len(rows) != 2 || string(rows[1].Payload) != `{"Count":3}` || len(rows[1].RequestWire) != 18 || len(rows[1].ResponseWire) != 18 {
		t.Fatal(rows)
	}
}
func TestPartialRetainedAndNoRetry(t *testing.T) {
	a, b := net.Pipe()
	defer a.Close()
	go func() { h := make([]byte, 13); io.ReadFull(b, h); b.Write([]byte{1, 2, 3}); b.Close() }()
	calls := 0
	var out bytes.Buffer
	status := run(testPlan(), func(context.Context, string, string) (net.Conn, error) { calls++; return a, nil }, &out)
	var row result
	json.Unmarshal(bytes.TrimSpace(out.Bytes()), &row)
	if status != 1 || calls != 1 || !bytes.Equal(row.ResponseWire, []byte{1, 2, 3}) || row.Classification != "truncated_response" {
		t.Fatal(out.String())
	}
}
func TestDecodePanicClassified(t *testing.T) {
	_, err := decode(request{Operation: "GetKline"}, []byte{1, 0})
	if err == nil {
		t.Fatal("malformed accepted")
	}
}
func TestHeaderBudget(t *testing.T) {
	a, b := net.Pipe()
	defer a.Close()
	defer b.Close()
	go reply(b, make([]byte, 100))
	left := 16
	_, raw, _, err := exchange(a, protocol.MConnect.Frame(), &left)
	if err == nil || len(raw) != 16 || left != 0 {
		t.Fatal("header not bounded")
	}
}

// A scripted in-memory transport counts every attempted write without a socket.
type scriptedConn struct {
	bytes.Buffer
	writes    [][]byte
	deadlines []time.Time
}

func (c *scriptedConn) Write(p []byte) (int, error) {
	c.writes = append(c.writes, append([]byte(nil), p...))
	return len(p), nil
}
func (c *scriptedConn) Close() error         { return nil }
func (c *scriptedConn) LocalAddr() net.Addr  { return &net.TCPAddr{} }
func (c *scriptedConn) RemoteAddr() net.Addr { return &net.TCPAddr{} }
func (c *scriptedConn) SetDeadline(t time.Time) error {
	c.deadlines = append(c.deadlines, t)
	return nil
}
func (c *scriptedConn) SetReadDeadline(t time.Time) error  { return nil }
func (c *scriptedConn) SetWriteDeadline(t time.Time) error { return nil }
func response(f *protocol.Frame, payload []byte) []byte {
	raw := make([]byte, 16+len(payload))
	binary.BigEndian.PutUint32(raw[:4], protocol.PrefixResp)
	raw[4] = 0x1c
	binary.LittleEndian.PutUint32(raw[5:9], f.MsgID)
	binary.LittleEndian.PutUint16(raw[10:12], f.Type)
	binary.LittleEndian.PutUint16(raw[12:14], uint16(len(payload)))
	binary.LittleEndian.PutUint16(raw[14:16], uint16(len(payload)))
	copy(raw[16:], payload)
	return raw
}
func TestNoWriteWithoutRemainingResponseHeaderBudget(t *testing.T) {
	for budget := 0; budget < 16; budget++ {
		c := &scriptedConn{}
		left := budget
		sent, raw, _, err := exchange(c, protocol.MConnect.Frame(), &left)
		if err == nil || len(c.writes) != 0 || len(sent) != 0 || len(raw) != 0 || left != budget {
			t.Fatalf("budget %d sent request", budget)
		}
		p := testPlan()
		p.MaxResponseBytes = 17 + budget
		f := protocol.MConnect.Frame()
		f.MsgID = 1
		c.Buffer.Write(response(f, []byte{0}))
		var out bytes.Buffer
		if run(p, func(context.Context, string, string) (net.Conn, error) { return c, nil }, &out) != 1 || len(c.writes) != 1 {
			t.Fatalf("budget %d advanced after exhaustion: %s", budget, out.String())
		}
	}
}
func TestReturnedItemBudgetsStopBeforeNextRequest(t *testing.T) {
	for _, r := range []request{
		{Operation: "GetCode", Market: 1, Count: 1000},
		{Operation: "GetKline", Market: 1, Code: "600001", Count: 1},
	} {
		// Complete structurally valid rows; rejection must be the count bound.
		size := 29
		expected := "directory_length_or_item_budget"
		if r.Operation == "GetKline" {
			size = 16
			expected = "kline_item_budget"
		}
		payload := make([]byte, 2+(r.Count+1)*size)
		binary.LittleEndian.PutUint16(payload[:2], uint16(r.Count+1))
		if r.Operation == "GetKline" {
			for i := 0; i <= r.Count; i++ {
				binary.LittleEndian.PutUint32(payload[2+i*size:6+i*size], 20260101)
			}
		}
		if _, err := decode(r, payload); err == nil || err.Error() != expected {
			t.Fatal("count not bounded", err)
		}
		p := testPlan()
		p.Requests = []request{r, {Operation: "GetCount", Market: 1}}
		p.MaxResponseBytes = 65536
		c := &scriptedConn{}
		hello := protocol.MConnect.Frame()
		hello.MsgID = 1
		f, _ := frame(r)
		f.MsgID = 2
		c.Buffer.Write(response(hello, []byte{0}))
		c.Buffer.Write(response(f, payload))
		var out bytes.Buffer
		if run(p, func(context.Context, string, string) (net.Conn, error) { return c, nil }, &out) != 1 || len(c.writes) != 2 {
			t.Fatal("continued after oversized response", out.String())
		}
		var row result
		d := json.NewDecoder(&out)
		d.Decode(&row)
		d.Decode(&row)
		if row.Classification != "decode_invalid" || !bytes.Equal(row.ResponseWire, response(f, payload)) {
			t.Fatal("missing rejected raw", row)
		}
	}
	// Exactly the requested bounds are accepted, not rejected by an off-by-one.
	directory := make([]byte, 2+29*1000)
	binary.LittleEndian.PutUint16(directory[:2], 1000)
	if _, err := decode(request{Operation: "GetCode", Count: 1000}, directory); err != nil {
		t.Fatal(err)
	}
	kline := make([]byte, 2+16)
	binary.LittleEndian.PutUint32(kline[2:6], 20260101)
	binary.LittleEndian.PutUint16(kline[:2], 1)
	if _, err := decode(request{Operation: "GetKline", Market: 1, Code: "600001", Count: 1}, kline); err != nil {
		t.Fatal(err)
	}
}
func TestAbsoluteDeadlineRejectsBeforeDial(t *testing.T) {
	for _, deadline := range []string{"", "invalid", "2026-01-01T00:00:00Z"} {
		p := testPlan()
		p.Deadline = deadline
		if valid(p) {
			t.Fatal("invalid deadline accepted")
		}
		var out bytes.Buffer
		calls := 0
		status := run(p, func(context.Context, string, string) (net.Conn, error) { calls++; return nil, io.EOF }, &out)
		if status != 1 || calls != 0 {
			t.Fatal("dial after deadline")
		}
	}
}
func TestAbsoluteDeadlineCapsDialAndTransport(t *testing.T) {
	p := testPlan()
	p.WallSeconds = 120
	limit := time.Now().Add(time.Minute).UTC()
	p.Deadline = limit.Format(time.RFC3339Nano)
	c := &scriptedConn{}
	var out bytes.Buffer
	run(p, func(ctx context.Context, _, _ string) (net.Conn, error) {
		deadline, ok := ctx.Deadline()
		if !ok || !deadline.Equal(limit) {
			t.Fatal("dial deadline not capped")
		}
		return c, nil
	}, &out)
	for _, deadline := range c.deadlines {
		if deadline.After(limit) {
			t.Fatal("transport beyond absolute deadline")
		}
	}
}

// Invented stock records following the pinned variable-width GetPrice grammar.
func stockKlineRecord(width int) []byte {
	row := make([]byte, 4)
	binary.LittleEndian.PutUint32(row, 20260101)
	for field := 0; field < 4; field++ {
		price := bytes.Repeat([]byte{0x80}, width)
		price[0] |= byte(field + 1)
		if field == 3 {
			price[0] |= 0x40 // negative delta, interpreted only by the SDK
		}
		price[len(price)-1] &= 0x7f
		row = append(row, price...)
	}
	return append(row, make([]byte, 8)...)
}
func klinePayload(count uint16, rows ...[]byte) []byte {
	payload := make([]byte, 2)
	binary.LittleEndian.PutUint16(payload, count)
	for _, row := range rows {
		payload = append(payload, row...)
	}
	return payload
}
func TestKlineVariableWidthPreservesPinnedProjection(t *testing.T) {
	r := request{Operation: "GetKline", Market: 1, Code: "600001", Count: 2}
	for _, width := range []int{1, 2, 3, 4, 6, 20} {
		payload := klinePayload(2, stockKlineRecord(width), stockKlineRecord(1))
		want, err := protocol.MKline.Decode(payload, protocol.KlineCache{Type: protocol.TypeKlineDay, Kind: protocol.KindStock, Code: "sh600001"})
		if err != nil || want.Count != 2 || len(want.List) != 2 {
			t.Fatal("fixture rejected by pinned SDK", err)
		}
		encoded, _ := json.Marshal(want)
		got, err := decode(r, payload)
		if err != nil || !bytes.Equal(got, encoded) {
			t.Fatalf("width %d changed SDK projection: %s / %s / %v", width, got, encoded, err)
		}
	}
	if _, err := decode(r, klinePayload(0)); err != nil {
		t.Fatal("exact empty response rejected", err)
	}
}
func TestKlineRejectsEveryTruncatedPrefix(t *testing.T) {
	r := request{Operation: "GetKline", Market: 1, Code: "600001", Count: 2}
	payload := klinePayload(2, stockKlineRecord(3), stockKlineRecord(1))
	for end := 0; end < len(payload); end++ {
		if data, err := decode(r, payload[:end]); err == nil || data != nil {
			t.Fatalf("truncated prefix %d accepted", end)
		}
	}
}
func TestKlinePayloadTailStopsBeforeNextRequest(t *testing.T) {
	r := request{Operation: "GetKline", Market: 1, Code: "600001", Count: 1}
	unterminated := append(stockKlineRecord(1)[:4:4], bytes.Repeat([]byte{0x80}, 12)...)
	for name, payload := range map[string][]byte{
		"second_complete_record": klinePayload(1, stockKlineRecord(2), stockKlineRecord(3)),
		"one_trailing_byte":      klinePayload(1, stockKlineRecord(1), []byte{0}),
		"zero_count_with_record": klinePayload(0, stockKlineRecord(2)),
		"unterminated_price":     klinePayload(1, unterminated),
	} {
		t.Run(name, func(t *testing.T) {
			p := testPlan()
			p.Requests = []request{r, {Operation: "GetCount", Market: 1}}
			c := &scriptedConn{}
			hello := protocol.MConnect.Frame()
			hello.MsgID = 1
			f, _ := frame(r)
			f.MsgID = 2
			next, _ := frame(p.Requests[1])
			next.MsgID = 3
			c.Buffer.Write(response(hello, []byte{0}))
			c.Buffer.Write(response(f, payload))
			c.Buffer.Write(response(next, []byte{3, 0}))
			var out bytes.Buffer
			status := run(p, func(context.Context, string, string) (net.Conn, error) { return c, nil }, &out)
			if status != 1 || len(c.writes) != 2 {
				t.Fatalf("invalid payload advanced to next request: status=%d writes=%d output=%s", status, len(c.writes), out.String())
			}
			d := json.NewDecoder(&out)
			var row result
			if d.Decode(&row) != nil || d.Decode(&row) != nil {
				t.Fatal("missing retained records")
			}
			if row.Classification != "decode_invalid" || string(row.Payload) != "null" || !bytes.Equal(row.ResponseWire, response(f, payload)) {
				t.Fatal("rejected raw or classification missing", row)
			}
			if d.Decode(&row) != io.EOF {
				t.Fatal("extra record after rejected payload")
			}
		})
	}
}
