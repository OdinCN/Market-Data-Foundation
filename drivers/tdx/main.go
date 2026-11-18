// Bounded, explicitly launched nonproduction Go/TDX source. Linux/POSIX IPC.
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
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/injoyai/ios"
	"github.com/injoyai/ios/client"
	"github.com/injoyai/logs"
	"github.com/injoyai/tdx"
	"github.com/injoyai/tdx/protocol"
)

// The offline builder sets this explicitly for either pinned SDK profile.
var release = "injoyai/tdx v1.0.0@2a9000681f5a9896ea98e4de8bc4904f1dce78ba+mdf-wait-preregister-r1"

const maxFrame = 131072

type config struct {
	Securities     []string `json:"securities"`
	Endpoints      []string `json:"endpoints"`
	SDKCandidates  bool     `json:"sdk_candidates"`
	SDKOffset      int      `json:"sdk_offset"`
	MaxProbes      int      `json:"max_probes"`
	MaxConnections int      `json:"max_connections"`
	MaxFailovers   int      `json:"max_failovers"`
	Rounds         int      `json:"rounds"`
	Period         float64  `json:"period_seconds"`
	Duration       float64  `json:"duration_seconds"`
	DialTimeout    float64  `json:"dial_timeout_seconds"`
	QuoteTimeout   float64  `json:"quote_timeout_seconds"`
	AckTimeout     float64  `json:"ack_timeout_seconds"`
	TTL            float64  `json:"ttl_seconds"`
	Start          string   `json:"start_at"`
	ID             string   `json:"session_id"`
}

func seconds(v float64) time.Duration { return time.Duration(v * float64(time.Second)) }
func validConfig(c config) bool {
	if (!c.SDKCandidates && len(c.Endpoints) == 0) || len(c.Securities) < 1 || len(c.Securities) > 32 || len(c.Endpoints) > 8 ||
		c.SDKOffset < 0 || c.SDKOffset > 255 || c.MaxProbes < 1 || c.MaxProbes > 8 ||
		c.MaxConnections < 1 || c.MaxConnections > 8 || c.MaxFailovers < 0 || c.MaxFailovers > 3 ||
		c.Rounds < 1 || c.Rounds > 120 || !regexp.MustCompile(`^[a-f0-9]{32}$`).MatchString(c.ID) {
		return false
	}
	seen := map[string]bool{}
	for _, s := range c.Securities {
		if seen[s] || !regexp.MustCompile(`^(sh6|sz[03])[0-9]{5}$`).MatchString(s) {
			return false
		}
		seen[s] = true
	}
	seen = map[string]bool{}
	for _, e := range c.Endpoints {
		if seen[e] || !validEndpoint(e) {
			return false
		}
		seen[e] = true
	}
	for _, b := range [][3]float64{{c.Period, 1, 60}, {c.Duration, 1, 900},
		{c.DialTimeout, .1, 10}, {c.QuoteTimeout, .1, 10}, {c.AckTimeout, .1, 10}, {c.TTL, 1, 300}} {
		if !(b[0] >= b[1] && b[0] <= b[2]) {
			return false
		}
	}
	return true
}
func validEndpoint(e string) bool {
	if !regexp.MustCompile(`^[A-Za-z0-9.-]{1,253}:[0-9]{1,5}$`).MatchString(e) {
		return false
	}
	_, p, err := net.SplitHostPort(e)
	if err != nil {
		return false
	}
	var port int
	for _, v := range p {
		port = port*10 + int(v-'0')
	}
	return port > 0 && port <= 65535
}
func frame(kind byte, body any) error {
	data, err := json.Marshal(body)
	if err != nil {
		return err
	}
	return writeFrame(kind, data)
}
func writeFrame(kind byte, data []byte) error {
	if len(data) < 1 || len(data) > maxFrame {
		return errors.New("frame_bound")
	}
	header := make([]byte, 5)
	header[0] = kind
	binary.BigEndian.PutUint32(header[1:], uint32(len(data)))
	_, err := os.Stdout.Write(append(header, data...))
	return err
}
func readFrame(reader io.Reader, expected byte, limit int) ([]byte, error) {
	h := make([]byte, 5)
	if _, err := io.ReadFull(reader, h); err != nil {
		return nil, err
	}
	n := int(binary.BigEndian.Uint32(h[1:]))
	if h[0] != expected || n < 1 || n > limit {
		return nil, errors.New("invalid_frame")
	}
	b := make([]byte, n)
	_, err := io.ReadFull(reader, b)
	return b, err
}

