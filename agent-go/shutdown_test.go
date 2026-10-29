package main

import (
	"context"
	"errors"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"runtime"
	"syscall"
	"testing"
	"time"
)

func TestShutdownCancelsBeaconSleep(t *testing.T) {
	previous := agentContext
	defer func() { agentContext = previous }()
	ctx, cancel := context.WithCancel(context.Background())
	agentContext = ctx
	cancel()
	started := time.Now()
	sleepWithJitter(&config{SleepSeconds: 3600}, rand.New(rand.NewSource(1)))
	if time.Since(started) > time.Second {
		t.Fatal("canceled beacon sleep did not return promptly")
	}
}

func TestCloseAgentLogIsIdempotent(t *testing.T) {
	previous := logFile
	defer func() { logFile = previous }()
	file, err := os.CreateTemp(t.TempDir(), "agent-log")
	if err != nil {
		t.Fatal(err)
	}
	logFile = file
	closeAgentLog()
	closeAgentLog()
	if logFile != nil {
		t.Fatal("closed log remains installed")
	}
	if _, err := file.WriteString("closed"); !errors.Is(err, os.ErrClosed) {
		t.Fatalf("log file remained open: %v", err)
	}
}

func TestAgentShutdownProcess(t *testing.T) {
	if os.Getenv("RMM_SHUTDOWN_TEST_CHILD") == "1" {
		main()
	}
}

func TestAgentBlockedCommand(t *testing.T) {
	if os.Getenv("RMM_SHUTDOWN_COMMAND_CHILD") == "1" {
		time.Sleep(time.Hour)
	}
}

func TestShutdownCancelsManagedCommand(t *testing.T) {
	previous := agentContext
	defer func() { agentContext = previous }()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	agentContext = ctx
	command := newCommand(os.Args[0], "-test.run=^TestAgentBlockedCommand$")
	command.Env = append(os.Environ(), "RMM_SHUTDOWN_COMMAND_CHILD=1")
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = command.Process.Kill() }()
	cancel()
	done := make(chan error, 1)
	go func() { done <- command.Wait() }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("canceled child unexpectedly completed successfully")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("managed command survived cancellation")
	}
}

func TestSignalCancelsInFlightRegistration(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Unix signal delivery test")
	}
	started := make(chan struct{}, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case started <- struct{}{}:
		default:
		}
		<-r.Context().Done()
	}))
	defer server.Close()
	cmd := exec.Command(os.Args[0], "-test.run=^TestAgentShutdownProcess$")
	cmd.Env = append(os.Environ(), "RMM_SHUTDOWN_TEST_CHILD=1", "RMM_BASE_URL="+server.URL,
		"RMM_BEACON_SECRET=test", "RMM_LOG_FILE="+t.TempDir()+"/agent.log")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = cmd.Process.Kill() }()
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case <-started:
	case err := <-done:
		t.Fatalf("agent exited before registration: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("agent did not register")
	}
	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("agent failed graceful shutdown: %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("signal did not cancel registration")
	}
}
