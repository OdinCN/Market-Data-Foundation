//go:build mdf_i4

// I-4 isolated candidate boundary. No init hook, no change to the frozen main IPC.
package main

import (
	"bytes"
	"compress/zlib"
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
	"time"

	"github.com/injoyai/tdx"
	"github.com/injoyai/tdx/protocol"
)

const I4Revision = "mdf.tdx.i4.raw-probe.v1"
const I4SDKCommit = "2a9000681f5a9896ea98e4de8bc4904f1dce78ba"
const I4MaxPayload = 65535

// Category is the directory category. Period is the Kline period, never category.
type I4Request struct {
	Operation string `json:"operation"`
	Market    uint16 `json:"market"`
	Code      string `json:"code"`
	Category  *uint8 `json:"category"`
	Start     uint16 `json:"start"`
	Count     uint16 `json:"count"`
	Date      string `json:"date"`
	Period    uint8  `json:"period"`
	Kind      string `json:"kind"`
}

func i4Error(code string) error { return errors.New(code) }
func i4Ex(r I4Request) bool     { return len(r.Operation) >= 2 && r.Operation[:2] == "Ex" }
func i4Date(s string) bool {
	t, e := time.Parse("20060102", s)
	return e == nil && t.Year() >= 1992 && t.Format("20060102") == s
}

// Frame uses only pinned SDK protocol constructors: no NewCodes, network or SDK timers.
func I4Frame(r I4Request) (*protocol.Frame, error) {
	if uint32(r.Start)+uint32(r.Count) > 65536 {
		return nil, i4Error("page_address_overflow")
	}
	ex := i4Ex(r)
	if ex {
		if r.Market > 255 || r.Category == nil || !regexp.MustCompile(`^[A-Za-z0-9._-]{1,9}$`).MatchString(r.Code) || r.Kind != "" {
			// Directory operations do not have a code/category.
			if r.Operation != "ExMarkets" && r.Operation != "ExCount" && r.Operation != "ExInstruments" {
				return nil, i4Error("invalid_ex_identity")
			}
		}
	} else if r.Market > 1 || r.Category != nil ||
		!regexp.MustCompile(`^(sh|sz)[0-9]{6}$`).MatchString(r.Code) ||
		(r.Market == 1) != (r.Code[:2] == "sh") {
		return nil, i4Error("invalid_standard_identity")
	}
	paged := r.Operation == "GetKline" || r.Operation == "GetTrade" || r.Operation == "GetHistoryTrade" ||
		r.Operation == "ExBars" || r.Operation == "ExTrade" || r.Operation == "ExHistTrade" || r.Operation == "ExInstruments"
	limit := uint16(2000)
	if r.Operation == "GetKline" || r.Operation == "ExBars" {
		limit = 800
	}
	if r.Operation == "GetTrade" {
		limit = 1800
	}
	if paged {
		if r.Count == 0 || r.Count > limit {
			return nil, i4Error("invalid_page_count")
		}
	} else if r.Start != 0 || r.Count != 0 {
		return nil, i4Error("unused_page")
	}
	bars := r.Operation == "GetKline" || r.Operation == "ExBars"
	if bars {
		switch r.Period {
		case 0, 1, 2, 3, 7, 8:
		default:
			return nil, i4Error("invalid_intraday_period")
		}
	} else if r.Period != 0 {
		return nil, i4Error("unused_period")
	}
	dated := r.Operation == "GetHistoryMinute" || r.Operation == "GetHistoryTrade" || r.Operation == "GetTrade" || r.Operation == "ExHistMinute" || r.Operation == "ExHistTrade"
	if dated && !i4Date(r.Date) || !dated && r.Date != "" {
		return nil, i4Error("invalid_date_context")
	}
	if r.Operation == "GetKline" {
		if r.Kind != "stock" && r.Kind != "etf" && r.Kind != "index" {
			return nil, i4Error("explicit_kind_required")
		}
	} else if r.Kind != "" {
		return nil, i4Error("unused_kind")
	}
	market := uint8(r.Market)
	day, _ := time.Parse("20060102", r.Date)
	dayNumber := uint32(day.Year()*10000 + int(day.Month())*100 + day.Day())
	switch r.Operation {
	case "GetHistoryMinute":
		return protocol.MHistoryMinute.Frame(r.Date, r.Code)
	case "GetKline":
		return protocol.MKline.Frame(r.Period, r.Code, r.Start, r.Count)
	case "GetTrade":
		return protocol.MTrade.Frame(r.Code, r.Start, r.Count)
	case "GetHistoryTrade":
		return protocol.MHistoryTrade.Frame(r.Date, r.Code, r.Start, r.Count)
	case "ExQuote":
		return protocol.MEx.FrameQuote(market, r.Code), nil
	case "ExBars":
		return protocol.MEx.FrameBars(r.Period, market, r.Code, r.Start, r.Count), nil
	case "ExMinute":
		return protocol.MEx.FrameMinute(market, r.Code), nil
	case "ExHistMinute":
		return protocol.MEx.FrameHistMinute(market, r.Code, dayNumber), nil
	case "ExTrade":
		return protocol.MEx.FrameTrade(market, r.Code, r.Start, r.Count), nil
	case "ExHistTrade":
		return protocol.MEx.FrameHistTrade(market, r.Code, dayNumber, r.Start, r.Count), nil
	case "ExMarkets", "ExCount", "ExInstruments":
		if r.Market != 0 || r.Code != "" || r.Category != nil {
			return nil, i4Error("directory_request_shape")
		}
		switch r.Operation {
		case "ExMarkets":
			return protocol.MEx.FrameMarkets(), nil
		case "ExCount":
			return protocol.MEx.FrameCount(), nil
		default:
			return protocol.MEx.FrameInstrument(uint32(r.Start), r.Count), nil
		}
	default:
		return nil, i4Error("unsupported_operation") // GetMinute is a history alias upstream.
	}
}

