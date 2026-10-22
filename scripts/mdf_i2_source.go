// Independent, explicitly reviewed DEV discovery source; never the runtime driver.
// Uses only the pinned SDK protocol package. No SDK Client, caches or redial.
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
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/injoyai/tdx/protocol"
)

// Set by the offline builder from the exact pinned hosts.go bytes.
var verifiedHosts = ""

const revision = "2a9000681f5a9896ea98e4de8bc4904f1dce78ba"

type request struct {
	Operation string `json:"operation"`
	Market    int    `json:"market"`
	Code      string `json:"code"`
	Start     int    `json:"start"`
	Count     int    `json:"count"`
}
type plan struct {
	Endpoint         string    `json:"endpoint"`
	Requests         []request `json:"requests"`
	MaxRequestBytes  int       `json:"max_request_bytes"`
	MaxResponseBytes int       `json:"max_response_bytes"`
	WallSeconds      int       `json:"wall_seconds"`
	Deadline         string    `json:"deadline"`
}
type result struct {
	Index          int             `json:"index"`
	Request        request         `json:"request"`
	Requested      string          `json:"requested_at"`
	Received       string          `json:"received_at"`
	Peer           string          `json:"peer"`
	RequestWire    []byte          `json:"request_wire"`
	ResponseWire   []byte          `json:"response_wire"`
	Payload        json.RawMessage `json:"source_projection"`
	Classification string          `json:"classification"`
	Upstream       string          `json:"upstream_revision"`
}

func frame(r request) (*protocol.Frame, error) {
	switch r.Operation {
	case "GetCount":
		return protocol.MCount.Frame(protocol.Exchange(r.Market)), nil
	case "GetCode":
		return protocol.MCode.Frame(protocol.Exchange(r.Market), uint16(r.Start)), nil
	case "GetKline":
		prefix := "sz"
		if r.Market == 1 {
			prefix = "sh"
		}
		return protocol.MKline.Frame(protocol.TypeKlineDay, prefix+r.Code, uint16(r.Start), uint16(r.Count))
	}
	return nil, errors.New("unsupported_operation")
}
func valid(p plan) bool {
	if deadline, err := time.Parse(time.RFC3339Nano, p.Deadline); err != nil || !time.Now().Before(deadline) {
		return false
	}
	host, port, err := net.SplitHostPort(p.Endpoint)
	if err != nil || net.ParseIP(host) == nil || !strings.Contains(","+verifiedHosts+",", ","+host+",") || port != "7709" || len(p.Requests) < 1 || len(p.Requests) > 64 || p.MaxRequestBytes < 13 || p.MaxRequestBytes > 8192 || p.MaxResponseBytes < 16 || p.MaxResponseBytes > 4194304 || p.WallSeconds < 1 || p.WallSeconds > 120 {
		return false
	}
	total := 13 // one explicit MConnect, no probes
	for _, r := range p.Requests {
		if r.Market < 0 || r.Market > 1 || r.Start < 0 || r.Start > 65535 {
			return false
		}
		switch r.Operation {
		case "GetCount":
			if r.Code != "" || r.Count != 0 || r.Start != 0 {
				return false
			}
		case "GetCode":
			if r.Code != "" || r.Count != 1000 {
				return false
			}
		case "GetKline":
			if !regexp.MustCompile(`^[0-9]{6}$`).MatchString(r.Code) || r.Count < 1 || r.Count > 800 || (r.Market == 1 && r.Code[0] != '6') || (r.Market == 0 && r.Code[0] != '0' && r.Code[0] != '3') {
				return false
			}
		default:
			return false
		}
		f, e := frame(r)
		if e != nil {
			return false
		}
		total += len(f.Bytes())
	}
	return total <= p.MaxRequestBytes
}
func class(err error) string {
	if errors.Is(err, os.ErrPermission) {
		return "execution_access_restricted"
	}
	var n net.Error
	if errors.As(err, &n) && n.Timeout() {
		return "timeout"
	}
	if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
		return "truncated_response"
	}
	return "source_or_transport_error"
}

// Retain exact bytes, including partial frames. No prefix resynchronization loop.
func exchange(c net.Conn, f *protocol.Frame, remaining *int) (sent, raw, payload []byte, err error) {
	wire := f.Bytes()
	if *remaining < 16 {
		err = errors.New("response_budget")
		return
	}
	n, err := c.Write(wire)
	sent = append([]byte(nil), wire[:n]...)
	if err != nil {
		return
	}
	if n != len(wire) {
		err = io.ErrShortWrite
		return
	}
	header := make([]byte, 16)
	n, err = io.ReadFull(c, header)
	raw = append(raw, header[:n]...)
	*remaining -= n
	if err != nil {
		return
	}
	size := int(binary.LittleEndian.Uint16(header[12:14]))
	unpacked := int(binary.LittleEndian.Uint16(header[14:16]))
	if binary.BigEndian.Uint32(header[:4]) != protocol.PrefixResp || binary.LittleEndian.Uint32(header[5:9]) != f.MsgID || binary.LittleEndian.Uint16(header[10:12]) != f.Type || header[4]&0x10 == 0 || size > *remaining {
		err = errors.New("response_header_or_budget")
		return
	}
	body := make([]byte, size)
	n, err = io.ReadFull(c, body)
	raw = append(raw, body[:n]...)
	*remaining -= n
	if err != nil {
		return
	}
	payload = body
	if size != unpacked {
		var z io.ReadCloser
		z, err = zlib.NewReader(bytes.NewReader(body))
		if err != nil {
			return
		}
		payload, err = io.ReadAll(io.LimitReader(z, int64(unpacked)+1))
		z.Close()
		if err != nil {
			return
		}
	}
	if len(payload) != unpacked {
		err = errors.New("decompression_length")
		return
	}
	return
}

