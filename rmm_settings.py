"""Typed, owner-local process configuration with write-only credentials.

Executable entry points call bootstrap before importing application modules.
The active snapshot never changes after startup; API writes affect the next
process start. No configuration values are injected into os.environ.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from urllib.parse import urlsplit


class SettingsError(ValueError):
    """Invalid settings or unreadable persistent configuration."""


SCHEMA = "minimal-rmm-settings-v1"
MAX_BYTES = 2 * 1024 * 1024
_LOCK = threading.RLock()
_active = None


def field(label, group, kind="string", default="", *, scopes=("server", "workspace"), secret=False,
          minimum=None, maximum=None, choices=None):
    """Describe one configuration value for validation and operator forms."""
    return dict(label=label, group=group, type=kind, default=default, scopes=scopes,
                secret=secret, minimum=minimum, maximum=maximum, choices=choices)


S = ("server",)
W = ("workspace",)
C = ("client",)
ALL = ("server", "workspace", "client")
FIELDS = {
    "RMM_ADMIN_BIND": field("Administration bind address", "Listeners", default="127.0.0.1", scopes=S),
    "RMM_ADMIN_PORT": field("Administration port", "Listeners", "integer", 8080, scopes=S, minimum=1, maximum=65535),
    "RMM_BEACON_BIND": field("Beacon bind address", "Listeners", default="127.0.0.1", scopes=S),
    "RMM_BEACON_PORT": field("Beacon port", "Listeners", "integer", 8081, scopes=S, minimum=1, maximum=65535),
    "RMM_API_TOKEN": field("Operator API token", "Authentication", scopes=("server", "client"), secret=True),
    "RMM_BEACON_SECRET": field("Beacon shared secret", "Authentication", scopes=S, secret=True),
    "RMM_WORKSPACES": field("Workspace IDs and tokens", "Authentication", "json", {}, scopes=S, secret=True),
    "RMM_WORKSPACE_ID": field("Workspace ID", "Connection", scopes=ALL),
    "RMM_WORKSPACE_TOKEN": field("Workspace token", "Connection", scopes=ALL, secret=True),
    "RMM_C2_URL": field("Central server URL", "Connection", default="http://127.0.0.1:8080", scopes=W),
    "RMM_SERVER_URL": field("Operator service URL", "Connection", default="http://127.0.0.1:8080", scopes=C),
    "RMM_BASE_URL": field("Legacy operator URL", "Connection", scopes=C),
    "RMM_WORKSPACE_BIND": field("Workspace bind address", "Listeners", default="127.0.0.1", scopes=W),
    "RMM_WORKSPACE_PORT": field("Workspace port", "Listeners", "integer", 8787, scopes=W, minimum=1, maximum=65535),
    "RMM_WORKSPACE_LOCAL_TOKEN": field("Local operator token", "Authentication", scopes=W, secret=True),
    "RMM_WORKSPACE_AUTO_CLAIM": field("Automatically claim sessions", "Workspace", "boolean", False, scopes=W),
    "RMM_WORKSPACE_POLL_INTERVAL": field("Sync interval (seconds)", "Workspace", "number", 1, scopes=W, minimum=0.2, maximum=3600),
    "RMM_WORKSPACE_EVENT_LIMIT": field("Events per workspace", "Limits", "integer", 10000, scopes=S, minimum=100, maximum=100000),
    "RMM_MAX_BODY_BYTES": field("Maximum request bytes", "Limits", "integer", 33554432, scopes=S, minimum=1024),
    "RMM_MAX_WORKSPACE_PAYLOAD_BYTES": field("Maximum workspace task bytes", "Limits", "integer", 33554432, scopes=S, minimum=1024),
    "RMM_WORKSPACE_MAX_BODY": field("Maximum request bytes", "Limits", "integer", 33554432, scopes=W, minimum=1024),
    "RMM_ASSET_MAX_BYTES": field("Maximum asset bytes", "Limits", "integer", 67108864, minimum=1024, maximum=536870912),
    "RMM_PLUGINS_DIR": field("Plugin directory", "Storage paths", scopes=S),
    "RMM_ASSETS_DIR": field("Asset directory", "Storage paths", scopes=S),
    "RMM_PROFILES_DIR": field("Transport bundle directory", "Storage paths", scopes=S),
    "RMM_UI_STATE_DB": field("Browser state database", "Storage paths", scopes=S),
    "RMM_PROFILE_NAME": field("Transport profile", "Beacon transport", default="cloudflared", scopes=S),
    "RMM_PROFILE_AUTOSTART": field("Start transport on boot", "Beacon transport", "boolean", True, scopes=S),
    "RMM_BEACON_PUBLIC_URL": field("Static beacon URL fallback", "Beacon transport", scopes=S),
    "RMM_CLOUDFLARE_MODE": field("Cloudflare tunnel mode", "Cloudflare", default="quick", scopes=S, choices=["quick", "named"]),
    "RMM_CLOUDFLARE_PUBLIC_URL": field("Named tunnel public URL", "Cloudflare", scopes=S),
    "RMM_CLOUDFLARE_TOKEN": field("Named tunnel token", "Cloudflare", scopes=S, secret=True),
    "RMM_CLOUDFLARE_BINARY": field("Cloudflared executable", "Cloudflare", default="cloudflared", scopes=S),
    "CLOUDFLARED_BIN": field("Legacy cloudflared executable", "Cloudflare", scopes=S),
    "RMM_CLOUDFLARE_ORIGIN_HOST": field("Quick tunnel origin host", "Cloudflare", scopes=S),
    "RMM_PLUGIN_REPOSITORY": field("Initial default plugin repository", "Plugin credentials", default="CyrilleFranchet/Minimal-RMM-plugins"),
    "RMM_PLUGIN_GITHUB_TOKEN": field("Default GitHub token", "Plugin credentials", secret=True),
    "RMM_RCLONE_BIN": field("Rclone executable", "Rclone"),
    "RMM_RCLONE_DEFAULT_PROFILE": field("Default remote profile", "Rclone", default="mega-lab"),
    "RMM_RCLONE_MAX_BYTES": field("Maximum transfer bytes (0 = unlimited)", "Rclone", "integer", 104857600, minimum=0),
    "RMM_RCLONE_PROFILES_FILE": field("Legacy remote profiles file", "Rclone"),
    "RMM_RCLONE_PROFILES": field("Legacy remote profiles JSON", "Rclone", "json", {}, secret=True),
    "RMM_TUN_HELPER": field("TUN helper executable", "TUN", scopes=S),
    "RMM_WORKSPACE_TUN_HELPER": field("TUN helper executable", "TUN", default="tools/rmm-tun", scopes=W),
    "RMM_TUN_LOG_LEVEL": field("TUN log level", "TUN", default="info", choices=["debug", "info", "warn", "error"]),
    "RMM_AI_MAX_TOOL_ROUNDS": field("Maximum AI tool rounds", "AI", "integer", 32, minimum=1, maximum=256, scopes=S),
    "RMM_AI_USE_MCP": field("Use MCP tools", "AI", "boolean", True, scopes=S),
    "RMM_AI_SKILLS_DIR": field("Additional AI skills directory", "AI"),
    "OPENAI_API_KEY": field("Default OpenAI API key", "AI", secret=True),
    "RMM_EXEGOL_MCP_URL": field("Exegol MCP URL", "AI"),
    "RMM_EXEGOL_MCP_TOKEN": field("Exegol MCP token", "AI", secret=True),
}


def definition(key, scope):
    """Resolve allowlisted keys, including named plugin credential references."""
    spec = FIELDS.get(key)
    if spec is None and re.fullmatch(r"RMM_PLUGIN_TOKEN_[A-Z0-9_]{1,100}", key):
        spec = field(key.removeprefix("RMM_PLUGIN_TOKEN_"), "Plugin credentials", secret=True)
    if not spec or scope not in spec["scopes"]:
        raise SettingsError(f"unsupported {scope} setting: {key}")
    return spec


def validate(key, value, scope):
    """Validate a value without including it in an error message."""
    spec = definition(key, scope)
    kind = spec["type"]
    valid = ((kind == "string" and isinstance(value, str))
             or (kind == "boolean" and isinstance(value, bool))
             or (kind == "integer" and type(value) is int)
             or (kind == "number" and type(value) in (int, float) and math.isfinite(value))
             or (kind == "json" and isinstance(value, (dict, list))))
    if not valid:
        raise SettingsError(f"{key} must be {kind}")
    if kind == "string" and (len(value) > 65536 or "\x00" in value or "\n" in value or "\r" in value):
        raise SettingsError(f"{key} contains invalid characters or is too long")
    if spec["choices"] and value not in spec["choices"]:
        raise SettingsError(f"{key} has an unsupported choice")
    if spec["minimum"] is not None and value < spec["minimum"]:
        raise SettingsError(f"{key} is below its minimum")
    if spec["maximum"] is not None and value > spec["maximum"]:
        raise SettingsError(f"{key} exceeds its maximum")
    if key.endswith("_URL") and value:
        try:
            parsed = urlsplit(value)
            good = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
            parsed.port
            if key == "RMM_CLOUDFLARE_PUBLIC_URL":
                good = (good and parsed.scheme == "https" and parsed.port in (None, 443)
                        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", parsed.hostname or "")
                        and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                        and not any(char.isspace() for char in value))
        except ValueError:
            good = False
        if not good:
            raise SettingsError(f"{key} must be a valid URL without credentials")
    if key == "RMM_WORKSPACES":
        rows = value.items() if isinstance(value, dict) else [(x.get("id") or x.get("workspace_id"), x.get("token")) for x in value if isinstance(x, dict)]
        if len(rows) != len(value) or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", k.strip()) or not isinstance(v, str) or not v.strip() for k, v in rows):
            raise SettingsError("RMM_WORKSPACES requires an ID-to-token JSON object or a list of {id, token} records; IDs must contain 1–64 letters, digits, dots, underscores, or hyphens, and tokens must be nonempty strings")
        # Match the existing registry's whitespace and id/workspace_id fallback
        # behavior without coercing malformed credentials into strings.
        return {key.strip(): token.strip() for key, token in rows}
    if key == "RMM_RCLONE_PROFILES" and not isinstance(value, dict):
        raise SettingsError("RMM_RCLONE_PROFILES must be an object")
    return value


def _decode_environment(key, raw, scope):
    """Decode legacy string variables according to the settings schema."""
    kind = definition(key, scope)["type"]
    try:
        if kind == "boolean":
            if raw.lower() not in {"1", "true", "yes", "0", "false", "no"}:
                raise ValueError()
            value = raw.lower() in {"1", "true", "yes"}
        elif kind in {"integer", "number", "json"}:
            value = json.loads(raw)
        else:
            value = raw
        return validate(key, value, scope)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"invalid environment override for {key}: expected valid JSON (line {exc.lineno}, column {exc.colno}); check shell quoting") from None
    except SettingsError as exc:
        raise SettingsError(f"invalid environment override for {key}: {exc}") from None
    except (ValueError, TypeError):
        raise SettingsError(f"invalid environment override for {key}") from None


class SettingsStore:
    """Atomic configuration file plus immutable effective startup snapshot."""

    def __init__(self, path, scope, *, environment=None):
        if scope not in ALL:
            raise SettingsError("invalid configuration scope")
        self.path = Path(path).expanduser().resolve() if path is not None else None
        self.scope = scope
        self.environment = dict(os.environ if environment is None else environment)
        self.saved_at_start = self._read()
        self.overrides = {}
        for key, raw in self.environment.items():
            if not raw.strip():
                continue
            try:
                definition(key, scope)
            except SettingsError:
                continue
            self.overrides[key] = _decode_environment(key, raw.strip(), scope)
        self.effective = {**self.saved_at_start, **self.overrides}
        self.flags = set()

    def _read(self):
        """Read validated settings, refusing corrupt or wrong-owner files."""
        if self.path is None or not self.path.exists():
            return {}
        try:
            if self.path.stat().st_size > MAX_BYTES:
                raise SettingsError("configuration file is too large")
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("schema") != SCHEMA or document.get("scope") != self.scope:
                raise SettingsError("configuration schema or scope does not match")
            values = document["settings"]
            if not isinstance(values, dict):
                raise SettingsError("settings must be an object")
            return {key: validate(key, value, self.scope) for key, value in values.items()}
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError):
            raise SettingsError("cannot read configuration file") from None

    def snapshot(self):
        """Return effective/saved values and provenance with secrets omitted."""
        with _LOCK:
            saved = self._read()
        keys = {key for key, spec in FIELDS.items() if self.scope in spec["scopes"]} | saved.keys() | self.effective.keys()
        rows = []
        for key in sorted(keys):
            spec = definition(key, self.scope)
            value = self.effective.get(key, self.default_value(key))
            pending = saved.get(key) != self.saved_at_start.get(key)
            rows.append({"key": key, **{k: spec[k] for k in ("label", "group", "type", "secret", "choices", "minimum", "maximum")},
                         "value": None if spec["secret"] else value,
                         "saved_value": None if spec["secret"] else saved.get(key),
                         "configured": bool(value), "saved": key in saved,
                         "source": "command line" if key in self.flags else "environment" if key in self.overrides else "config" if key in self.saved_at_start else "default",
                         "restart_required": pending})
        return {"scope": self.scope, "settings": rows, "restart_required": any(row["restart_required"] for row in rows)}

    def default_value(self, key):
        """Report derived defaults using the same inputs as runtime consumers."""
        root = self.environment.get("RMM_LOG_DIR") or "RMM_logs"
        code = Path(__file__).resolve().parent
        defaults = {
            "RMM_MAX_WORKSPACE_PAYLOAD_BYTES": self.effective.get("RMM_MAX_BODY_BYTES", 33554432),
            "RMM_BEACON_PORT": self.effective.get("RMM_ADMIN_PORT", 8080) + 1,
            "RMM_BEACON_BIND": self.effective.get("RMM_ADMIN_BIND", "127.0.0.1"),
            "RMM_PLUGINS_DIR": str(Path(root) / "plugins"),
            "RMM_ASSETS_DIR": str(Path(root) / "assets"),
            "RMM_PROFILES_DIR": str(Path(root) / "profiles"),
            "RMM_UI_STATE_DB": str(Path(root) / "ui-state.db"),
            "RMM_TUN_HELPER": str(code / "tools" / "rmm-tun"),
            "RMM_RCLONE_BIN": str(code / "tools" / "rclone" / "rclone.exe"),
            "RMM_CLOUDFLARE_BINARY": self.effective.get("CLOUDFLARED_BIN", "cloudflared"),
        }
        return defaults.get(key, definition(key, self.scope)["default"])

    def update(self, payload):
        """Validate an entire patch and atomically save it for the next restart."""
        if self.path is None:
            raise SettingsError("internal client configuration is read-only")
        if not isinstance(payload, dict) or set(payload) != {"settings"} or not isinstance(payload["settings"], dict):
            raise SettingsError("expected an object containing settings")
        patch = payload["settings"]
        for key, value in patch.items():
            definition(key, self.scope)
            if value is not None:
                validate(key, value, self.scope)
        with _LOCK:
            saved = self._read()
            for key, value in patch.items():
                if value is None:
                    saved.pop(key, None)
                else:
                    saved[key] = validate(key, value, self.scope)
            data = json.dumps({"schema": SCHEMA, "scope": self.scope, "settings": saved}, indent=2, allow_nan=False) + "\n"
            if len(data.encode()) > MAX_BYTES:
                raise SettingsError("configuration exceeds size limit")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temp = tempfile.mkstemp(prefix=".settings-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, self.path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        return self.snapshot()


def getenv(key, default=""):
    """Read the startup snapshot in legacy string form without changing env."""
    if _active is not None and key in _active.effective:
        value = _active.effective[key]
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"))
        return str(value)
    value = os.environ.get(key, "")
    return value if value.strip() else default


def active_store(scope, default_path):
    """Return the process settings owner, or an isolated embedded-service store."""
    if _active is not None and _active.scope == scope:
        return _active
    return SettingsStore(default_path, scope)


def settings_api(store, method, payload=None):
    """Serve a redacted settings read or atomic patch with safe errors."""
    try:
        return 200, store.snapshot() if method == "GET" else store.update(payload)
    except SettingsError as exc:
        return (500 if method == "GET" else 400), {"error": "invalid_settings", "detail": str(exc)}
    except OSError:
        return 500, {"error": "settings_storage_unavailable"}


def record_flags(mapping, argv=None):
    """Record explicit startup overrides so consumers and provenance agree."""
    if _active is None:
        return
    arguments = sys.argv[1:] if argv is None else argv
    for flag, (key, value) in mapping.items():
        if any(arg == flag or arg.startswith(flag + "=") for arg in arguments):
            _active.effective[key] = validate(key, value, _active.scope)
            _active.flags.add(key)


def bootstrap(scope, argv=None):
    """Select a configuration file before application imports evaluate defaults."""
    global _active
    if scope == "client" and os.environ.get("RMM_INTERNAL_MCP") == "1":
        _active = SettingsStore(None, scope)
        return _active
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config")
    parser.add_argument("--data-dir")
    args, _ = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    if scope == "server":
        root = os.environ.get("RMM_LOG_DIR", "RMM_logs")
    elif scope == "workspace":
        root = args.data_dir or os.environ.get("RMM_WORKSPACE_DATA", "~/.minimal-rmm")
    else:
        root = "~/.minimal-rmm"
    path = args.config or os.environ.get("RMM_CONFIG_FILE") or str(Path(root).expanduser() / ("client-settings.json" if scope == "client" else "settings.json"))
    try:
        _active = SettingsStore(path, scope)
    except SettingsError as exc:
        parser.exit(2, f"Configuration error: {exc}\n")
    return _active


def main():
    """Configure services offline before first authentication or restart."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=ALL, required=True)
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("show")
    set_parser = sub.add_parser("set")
    set_parser.add_argument("key")
    set_parser.add_argument("value", nargs="?")
    set_parser.add_argument("--value-file", help="Read a value from a file or - for stdin")
    sub.add_parser("unset").add_argument("key")
    sub.add_parser("import").add_argument("file")
    args = parser.parse_args()
    try:
        store = SettingsStore(args.config, args.scope)
        if args.action == "show":
            result = store.snapshot()
        elif args.action == "unset":
            result = store.update({"settings": {args.key: None}})
        elif args.action == "import":
            document = json.loads(sys.stdin.read() if args.file == "-" else Path(args.file).read_text())
            result = store.update({"settings": document.get("settings", document)})
        else:
            raw = args.value
            if args.value_file:
                raw = sys.stdin.read() if args.value_file == "-" else Path(args.value_file).read_text()
                raw = raw.rstrip("\r\n")
            if raw is None:
                raise SettingsError("provide a value or --value-file")
            spec = definition(args.key, args.scope)
            value = raw if spec["type"] == "string" else json.loads(raw)
            result = store.update({"settings": {args.key: value}})
        print(json.dumps(result, indent=2))
        return 0
    except (SettingsError, OSError, json.JSONDecodeError):
        print("Configuration operation failed; check keys, value types, scope, and file permissions.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
