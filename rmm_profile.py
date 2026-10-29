"""Generic server-side RMM-Profile process contract.

An RMM-Profile is a trusted, dynamically installed connector that exposes the
beacon listener through an external transport. The RMM server only knows how
to start a profile runner and consume newline-delimited JSON lifecycle events;
transport-specific commands and parsing remain in the profile repository.
"""

from __future__ import annotations

from rmm_settings import getenv as settings_getenv

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
from typing import Any, Callable
from urllib.parse import urlsplit


PROFILE_SCHEMA = "minimal-rmm-profile-v1"
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_PROFILE_TIMEOUT = 30.0


class ProfileError(RuntimeError):
    """Raised when an RMM-Profile is missing or cannot start."""


def profile_directory() -> str:
    """Return the runtime directory containing installed profile bundles."""
    return os.path.realpath(
        settings_getenv("RMM_PROFILES_DIR", "").strip()
        or os.path.join(settings_getenv("RMM_LOG_DIR", "RMM_logs"), "profiles")
    )


def _profile_name(value: str) -> str:
    """Validate a profile name used as a directory component."""
    name = str(value or "").strip().lower()
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ProfileError("invalid profile name")
    return name


def _safe_entrypoint(root: Path, entrypoint: str) -> Path:
    """Resolve a manifest entrypoint without allowing path traversal."""
    raw = str(entrypoint or "").strip()
    if not raw or raw.startswith(("/", "\\")) or ".." in Path(raw).parts:
        raise ProfileError("invalid profile entrypoint")
    path = (root / raw).resolve()
    if path != root and root not in path.parents:
        raise ProfileError("profile entrypoint escapes its bundle")
    if not path.is_file():
        raise ProfileError("profile entrypoint is missing")
    return path


def _load_manifest(path: Path) -> dict[str, Any] | None:
    """Read and validate one profile manifest."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != PROFILE_SCHEMA:
        return None
    try:
        name = _profile_name(str(payload.get("name") or ""))
        version = str(payload.get("version") or "").strip()[:64]
        entrypoint = str(payload.get("entrypoint") or "").strip()
    except ProfileError:
        return None
    if not version or not entrypoint:
        return None
    return {**payload, "name": name, "version": version, "entrypoint": entrypoint}


def discover_profiles(root: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Return validated manifests from the installed profile directory."""
    directory = Path(root or profile_directory()).expanduser().resolve()
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for child in sorted(directory.iterdir(), key=lambda item: item.name):
        if not child.is_dir():
            continue
        manifest = _load_manifest(child / "profile.json")
        if not manifest:
            continue
        manifest["directory"] = str(child)
        try:
            manifest["entrypoint_path"] = str(_safe_entrypoint(child, manifest["entrypoint"]))
        except ProfileError:
            continue
        rows.append(manifest)
    return rows