// All bounds include handshake traffic; one endpoint/connection, no redial or failover.
type I4Plan struct {
	Schema               string      `json:"schema"`
	SDKCommit            string      `json:"sdk_commit"`
	RunID                string      `json:"run_id"`
	Source               string      `json:"source"`
	Binding              string      `json:"binding"`
	Endpoint             string      `json:"endpoint"`
	Family               string      `json:"family"` // standard or exhq
	DirectoryIdentity    string      `json:"directory_identity"`
	Requests             []I4Request `json:"requests"`
	MaxRequests          int         `json:"max_requests"`
	MaxRequestBytes      int         `json:"max_request_bytes"`
	MaxResponseBytes     int         `json:"max_response_bytes"` // compressed headers+bodies
	MaxDecodedBytes      int         `json:"max_decoded_bytes"`  // before allocating/decompressing payload
	MaxFrames            int         `json:"max_frames"`
	WallSeconds          float64     `json:"wall_seconds"`
	OperationSeconds     float64     `json:"operation_seconds"`
	RoundIntervalSeconds float64     `json:"round_interval_seconds"`
}

func I4ValidatePlan(p I4Plan) error {
	if p.Schema != I4Revision || p.SDKCommit != I4SDKCommit ||
		!regexp.MustCompile(`^[a-f0-9]{32}$`).MatchString(p.RunID) ||
		len(p.Source) < 1 || len(p.Source) > 128 || len(p.Binding) < 1 || len(p.Binding) > 128 ||
		len(p.Requests) < 1 || len(p.Requests) > 24 || p.MaxRequests != len(p.Requests) ||
		p.MaxRequestBytes < 1 || p.MaxRequestBytes > 65536 ||
		p.MaxResponseBytes < 16 || p.MaxResponseBytes > 4*1024*1024 ||
		p.MaxDecodedBytes < 1 || p.MaxDecodedBytes > 4*1024*1024 ||
		p.MaxFrames < len(p.Requests) || p.MaxFrames > 2*len(p.Requests)+1 ||
		!(p.WallSeconds >= 1 && p.WallSeconds <= 180) ||
		!(p.OperationSeconds >= .1 && p.OperationSeconds <= 10) ||
		!(p.RoundIntervalSeconds >= 0 && p.RoundIntervalSeconds <= 10) {
		return i4Error("invalid_plan_budget_or_identity")
	}
	ex := p.Family == "exhq"
	if !ex && p.Family != "standard" {
		return i4Error("invalid_family")
	}
	hosts, port := tdx.Hosts, "7709"
	if ex {
		hosts, port = tdx.ExHosts, tdx.ExPort
	}
	found := false
	for _, host := range hosts {
		if p.Endpoint == net.JoinHostPort(host, port) {
			found = true
			break
		}
	}
	if !found {
		return i4Error("endpoint_not_in_pinned_sdk_family")
	}
	if p.DirectoryIdentity != "" && !regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(p.DirectoryIdentity) {
		return i4Error("invalid_directory_identity")
	}
	size := 0
	setup := protocol.MConnect.Frame()
	if ex {
		setup = protocol.MEx.FrameSetup()
	}
	size += len(setup.Bytes())
	for _, r := range p.Requests {
		if i4Ex(r) != ex {
			return i4Error("mixed_provider_family")
		}
		f, err := I4Frame(r)
		if err != nil {
			return err
		}
		size += len(f.Bytes())
	}
	if size > p.MaxRequestBytes {
		return i4Error("request_bytes_preflight")
	}
	return nil
}

