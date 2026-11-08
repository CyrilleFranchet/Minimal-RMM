# stage1 — compact psexec launcher

The psexec lateral module pushes this binary to the target's `ADMIN$` share
and starts it via the SCM.  Its only job is to fetch and launch the full agent.

## Two implementations

| File | Size (stripped) | Toolchain |
|------|-----------------|-----------|
| `stage1.c` | ~30–50 KB | MinGW (x86_64-w64-mingw32-gcc) |
| `main.go` | ~1.5–2 MB | Go cross-compiler |

The server (`build_go_agent`) automatically prefers the C build when
`x86_64-w64-mingw32-gcc` is on the `PATH`, and falls back to Go otherwise.

## Building the C stage1 manually

```bash
x86_64-w64-mingw32-gcc -Os -s -mwindows \
    -DBASE_URL=L"https://<rmm-server>:<port>" \
    -DSTAGE1_INSECURE \
    -o stage1.exe stage1/stage1.c \
    -lwinhttp -lbcrypt -lshell32
```

* **`-DBASE_URL=L"..."`** — RMM server URL, injected at compile time.
* **`-DSTAGE1_INSECURE`** — Skips TLS certificate validation; required for
  lab environments that use a self-signed certificate.  Remove for production.

## Install MinGW on Debian/Ubuntu

```bash
sudo apt-get install -y mingw-w64
```

## Protocol

1. `GET {BASE_URL}/tools/stage/{staging_id}` — returns XOR-masked PE.
2. Decode: SHA-256 counter keystream seeded by `staging_id` (32-byte blocks).
3. Verify MZ header.
4. Create `%TEMP%\minimal-rmm-agent-<session_id>.exe` without overwriting an existing file (the C debug variant uses `minimal-rmm-agent-debug-<session_id>.exe`).
5. Launch that executable detached with `--lateral-session <id>`.

For psexec, the service reports the launcher result through SCM/RPC. The
launcher does not send a separate HTTP status request.

The staging endpoint requires no authentication — the unguessable `staging_id`
(32 hex chars from `secrets.token_hex(16)`) is the capability token.  The
endpoint is single-use: the server marks it fetched after the first GET.
