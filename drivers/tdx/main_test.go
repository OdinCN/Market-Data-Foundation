package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/injoyai/ios"
	"github.com/injoyai/ios/client"
	"github.com/injoyai/tdx/protocol"
)

func testConfig() config {
	return config{
		Securities: []string{"sh600519", "sz000001", "sz000002"}, Endpoints: []string{"mock-a:7709", "mock-b:7709", "mock-c:7709"},
		SDKCandidates: true, MaxProbes: 3, MaxConnections: 3, MaxFailovers: 1, Rounds: 4, Period: 1, Duration: 20,
		DialTimeout: 1, QuoteTimeout: 1, AckTimeout: 1, TTL: 30, ID: strings.Repeat("a", 32),
	}
}
func TestRankingBoundedAndClassified(t *testing.T) {
	c := testConfig()
	c.MaxProbes = 2
	calls := []string{}
	events := []event{}
	good, err := rank(context.Background(), c, func(_ context.Context, e string) (net.Conn, error) {
		calls = append(calls, e)
		if e == "mock-a:7709" {
			return nil, errors.New("private message must not escape")
		}
		a, b := net.Pipe()
		b.Close()
		return a, nil
	}, func(e event) error { events = append(events, e); return nil })
	if err != nil || len(calls) != 2 || len(good) != 1 || good[0].Endpoint != "mock-b:7709" || events[0].Reason != "source_error" {
		t.Fatalf("bad bounded ranking: %+v %+v", good, events)
	}
}
func TestRankMeasuredLatency(t *testing.T) {
	c := testConfig()
	c.MaxProbes = 2
	good, err := rank(context.Background(), c, func(_ context.Context, e string) (net.Conn, error) {
		if e == "mock-a:7709" {
			time.Sleep(20 * time.Millisecond)
		}
		a, b := net.Pipe()
		b.Close()
		return a, nil
	}, func(event) error { return nil })
	if err != nil || good[0].Endpoint != "mock-b:7709" || good[0].Elapsed >= good[1].Elapsed {
		t.Fatal("latency ordering failed")
	}
}
func TestACKValidation(t *testing.T) {
	now := time.Now().UTC()
	b := batch{Session: strings.Repeat("a", 32), Round: 1, Segment: 0, Securities: []string{"sh600519", "sz000001", "sz000002"}, Clocks: clocks{Frozen: now.Add(-time.Second)}}
	wire := []byte(`{"invented":"payload"}`)
	h := sha256.Sum256(wire)
	good := ack{b.Session, 1, 0, hex.EncodeToString(h[:]), now.Add(-time.Millisecond), []time.Time{now, now, now}, true}
	raw, _ := json.Marshal(good)
	if !validACK(wire, raw, b, now) {
		t.Fatal("valid multi-member ACK rejected")
	}
	for _, mutate := range []func(*ack){
		func(a *ack) { a.SHA = strings.Repeat("0", 64) }, func(a *ack) { a.Round = 2 }, func(a *ack) { a.Session = "wrong" },
		func(a *ack) { a.Segment = 1 }, func(a *ack) { a.Accepted = false }, func(a *ack) { a.Reads = a.Reads[:2] },
		func(a *ack) { a.Known = now.Add(-2 * time.Second) }, func(a *ack) { a.Reads[0] = now.Add(time.Second) },
	} {
		a := good
		a.Reads = append([]time.Time{}, good.Reads...)
		mutate(&a)
		raw, _ = json.Marshal(a)
		if validACK(wire, raw, b, now) {
			t.Fatal("invalid ACK accepted")
		}
	}
}
func TestNoNextQuoteWithoutACK(t *testing.T) {
	for _, accepted := range []bool{false, true} {
		c := testConfig()
		c.Period = 0
		calls := 0
		acks := 0
		reason := sampleLoop(context.Background(), c, func(segment int) (*source, error) { return &source{binding: binding{Segment: segment}}, nil },
			func(*source) (protocol.QuotesResp, error) {
				if calls > 0 && acks != calls {
					t.Fatal("GetQuote before ACK")
				}
				calls++
				return nil, nil
			},
			func(b batch) ([]byte, error) { return json.Marshal(b) },
			func([]byte, batch) bool {
				if accepted {
					acks++
				}
				return accepted
			}, func(string, string, int, int) error { return nil })
		if !accepted && (calls != 1 || reason != "ack_rejected_or_timeout") {
			t.Fatal("ACK rejection did not terminate")
		}
		if accepted && (calls != 4 || acks != 4 || reason != "budget_exhausted") {
			t.Fatal("dynamic four-round session failed")
		}
	}
}
func TestRecoveryNewSegmentAndACKBarrier(t *testing.T) {
	c := testConfig()
	c.Rounds = 2
	c.Period = 0
	calls := 0
	acks := 0
	segments := []int{}
	reason := sampleLoop(context.Background(), c, func(segment int) (*source, error) {
		if segment > 0 && acks != 1 {
			t.Fatal("failover before failed-batch ACK")
		}
		segments = append(segments, segment)
		return &source{binding: binding{Segment: segment, Endpoint: []string{"mock-a:7709", "mock-b:7709"}[segment]}}, nil
	}, func(*source) (protocol.QuotesResp, error) {
		calls++
		if calls == 1 {
			return nil, net.ErrClosed
		}
		return nil, nil
	},
		func(b batch) ([]byte, error) {
			if b.Segment != calls-1 {
				t.Fatal("segment lost")
			}
			return json.Marshal(b)
		},
		func([]byte, batch) bool { acks++; return true }, func(string, string, int, int) error { return nil })
	if reason != "budget_exhausted" || len(segments) != 2 || segments[1] != 1 {
		t.Fatal("recovery failed")
	}
}
func TestFramingAndOptInConfig(t *testing.T) {
	if validConfig(config{}) {
		t.Fatal("unbounded config accepted")
	}
	if !validConfig(testConfig()) {
		t.Fatal("valid config rejected")
	}
	for _, raw := range [][]byte{{'A', 0, 0, 0, 0}, {'A', 0, 1, 0, 0}, {'B', 0, 0, 0, 1, 0}, {'A', 0, 0, 0, 1}} {
		if _, err := readFrame(strings.NewReader(string(raw)), 'A', 8192); err == nil {
			t.Fatal("invalid frame accepted")
		}
	}
}