type I4Record struct {
	Peer           string     `json:"peer,omitempty"`
	Plan           *I4Plan    `json:"plan,omitempty"`
	Schema         string     `json:"schema"`
	RunID          string     `json:"run_id"`
	PlanSHA256     string     `json:"plan_sha256"`
	SDKCommit      string     `json:"sdk_commit"`
	Endpoint       string     `json:"endpoint"`
	Index          int        `json:"index"`
	Request        *I4Request `json:"request,omitempty"`
	RequestWire    []byte     `json:"request_wire,omitempty"`
	Wire           []byte     `json:"wire,omitempty"`
	WireSHA256     string     `json:"wire_sha256,omitempty"`
	Payload        []byte     `json:"payload,omitempty"`
	PayloadSHA256  string     `json:"payload_sha256,omitempty"`
	RequestedAt    time.Time  `json:"requested_at"`
	ReceivedAt     time.Time  `json:"received_at"`
	Classification string     `json:"classification"`
	RequestedBytes int        `json:"requested_bytes"`
	WrittenBytes   int        `json:"written_bytes"`
	ResponseBytes  int        `json:"response_bytes"`
	DecodedBytes   int        `json:"decoded_bytes"`
	Frames         int        `json:"frames"`
}

func i4SHA(raw []byte) string { h := sha256.Sum256(raw); return hex.EncodeToString(h[:]) }

// Strict one-frame read. No prefix scanning, unbounded io.ReadAll, or SDK Decode fallback.
func I4ReadFrame(reader io.Reader, wireRemaining, decodedRemaining int) ([]byte, []byte, error) {
	if wireRemaining < 16 {
		return nil, nil, i4Error("response_byte_budget")
	}
	header := make([]byte, 16)
	n, err := io.ReadFull(reader, header)
	if err != nil {
		return header[:n], nil, err
	}
	if !bytes.Equal(header[:4], []byte{0xb1, 0xcb, 0x74, 0x00}) {
		return header, nil, i4Error("response_prefix")
	}
	zipped := int(binary.LittleEndian.Uint16(header[12:14]))
	decoded := int(binary.LittleEndian.Uint16(header[14:16]))
	if 16+zipped > wireRemaining || decoded > decodedRemaining {
		return header, nil, i4Error("response_byte_budget")
	}
	body := make([]byte, zipped)
	n, err = io.ReadFull(reader, body)
	wire := append(header, body[:n]...)
	if err != nil {
		return wire, nil, err
	}
	if header[4]&0x10 == 0 {
		return wire, nil, i4Error("source_response_failure_flag")
	}
	payload := body
	if zipped != decoded {
		// bytes.Reader implements io.ByteReader, so zlib cannot read ahead and
		// hide bytes after the checksum. Read through EOF, then reject any tail.
		compressed := bytes.NewReader(body)
		stream, err := zlib.NewReader(compressed)
		if err != nil {
			return wire, nil, i4Error("invalid_zlib")
		}
		payload, err = io.ReadAll(io.LimitReader(stream, int64(decoded)+1))
		closeErr := stream.Close()
		if err != nil || closeErr != nil {
			return wire, nil, i4Error("invalid_zlib")
		}
		if len(payload) != decoded || compressed.Len() != 0 {
			return wire, nil, i4Error("wire_decompression_bound_or_tail")
		}
	}
	if len(payload) != decoded {
		return wire, nil, i4Error("decoded_length_mismatch")
	}
	return wire, payload, nil
}

