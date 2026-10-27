//go:build windows

package main

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// pluginTestHost exercises the actual parent download and child launch path.
func pluginTestHost(t *testing.T) (func(context.Context, string, string) (string, error), string) {
	t.Helper()
	pluginPath, agentPath := os.Getenv("RMM_MSVC_TEST_DLL"), os.Getenv("RMM_MSVC_TEST_AGENT")
	if pluginPath == "" || agentPath == "" {
		t.Skip("set RMM_MSVC_TEST_DLL and RMM_MSVC_TEST_AGENT for Windows integration tests")
	}
	plugin, err := os.ReadFile(pluginPath)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/tools/plugins/test.dll" ||
			r.URL.Query().Get("id") != "test-session" || r.Header.Get("X-RMM-Beacon-Token") != "test-secret" {
			t.Error("plugin download did not preserve beacon authentication and session")
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		_, _ = w.Write(plugin)
	}))
	t.Cleanup(server.Close)
	original := os.Args[0]
	os.Args[0] = agentPath
	t.Cleanup(func() { os.Args[0] = original })
	root := filepath.Join(t.TempDir(), "staging with spaces ü")
	if err := os.Mkdir(root, 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("TEMP", root)
	t.Setenv("TMP", root)
	cfg := &config{BaseURL: server.URL, SessionID: "test-session", BeaconSecret: "test-secret"}
	return func(ctx context.Context, export, input string) (string, error) {
		return loadPluginPEContext(ctx, server.Client(), cfg, "/tools/plugins/test.dll", export, input)
	}, root
}

// assertPluginCleanup checks both the staged DLL and its directory disappeared.
func assertPluginCleanup(t *testing.T, root string) {
	t.Helper()
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("plugin staging left %d entries after child exit", len(entries))
	}
}

// TestWindowsPluginLifecycle covers CRT, new-thread TLS, errors, and cancellation.
func TestWindowsPluginLifecycle(t *testing.T) {
	invoke, root := pluginTestHost(t)
	cases := []struct{ name, export, input, want, failure string }{
		{"constructor", "Run", "hello", "msvc-ready", ""},
		{"module", "Run", "module", "registered", ""},
		{"unwind", "Run", "unwind", "registered", ""},
		{"exception", "Run", "throw", "caught", ""},
		{"worker_tls", "Run", "thread", "thread-tls-ready", ""},
		{"plugin_error", "Run", "fail", "", "fixture failure"},
		{"missing_export", "MissingExport", "", "", "plugin export unavailable"},
		{"native_exit", "Run", "crash", "", "plugin child failed"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
			defer cancel()
			result, err := invoke(ctx, tc.export, tc.input)
			if tc.failure != "" {
				if err == nil || !strings.Contains(err.Error(), tc.failure) {
					t.Fatalf("expected %q, got %v", tc.failure, err)
				}
			} else if err != nil || result != tc.want {
				t.Fatalf("result=%q error=%v, want %q", result, err, tc.want)
			}
			assertPluginCleanup(t, root)
		})
	}
	t.Run("cancel_loaded_child", func(t *testing.T) {
		ready := filepath.Join(filepath.Dir(root), "ready")
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		done := make(chan error, 1)
		go func() { _, err := invoke(ctx, "Run", "wait:"+ready); done <- err }()
		for {
			if _, err := os.Stat(ready); err == nil {
				break
			}
			select {
			case err := <-done:
				t.Fatalf("child stopped before readiness: %v", err)
			case <-ctx.Done():
				t.Fatal("child never signaled readiness")
			case <-time.After(10 * time.Millisecond):
			}
		}
		cancel()
		select {
		case err := <-done:
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("cancel error = %v", err)
			}
		case <-time.After(10 * time.Second):
			t.Fatal("canceled child did not exit")
		}
		assertPluginCleanup(t, root)
	})
}