// Validate consumption using v1.0.0 model_kline.go / types_price.go:
// KindStock = time[4], four GetPrice varints (0x80 continuation),
// volume[4], amount[4]. The SDK does not report unconsumed bytes and returns
// zero without consuming an unterminated price. Do not invent a fixed row or
// varint width: traversal is bounded by the already bounded payload and count.
// Price interpretation remains exclusively with the pinned SDK.
func validateStockKlinePayload(payload []byte, count int) error {
	pos := 2
	for i := 0; i < count; i++ {
		if len(payload)-pos < 4 {
			return io.ErrUnexpectedEOF
		}
		pos += 4
		for field := 0; field < 4; field++ {
			for {
				if pos == len(payload) {
					return io.ErrUnexpectedEOF
				}
				b := payload[pos]
				pos++
				if b&0x80 == 0 {
					break
				}
			}
		}
		if len(payload)-pos < 8 {
			return io.ErrUnexpectedEOF
		}
		pos += 8
	}
	if pos != len(payload) {
		return errors.New("kline_trailing_payload")
	}
	return nil
}

func decode(r request, payload []byte) (data []byte, err error) {
	defer func() {
		if recover() != nil {
			err = errors.New("decoder_panic")
			data = nil
		}
	}()
	var value any
	switch r.Operation {
	case "GetCount":
		value, err = protocol.MCount.Decode(payload)
	case "GetCode":
		if len(payload) < 2 || int(binary.LittleEndian.Uint16(payload[:2])) > 1000 || int(binary.LittleEndian.Uint16(payload[:2])) > r.Count || len(payload) != 2+29*int(binary.LittleEndian.Uint16(payload[:2])) {
			return nil, errors.New("directory_length_or_item_budget")
		}
		value, err = protocol.MCode.Decode(payload)
	case "GetKline":
		if len(payload) < 2 || int(binary.LittleEndian.Uint16(payload[:2])) > r.Count {
			return nil, errors.New("kline_item_budget")
		}
		if err := validateStockKlinePayload(payload, int(binary.LittleEndian.Uint16(payload[:2]))); err != nil {
			return nil, err
		}
		prefix := "sz"
		if r.Market == 1 {
			prefix = "sh"
		}
		value, err = protocol.MKline.Decode(payload, protocol.KlineCache{Type: protocol.TypeKlineDay, Kind: protocol.KindStock, Code: prefix + r.Code})
	}
	if err != nil {
		return nil, err
	}
	return json.Marshal(value)
}
func run(p plan, dial func(context.Context, string, string) (net.Conn, error), out io.Writer) int {
	enc := json.NewEncoder(out)
	emit := func(r result) { r.Upstream = revision; enc.Encode(r) }
	absolute, err := time.Parse(time.RFC3339Nano, p.Deadline)
	if err != nil || !time.Now().Before(absolute) {
		emit(result{Index: -1, Classification: "execution_deadline"})
		return 1
	}
	hard := time.Now().Add(time.Duration(p.WallSeconds) * time.Second)
	if absolute.Before(hard) {
		hard = absolute
	}
	ctx, cancel := context.WithDeadline(context.Background(), hard)
	defer cancel()
	c, err := dial(ctx, "tcp", p.Endpoint)
	if err != nil {
		emit(result{Index: -1, Classification: class(err)})
		return 1
	}
	defer c.Close()
	remaining := p.MaxResponseBytes
	// Independent finite handshake. Every request follows a fully retained response.
	for i := -1; i < len(p.Requests); i++ {
		if !time.Now().Before(hard) {
			emit(result{Index: i, Classification: "execution_deadline"})
			return 1
		}
		r := request{Operation: "MConnect"}
		f := protocol.MConnect.Frame()
		if i >= 0 {
			r = p.Requests[i]
			f, err = frame(r)
			if err != nil {
				return 1
			}
		}
		f.MsgID = uint32(i + 2)
		end := time.Now().Add(3 * time.Second)
		if hard.Before(end) {
			end = hard
		}
		if err = c.SetDeadline(end); err != nil {
			return 1
		}
		row := result{Index: i, Request: r, Peer: c.RemoteAddr().String(), Requested: time.Now().UTC().Format(time.RFC3339Nano)}
		var payload []byte
		row.RequestWire, row.ResponseWire, payload, err = exchange(c, f, &remaining)
		row.Received = time.Now().UTC().Format(time.RFC3339Nano)
		if err != nil {
			row.Classification = class(err)
			emit(row)
			return 1
		}
		row.Classification = "retained_raw"
		if i >= 0 {
			row.Payload, err = decode(r, payload)
			if err != nil {
				row.Classification = "decode_invalid"
			}
		}
		emit(row)
		if err != nil {
			return 1
		}
	}
	return 0
}
func main() {
	// All diagnostics are classified JSON; no SDK logger/client is initialized.
	if len(os.Args) != 2 || os.Args[1] != "--live" {
		os.Exit(2)
	}
	var p plan
	d := json.NewDecoder(io.LimitReader(os.Stdin, 65537))
	d.DisallowUnknownFields()
	if d.Decode(&p) != nil || d.Decode(new(any)) != io.EOF || !valid(p) {
		os.Exit(2)
	}
	time.Local = time.FixedZone("Asia/Shanghai", 8*3600) // explicit SDK projection clock; never source event time
	dialer := net.Dialer{Timeout: 2 * time.Second}
	os.Exit(run(p, dialer.DialContext, os.Stdout))
}