// Reject duplicate keys at every nesting level before decoding typed IPC.
func uniqueJSON(d *json.Decoder, depth int) error {
	if depth > 32 {
		return errors.New("json_depth")
	}
	token, err := d.Token()
	if err != nil {
		return err
	}
	delimiter, ok := token.(json.Delim)
	if !ok {
		return nil
	}
	switch delimiter {
	case '{':
		seen := map[string]bool{}
		for d.More() {
			key, err := d.Token()
			if err != nil {
				return err
			}
			name, ok := key.(string)
			if !ok || seen[name] {
				return errors.New("duplicate_json_key")
			}
			seen[name] = true
			if err := uniqueJSON(d, depth+1); err != nil {
				return err
			}
		}
	case '[':
		for d.More() {
			if err := uniqueJSON(d, depth+1); err != nil {
				return err
			}
		}
	default:
		return errors.New("json_delimiter")
	}
	_, err = d.Token()
	return err
}
func strictJSON(data []byte, value any) error {
	if err := uniqueJSON(json.NewDecoder(bytes.NewReader(data)), 0); err != nil {
		return err
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if err := d.Decode(value); err != nil {
		return err
	}
	if d.Decode(new(any)) != io.EOF {
		return errors.New("extra_json")
	}
	return nil
}
func timedFrame(ctx context.Context, reader *os.File, kind byte, limit int, timeout time.Duration) ([]byte, error) {
	type result struct {
		wire []byte
		err  error
	}
	ch := make(chan result, 1)
	go func() { b, e := readFrame(reader, kind, limit); ch <- result{b, e} }()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case r := <-ch:
		return r.wire, r.err
	case <-ctx.Done():
		reader.Close()
		<-ch
		return nil, ctx.Err()
	case <-timer.C:
		reader.Close()
		<-ch
		return nil, context.DeadlineExceeded
	}
}

type event struct {
	Classification string `json:"classification"`
	Reason         string `json:"reason"`
	Session        string `json:"session_id"`
	Endpoint       string `json:"endpoint"`
	Origin         string `json:"candidate_origin"`
	Elapsed        int64  `json:"elapsed_ns"`
	Attempt        int    `json:"attempt"`
	Round          int    `json:"round"`
	Segment        int    `json:"segment"`
	RequestedBytes int    `json:"requested_bytes,omitempty"`
	WrittenBytes   int    `json:"written_bytes,omitempty"`
}

func emit(c config, class, reason string, n, segment int) error {
	return frame('E', event{Classification: class, Reason: reason, Session: c.ID, Round: n, Segment: segment})
}
func classification(err error) string {
	// Pinned base/maps/wait emits this exact untyped timeout; never export text.
	if err != nil && err.Error() == "超时" {
		return "timeout"
	}
	if errors.Is(err, syscall.ECONNREFUSED) {
		return "refused"
	}
	if errors.Is(err, syscall.EACCES) || errors.Is(err, syscall.EPERM) {
		return "access_restricted"
	}
	var n net.Error
	if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &n) && n.Timeout()) {
		return "timeout"
	}
	if errors.Is(err, io.EOF) || errors.Is(err, net.ErrClosed) {
		return "disconnected"
	}
	return "source_error"
}

type candidate struct {
	Endpoint, Origin string
	Elapsed          int64
}
type dialFunc func(context.Context, string) (net.Conn, error)

func candidatePool(c config) []candidate {
	pool := []candidate{}
	seen := map[string]bool{}
	add := func(e, o string) {
		if !seen[e] && len(pool) < c.MaxProbes {
			seen[e] = true
			pool = append(pool, candidate{Endpoint: e, Origin: o})
		}
	}
	for _, e := range c.Endpoints {
		add(e, "configured")
	}
	for i := c.SDKOffset; c.SDKCandidates && i < len(tdx.Hosts) && len(pool) < c.MaxProbes; i++ {
		// Pinned tdx.Hosts contains bare IPv4 addresses: TCP requires :7709.
		host := tdx.Hosts[i]
		if !strings.Contains(host, ":") {
			host = net.JoinHostPort(host, "7709")
		}
		add(host, "pinned_sdk_hosts")
	}
	return pool
}

