package main

import (
	"runtime"
	"strings"
	"testing"
)

func TestParseMakeTokenPayloadRequiresJSONBody(t *testing.T) {
	for _, command := range []string{
		"__MAKE_TOKEN__",
		"__MAKE_TOKEN__ ",
		"__MAKE_TOKEN__\n",
		"__MAKE_TOKEN__\nnot-json",
	} {
		if _, err := parseMakeTokenPayload(command); err == nil {
			t.Fatalf("payload %q must be rejected", command)
		}
	}
}

func TestParseMakeTokenPayloadRequiresUser(t *testing.T) {
	if _, err := parseMakeTokenPayload(`__MAKE_TOKEN__
{"user":"","domain":"LAB","password":"pw"}`); err == nil {
		t.Fatal("empty user must be rejected")
	}
}

func TestParseMakeTokenPayloadSplitsCombinedIdentity(t *testing.T) {
	payload, err := parseMakeTokenPayload(`__MAKE_TOKEN__
{"user":"LAB\\alice","password":"s3cret pw"}`)
	if err != nil {
		t.Fatalf("combined identity must parse: %v", err)
	}
	if payload.Domain != "LAB" || payload.User != "alice" {
		t.Fatalf("combined identity not split: %#v", payload)
	}
	if payload.Password != "s3cret pw" {
		t.Fatalf("password with spaces must survive parsing: %q", payload.Password)
	}
}

func TestParseMakeTokenPayloadKeepsExplicitDomain(t *testing.T) {
	payload, err := parseMakeTokenPayload(`__MAKE_TOKEN__
{"user":"alice","domain":"LAB","password":"pw"}`)
	if err != nil {
		t.Fatalf("explicit domain must parse: %v", err)
	}
	if payload.Domain != "LAB" || payload.User != "alice" {
		t.Fatalf("explicit identity not preserved: %#v", payload)
	}
}

func TestMakeTokenCommandErrorsCarryNoPassword(t *testing.T) {
	const password = "do-not-leak-me"
	result, err := makeTokenCommand("__MAKE_TOKEN__\n" + `{"user":"LAB\alice","password":"` + password + `"}`)
	if err == nil {
		t.Fatalf("malformed JSON must fail before creating an identity, got result %q", result)
	}
	if strings.Contains(err.Error(), password) || strings.Contains(result, password) {
		t.Fatalf("password leaked in error %q / result %q", err, result)
	}
}

func TestExecuteDispatchesIdentityCommands(t *testing.T) {
	cfg := &config{BaseURL: "http://127.0.0.1:8787", BeaconSecret: "s", SessionID: "go-test"}
	// Invalid input exercises dispatch on Windows without creating a token.
	output, resultType := execute(`__MAKE_TOKEN__
{"user":"","password":"pw"}`, cfg, nil)
	if resultType != "output" || !strings.HasPrefix(output, "Make token failed: ") {
		t.Fatalf("unexpected make_token dispatch: %q / %q", output, resultType)
	}
	// The unsupported-platform adapter can be tested without changing process
	// identity state. Native Windows reset needs a controlled token fixture.
	if runtime.GOOS != "windows" {
		output, resultType = execute("__REV2SELF__", cfg, nil)
		if resultType != "output" || !strings.HasPrefix(output, "Rev2self failed: ") {
			t.Fatalf("unexpected rev2self dispatch: %q / %q", output, resultType)
		}
	}
	if strings.Contains(output, "password") {
		t.Fatalf("credential material must not appear in rev2self output: %q", output)
	}
}
