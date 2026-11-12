// P0 stock daily capability. Explicit IPC opt-in, one connection per worker.
package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"regexp"
	"sync"
	"time"

	"github.com/injoyai/ios"
	"github.com/injoyai/ios/client"
	"github.com/injoyai/logs"
	"github.com/injoyai/tdx"
	"github.com/injoyai/tdx/protocol"
)

type dailyRequest struct {
	ID        string `json:"id"`
	Operation string `json:"operation"`
	Market    int    `json:"market"`
	Code      string `json:"code"`
	Start     int    `json:"start"`
	Count     int    `json:"count"`
}

func (r dailyRequest) valid() bool {
	if !regexp.MustCompile(`^[a-f0-9]{32}$`).MatchString(r.ID) || r.Market < 0 || r.Market > 2 || r.Start < 0 || r.Start > 65535 {
		return false
	}
	switch r.Operation {
	case "GetCount":
		return r.Market < 2 && r.Code == "" && r.Start == 0 && r.Count == 0
	case "GetCode":
		return r.Market < 2 && r.Code == "" && r.Count == 1000
	case "GetBJDirectory":
		return r.Market == 2 && r.Code == "" && r.Start == 0 && r.Count == 0
	case "GetKline":
		prefix := []string{`^(000|001|002|003|300|301|302)[0-9]{3}$`, `^(600|601|603|605|688)[0-9]{3}$`, `^(4|8|92)[0-9]{4,5}$`}[r.Market]
		return len(r.Code) == 6 && regexp.MustCompile(prefix).MatchString(r.Code) && r.Count >= 1 && r.Count <= 800
	}
	return false
}

type dailyConfig struct {
	Endpoint string `json:"endpoint"`
	Timeout  int    `json:"timeout_seconds"`
}
type dailyConn struct {
	net.Conn
	mu             sync.Mutex
	sent, received bytes.Buffer
	overflow       bool
}

func (c *dailyConn) record(buffer *bytes.Buffer, data []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if buffer.Len()+len(data) > 64*1024*1024 {
		c.overflow = true
		return
	}
	buffer.Write(data)
}
func (c *dailyConn) Read(p []byte) (int, error) {
	n, e := c.Conn.Read(p)
	c.record(&c.received, p[:n])
	return n, e
}
func (c *dailyConn) Write(p []byte) (int, error) {
	n, e := c.Conn.Write(p)
	c.record(&c.sent, p[:n])
	return n, e
}
func (c *dailyConn) reset() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.sent.Reset()
	c.received.Reset()
	c.overflow = false
}
func (c *dailyConn) snapshot() ([]byte, []byte, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]byte(nil), c.sent.Bytes()...), append([]byte(nil), c.received.Bytes()...), c.overflow
}
func dailyFrame(value any) error {
	b, e := json.Marshal(value)
	if e != nil {
		return e
	}
	if len(b) > 96*1024*1024 {
		return errors.New("daily_frame_budget")
	}
	h := make([]byte, 5)
	h[0] = 'D'
	binary.BigEndian.PutUint32(h[1:], uint32(len(b)))
	_, e = os.Stdout.Write(append(h, b...))
	return e
}
func dailyDispatch() bool {
	if len(os.Args) != 2 || os.Args[1] != "--daily-live" {
		return false
	}
	logs.SetWriter(io.Discard)
	// No default source node, discovery, redial, HTTP server, SQLite or code cache.
	raw, e := readFrame(os.Stdin, 'C', 4096)
	var cfg dailyConfig
	if e != nil || strictJSON(raw, &cfg) != nil || !validEndpoint(cfg.Endpoint) || cfg.Timeout < 1 || cfg.Timeout > 300 {
		os.Exit(2)
	}
	var transport *client.Client
	var socket *dailyConn
	dialed := false
	dial := func(ctx context.Context) (ios.ReadWriteCloser, string, error) {
		if dialed {
			return nil, cfg.Endpoint, net.ErrClosed
		}
		dialed = true
		transport.Event.OnConnected = func(cl *client.Client) error { _, e := cl.Write(protocol.MConnect.Frame().Bytes()); return e }
		c, e := (&net.Dialer{Timeout: 5 * time.Second}).DialContext(ctx, "tcp", cfg.Endpoint)
		if e != nil {
			return nil, cfg.Endpoint, e
		}
		socket = &dailyConn{Conn: c}
		c.SetDeadline(time.Now().Add(time.Duration(cfg.Timeout) * time.Second))
		return socket, cfg.Endpoint, nil
	}
	sdk, e := tdx.DialWith(dial, tdx.WithRedial(false), tdx.WithDebug(false), tdx.WithLevel(tdx.LevelNone), func(cl *client.Client) { transport = cl; cl.Event.OnWrite = client.NewWriteSafe() })
	if e != nil {
		dailyFrame(map[string]any{"classification": classification(e), "stage": "connect", "sdk_release": release})
		return true
	}
	defer sdk.Close()
	defer socket.Close()
	sdk.SetTimeout(time.Duration(cfg.Timeout) * time.Second)
	dailyFrame(map[string]any{"classification": "connected", "peer": socket.RemoteAddr().String(), "sdk_release": release})
	for {
		raw, e = readFrame(os.Stdin, 'R', 4096)
		if e == io.EOF {
			return true
		}
		if e != nil {
			return true
		}
		var r dailyRequest
		if strictJSON(raw, &r) != nil || !r.valid() {
			dailyFrame(map[string]any{"classification": "request_invalid"})
			return true
		}
		socket.reset()
		socket.SetDeadline(time.Now().Add(time.Duration(cfg.Timeout) * time.Second))
		requested := time.Now().UTC()
		var result any
		switch r.Operation {
		case "GetCount":
			result, e = sdk.GetCount(protocol.Exchange(r.Market))
		case "GetCode":
			result, e = sdk.GetCode(protocol.Exchange(r.Market), uint16(r.Start))
		case "GetBJDirectory":
			result, e = sdk.GetCodeAll(protocol.Exchange(2))
		case "GetKline":
			result, e = sdk.GetKline(protocol.TypeKlineDay, []string{"sz", "sh", "bj"}[r.Market]+r.Code, uint16(r.Start), uint16(r.Count))
		}
		received := time.Now().UTC()
		sent, wire, overflow := socket.snapshot()
		state := "retained_raw"
		if e != nil {
			state = classification(e)
		}
		if overflow {
			state = "wire_budget_exceeded"
		}
		if dailyFrame(map[string]any{"request": r, "requested_at": requested, "received_at": received, "peer": socket.RemoteAddr().String(), "sdk_release": release, "classification": state, "request_wire": sent, "response_wire": wire, "source_projection": result}) != nil {
			return true
		}
		if state != "retained_raw" {
			return true
		}
	}
}
