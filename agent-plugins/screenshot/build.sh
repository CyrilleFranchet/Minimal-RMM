#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
NAME="${1:-screenshot.dll}"
[[ "$NAME" == *.dll ]] || NAME="${NAME}.dll"
if ! command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1; then
  echo "error: x86_64-w64-mingw32-gcc not found (install mingw-w64)" >&2
  exit 1
fi
CGO_ENABLED=1 GOOS=windows GOARCH=amd64 CC=x86_64-w64-mingw32-gcc \
  go build -trimpath -ldflags="-s -w" -buildmode=c-shared -o "$NAME" .
echo "Built $NAME; copy it to the server plugin directory."
