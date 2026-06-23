# rclone binary (agent bootstrap)

Place a Windows **amd64** `rclone.exe` in this directory, or set `RMM_RCLONE_BIN` to its path on the server.

Agents download it once via beacon-authenticated `GET /tools/rclone.exe` and cache under `%LOCALAPPDATA%\RMM\rclone.exe`.

Configure upload destinations in `profiles.example.json` (copy values into `RMM_RCLONE_PROFILES` or `RMM_RCLONE_PROFILES_FILE`). See [docs/rclone-exfil.md](../../docs/rclone-exfil.md).

Download rclone: <https://rclone.org/downloads/>