def _utc_now() -> str:
    """Return a compact UTC timestamp for lifecycle status."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ready_event(payload: dict[str, Any]) -> dict[str, str]:
    """Validate a public origin and retain only credential-free handshake fields."""
    value = payload.get("public_url")
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ProfileError("profile ready event has an invalid public URL")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"https", "http"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and not any(character.isspace() for character in value)
            and "\\" not in value
        )
        # Accessing port also validates its syntax and range.
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ProfileError("profile ready event has an invalid public URL")
    return {"event": "ready", "public_url": value.rstrip("/"), "hostname": parsed.hostname}


class ProfileProcess:
    """Run one profile connector and wait for its generic ready event."""

    def __init__(
        self,
        manifest: dict[str, Any],
        *,
        log_fn: Callable[[str, str], None] | None = None,
    ):
        self.manifest = dict(manifest)
        self.log_fn = log_fn or (lambda _message, _level="INFO": None)
        self.process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._error: str | None = None
        self._event: dict[str, Any] = {}
        self._started_at: str | None = None

    def _log(self, message: str, level: str = "INFO") -> None:
        """Forward a profile lifecycle message to the server logger."""
        try:
            self.log_fn(message, level)
        except Exception:
            pass

    def _command(self, entrypoint: Path) -> list[str]:
        """Build the platform-neutral runner command from its manifest."""
        command = self.manifest.get("command")
        if isinstance(command, list) and all(isinstance(item, str) and item for item in command):
            rendered = [item.replace("{entrypoint}", str(entrypoint)) for item in command]
        elif entrypoint.suffix.lower() == ".py":
            rendered = [sys.executable, str(entrypoint)]
        else:
            rendered = [str(entrypoint)]
        extra = self.manifest.get("args")
        if isinstance(extra, list) and all(isinstance(item, str) for item in extra):
            rendered.extend(extra)
        return rendered

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        """Consume bounded lifecycle events without trusting arbitrary event fields."""
        if not process or not process.stdout:
            return
        try:
            while True:
                line = process.stdout.readline(65537)
                if not line:
                    break
                if len(line) > 65536:
                    with self._lock:
                        if self.process is process:
                            self._error = "profile runner emitted an oversized event"
                            self._ready.set()
                    return
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                with self._lock:
                    if self.process is not process:
                        return
                    if event.get("event") == "ready" and not self._error:
                        try:
                            self._event = _ready_event(event)
                        except ProfileError as exc:
                            self._error = str(exc)
                        self._ready.set()
                    elif event.get("event") == "error":
                        # Connector error strings may contain provider tokens or URLs.
                        self._error = "profile runner reported a failure"
                        self._ready.set()
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                if self.process is process:
                    if not self._error:
                        self._error = "profile runner closed its event stream"
                    self._ready.set()

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        """Drain provider diagnostics without exposing credentials to server logs."""
        if not process or not process.stderr:
            return
        try:
            while process.stderr.read(4096):
                pass
        except (OSError, ValueError):
            pass

    def start(
        self,
        *,
        listen_host: str,
        listen_port: int,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_PROFILE_TIMEOUT,
    ) -> dict[str, Any]:
        """Start a runner and return the event emitted by its ready handshake."""
        with self._lock:
            if self.process and self.process.poll() is None:
                raise ProfileError("profile is already running")
            root = Path(str(self.manifest["directory"])).resolve()
            entrypoint = _safe_entrypoint(root, str(self.manifest["entrypoint"]))
            environment = os.environ.copy()
            environment.update(
                {
                    "RMM_PROFILE_SCHEMA": PROFILE_SCHEMA,
                    "RMM_PROFILE_NAME": str(self.manifest["name"]),
                    "RMM_PROFILE_LISTEN_HOST": str(listen_host),
                    "RMM_PROFILE_LISTEN_PORT": str(int(listen_port)),
                    "RMM_PROFILE_CONFIG_JSON": json.dumps(config or {}, separators=(",", ":")),
                }
            )
            try:
                self.process = subprocess.Popen(
                    self._command(entrypoint),
                    cwd=root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                raise ProfileError("cannot start profile runner") from exc
            self._ready.clear()
            self._error = None
            self._event = {}
            self._started_at = _utc_now()
            threading.Thread(
                target=self._read_stdout,
                args=(self.process,),
                name=f"profile-{self.manifest['name']}-stdout",
                daemon=True,
            ).start()
            threading.Thread(
                target=self._read_stderr,
                args=(self.process,),
                name=f"profile-{self.manifest['name']}-stderr",
                daemon=True,
            ).start()
        if not self._ready.wait(max(0.1, float(timeout))):
            self.stop()
            raise ProfileError("profile did not emit a ready event before the timeout")
        with self._lock:
            error = self._error
            event = dict(self._event)
            if not error and (not self.process or self.process.poll() is not None):
                error = "profile runner exited before startup completed"
        if error:
            self.stop()
            raise ProfileError(error)
        if not event.get("public_url"):
            self.stop()
            raise ProfileError("profile ready event is missing public_url")
        return event

    def stop(self) -> None:
        """Terminate the profile runner and wait briefly for it to exit."""
        with self._lock:
            process = self.process
            self.process = None
            self._ready.set()
        if not process:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, AttributeError):
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The runner may exit before its children. Always terminate the
            # remaining process group, even when wait() returned immediately.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, AttributeError):
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def status(self) -> dict[str, Any]:
        """Return safe process and public URL status."""
        with self._lock:
            process = self.process
            event = dict(self._event)
            running = bool(process and process.poll() is None and not self._error)
            return {
                "name": self.manifest.get("name"),
                "version": self.manifest.get("version"),
                "running": running,
                "public_url": event.get("public_url") if running else None,
                "hostname": event.get("hostname") if running else None,
                "started_at": self._started_at if running else None,
                "capabilities": self.manifest.get("capabilities", []),
                "error": self._error,
            }


class ProfileManager:
    """Discover, start, and stop one active RMM-Profile."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        log_fn: Callable[[str, str], None] | None = None,
    ):
        self.root = Path(root or profile_directory()).expanduser().resolve()
        self.log_fn = log_fn or (lambda _message, _level="INFO": None)
        self._lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._closed = False
        self._active: ProfileProcess | None = None
        self._active_manifest: dict[str, Any] | None = None
        self._last_error: str | None = None

    def list(self) -> list[dict[str, Any]]:
        """Return installed profile metadata and active state."""
        with self._lock:
            active_name = self._active_manifest.get("name") if self._active_manifest else None
            active_status = self._active.status() if self._active else None
        rows: list[dict[str, Any]] = []
        for manifest in discover_profiles(self.root):
            row = {
                "name": manifest["name"],
                "version": manifest["version"],
                "description": manifest.get("description", ""),
                "capabilities": manifest.get("capabilities", []),
                "running": bool(manifest["name"] == active_name and active_status and active_status["running"]),
            }
            if active_status and manifest["name"] == active_name:
                row.update(
                    {key: active_status.get(key) for key in ("public_url", "hostname", "started_at")}
                )
            rows.append(row)
        return rows

    def start(
        self,
        name: str,
        *,
        listen_host: str,
        listen_port: int,
        config: dict[str, Any] | None = None,
        timeout: float = DEFAULT_PROFILE_TIMEOUT,
    ) -> dict[str, Any]:
        """Stop the current profile, start the named one, and return its ready event."""
        name = _profile_name(name)
        manifest = next((row for row in discover_profiles(self.root) if row["name"] == name), None)
        if not manifest:
            raise ProfileError(f"profile {name!r} is not installed")
        with self._operation_lock:
            if self._closed:
                raise ProfileError("profile manager is stopping")
            with self._lock:
                previous = self._active
                self._active = None
                self._active_manifest = None
            if previous:
                previous.stop()
            process = ProfileProcess(manifest, log_fn=self.log_fn)
            with self._lock:
                self._active = process
                self._active_manifest = manifest
                self._last_error = None
            try:
                return process.start(
                    listen_host=listen_host,
                    listen_port=listen_port,
                    config=config,
                    timeout=timeout,
                )
            except BaseException as exc:
                process.stop()
                error = str(exc) if isinstance(exc, ProfileError) else "profile startup failed"
                with self._lock:
                    self._last_error = error
                    self._active = None
                    self._active_manifest = None
                if not isinstance(exc, Exception):
                    raise
                raise ProfileError(error) from exc

    def close(self) -> bool:
        """Stop the connector and permanently reject new starts at shutdown."""
        with self._operation_lock:
            self._closed = True
            return self.stop()

    def stop(self) -> bool:
        """Stop the active profile, if any."""
        with self._operation_lock:
            with self._lock:
                process = self._active
                self._active = None
                self._active_manifest = None
            if not process:
                return False
            process.stop()
            return True

    def status(self) -> dict[str, Any]:
        """Return the active profile, public URL, and last startup error."""
        with self._lock:
            row = self._active.status() if self._active else None
            return {
                "directory": str(self.root),
                "installed": self.list(),
                "active": row,
                "public_url": row.get("public_url") if row else None,
                "last_error": self._last_error or (row.get("error") if row else None),
            }