type I4Dial func(context.Context, string, string) (net.Conn, error)

// I4Run is inert until explicitly invoked by an independently reviewed Host launcher.
// emit MUST persist synchronously before returning. Any sink failure stops transport.
func I4Run(ctx context.Context, p I4Plan, dial I4Dial, emit func(I4Record) error) error {
	if err := I4ValidatePlan(p); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, seconds(p.WallSeconds))
	defer cancel()
	frozen, _ := json.Marshal(p)
	var canonicalPlan map[string]any
	_ = json.Unmarshal(frozen, &canonicalPlan)
	frozen, _ = json.Marshal(canonicalPlan)
	base := I4Record{Schema: I4Revision, RunID: p.RunID, PlanSHA256: i4SHA(frozen), SDKCommit: I4SDKCommit, Endpoint: p.Endpoint, Index: -1}
	base.Plan = &p
	if err := emit(base); err != nil {
		return err
	} // intent retained before connection
	base.Plan = nil
	began := time.Now().UTC()
	dialCtx, dialCancel := context.WithTimeout(ctx, seconds(p.OperationSeconds))
	conn, err := dial(dialCtx, "tcp", p.Endpoint)
	dialCancel()
	if err != nil {
		base.Classification = classification(err)
		base.RequestedAt = began
		base.ReceivedAt = time.Now().UTC()
		if e := emit(base); e != nil {
			return e
		}
		return err
	}
	defer conn.Close()
	base.Peer = conn.RemoteAddr().String()
	stop := context.AfterFunc(ctx, func() { conn.Close() })
	defer stop()
	setup := protocol.MConnect.Frame()
	if p.Family == "exhq" {
		setup = protocol.MEx.FrameSetup()
	}
	// Setup is written once. Some servers do not acknowledge it; one matching
	// setup frame may be consumed while awaiting an explicitly correlated request.
	setup.MsgID = 0
	base.RequestWire = setup.Bytes()
	base.RequestedAt = time.Now().UTC()
	if err := conn.SetDeadline(operationDeadline(ctx, time.Now(), seconds(p.OperationSeconds))); err != nil {
		return err
	}
	n, err := conn.Write(base.RequestWire)
	base.RequestedBytes = len(base.RequestWire)
	base.WrittenBytes = n
	base.ReceivedAt = time.Now().UTC()
	base.Classification = "setup_written"
	if err != nil || n != len(base.RequestWire) {
		base.Classification = "setup_write_failed"
	}
	if e := emit(base); e != nil {
		return e
	}
	if err != nil || n != len(base.RequestWire) {
		return i4Error("setup_write_failed")
	}
	totalWritten, responseBytes, decodedBytes, frames := n, 0, 0, 0
	setupSeen := false
	for index, request := range p.Requests {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if index > 0 && p.RoundIntervalSeconds > 0 {
			timer := time.NewTimer(seconds(p.RoundIntervalSeconds))
			select {
			case <-timer.C:
			case <-ctx.Done():
				timer.Stop()
				return ctx.Err()
			}
		}
		f, _ := I4Frame(request)
		f.MsgID = uint32(index + 1)
		outbound := f.Bytes()
		record := I4Record{Schema: I4Revision, RunID: p.RunID, PlanSHA256: base.PlanSHA256, SDKCommit: I4SDKCommit,
			Endpoint: p.Endpoint, Peer: base.Peer, Index: index, Request: &request, RequestWire: outbound, RequestedAt: time.Now().UTC()}
		if totalWritten+len(outbound) > p.MaxRequestBytes {
			return i4Error("request_byte_budget")
		}
		deadline := operationDeadline(ctx, time.Now(), seconds(p.OperationSeconds))
		if err := conn.SetDeadline(deadline); err != nil {
			return err
		}
		n, err := conn.Write(outbound)
		totalWritten += n
		record.RequestedBytes = len(outbound)
		record.WrittenBytes = n
		if err != nil || n != len(outbound) {
			record.Classification = "request_write_failed"
			record.ReceivedAt = time.Now().UTC()
			if e := emit(record); e != nil {
				return e
			}
			return i4Error("request_write_failed")
		}
		for {
			if frames >= p.MaxFrames {
				return i4Error("frame_budget")
			}
			wire, payload, err := I4ReadFrame(conn, p.MaxResponseBytes-responseBytes, p.MaxDecodedBytes-decodedBytes)
			responseBytes += len(wire)
			decodedBytes += len(payload)
			frames++
			record.Wire = wire
			record.WireSHA256 = i4SHA(wire)
			record.Payload = payload
			record.PayloadSHA256 = i4SHA(payload)
			record.ReceivedAt = time.Now().UTC()
			record.ResponseBytes = responseBytes
			record.DecodedBytes = decodedBytes
			record.Frames = frames
			if err != nil {
				record.Classification = "response_read_or_validation_failed"
				if e := emit(record); e != nil {
					return e
				}
				return err
			}
			msgID := binary.LittleEndian.Uint32(wire[5:9])
			typ := binary.LittleEndian.Uint16(wire[10:12])
			if !setupSeen && msgID == 0 && typ == setup.Type {
				setupSeen = true
				record.Classification = "setup_response"
				if e := emit(record); e != nil {
					return e
				}
				continue
			}
			if msgID != f.MsgID || typ != f.Type {
				record.Classification = "response_identity_mismatch"
				if e := emit(record); e != nil {
					return e
				}
				return i4Error("response_identity_mismatch")
			}
			record.Classification = "payload_retained_not_semantically_qualified"
			if e := emit(record); e != nil {
				return e
			}
			break
		}
	}
	base.RequestWire = nil
	base.RequestedBytes = totalWritten
	base.WrittenBytes = totalWritten
	base.ResponseBytes = responseBytes
	base.DecodedBytes = decodedBytes
	base.Frames = frames
	base.Classification = "budget_complete"
	base.ReceivedAt = time.Now().UTC()
	return emit(base)
}