func TestACKPipeTimeoutClosesReaderAndStops(t *testing.T) {
	c := testConfig()
	c.Period = 0
	calls := 0
	reader, writer, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	reason := sampleLoop(context.Background(), c, func(int) (*source, error) { return &source{}, nil },
		func(*source) (protocol.QuotesResp, error) { calls++; return nil, nil },
		func(b batch) ([]byte, error) { return json.Marshal(b) },
		func(w []byte, b batch) bool {
			_, e := timedFrame(context.Background(), reader, 'A', 8192, 10*time.Millisecond)
			return e == nil
		},
		func(string, string, int, int) error { return nil })
	if calls != 1 || reason != "ack_rejected_or_timeout" {
		t.Fatal("timeout allowed another request")
	}
	if _, e := reader.Read(make([]byte, 1)); e == nil {
		t.Fatal("ACK reader survived timeout")
	}
}
func TestDuplicateAndMissingACKKeys(t *testing.T) {
	now := time.Now().UTC()
	b := batch{Session: strings.Repeat("a", 32), Round: 1, Securities: []string{"sh600519"}, Clocks: clocks{Frozen: now.Add(-time.Second)}}
	wire := []byte(`{"invented":true}`)
	h := sha256.Sum256(wire)
	a := ack{b.Session, 1, 0, hex.EncodeToString(h[:]), now, []time.Time{now}, true}
	raw, _ := json.Marshal(a)
	duplicate := append([]byte(`{"round":1,`), raw[1:]...)
	if validACK(wire, duplicate, b, now) {
		t.Fatal("duplicate ACK keys accepted")
	}
	missing := bytes.Replace(raw, []byte(`"segment":0,`), nil, 1)
	if validACK(wire, missing, b, now) {
		t.Fatal("missing required ACK segment accepted")
	}
}
func TestDriverRequiresExplicitLiveFlag(t *testing.T) {
	original := os.Args
	defer func() { os.Args = original }()
	os.Args = []string{"driver"}
	if run() != "invocation_invalid" {
		t.Fatal("driver could start without live opt-in")
	}
}

