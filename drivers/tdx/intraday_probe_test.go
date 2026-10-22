//go:build mdf_i4

package main

import (
	"bytes"
	"compress/zlib"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/injoyai/tdx/protocol"
)

func i4Category(v uint8) *uint8 { return &v }
func i4TestPlan() I4Plan {
	return I4Plan{Schema: I4Revision, SDKCommit: I4SDKCommit, RunID: strings.Repeat("a", 32),
		Source: "synthetic", Binding: "synthetic-binding", Endpoint: "116.205.143.214:7727", Family: "exhq",
		Requests:    []I4Request{{Operation: "ExQuote", Market: 31, Code: "00700", Category: i4Category(2)}},
		MaxRequests: 1, MaxRequestBytes: 4096, MaxResponseBytes: 4096, MaxDecodedBytes: 4096, MaxFrames: 2,
		WallSeconds: 2, OperationSeconds: 1}
}
func i4Response(id uint32, typ uint16, body []byte) []byte {
	h := make([]byte, 16)
	copy(h, []byte{0xb1, 0xcb, 0x74, 0})
	h[4] = 0x1c
	binary.LittleEndian.PutUint32(h[5:9], id)
	binary.LittleEndian.PutUint16(h[10:12], typ)
	binary.LittleEndian.PutUint16(h[12:14], uint16(len(body)))
	binary.LittleEndian.PutUint16(h[14:16], uint16(len(body)))
	return append(h, body...)
}
func i4ReadRequest(r io.Reader) ([]byte, error) {
	h := make([]byte, 12)
	if _, e := io.ReadFull(r, h); e != nil {
		return nil, e
	}
	n := int(binary.LittleEndian.Uint16(h[6:8])) - 2
	b := make([]byte, n)
	_, e := io.ReadFull(r, b)
	return append(h, b...), e
}

func TestI4FrameNativeConstructorsAndIdentity(t *testing.T) {
	tests := []I4Request{
		{Operation: "GetHistoryMinute", Market: 0, Code: "sz000001", Date: "20261021"},
		{Operation: "GetTrade", Market: 0, Code: "sz000001", Date: "20261022", Count: 2},
		{Operation: "GetHistoryTrade", Market: 0, Code: "sz000001", Date: "20261021", Count: 2},
		{Operation: "GetKline", Market: 1, Code: "sh600519", Period: 7, Kind: "stock", Count: 2},
		{Operation: "ExQuote", Market: 31, Code: "00700", Category: i4Category(2)},
		{Operation: "ExBars", Market: 30, Code: "rb2610", Category: i4Category(3), Period: 7, Count: 2},
		{Operation: "ExMinute", Market: 31, Code: "00700", Category: i4Category(2)},
		{Operation: "ExHistMinute", Market: 31, Code: "00700", Category: i4Category(2), Date: "20261021"},
		{Operation: "ExTrade", Market: 30, Code: "rb2610", Category: i4Category(3), Count: 2},
		{Operation: "ExHistTrade", Market: 30, Code: "rb2610", Category: i4Category(3), Date: "20261021", Count: 2},
		{Operation: "ExMarkets"}, {Operation: "ExCount"}, {Operation: "ExInstruments", Count: 4},
	}
	for _, r := range tests {
		f, e := I4Frame(r)
		if e != nil || len(f.Bytes()) < 12 {
			t.Fatalf("frame %s: %v", r.Operation, e)
		}
		if i4Ex(r) && f.Prefix != 1 {
			t.Fatal("ex prefix")
		}
	}
	f, e := I4Frame(tests[7])
	if e != nil {
		t.Fatal(e)
	}
	if binary.LittleEndian.Uint32(f.Data[:4]) != 20261021 || f.Data[4] != 31 || string(f.Data[5:10]) != "00700" {
		t.Fatal("date/code changed")
	}
	f, _ = I4Frame(tests[5])
	if binary.LittleEndian.Uint16(f.Data[10:12]) != 7 {
		t.Fatal("directory category used as period")
	}
	for _, r := range []I4Request{
		{Operation: "GetMinute", Market: 0, Code: "sz000001"},
		{Operation: "GetTrade", Market: 0, Code: "sh600519", Date: "20261022", Count: 2},
		{Operation: "GetTrade", Market: 0, Code: "x", Date: "20261022", Count: 2},
		{Operation: "GetTrade", Market: 0, Code: "sz000001", Date: "20260230", Count: 2},
		{Operation: "ExTrade", Market: 31, Code: "00700", Category: i4Category(2), Count: 2, Start: 65535},
		{Operation: "ExQuote", Market: 31, Code: "0012345678", Category: i4Category(2)},
		{Operation: "ExQuote", Market: 31, Code: "00700"},
	} {
		if _, e := I4Frame(r); e == nil {
			t.Fatalf("accepted invalid %s", r.Operation)
		}
	}
}

