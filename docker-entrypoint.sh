#!/bin/sh
# Start the requested Minimal-RMM process. Workspaces publish agent plugins
# into the central workspace-scoped delivery cache.

set -eu

mkdir -p "${RMM_PROFILES_DIR:-/app/profiles}"

exec "$@"
