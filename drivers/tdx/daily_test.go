package main

import (
	"bytes"
	"io"
	"net"
	"strings"
	"sync"
	"testing"
)

func TestDailyCapabilityBounds(t *testing.T) {
	r := dailyRequest{ID: strings.Repeat("a", 32), Operation: "GetKline", Market: 2, Code: "920002", Count: 800}
	if !r.valid() {
		t.Fatal("BJ daily request rejected")
	}
	for _, mutate := range []func(*dailyRequest){
		func(r *dailyRequest) { r.Market = 3 },
		func(r *dailyRequest) { r.Code = "000001" },
		func(r *dailyRequest) { r.Count = 801 },
		func(r *dailyRequest) { r.Count = 0 },
		func(r *dailyRequest) { r.Start = 65536 },
		func(r *dailyRequest) { r.Start = -1 },
		func(r *dailyRequest) { r.Operation = "GetQuote" },
		func(r *dailyRequest) { r.ID = "unbound" },
	} {
		bad := r
		mutate(&bad)
		if bad.valid() {
			t.Fatalf("out-of-capability request accepted: %+v", bad)
		}
	}
	r = dailyRequest{ID: r.ID, Operation: "GetBJDirectory", Market: 2}
	if !r.valid() {
		t.Fatal("BJ file directory rejected")
	}
	r.Operation = "GetCode"
	r.Count = 1000
	if r.valid() {
		t.Fatal("BJ incorrectly routed to SH/SZ directory")
	}
}

func TestDailyWireCaptureAndConcurrentSnapshots(t *testing.T) {
	a, b := net.Pipe()
	defer a.Close()
	defer b.Close()
	c := &dailyConn{Conn: a}
	done := make(chan error, 1)
	go func() {
		wire := make([]byte, 7)
		_, err := io.ReadFull(b, wire)
		if err == nil && !bytes.Equal(wire, []byte("request")) {
			err = io.ErrUnexpectedEOF
		}
		if err == nil {
			_, err = b.Write([]byte("response"))
		}
		done <- err
	}()
	if _, err := c.Write([]byte("request")); err != nil {
		t.Fatal(err)
	}
	if _, err := io.ReadFull(c, make([]byte, 8)); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	sent, received, overflow := c.snapshot()
	if string(sent) != "request" || string(received) != "response" || overflow {
		t.Fatal("wire evidence changed")
	}
	var wg sync.WaitGroup
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for n := 0; n < 100; n++ {
				c.record(&c.sent, []byte{1})
				c.snapshot()
				c.reset()
			}
		}()
	}
	wg.Wait()
	// A snapshot owns its bytes even after the following request resets buffers.
	if string(sent) != "request" || string(received) != "response" {
		t.Fatal("prior request evidence was overwritten")
	}
}