// Sequential finite TCP probes, sorted by measured successful dial latency.
func rank(ctx context.Context, c config, dial dialFunc, report func(event) error) ([]candidate, error) {
	good := []candidate{}
	for i, v := range candidatePool(c) {
		if ctx.Err() != nil {
			return good, ctx.Err()
		}
		began := time.Now()
		conn, err := dial(ctx, v.Endpoint)
		v.Elapsed = time.Since(began).Nanoseconds()
		class := "probe_ok"
		reason := ""
		if err != nil {
			class = "probe_failed"
			reason = classification(err)
		} else {
			conn.Close()
			good = append(good, v)
		}
		if err = report(event{class, reason, c.ID, v.Endpoint, v.Origin, v.Elapsed, i + 1, 0, 0, 0, 0}); err != nil {
			return nil, err
		}
	}
	sort.SliceStable(good, func(i, j int) bool { return good[i].Elapsed < good[j].Elapsed })
	return good, nil
}

type connection struct {
	Peer      string    `json:"peer"`
	Requested time.Time `json:"requested"`
	Received  time.Time `json:"received"`
	Elapsed   int64     `json:"elapsed_ns"`
	Redial    bool      `json:"redial"`
	Attempts  int       `json:"attempts"`
}
type binding struct {
	Session    string     `json:"session_id"`
	Release    string     `json:"sdk_release"`
	Endpoint   string     `json:"endpoint"`
	Segment    int        `json:"segment"`
	Connection connection `json:"connection"`
}
type clocks struct {
	Requested time.Time `json:"requested"`
	Received  time.Time `json:"received"`
	Frozen    time.Time `json:"frozen"`
}
type batch struct {
	Schema     string          `json:"schema"`
	Origin     string          `json:"input_origin"`
	Provenance string          `json:"clock_provenance"`
	Release    string          `json:"sdk_release"`
	Endpoint   string          `json:"endpoint"`
	Securities []string        `json:"securities"`
	ID         string          `json:"batch_id"`
	Round      int             `json:"round"`
	Maximum    int             `json:"max_rounds"`
	Segment    int             `json:"segment"`
	Session    string          `json:"session_id"`
	Connection connection      `json:"connection"`
	Clocks     clocks          `json:"clocks"`
	QuoteNS    int64           `json:"quote_elapsed_ns"`
	Event      string          `json:"source_event_clock"`
	Kline      string          `json:"kline_time_semantics"`
	Server     string          `json:"server_time_semantics"`
	Units      []string        `json:"units"`
	Failure    *string         `json:"failure"`
	Rows       json.RawMessage `json:"rows"`
}
type ack struct {
	Session  string      `json:"session_id"`
	Round    int         `json:"round"`
	Segment  int         `json:"segment"`
	SHA      string      `json:"sha256"`
	Known    time.Time   `json:"known"`
	Reads    []time.Time `json:"reads"`
	Accepted bool        `json:"accepted"`
}

func validACK(wire, raw []byte, b batch, now time.Time) bool {
	var a ack
	var fields map[string]json.RawMessage
	if json.Unmarshal(raw, &fields) != nil || len(fields) != 7 {
		return false
	}
	for _, key := range []string{"session_id", "round", "segment", "sha256", "known", "reads", "accepted"} {
		if _, ok := fields[key]; !ok {
			return false
		}
	}
	if strictJSON(raw, &a) != nil {
		return false
	}
	h := sha256.Sum256(wire)
	_, offset := a.Known.Zone()
	if offset != 0 || !a.Accepted || a.Session != b.Session || a.Round != b.Round || a.Segment != b.Segment ||
		a.SHA != hex.EncodeToString(h[:]) || a.Known.Before(b.Clocks.Frozen) || a.Known.After(now) ||
		len(a.Reads) != len(b.Securities) {
		return false
	}
	for _, r := range a.Reads {
		_, offset := r.Zone()
		if offset != 0 || r.Before(a.Known) || r.After(now) {
			return false
		}
	}
	return true
}

// Quote telemetry is captured at the SDK/socket boundaries and emitted by the
// sole IPC writer after the worker returns. No response bytes or source clocks
// are invented. At most one SDK call and one protocol Write occur per quote.
type quoteTrace struct {
	mu     sync.Mutex
	events []event
}

func (t *quoteTrace) add(e event) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if len(t.events) < 4 {
		t.events = append(t.events, e)
	}
}
func (t *quoteTrace) snapshot() []event {
	t.mu.Lock()
	defer t.mu.Unlock()
	return append([]event(nil), t.events...)
}
func operationDeadline(ctx context.Context, began time.Time, timeout time.Duration) time.Time {
	end := began.Add(timeout)
	if hard, ok := ctx.Deadline(); ok && hard.Before(end) {
		return hard
	}
	return end
}

