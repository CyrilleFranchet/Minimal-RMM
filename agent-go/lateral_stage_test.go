package main

import (
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// serverStageMask reproduces the server implementation independently of the
// agent code under test, so the keystream contract stays pinned.
func serverStageMask(data []byte, stagingID string) []byte {
	seed := stagingID
	dataCopy := append([]byte(nil), data...)
	for offset := 0; offset < len(dataCopy); offset += sha256.Size {
		counter := make([]byte, 8)
		binary.BigEndian.PutUint64(counter, uint64(offset/sha256.Size))
		block := sha256.Sum256(append([]byte(seed), counter...))
		for index := 0; index < sha256.Size && offset+index < len(dataCopy); index++ {
			dataCopy[offset+index] ^= block[index]
		}
	}
	return dataCopy
}

func TestLateralStageUnmaskMatchesListenerMask(t *testing.T) {
	stagingID := "0123456789abcdef0123456789abcdef"
	payload := append([]byte("MZ"), make([]byte, 300)...)
	for i := range payload {
		payload[i] = byte(i * 7)
	}
	masked := serverStageMask(payload, stagingID)
	if string(masked) == string(payload) {
		t.Fatal("masked payload must differ from the artifact")
	}
	unmasked := lateralStageUnmask(masked, stagingID)
	if string(unmasked) != string(payload) {
		t.Fatal("unmask must restore the exact staged artifact bytes")
	}
	wrong := lateralStageUnmask(masked, "ffffffffffffffffffffffffffffffff")
	if string(wrong) == string(payload) {
		t.Fatal("a different staging identifier must not restore the artifact")
	}
}

func TestLateralStageFetchVerifiesAndServeOnce(t *testing.T) {
	stagingID := "0123456789abcdef0123456789abcdef"
	artifact := []byte("MZ staged artifact bytes for the loader chain")
	digest := fmt.Sprintf("%x", sha256.Sum256(artifact))
	fetched := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/tools/stage/"+stagingID {
			http.NotFound(w, r)
			return
		}
		// The listener serves a staging entry once per fetch; extra retries
		// see the consumed-entry failure.
		fetched++
		if fetched > 2 {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write(serverStageMask(artifact, stagingID))
	}))
	defer server.Close()
	cfg := &config{BaseURL: server.URL}
	got, err := lateralStageFetch(cfg, stagingID, digest, 10*time.Second)
	if err != nil {
		t.Fatalf("fetch must succeed: %v", err)
	}
	if string(got) != string(artifact) {
		t.Fatal("fetch must return the unmasked artifact bytes")
	}
	if fetched != 1 {
		t.Fatalf("expected one fetch, got %d", fetched)
	}
	if _, err := lateralStageFetch(cfg, stagingID, strings.Repeat("b", 64), 10*time.Second); err == nil ||
		!strings.Contains(err.Error(), "digest mismatch") {
		t.Fatalf("digest mismatch must fail: %v", err)
	}
	if _, err := lateralStageFetch(cfg, stagingID, digest, 10*time.Second); err == nil {
		t.Fatal("a consumed staging entry must fail the retry")
	}
	for name, check := range map[string]func() error{
		"invalid digest field": func() error {
			_, err := lateralStageFetch(cfg, stagingID, "zz", time.Second)
			return err
		},
		"missing base URL": func() error {
			_, err := lateralStageFetch(&config{}, stagingID, digest, time.Second)
			return err
		},
	} {
		if err := check(); err == nil {
			t.Fatalf("%s must fail", name)
		}
	}
}
