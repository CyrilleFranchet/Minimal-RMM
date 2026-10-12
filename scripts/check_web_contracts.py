#!/usr/bin/env python3
"""Verify WebUI completion and configuration contracts.

This check intentionally uses only the standard library. It catches common
drift where an agent updates one WebUI surface and forgets its documentation,
HTML wiring, or configuration allowlist.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _quoted_array(text: str, name: str) -> set[str]:
    match = re.search(
        rf"(?:const|let)\s+{re.escape(name)}\s*=\s*\[(.*?)\];",
        text,
        re.DOTALL,
    )
    if not match:
        raise ValueError(f"{name} declaration not found")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _shell_meta_docs(text: str) -> set[str]:
    return set(re.findall(r"^\| `([a-z?][a-z0-9_?-]*)[^`]*` \|", text, re.MULTILINE))


def _check_shell_contract(errors: list[str]) -> None:
    app = _read("web/app.js")
    docs = _read("docs/web-shell-completion.md")

    try:
        commands = _quoted_array(app, "SHELL_META_COMMANDS")
    except ValueError as error:
        errors.append(str(error))
        return

    documented = _shell_meta_docs(docs)
    missing_docs = commands - documented
    stale_docs = documented - commands
    if missing_docs:
        errors.append(f"web shell commands missing from docs/web-shell-completion.md: {sorted(missing_docs)}")
    if stale_docs:
        errors.append(f"docs/web-shell-completion.md lists unknown shell commands: {sorted(stale_docs)}")

    for command in commands:
        if not re.search(rf"verb\s*===\s*[\"']{re.escape(command)}[\"']", app):
            errors.append(f"web shell command {command!r} has no dispatch branch in web/app.js")

    for function_name in ("navigateShellHistory", "applyShellTabCompletion", "updateShellCompletionHint"):
        if not re.search(rf"function\s+{function_name}\s*\(", app):
            errors.append(f"web shell completion function missing from web/app.js: {function_name}")


def _check_configuration_contract(errors: list[str]) -> None:
    config = _read("web/config.js")
    html = _read("web/index.html")
    docs = _read("docs/web-ui-configuration.md")

    try:
        session_keys = _quoted_array(config, "SESSION_KEYS")
        secret_keys = _quoted_array(config, "SECRET_KEYS")
    except ValueError as error:
        errors.append(str(error))
        return

    if session_keys & secret_keys:
        errors.append(f"configuration keys appear in both allowlists: {sorted(session_keys & secret_keys)}")

    required_ids = {
        "config-toggle-btn",
        "ui-config-dialog",
        "ui-config-export",
        "ui-config-import",
        "ui-config-file",
    }
    missing_ids = [element_id for element_id in sorted(required_ids) if f'id="{element_id}"' not in html]
    if missing_ids:
        errors.append(f"WebUI configuration controls missing from web/index.html: {missing_ids}")
    if 'src="config.js"' not in html:
        errors.append("web/index.html does not load config.js")

    for key in sorted(session_keys | secret_keys):
        if key not in docs:
            errors.append(f"configuration key {key!r} is not documented in docs/web-ui-configuration.md")

    if "SESSION_KEYS" not in docs or "SECRET_KEYS" not in docs:
        errors.append("docs/web-ui-configuration.md must document the configuration allowlists")
    if "readConfiguration" not in config or "importConfiguration" not in config:
        errors.append("web/config.js must define both export and import paths")


def _check_socks_contract(errors: list[str]) -> None:
    app = _read("web/app.js")
    html = _read("web/index.html")
    required_ids = {
        "open-socks-dialog",
        "socks-dialog",
        "socks-port",
        "socks-bind-host",
        "socks-start",
        "socks-stop",
        "socks-refresh",
        "socks-relays-list",
    }
    missing_ids = [element_id for element_id in sorted(required_ids) if f'id="{element_id}"' not in html]
    if missing_ids:
        errors.append(f"WebUI SOCKS controls missing from web/index.html: {missing_ids}")
    if 'id="open-socks-dialog">Pivots<' not in html:
        errors.append("WebUI session header must expose the pivot control as Pivots")
    if "SOCKS5" not in html or "SOCKS4 is not supported" not in html:
        errors.append("WebUI must identify SOCKS5 and state that SOCKS4 is unsupported")
    if "socks5" not in html:
        errors.append("WebUI must show the client SOCKS5 configuration keyword")
    for function_name in ("refreshSocksRelays", "startSelectedSocks", "stopSocksRelay"):
        if not re.search(rf"function\s+{function_name}\s*\(", app):
            errors.append(f"WebUI SOCKS function missing from web/app.js: {function_name}")
    if 'api("/socks")' not in app:
        errors.append("WebUI SOCKS inventory must call GET /api/v1/socks")


def _check_tun_contract(errors: list[str]) -> None:
    app = _read("web/app.js")
    html = _read("web/index.html")
    required_ids = {
        "open-tun-dialog",
        "tun-dialog",
        "tun-device",
        "tun-route",
        "tun-address",
        "tun-interface",
        "tun-mtu",
        "tun-udp-timeout",
        "tun-start",
        "tun-stop",
        "tun-refresh",
        "tun-relays-list",
    }
    missing_ids = [element_id for element_id in sorted(required_ids) if f'id="{element_id}"' not in html]
    if missing_ids:
        errors.append(f"WebUI TUN controls missing from web/index.html: {missing_ids}")
    if 'id="open-tun-dialog">TUN routing<' not in html:
        errors.append("WebUI session header must expose the TUN routing control")
    if "Server-owned device" not in html or "target-side TCP and UDP sockets" not in html:
        errors.append("WebUI must explain that the TUN device belongs to the server")
    for function_name in ("refreshTunRelays", "startSelectedTun", "stopTunRelay"):
        if not re.search(rf"function\s+{function_name}\s*\(", app):
            errors.append(f"WebUI TUN function missing from web/app.js: {function_name}")
    if 'api("/tun")' not in app:
        errors.append("WebUI TUN inventory must call GET /api/v1/tun")
    if "/sessions/${encodeURIComponent(sessionId)}/tun" not in app:
        errors.append("WebUI TUN controls must call the per-session /tun endpoint")


def main() -> int:
    errors: list[str] = []
    _check_shell_contract(errors)
    _check_configuration_contract(errors)
    _check_socks_contract(errors)
    _check_tun_contract(errors)
    if errors:
        print("WebUI contract check FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("WebUI contract check OK: shell, configuration, SOCKS, and TUN controls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