type gatedConn struct {
	net.Conn
	ctx     context.Context
	writes  *atomic.Int32
	maximum int32
	trace   *atomic.Pointer[quoteTrace]
}

func (c *gatedConn) Write(b []byte) (int, error) {
	var trace *quoteTrace
	if c.trace != nil {
		trace = c.trace.Load()
	}
	if trace != nil {
		trace.add(event{Classification: "protocol_write_attempt", RequestedBytes: len(b)})
	}
	n := 0
	var err error
	if c.ctx.Err() != nil || c.writes.Add(1) > c.maximum {
		err = net.ErrClosed
	} else {
		n, err = c.Conn.Write(b)
	}
	if trace != nil {
		outcome := "protocol_write_complete"
		if n != len(b) || err != nil {
			outcome = "protocol_write_failed"
			if n > 0 && n < len(b) {
				outcome = "protocol_write_partial"
			}
		}
		trace.add(event{Classification: outcome, RequestedBytes: len(b), WrittenBytes: n})
	}
	return n, err
}

type source struct {
	sdk     *tdx.Client
	socket  net.Conn
	binding binding
	trace   atomic.Pointer[quoteTrace]
	report  func(event) error
}

func (s *source) close() {
	if s.socket != nil {
		s.socket.Close()
	}
	if s.sdk != nil {
		// Let the SDK reader unwind and own its pooled-reader cleanup. Calling
		// CloseAll concurrently with Run can recycle a buffer still being read.
		select {
		case <-s.sdk.Done():
		case <-time.After(100 * time.Millisecond):
		}
	}
}
func connect(ctx context.Context, c config, v candidate, attempt, segment int, writes *atomic.Int32, tcpDial dialFunc) (*source, error) {
	s := &source{binding: binding{c.ID, release, v.Endpoint, segment, connection{Attempts: attempt}}}
	var transport *client.Client
	var dialCalls atomic.Int32
	dial := func(_ context.Context) (ios.ReadWriteCloser, string, error) {
		if dialCalls.Add(1) != 1 || ctx.Err() != nil {
			return nil, v.Endpoint, net.ErrClosed
		}
		// SDK overwrites callbacks after options. Override inside dial before OnConnected.
		transport.Event.OnConnected = func(cl *client.Client) error { _, e := cl.Write(protocol.MConnect.Frame().Bytes()); return e }
		s.binding.Connection.Requested = time.Now().UTC()
		began := time.Now()
		conn, e := tcpDial(ctx, v.Endpoint)
		s.binding.Connection.Received = time.Now().UTC()
		s.binding.Connection.Elapsed = time.Since(began).Nanoseconds()
		if e != nil {
			return nil, v.Endpoint, e
		}
		s.socket = conn
		s.binding.Connection.Peer = conn.RemoteAddr().String()
		if e = conn.SetDeadline(operationDeadline(ctx, began, seconds(c.DialTimeout))); e != nil {
			conn.Close()
			return nil, v.Endpoint, e
		}
		return &gatedConn{conn, ctx, writes, int32(c.MaxConnections + c.Rounds), &s.trace}, v.Endpoint, nil
	}
	sdk, e := tdx.DialWith(dial, tdx.WithRedial(false), tdx.WithDebug(false), tdx.WithLevel(tdx.LevelNone),
		func(cl *client.Client) { transport = cl; cl.Event.OnWrite = client.NewWriteSafe() })
	s.sdk = sdk
	if e != nil {
		s.close()
		return nil, e
	}
	sdk.SetTimeout(seconds(c.QuoteTimeout) * 9 / 10)
	end, _ := ctx.Deadline()
	if e = s.socket.SetDeadline(end); e != nil {
		s.close()
		return nil, e
	}
	return s, nil
}

var errWorkerStuck = errors.New("worker_stuck")