// Integration delta: main.go may route exactly --i4-probe to this function after
// Host intake and independent binary pinning. This file registers no flag/init.
func I4HostMain(input io.Reader, output io.Writer, allowLive bool) int {
	if !allowLive {
		return 2
	}
	raw, err := io.ReadAll(io.LimitReader(input, 65537))
	if err != nil || len(raw) > 65536 {
		return 2
	}
	var plan I4Plan
	if strictJSON(raw, &plan) != nil || I4ValidatePlan(plan) != nil {
		return 2
	}
	encoder := json.NewEncoder(output)
	dialer := &net.Dialer{}
	err = I4Run(context.Background(), plan, dialer.DialContext, func(record I4Record) error { return encoder.Encode(record) })
	if err != nil {
		_ = encoder.Encode(map[string]string{"schema": I4Revision, "run_id": plan.RunID, "classification": "stopped", "exception_classification": classification(err)})
		return 1
	}
	return 0
}

// This helper is called only by the reviewed main delta, never by init().
func I4HostOptIn() bool { return os.Getenv("MDF_I4_ALLOW_LIVE") == "1" }

// I4Dispatch requires the central main.go one-line delta and the mdf_i4 build tag.
func I4Dispatch() bool {
	if len(os.Args) != 2 || os.Args[1] != "--i4-probe" {
		return false
	}
	os.Exit(I4HostMain(os.Stdin, os.Stdout, I4HostOptIn()))
	return true
}
