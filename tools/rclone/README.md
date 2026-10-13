# rclone binary (agent bootstrap)

Place a Windows **amd64** `rclone.exe` in this directory when the central
control plane must provide the binary fallback, or set `RMM_RCLONE_BIN` to its
path on the server.

Agents download it once via beacon-authenticated `GET /tools/rclone.exe` and cache under `%LOCALAPPDATA%\RMM\rclone.exe`.

Configure upload destinations in `profiles.example.json`. In workspace mode,
copy it to `profiles.json` in the operator workspace and set
`RMM_RCLONE_PROFILES_FILE` there. In server mode, set the same variable on the
RMM server. See [docs/rclone-exfil.md](../../docs/rclone-exfil.md).

An operator can also install a workspace profile file from an HTTP(S) URL:

```bash
python rmm_cli.py assets add rclone \
  https://example.invalid/rclone/profiles.json
```

The WebUI exposes the same action under **Config → Runtime assets and beacon
transport**. Enabled `.json` assets are merged with the workspace profile
file, and the selected job is sent to the central broker without moving the
workspace profile file into the control-plane store. See
[operator-managed assets](../../docs/operator-assets.md).

Download rclone: <https://rclone.org/downloads/>
