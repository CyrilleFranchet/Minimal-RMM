#!/usr/bin/env python3
"""Build the server helper and start the Minimal-RMM server."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent


def main() -> int:
    """Build the server TUN helper, then exec the server."""
    os.chdir(ROOT)

    if any(argument in ("-h", "--help") for argument in sys.argv[1:]):
        os.execv(sys.executable, [sys.executable, str(ROOT / "server_rmm.py"), *sys.argv[1:]])
        return 0

    try:
        print("[*] Building server TUN helper: tools/rmm-tun", flush=True)
        subprocess.run(["make", "tun-build"], cwd=ROOT, check=True)

        from rmm_profile import discover_profiles, profile_directory
        from server_rmm import list_plugin_files

        plugins = list_plugin_files()
        if plugins:
            print(f"[+] Found {len(plugins)} deployed agent plugin artifact(s)", flush=True)
        else:
            print(
                "[!] No agent plugin artifacts found; install a bundle from "
                "Minimal-RMM-plugins into RMM_PLUGINS_DIR",
                flush=True,
            )
        profiles = discover_profiles(profile_directory())
        if profiles:
            names = ", ".join(row["name"] for row in profiles)
            print(f"[+] Found installed RMM-Profile bundle(s): {names}", flush=True)
        else:
            print(
                "[!] No RMM-Profile bundles found; install one from "
                "Minimal-RMM-profiles into RMM_PROFILES_DIR",
                flush=True,
            )
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"[!] Startup build failed: {exc}", file=sys.stderr, flush=True)
        return 1

    print("[*] Starting server", flush=True)
    os.execv(sys.executable, [sys.executable, str(ROOT / "server_rmm.py"), *sys.argv[1:]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
