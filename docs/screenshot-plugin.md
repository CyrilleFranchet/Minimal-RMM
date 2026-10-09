# Screenshot plugin

The Go agent captures Windows screenshots through the `screenshot` PE plugin.
The plugin uses native GDI APIs, encodes the primary display as PNG, and
returns the existing base64 payload expected by the server.

The `__SCREENSHOT__` command downloads `screenshot.dll` through the
authenticated plugin endpoint and runs it in the isolated child process. The
DLL bytes are passed over stdin to the child and are manually mapped there;
the plugin is not written to disk.

Build it from the repository root with the server plugin builder, or locally:

```bash
cd agent-plugins/screenshot
./build.sh screenshot.dll
```

The resulting DLL must be placed in `RMM_logs/plugins/` before a screenshot is
queued. If it is missing, the agent reports a screenshot error and the server
does not create an artifact.

## Function reference

| Function | Role |
|----------|------|
| `Run` | Plugin ABI entry point; captures the primary display and writes base64 PNG output |
| `capture` | Uses GDI to copy the display into a DIB section and encode PNG data |
| `writeOutput` | Writes a bounded NUL-terminated result into the ABI buffer |
