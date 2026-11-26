package main

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

type correlationTransport func(*http.Request) (*http.Response, error)

func (f correlationTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestPollOnceIncludesCommandInTextResult(t *testing.T) {
	commands := []string{"echo first", "echo second"}
	var results []map[string]string
	client := &http.Client{Transport: correlationTransport(func(r *http.Request) (*http.Response, error) {
		body := "{}"
		switch r.URL.Path {
		case "/cmd":
			encoded, _ := json.Marshal(map[string]string{"command": commands[len(results)]})
			body = string(encoded)
		case "/result":
			var result map[string]string
			if err := json.NewDecoder(r.Body).Decode(&result); err != nil {
				t.Error(err)
			}
			results = append(results, result)
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}, nil
	})}
	cfg := &config{BaseURL: "http://example.test", SessionID: "test"}
	for range commands {
		if err := pollOnce(client, cfg, &pluginController{}, &pluginController{}); err != nil {
			t.Fatal(err)
		}
	}
	for i, command := range commands {
		if results[i]["rmm_cmd"] != command || results[i]["rmm_output"] == "" {
			t.Fatalf("result %d: %#v", i, results[i])
		}
	}
}
