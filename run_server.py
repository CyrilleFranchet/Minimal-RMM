#!/usr/bin/env python3
"""Build local server artifacts and start the Minimal-RMM server."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent


def main() -> int:
    """Build all checked-in Go plugins and the TUN helper, then exec the server."""
    os.chdir(ROOT)

    if any(argument in ("-h", "--help") for argument in sys.argv[1:]):
        os.execv(sys.executable, [sys.executable, str(ROOT / "server_rmm.py"), *sys.argv[1:]])
        return 0

    try:
        from server_rmm import build_plugin, plugin_build_targets

        targets = plugin_build_targets()
        if not targets:
            raise RuntimeError("no checked-in Go plugin targets were found")

        for name in targets:
            print(f"[*] Building Go plugin: {name}", flush=True)
            result = build_plugin(name)
            print(
                f"[+] Built {result['output']} ({result['size']} bytes)",
                flush=True,
            )

        print("[*] Building server TUN helper: tools/rmm-tun", flush=True)
        subprocess.run(["make", "tun-build"], cwd=ROOT, check=True)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"[!] Startup build failed: {exc}", file=sys.stderr, flush=True)
        return 1

    print("[*] Starting server", flush=True)
    os.execv(sys.executable, [sys.executable, str(ROOT / "server_rmm.py"), *sys.argv[1:]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
