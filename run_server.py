#!/usr/bin/env python3
"""Build the server helper and start the Minimal-RMM server."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
TUN_BUILD_PATH = ROOT / "tools" / "rmm-tun"
TUN_SOURCE_ROOT = ROOT / "server-tun"


def _truthy(value: str) -> bool:
    """Return whether an environment switch uses a true value."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _wrapper_arguments(arguments: list[str]) -> tuple[list[str], bool]:
    """Remove wrapper-only flags and return server arguments plus rebuild state."""
    server_arguments: list[str] = []
    force_rebuild = _truthy(os.environ.get("RMM_TUN_REBUILD", ""))
    for argument in arguments:
        if argument == "--rebuild-tun":
            force_rebuild = True
        else:
            server_arguments.append(argument)
    return server_arguments, force_rebuild


def _tun_source_files() -> list[Path]:
    """Return server TUN source and module files that can require a rebuild."""
    if not TUN_SOURCE_ROOT.is_dir():
        return []
    return sorted(
        path
        for path in TUN_SOURCE_ROOT.rglob("*")
        if path.is_file() and (path.suffix in {".go", ".mod", ".sum"} or path.name == "go.work")
    )


def _tun_helper_path() -> Path:
    """Return the helper path used by the server and startup wrapper."""
    configured = os.environ.get("RMM_TUN_HELPER", "").strip()
    return Path(configured).expanduser() if configured else TUN_BUILD_PATH


def _tun_helper_needs_build(helper_path: Path, force_rebuild: bool) -> tuple[bool, str]:
    """Decide whether the server TUN helper must be compiled."""
    if force_rebuild:
        return True, "forced"
    try:
        helper_stat = helper_path.stat()
    except FileNotFoundError:
        return True, "missing"
    except OSError as exc:
        return True, f"unreadable ({exc})"
    if not helper_path.is_file() or not os.access(helper_path, os.X_OK):
        return True, "not executable"

    source_files = _tun_source_files()
    if not source_files:
        return True, "TUN sources are unavailable"
    try:
        newest_source = max(path.stat().st_mtime_ns for path in source_files)
    except OSError as exc:
        return True, f"source metadata unavailable ({exc})"
    if newest_source > helper_stat.st_mtime_ns:
        return True, "source files are newer"
    return False, "up to date"


def _ensure_tun_helper(force_rebuild: bool) -> Path:
    """Build the helper only when missing, stale, or explicitly requested."""
    helper_path = _tun_helper_path()
    needs_build, reason = _tun_helper_needs_build(helper_path, force_rebuild)
    if needs_build:
        print(f"[*] Building server TUN helper: {helper_path} ({reason})", flush=True)
        if helper_path.resolve() == TUN_BUILD_PATH.resolve():
            subprocess.run(["make", "tun-build"], cwd=ROOT, check=True)
        else:
            helper_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "go",
                    "build",
                    "-buildvcs=false",
                    "-trimpath",
                    "-ldflags",
                    "-s -w",
                    "-o",
                    str(helper_path.resolve()),
                    ".",
                ],
                cwd=TUN_SOURCE_ROOT,
                check=True,
            )
    else:
        print(f"[+] Reusing server TUN helper: {helper_path} ({reason})", flush=True)

    if not helper_path.is_file() or not os.access(helper_path, os.X_OK):
        raise RuntimeError(f"server TUN helper is unavailable: {helper_path}")
    return helper_path


def main() -> int:
    """Prepare the server TUN helper, then exec the server."""
    os.chdir(ROOT)
    server_arguments, force_rebuild = _wrapper_arguments(sys.argv[1:])

    if any(argument in ("-h", "--help") for argument in server_arguments):
        os.execv(sys.executable, [sys.executable, str(ROOT / "server_rmm.py"), *server_arguments])
        return 0

    try:
        _ensure_tun_helper(force_rebuild)

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
    os.execv(sys.executable, [sys.executable, str(ROOT / "server_rmm.py"), *server_arguments])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