func TestI4ReadFrameBoundedCompressionAndFailures(t *testing.T) {
	wire := i4Response(1, protocol.TypeExQuote, bytes.Repeat([]byte{0}, 150))
	for n := 0; n < len(wire); n++ {
		if _, _, e := I4ReadFrame(bytes.NewReader(wire[:n]), 4096, 4096); e == nil {
			t.Fatalf("truncation %d", n)
		}
	}
	if _, _, e := I4ReadFrame(bytes.NewReader(wire), 149, 4096); e == nil {
		t.Fatal("wire budget")
	}
	if _, _, e := I4ReadFrame(bytes.NewReader(wire), 4096, 149); e == nil {
		t.Fatal("decode budget")
	}
	_, payload, e := I4ReadFrame(bytes.NewReader(wire), 4096, 4096)
	if e != nil || len(payload) != 150 {
		t.Fatal("valid payload")
	}
	invalid := append([]byte(nil), wire...)
	invalid[4] = 0x0c
	if _, _, e := I4ReadFrame(bytes.NewReader(invalid), 4096, 4096); e == nil {
		t.Fatal("failure control")
	}
	invalid = append([]byte(nil), wire...)
	invalid[0] = 0
	if _, _, e := I4ReadFrame(bytes.NewReader(invalid), 4096, 4096); e == nil {
		t.Fatal("prefix scan")
	}
	var compressed bytes.Buffer
	z := zlib.NewWriter(&compressed)
	z.Write(bytes.Repeat([]byte("x"), 20000))
	z.Close()
	bomb := i4Response(1, protocol.TypeExQuote, compressed.Bytes())
	binary.LittleEndian.PutUint16(bomb[14:16], 5)
	if _, _, e := I4ReadFrame(bytes.NewReader(bomb), 4096, 4096); e == nil {
		t.Fatal("decompression exceeded advertised bound")
	}
}

func TestI4MockOneConnectionExactWritesAndRetainedFrames(t *testing.T) {
	p := i4TestPlan()
	calls := 0
	parent, peer := net.Pipe()
	defer parent.Close()
	defer peer.Close()
	finished := make(chan error, 1)
	go func() {
		setup, e := i4ReadRequest(peer)
		if e != nil {
			finished <- e
			return
		}
		if setup[0] != 1 || binary.LittleEndian.Uint16(setup[10:12]) != protocol.TypeExSetup {
			finished <- errors.New("setup")
			return
		}
		request, e := i4ReadRequest(peer)
		if e != nil {
			finished <- e
			return
		}
		if binary.LittleEndian.Uint32(request[1:5]) != 1 {
			finished <- errors.New("id")
			return
		}
		_, e = peer.Write(i4Response(0, protocol.TypeExSetup, nil))
		if e == nil {
			_, e = peer.Write(i4Response(1, protocol.TypeExQuote, make([]byte, 150)))
		}
		finished <- e
	}()
	records := []I4Record{}
	e := I4Run(context.Background(), p, func(context.Context, string, string) (net.Conn, error) { calls++; return parent, nil },
		func(r I4Record) error { records = append(records, r); return nil })
	if e != nil {
		t.Fatal(e)
	}
	if e = <-finished; e != nil {
		t.Fatal(e)
	}
	if calls != 1 || len(records) != 5 || records[0].Plan == nil {
		t.Fatalf("effect counts: %d %d", calls, len(records))
	}
	got := records[3]
	if got.Classification != "payload_retained_not_semantically_qualified" || len(got.Payload) != 150 || got.WireSHA256 != i4SHA(got.Wire) {
		t.Fatal("raw evidence")
	}
	if records[4].Frames != 2 || records[4].Classification != "budget_complete" {
		t.Fatal("terminal")
	}
}

func TestI4MismatchAndSinkFailureNeverRetry(t *testing.T) {
	p := i4TestPlan()
	parent, peer := net.Pipe()
	defer peer.Close()
	go func() {
		defer peer.Close()
		i4ReadRequest(peer)
		i4ReadRequest(peer)
		peer.Write(i4Response(9, protocol.TypeExQuote, nil))
	}()
	calls := 0
	records := []I4Record{}
	e := I4Run(context.Background(), p, func(context.Context, string, string) (net.Conn, error) { calls++; return parent, nil }, func(r I4Record) error { records = append(records, r); return nil })
	if e == nil || calls != 1 || records[len(records)-1].Classification != "response_identity_mismatch" {
		t.Fatal("mismatch did not stop")
	}
	calls = 0
	e = I4Run(context.Background(), p, func(context.Context, string, string) (net.Conn, error) {
		calls++
		return nil, errors.New("must not dial")
	}, func(I4Record) error { return errors.New("sink failed") })
	if e == nil || calls != 0 {
		t.Fatal("effect before intent persisted")
	}
}

