#!/bin/sh
# Start the requested Minimal-RMM process. Agent plugin artifacts are deployed
# separately into the persistent RMM_PLUGINS_DIR volume.

set -eu

mkdir -p "${RMM_PLUGINS_DIR:-/app/plugins}"
mkdir -p "${RMM_PROFILES_DIR:-/app/profiles}"

exec "$@"