// Exercise the actual pinned SDK handshake and GetQuote with memory pipes only.
func TestPinnedSDKHandshakeOnlyDeadlineAndNoRedial(t *testing.T) {
	c := testConfig()
	c.QuoteTimeout = .1
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	left, right := net.Pipe()
	defer right.Close()
	peer := make(chan error, 1)
	go func() {
		expected := protocol.MConnect.Frame().Bytes()
		actual := make([]byte, len(expected))
		if _, e := io.ReadFull(right, actual); e != nil {
			peer <- e
			return
		}
		if !bytes.Equal(actual, expected) {
			peer <- errors.New("wrong handshake")
			return
		}
		// Drain the sole quote request; deliberately return no synthetic response.
		_, e := io.Copy(io.Discard, right)
		peer <- e
	}()
	dials := 0
	var writes atomic.Int32
	s, e := connect(ctx, c, candidate{Endpoint: "memory:7709"}, 1, 0, &writes,
		func(context.Context, string) (net.Conn, error) { dials++; return left, nil })
	if e != nil {
		t.Fatal("SDK memory-pipe handshake failed")
	}
	if writes.Load() != 1 || dials != 1 {
		t.Fatal("extra handshake writer or dial")
	}
	_, e = s.quote(ctx, c)
	if classification(e) != "timeout" {
		s.close()
		t.Fatalf("SDK timeout classification: %s", classification(e))
	}
	if s.sdk.Wait.IsWait("1") {
		t.Fatal("timed-out waiter retained")
	}
	s.close()
	if writes.Load() != 2 || dials != 1 {
		t.Fatal("implicit writer/redial occurred")
	}
	select {
	case <-peer:
	case <-time.After(time.Second):
		t.Fatal("socket peer survived close")
	}
}

// SDK's pinned Hosts are bare IPv4; default selection must resolve TCP port.
func TestSDKPoolRequiresPort(t *testing.T) {
	c := testConfig()
	c.Endpoints = nil
	c.SDKCandidates = true
	c.MaxProbes = 2
	entries := candidatePool(c)
	if len(entries) != 2 {
		t.Fatalf("candidate count %d", len(entries))
	}
	for _, v := range entries {
		if !validEndpoint(v.Endpoint) || !strings.HasSuffix(v.Endpoint, ":7709") || v.Origin != "pinned_sdk_hosts" {
			t.Fatalf("invalid pinned SDK candidate: %+v", v)
		}
	}
}

// This forces the old race, without scheduling luck: Write cannot return until
// the actual SDK reader/decoder has dispatched this request's valid response.
type earlyResponseConn struct {
	net.Conn
	processed chan struct{}
	hold      atomic.Bool
}

func (c *earlyResponseConn) Write(b []byte) (int, error) {
	n, err := c.Conn.Write(b)
	if err == nil && c.hold.Load() {
		select {
		case <-c.processed:
		case <-time.After(time.Second):
			return n, errors.New("dispatch barrier timeout")
		}
	}
	return n, err
}
func TestPinnedSDKImmediateValidResponseBeforeWriteReturns(t *testing.T) {
	c := testConfig()
	c.Securities = []string{"sz000001"}
	c.Rounds = 32
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	left, right := net.Pipe()
	defer right.Close()
	conn := &earlyResponseConn{Conn: left, processed: make(chan struct{}, 1)}
	peer := make(chan error, 1)
	go func() {
		handshake := make([]byte, len(protocol.MConnect.Frame().Bytes()))
		if _, e := io.ReadFull(right, handshake); e != nil {
			peer <- e
			return
		}
		for i := 0; i < c.Rounds; i++ {
			head := make([]byte, 12)
			if _, e := io.ReadFull(right, head); e != nil {
				peer <- e
				return
			}
			payload := make([]byte, int(binary.LittleEndian.Uint16(head[6:8]))-2)
			if _, e := io.ReadFull(right, payload); e != nil {
				peer <- e
				return
			}
			// Invented protocol-valid one-row response, never retained as real data.
			data := make([]byte, 60)
			data[2] = 1
			copy(data[5:11], "000001")
			data[13] = 1
			response := make([]byte, 16)
			binary.BigEndian.PutUint32(response, protocol.PrefixResp)
			response[4] = 0x1c
			copy(response[5:9], head[1:5])
			binary.LittleEndian.PutUint16(response[10:12], protocol.TypeQuote)
			binary.LittleEndian.PutUint16(response[12:14], uint16(len(data)))
			binary.LittleEndian.PutUint16(response[14:16], uint16(len(data)))
			if _, e := right.Write(append(response, data...)); e != nil {
				peer <- e
				return
			}
		}
		peer <- nil
	}()
	var writes atomic.Int32
	s, err := connect(ctx, c, candidate{Endpoint: "memory:7709"}, 1, 0, &writes,
		func(context.Context, string) (net.Conn, error) { return conn, nil })
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	handler := s.sdk.Event.OnDealMessage
	s.sdk.Event.OnDealMessage = func(cl *client.Client, msg ios.Acker) {
		handler(cl, msg)
		conn.processed <- struct{}{}
	}
	conn.hold.Store(true)
	for i := 0; i < c.Rounds; i++ {
		rows, err := s.quote(ctx, c)
		if err != nil || len(rows) != 1 || rows[0].Code != "000001" || rows[0].Kline.Close != 10 {
			t.Fatalf("early response lost or decoded incorrectly: round=%d err=%v rows=%d", i+1, err, len(rows))
		}
		if s.sdk.Wait.IsWait(itoa(i + 1)) {
			t.Fatal("completed waiter retained")
		}
		trace := s.trace.Load().snapshot()
		if len(trace) != 3 || trace[0].Classification != "sdk_getquote_entered" || trace[1].Classification != "protocol_write_attempt" || trace[2].Classification != "protocol_write_complete" || trace[2].WrittenBytes != trace[1].RequestedBytes {
			t.Fatalf("incorrect actual boundary telemetry: %+v", trace)
		}
	}
	if err := <-peer; err != nil {
		t.Fatal(err)
	}
}