func TestI4PlanValidationBeforeAnyConnection(t *testing.T) {
	base := i4TestPlan()
	mutations := []func(*I4Plan){
		func(p *I4Plan) { p.Endpoint = "127.0.0.1:7727" }, func(p *I4Plan) { p.Endpoint = "116.205.143.214:7709" },
		func(p *I4Plan) { p.SDKCommit = "other" }, func(p *I4Plan) { p.MaxRequestBytes = 1 },
		func(p *I4Plan) { p.MaxRequests = 2 }, func(p *I4Plan) { p.MaxDecodedBytes = 5000000 },
		func(p *I4Plan) { p.OperationSeconds = 11 }, func(p *I4Plan) { p.WallSeconds = 181 },
		func(p *I4Plan) { p.MaxFrames = 99 }, func(p *I4Plan) { p.Family = "standard" },
	}
	for _, change := range mutations {
		p := base
		change(&p)
		calls := 0
		e := I4Run(context.Background(), p, func(context.Context, string, string) (net.Conn, error) { calls++; return nil, errors.New("unexpected") }, func(I4Record) error { return nil })
		if e == nil || calls != 0 {
			t.Fatal("invalid plan dialed")
		}
	}
	var output bytes.Buffer
	raw, _ := json.Marshal(base)
	if I4HostMain(bytes.NewReader(raw), &output, false) != 2 || output.Len() != 0 {
		t.Fatal("opt-in gate")
	}
	bad := strings.Replace(string(raw), `"run_id":`, `"run_id":"duplicate", "run_id":`, 1)
	if I4HostMain(strings.NewReader(bad), &output, true) != 2 {
		t.Fatal("duplicate config")
	}
}

func TestI4DeadlineClosesBlockedMemoryPeer(t *testing.T) {
	p := i4TestPlan()
	p.OperationSeconds = .1
	p.WallSeconds = 1
	parent, peer := net.Pipe()
	defer peer.Close()
	before := time.Now()
	e := I4Run(context.Background(), p, func(context.Context, string, string) (net.Conn, error) { return parent, nil }, func(I4Record) error { return nil })
	if e == nil || time.Since(before) > time.Second {
		t.Fatal("blocked setup exceeded operation deadline")
	}
}

func i4CompressedResponse(payload, tail []byte) []byte {
	var compressed bytes.Buffer
	z := zlib.NewWriter(&compressed)
	_, _ = z.Write(payload)
	_ = z.Close()
	body := append(compressed.Bytes(), tail...)
	wire := i4Response(1, protocol.TypeExQuote, body)
	binary.LittleEndian.PutUint16(wire[14:16], uint16(len(payload)))
	return wire
}

func TestI4ZlibStrictEOFAndCompressedTail(t *testing.T) {
	payload := bytes.Repeat([]byte{0}, 150)
	valid := i4CompressedResponse(payload, nil)
	if _, got, err := I4ReadFrame(bytes.NewReader(valid), 4096, 4096); err != nil || !bytes.Equal(got, payload) {
		t.Fatalf("valid compressed stream: %v", err)
	}
	for _, tail := range [][]byte{{0}, []byte("tail"), valid[16:]} {
		wire := i4CompressedResponse(payload, tail)
		gotWire, got, err := I4ReadFrame(bytes.NewReader(wire), 4096, 4096)
		if err == nil || got != nil || !bytes.Equal(gotWire, wire) {
			t.Fatal("compressed tail accepted or original wire lost")
		}
	}
	for _, cut := range []int{1, 4} {
		wire := append([]byte(nil), valid[:len(valid)-cut]...)
		binary.LittleEndian.PutUint16(wire[12:14], uint16(len(wire)-16))
		if _, _, err := I4ReadFrame(bytes.NewReader(wire), 4096, 4096); err == nil {
			t.Fatal("truncated zlib checksum accepted")
		}
	}
	bad := append([]byte(nil), valid...)
	bad[len(bad)-1] ^= 1
	if _, _, err := I4ReadFrame(bytes.NewReader(bad), 4096, 4096); err == nil {
		t.Fatal("invalid checksum accepted")
	}
}

func TestI4ZlibTailStopsMemoryPeerBeforeSecondRequest(t *testing.T) {
	p := i4TestPlan()
	p.Requests = append(p.Requests, p.Requests[0])
	p.MaxRequests = 2
	p.MaxFrames = 3
	parent, peer := net.Pipe()
	defer peer.Close()
	finished := make(chan error, 1)
	go func() {
		if _, err := i4ReadRequest(peer); err != nil {
			finished <- err
			return
		}
		if _, err := i4ReadRequest(peer); err != nil {
			finished <- err
			return
		}
		if _, err := peer.Write(i4CompressedResponse(make([]byte, 150), []byte{0})); err != nil {
			finished <- err
			return
		}
		_ = peer.SetReadDeadline(time.Now().Add(time.Second))
		b := make([]byte, 1)
		n, err := peer.Read(b)
		if n != 0 || err != io.EOF {
			finished <- errors.New("connection not closed before subsequent request")
			return
		}
		finished <- nil
	}()
	calls := 0
	var records []I4Record
	err := I4Run(context.Background(), p, func(context.Context, string, string) (net.Conn, error) {
		calls++
		return parent, nil
	}, func(r I4Record) error { records = append(records, r); return nil })
	if err == nil || calls != 1 || records[len(records)-1].Classification != "response_read_or_validation_failed" {
		t.Fatal("tail did not stop exactly one transport")
	}
	for _, r := range records {
		if r.Classification == "budget_complete" || r.Index > 0 {
			t.Fatal("continued after tail")
		}
	}
	if err := <-finished; err != nil {
		t.Fatal(err)
	}
}