// TestLegacyPluginLifecycle keeps unmarked DLLs on the original loader path.
func TestLegacyPluginLifecycle(t *testing.T) {
	invoke, root := pluginTestHost(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	result, err := invoke(ctx, "Run", "")
	if err != nil || result != "legacy-ready" {
		t.Fatalf("legacy result=%q error=%v", result, err)
	}
	assertPluginCleanup(t, root)
}

// TestDocumentEmbeddingsPlugin verifies a real model result and staging cleanup.
func TestDocumentEmbeddingsPlugin(t *testing.T) {
	input := os.Getenv("RMM_MSVC_TEST_INPUT")
	if input == "" {
		t.Skip("set RMM_MSVC_TEST_INPUT to a public-model request")
	}
	invoke, root := pluginTestHost(t)
	var request map[string]json.RawMessage
	if err := json.Unmarshal([]byte(input), &request); err != nil {
		t.Fatal(err)
	}
	var documents []string
	if err := json.Unmarshal(request["documents"], &documents); err != nil || len(documents) != 1 {
		t.Fatal("real-model fixture needs one strategic document")
	}
	reference := filepath.Join(filepath.Dir(root), "reference.txt")
	if err := os.WriteFile(reference, []byte("General reference notes about everyday office supplies."), 0600); err != nil {
		t.Fatal(err)
	}
	documents = append(documents, reference, filepath.Join(filepath.Dir(root), "missing.txt"))
	request["documents"], _ = json.Marshal(documents)
	requestBytes, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	requests := []struct{ name, input string }{{"minilm_mean", string(requestBytes)}}
	request["model"] = json.RawMessage(`{
		"repo":"Xenova/bge-small-en-v1.5",
		"revision":"ea104dacec62c0de699686887e3f920caeb4f3e3",
		"onnx_file":"onnx/model_quantized.onnx",
		"tokenizer_file":"tokenizer.json",
		"sha256":"6c9c6101a956d62dfb5e7190c538226c0c5bb9cb27b651234b6df063ee7dbfe4",
		"input_names":["input_ids","attention_mask","token_type_ids"],
		"output_name":"last_hidden_state","pooling":"cls"
	}`)
	request["prompt"], request["label_prompt"] = json.RawMessage(`""`), json.RawMessage(`""`)
	requestBytes, err = json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	requests = append(requests, struct{ name, input string }{"bge_quantized_cls", string(requestBytes)})
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	defer cancel()
	for _, testRequest := range requests {
		t.Run(testRequest.name, func(t *testing.T) {
			result, err := invoke(ctx, "Run", testRequest.input)
			if err != nil {
				t.Fatal(err)
			}
			var response struct {
				Results []struct {
					Path           string    `json:"path"`
					Embedding      []float64 `json:"embedding"`
					Error          string    `json:"error"`
					Classification *struct {
						Label string  `json:"label"`
						Score float64 `json:"score"`
					} `json:"classification"`
				} `json:"results"`
			}
			if err := json.Unmarshal([]byte(result), &response); err != nil {
				t.Fatal(err)
			}
			if len(response.Results) != len(documents) {
				t.Fatalf("got %d document results", len(response.Results))
			}
			for i, doc := range response.Results {
				if doc.Path != documents[i] {
					t.Fatal("document result order or path changed")
				}
				if i == 2 {
					if doc.Error == "" || len(doc.Embedding) != 0 || doc.Classification != nil {
						t.Fatal("missing document did not produce an isolated error")
					}
					continue
				}
				if doc.Error != "" || len(doc.Embedding) != 384 {
					t.Fatalf("embedding dimension=%d error=%q", len(doc.Embedding), doc.Error)
				}
				var norm float64
				for _, value := range doc.Embedding {
					norm += value * value
				}
				if math.IsNaN(norm) || math.Abs(norm-1) > 1e-4 {
					t.Fatalf("embedding squared norm=%g", norm)
				}
			}
			doc := response.Results[0]
			if doc.Classification == nil || doc.Classification.Label != "strategic" || math.IsNaN(doc.Classification.Score) || math.Abs(doc.Classification.Score) > 1.0001 {
				t.Fatalf("unexpected classification: %+v", doc.Classification)
			}
			assertPluginCleanup(t, root)
		})
	}
	// A rejected request must also return its error and remove the staged DLL.
	_, err = invoke(ctx, "Run", "{}")
	if err == nil || !strings.Contains(err.Error(), "model.repo") {
		t.Fatalf("invalid-input error=%v", err)
	}
	assertPluginCleanup(t, root)
}