func TestHandshakeDeadlineClampedAndClassified(t *testing.T) {
	began := time.Now()
	for _, remaining := range []time.Duration{time.Millisecond, 20 * time.Second} {
		ctx, cancel := context.WithDeadline(context.Background(), began.Add(remaining))
		got := operationDeadline(ctx, began, 10*time.Second)
		expected := began.Add(remaining)
		if remaining > 10*time.Second {
			expected = began.Add(10 * time.Second)
		}
		if !got.Equal(expected) {
			t.Fatal("deadline not minimum")
		}
		cancel()
	}
	c := testConfig()
	c.DialTimeout = 10
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	left, right := net.Pipe()
	defer right.Close() // No reader: handshake Write must end at session deadline.
	var writes atomic.Int32
	start := time.Now()
	_, err := connect(ctx, c, candidate{Endpoint: "memory:7709"}, 1, 0, &writes,
		func(context.Context, string) (net.Conn, error) { return left, nil })
	if classification(err) != "timeout" || time.Since(start) > time.Second {
		t.Fatal("handshake escaped session deadline")
	}
}

type failingQuoteConn struct {
	net.Conn
	deadlineError atomic.Bool
	partial       bool
	full          bool
}

func (c *failingQuoteConn) SetDeadline(t time.Time) error {
	if c.deadlineError.Load() {
		return net.ErrClosed
	}
	return c.Conn.SetDeadline(t)
}
func (c *failingQuoteConn) Write(b []byte) (int, error) {
	if c.full {
		return len(b), io.ErrUnexpectedEOF
	}
	if c.partial {
		return len(b) / 2, io.ErrUnexpectedEOF
	}
	return 0, net.ErrClosed
}
func TestQuotePreflightFailureDoesNotEnterSDK(t *testing.T) {
	left, right := net.Pipe()
	defer right.Close()
	conn := &failingQuoteConn{Conn: left}
	defer conn.Close()
	conn.deadlineError.Store(true)
	s := &source{socket: conn} // nil SDK would panic if called
	_, err := s.quote(context.Background(), testConfig())
	if !errors.Is(err, net.ErrClosed) || len(s.trace.Load().snapshot()) != 0 {
		t.Fatal("preflight counted SDK call")
	}
}
func TestSocketWritePartialAndFailureResults(t *testing.T) {
	for _, mode := range []int{0, 1, 2} {
		partial := mode == 1
		left, right := net.Pipe()
		conn := &failingQuoteConn{Conn: left, partial: partial, full: mode == 2}
		var writes atomic.Int32
		var trace atomic.Pointer[quoteTrace]
		trace.Store(&quoteTrace{})
		gated := &gatedConn{conn, context.Background(), &writes, 1, &trace}
		n, err := gated.Write([]byte("invented"))
		events := trace.Load().snapshot()
		expected := "protocol_write_failed"
		if partial {
			expected = "protocol_write_partial"
		}
		if err == nil || len(events) != 2 || events[1].Classification != expected || events[1].WrittenBytes != n {
			t.Fatal("write result misreported")
		}
		right.Close()
		left.Close()
	}
}

type armedWriteConn struct {
	net.Conn
	fail atomic.Bool
}