func (s *source) quote(ctx context.Context, c config) (protocol.QuotesResp, error) {
	trace := &quoteTrace{}
	s.trace.Store(trace)
	end := operationDeadline(ctx, time.Now(), seconds(c.QuoteTimeout))
	hard, _ := ctx.Deadline()
	if e := s.socket.SetDeadline(end); e != nil {
		return nil, e
	}
	type result struct {
		rows protocol.QuotesResp
		err  error
	}
	ch := make(chan result, 1)
	go func() {
		defer func() {
			if recover() != nil {
				ch <- result{err: errors.New("source_error")}
			}
		}()
		trace.add(event{Classification: "sdk_getquote_entered"})
		q, e := s.sdk.GetQuote(c.Securities...)
		ch <- result{q, e}
	}()
	timer := time.NewTimer(time.Until(end))
	defer timer.Stop()
	select {
	case r := <-ch:
		if r.err == nil {
			r.err = s.socket.SetDeadline(hard)
		}
		return r.rows, r.err
	case <-timer.C:
		s.close()
		select {
		case <-ch:
			return nil, context.DeadlineExceeded
		case <-time.After(100 * time.Millisecond):
			return nil, errWorkerStuck
		}
	case <-ctx.Done():
		s.close()
		select {
		case <-ch:
			return nil, ctx.Err()
		case <-time.After(100 * time.Millisecond):
			return nil, errWorkerStuck
		}
	}
}
func sleepUntil(ctx context.Context, due time.Time) bool {
	timer := time.NewTimer(time.Until(due))
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

// Sole quote loop: the next call is unreachable until valid exact-wire ACK.
func sampleLoop(ctx context.Context, c config, open func(int) (*source, error), quote func(*source) (protocol.QuotesResp, error),
	send func(batch) ([]byte, error), acknowledge func([]byte, batch) bool, notify func(string, string, int, int) error) string {
	var active *source
	defer func() {
		if active != nil {
			active.close()
		}
	}()
	segment := 0
	var due time.Time
	for n := 1; n <= c.Rounds; n++ {
		if active == nil {
			if segment > c.MaxFailovers {
				return "failover_budget_exhausted"
			}
			var err error
			active, err = open(segment)
			if err != nil {
				return "connection_budget_exhausted"
			}
			if err = notify("connected", "", n, segment); err != nil {
				return "ipc_error"
			}
			// Recovery may rebase cadence. New segment explicitly breaks continuity.
			if due.Before(time.Now()) {
				due = time.Now()
			}
		}
		if !sleepUntil(ctx, due) {
			return "duration_exhausted"
		}
		if time.Since(due) > 500*time.Millisecond {
			return "cadence_missed"
		}
		if ctx.Err() != nil {
			return "duration_exhausted"
		}
		if notify("quote_attempt", "", n, segment) != nil {
			return "ipc_error"
		}
		requested := time.Now().UTC()
		began := time.Now()
		rows, err := quote(active)
		if trace := active.trace.Load(); trace != nil && active.report != nil {
			for _, e := range trace.snapshot() {
				e.Session, e.Round, e.Segment = c.ID, n, segment
				if active.report(e) != nil {
					return "ipc_error"
				}
			}
		}
		if errors.Is(err, errWorkerStuck) {
			return "worker_stuck"
		}
		received := time.Now().UTC()
		elapsed := time.Since(began).Nanoseconds()
		var failure *string
		raw := json.RawMessage("null")
		if err != nil {
			class := classification(err)
			if class == "refused" || class == "access_restricted" {
				class = "source_error"
			}
			failure = &class
			if notify("quote_failed", classification(err), n, segment) != nil {
				return "ipc_error"
			}
		} else {
			raw, err = json.Marshal(rows)
			if err != nil {
				return "freeze_error"
			}
		}
		b := batch{"mdf.go-tdx.watchlist.session.r1", "real_go_sdk_decoded_quote", "local_probe_wall_clock_utc",
			release, active.binding.Endpoint, c.Securities, c.ID + "-" + itoa(n), n, c.Rounds, segment, c.ID,
			active.binding.Connection, clocks{requested, received, time.Now().UTC()}, elapsed, "UNKNOWN",
			"SDK_CLIENT_SAMPLED", "OPAQUE", []string{"integer_li", "sdk_volume_count_UNKNOWN", "sdk_level_count_UNKNOWN"}, failure, raw}
		wire, e := send(b)
		if e != nil {
			return "ipc_error"
		}
		if !acknowledge(wire, b) {
			return "ack_rejected_or_timeout"
		}
		if notify("ack_received", "", n, segment) != nil {
			return "ipc_error"
		}
		due = began.Add(seconds(c.Period))
		if failure != nil {
			active.close()
			active = nil
			segment++
		}
	}
	return "budget_exhausted"
}
func itoa(n int) string { // bounded positive round, no formatting of external errors
	if n < 10 {
		return string(rune('0' + n))
	}
	return itoa(n/10) + string(rune('0'+n%10))
}
func run() string {
	if len(os.Args) != 2 || os.Args[1] != "--live" || runtime.Version() != "go1.25.3" {
		return "invocation_invalid"
	}
	parent := os.Getppid()
	_, _, errno := syscall.Syscall6(syscall.SYS_PRCTL, 1, uintptr(syscall.SIGKILL), 0, 0, 0, 0)
	if errno != 0 || parent <= 1 || os.Getppid() != parent {
		return "parent_watchdog_invalid"
	}
	data, err := timedFrame(context.Background(), os.Stdin, 'K', 16384, 2*time.Second)
	var c config
	if err != nil || strictJSON(data, &c) != nil || !validConfig(c) {
		return "config_invalid"
	}
	start := time.Now()
	if c.Start != "" {
		start, err = time.Parse(time.RFC3339Nano, c.Start)
		_, offset := start.Zone()
		if err != nil || offset != 0 || time.Until(start) > 300*time.Second || time.Until(start) < -time.Second {
			return "start_invalid"
		}
	}
	end := start.Add(seconds(c.Duration))
	ctx, cancel := context.WithDeadline(context.Background(), end)
	defer cancel()
	watchdog := time.AfterFunc(time.Until(end)+time.Second, func() { os.Exit(124) })
	defer watchdog.Stop()
	if !sleepUntil(ctx, start) {
		return "duration_exhausted"
	}
	tcpDial := func(ctx context.Context, e string) (net.Conn, error) {
		return (&net.Dialer{Timeout: seconds(c.DialTimeout)}).DialContext(ctx, "tcp", e)
	}
	pool, err := rank(ctx, c, tcpDial, func(e event) error { return frame('E', e) })
	if err != nil {
		emit(c, "stopped", "selection_failed", 0, 0)
		return "selection_failed"
	}
	attempts := 0
	index := 0
	var writes atomic.Int32
	open := func(segment int) (*source, error) {
		for index < len(pool) && attempts < c.MaxConnections && ctx.Err() == nil {
			v := pool[index]
			index++
			attempts++
			if e := frame('E', event{"connection_attempt", "", c.ID, v.Endpoint, v.Origin, 0, attempts, 0, segment, 0, 0}); e != nil {
				return nil, e
			}
			began := time.Now()
			s, e := connect(ctx, c, v, attempts, segment, &writes, tcpDial)
			if e != nil {
				if e = frame('E', event{"connection_failed", classification(e), c.ID, v.Endpoint, v.Origin, time.Since(began).Nanoseconds(), attempts, 0, segment, 0, 0}); e != nil {
					return nil, e
				}
				continue
			}
			s.report = func(e event) error { return frame('E', e) }
			if e = frame('C', s.binding); e != nil {
				s.close()
				return nil, e
			}
			return s, nil
		}
		return nil, errors.New("connection_budget")
	}
	lastSegment := 0
	reason := sampleLoop(ctx, c, open, func(s *source) (protocol.QuotesResp, error) { return s.quote(ctx, c) },
		func(b batch) ([]byte, error) {
			wire, e := json.Marshal(b)
			if e != nil {
				return nil, e
			}
			e = writeFrame('B', wire)
			return wire, e
		},
		func(wire []byte, b batch) bool {
			raw, e := timedFrame(ctx, os.Stdin, 'A', 8192, seconds(c.AckTimeout))
			return e == nil && validACK(wire, raw, b, time.Now().UTC())
		}, func(class, reason string, n, segment int) error {
			lastSegment = segment
			return emit(c, class, reason, n, segment)
		})
	class := "stopped"
	if reason == "budget_exhausted" {
		class = "complete"
	}
	emit(c, class, reason, c.Rounds, lastSegment)
	return reason
}
func main() {
	if dailyDispatch() {
		return
	}
	if I4Dispatch() {
		return
	}
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		os.Stdout.WriteString(release + "\n")
		return
	}
	logs.SetWriter(io.Discard)
	// Suppress dependency panic/stack text; only classified IPC observations escape.
	null, e := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	if e != nil {
		os.Exit(2)
	}
	if syscall.Dup2(int(null.Fd()), 2) != nil {
		os.Exit(2)
	}
	null.Close()
	defer func() {
		if recover() != nil {
			emit(config{}, "stopped", "driver_panic", 0, 0)
			os.Exit(1)
		}
	}()
	reason := run()
	if reason != "budget_exhausted" {
		os.Exit(1)
	}
}
