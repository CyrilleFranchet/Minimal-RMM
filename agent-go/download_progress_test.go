package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestDownloadProgressAndExactChunks(t *testing.T) {
	path := filepath.Join(t.TempDir(), "Logfile.PML")
	content := make([]byte, 2*1024*1024+17)
	for index := range content {
		content[index] = byte(index % 251)
	}
	if err := os.WriteFile(path, content, 0600); err != nil {
		t.Fatal(err)
	}
	var sizes []float64
	var received []byte
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
			return
		}
		switch r.URL.Query().Get("type") {
		case "download_progress":
			if payload["remote_path"] != path || payload["total_bytes"] != float64(len(content)) {
				t.Error(payload)
			}
			sizes = append(sizes, payload["bytes"].(float64))
		case "file_upload":
			chunk, err := base64.StdEncoding.DecodeString(payload["content"].(string))
			if err != nil {
				t.Error(err)
			}
			received = append(received, chunk...)
		default:
			t.Error("unexpected result type")
		}
	}))
	defer server.Close()
	if err := sendFileDownload(server.Client(), &config{BaseURL: server.URL, SessionID: "test"}, path); err != nil {
		t.Fatal(err)
	}
	if len(received) != len(content) || len(sizes) != 2 || sizes[0] != 0 || sizes[1] != 2*1024*1024 {
		t.Fatalf("bytes=%d progress=%v", len(received), sizes)
	}
	if !bytes.Equal(received, content) {
		t.Fatal("downloaded bytes differ")
	}
}

func TestDownloadProgressFailureDoesNotAbortEmptyFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "empty")
	if err := os.WriteFile(path, nil, 0600); err != nil {
		t.Fatal(err)
	}
	completed := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("type") == "download_progress" {
			w.WriteHeader(503)
			return
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Error(err)
			return
		}
		completed = payload["eof"] == true && payload["content"] == ""
	}))
	defer server.Close()
	if err := sendFileDownload(server.Client(), &config{BaseURL: server.URL, SessionID: "test"}, path); err != nil {
		t.Fatal(err)
	}
	if !completed {
		t.Fatal("empty file never completed")
	}
}

func TestDownloadProgressTimeoutDoesNotWaitForDefaultHTTPTimeout(t *testing.T) {
	path := filepath.Join(t.TempDir(), "empty")
	if err := os.WriteFile(path, nil, 0600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("type") == "download_progress" {
			time.Sleep(2500 * time.Millisecond)
		}
	}))
	defer server.Close()
	started := time.Now()
	if err := sendFileDownload(server.Client(), &config{BaseURL: server.URL, SessionID: "test"}, path); err != nil {
		t.Fatal(err)
	}
	if time.Since(started) > 2400*time.Millisecond {
		t.Fatal("advisory progress exceeded its deadline")
	}
}