func (c *armedWriteConn) Write(b []byte) (int, error) {
	if c.fail.Load() {
		return 0, net.ErrClosed
	}
	return c.Conn.Write(b)
}
func TestPinnedSDKWriteFailureRemovesRegisteredWaiter(t *testing.T) {
	c := testConfig()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	left, right := net.Pipe()
	defer right.Close()
	conn := &armedWriteConn{Conn: left}
	peer := make(chan struct{})
	go func() { io.Copy(io.Discard, right); close(peer) }()
	var writes atomic.Int32
	s, err := connect(ctx, c, candidate{Endpoint: "memory:7709"}, 1, 0, &writes,
		func(context.Context, string) (net.Conn, error) { return conn, nil })
	if err != nil {
		t.Fatal(err)
	}
	conn.fail.Store(true)
	_, err = s.quote(ctx, c)
	if err == nil || s.sdk.Wait.IsWait("1") {
		t.Fatal("write failure retained waiter")
	}
	events := s.trace.Load().snapshot()
	if len(events) != 3 || events[0].Classification != "sdk_getquote_entered" || events[2].Classification != "protocol_write_failed" || events[2].WrittenBytes != 0 {
		t.Fatal("write failure ledger incorrect")
	}
	s.close()
	select {
	case <-peer:
	case <-ctx.Done():
		t.Fatal("peer survived close")
	}
}

// Near-boundary real SDK response and timeout cleanup share one waiter.
// The derived client must serialize reader/cleanup Done and leave no key.
func TestPinnedSDKTimeoutVsReplyDoneSerialized(t *testing.T) {
	c := testConfig()
	c.Securities = []string{"sz000001"}
	c.Rounds = 48
	c.QuoteTimeout = 0.2
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	left, right := net.Pipe()
	defer right.Close()
	peer := make(chan error, 1)
	go func() {
		handshake := make([]byte, len(protocol.MConnect.Frame().Bytes()))
		if _, e := io.ReadFull(right, handshake); e != nil {
			peer <- e
			return
		}
		for n := 0; n < c.Rounds; n++ {
			head := make([]byte, 12)
			if _, e := io.ReadFull(right, head); e != nil {
				peer <- e
				return
			}
			size := int(binary.LittleEndian.Uint16(head[6:8])) - 2
			if size < 0 || size > 8192 {
				peer <- errors.New("request bound")
				return
			}
			if _, e := io.CopyN(io.Discard, right, int64(size)); e != nil {
				peer <- e
				return
			}
			// Hover at the 2 ms deadline; both outcomes are valid, overlap is the test.
			time.Sleep(time.Duration(1+n%3) * time.Millisecond)
			body := make([]byte, 60)
			body[2] = 1
			copy(body[5:11], "000001")
			body[13] = 1
			response := make([]byte, 16)
			binary.BigEndian.PutUint32(response, protocol.PrefixResp)
			response[4] = 0x1c
			copy(response[5:9], head[1:5])
			binary.LittleEndian.PutUint16(response[10:12], protocol.TypeQuote)
			binary.LittleEndian.PutUint16(response[12:14], uint16(len(body)))
			binary.LittleEndian.PutUint16(response[14:16], uint16(len(body)))
			if _, e := right.Write(append(response, body...)); e != nil {
				peer <- e
				return
			}
		}
		peer <- nil
	}()
	var writes atomic.Int32
	session, err := connect(ctx, c, candidate{Endpoint: "memory:7709"}, 1, 0, &writes,
		func(context.Context, string) (net.Conn, error) { return left, nil })
	if err != nil {
		t.Fatal(err)
	}
	defer session.close()
	session.sdk.SetTimeout(2 * time.Millisecond)
	for n := 1; n <= c.Rounds; n++ {
		rows, e := session.sdk.GetQuote("sz000001")
		if e != nil && e.Error() != "超时" {
			t.Fatalf("unexpected sdk result at %d: %v", n, e)
		}
		if e == nil && (len(rows) != 1 || rows[0].Code != "000001") {
			t.Fatal("cross-key response or corrupt frame")
		}
		if session.sdk.Wait.IsWait(itoa(n)) {
			t.Fatalf("waiter %d leaked", n)
		}
	}
	select {
	case e := <-peer:
		if e != nil {
			t.Fatal(e)
		}
	case <-ctx.Done():
		t.Fatal("peer hangs at response timeout boundary")
	}
}
