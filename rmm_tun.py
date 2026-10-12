"""Server-side TUN lifecycle for the built-in RMM pivot.

The RMM server owns the TUN interface and starts a small Go user-space IP
stack. That stack uses a private per-session relay bridge as its egress, so the
Go agent only needs ordinary outbound TCP and UDP sockets and remains
privilege-free.
"""

from __future__ import annotations

import ipaddress
import os
import platform
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlparse


DEFAULT_TUN_ADDRESS = "198.18.0.1/15"
DEFAULT_TUN_MTU = 1500
DEFAULT_UDP_TIMEOUT_SECONDS = 300
TUN_DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
TUN_INTERFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class TunError(RuntimeError):
    """Raised when a server-side TUN relay cannot be prepared or started."""


def _device_name(value: str) -> str:
    """Extract and validate the operating-system TUN device name."""
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme:
        if (
            parsed.scheme != "tun"
            or not parsed.netloc
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("device must be tun://<name> or a platform TUN name")
        name = parsed.netloc
    else:
        name = raw
    if "?" in name or not TUN_DEVICE_RE.fullmatch(name):
        raise ValueError("device name contains unsupported characters")
    return name


def normalize_tun_config(body: dict) -> dict:
    """Validate and normalize operator supplied TUN settings."""
    if not isinstance(body, dict):
        raise ValueError("TUN configuration must be an object")
    device = str(body.get("device", "")).strip()
    if not device:
        raise ValueError("device is required")
    device_name = _device_name(device)

    address_text = str(body.get("tun_address", DEFAULT_TUN_ADDRESS)).strip()
    try:
        tun_address = ipaddress.ip_interface(address_text)
    except ValueError as exc:
        raise ValueError("tun_address must be an IPv4 address with a prefix") from exc
    if tun_address.version != 4:
        raise ValueError("tun_address must be IPv4")

    route_text = str(body.get("route", "")).strip()
    route = None
    if route_text:
        try:
            route = ipaddress.ip_network(route_text, strict=False)
        except ValueError as exc:
            raise ValueError("route must be an IPv4 CIDR") from exc
        if route.version != 4 or route.prefixlen == 0:
            raise ValueError("route must be a non-default IPv4 CIDR")
        if route.overlaps(tun_address.network):
            raise ValueError("route must not overlap the TUN address network")

    interface = str(body.get("interface", "")).strip()
    if interface and not TUN_INTERFACE_RE.fullmatch(interface):
        raise ValueError("interface contains unsupported characters")

    try:
        mtu = int(body.get("mtu", DEFAULT_TUN_MTU))
    except (TypeError, ValueError) as exc:
        raise ValueError("mtu must be an integer") from exc
    if mtu < 576 or mtu > 9000:
        raise ValueError("mtu must be between 576 and 9000")

    try:
        udp_timeout = int(body.get("udp_timeout_seconds", DEFAULT_UDP_TIMEOUT_SECONDS))
    except (TypeError, ValueError) as exc:
        raise ValueError("udp_timeout_seconds must be an integer") from exc
    if udp_timeout < 1 or udp_timeout > 86400:
        raise ValueError("udp_timeout_seconds must be between 1 and 86400")

    return {
        "device": f"tun://{device_name}" if device.startswith("tun://") else device_name,
        "device_name": device_name,
        "tun_address": str(tun_address),
        "route": str(route) if route else "",
        "interface": interface,
        "mtu": mtu,
        "udp_timeout_seconds": udp_timeout,
    }


def _run_system_command(args: list[str]) -> None:
    """Run a fixed-argument network configuration command."""
    try:
        result = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise TunError(f"required command is unavailable: {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise TunError(f"network command failed: {args[0]} {detail}".strip()) from exc
    except subprocess.TimeoutExpired as exc:
        raise TunError(f"network command timed out: {args[0]}") from exc
    del result


class ServerTunRelay:
    """One TUN process connected to one private per-session relay bridge."""

    def __init__(self, session_id: str, config: dict, helper_path: str, socks_manager: Any, log_fn=None):
        self.session_id = session_id
        self.config = config
        self.helper_path = helper_path
        self.socks_manager = socks_manager
        self.log_fn = log_fn or (lambda _message, _level="INFO": None)
        self.process: subprocess.Popen | None = None
        self.socks_port: int | None = None
        self._created_linux_device = False
        self._route_configured = False

    def log(self, message: str, level: str = "INFO") -> None:
        self.log_fn(message, level)

    def start(self) -> None:
        """Start the private relay bridge, TUN helper, and host route."""
        if not os.path.isfile(self.helper_path) or not os.access(self.helper_path, os.X_OK):
            raise TunError(f"TUN helper is unavailable: {self.helper_path}")
        system = platform.system().lower()
        if system not in ("linux", "darwin"):
            raise TunError(f"server TUN support is not implemented for {system or 'unknown'}")

        bridge = self.socks_manager.start(
            self.session_id,
            port=0,
            bind_host="127.0.0.1",
            udp_idle_timeout=self.config["udp_timeout_seconds"],
        )
        self.socks_port = bridge.port
        try:
            if system == "linux":
                self._prepare_linux_device()
            command = [
                self.helper_path,
                "--device",
                self.config["device"],
                "--proxy",
                f"socks5://127.0.0.1:{self.socks_port}",
                "--mtu",
                str(self.config["mtu"]),
                "--udp-timeout",
                f"{self.config['udp_timeout_seconds']}s",
                "--log-level",
                "warn",
            ]
            if self.config["interface"]:
                command.extend(("--interface", self.config["interface"]))
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=(os.name != "nt"),
            )
            time.sleep(0.15)
            if self.process.poll() is not None:
                raise TunError(f"TUN helper exited with status {self.process.returncode}")
            self._configure_interface(system)
        except Exception:
            self.stop()
            raise
        self.log(
            f"TUN relay started on {self.config['device_name']} for session {self.session_id[:8]}",
            "SUCCESS",
        )

    def stop(self) -> None:
        """Stop the helper, remove the route, and stop the private SOCKS bridge."""
        system = platform.system().lower()
        try:
            self._remove_route(system)
        except TunError as exc:
            self.log(str(exc), "WARNING")
        process = self.process
        self.process = None
        if process and process.poll() is None:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        if system == "linux" and self._created_linux_device:
            try:
                _run_system_command(["ip", "link", "set", "dev", self.config["device_name"], "down"])
                _run_system_command(["ip", "tuntap", "del", "dev", self.config["device_name"], "mode", "tun"])
            except TunError as exc:
                self.log(str(exc), "WARNING")
            self._created_linux_device = False
        self.socks_manager.stop(self.session_id)

    def alive(self) -> bool:
        """Return whether the user-space stack process is still running."""
        return bool(self.process and self.process.poll() is None)

    def status(self) -> dict:
        """Return safe operator-visible relay state."""
        return {
            "session_id": self.session_id,
            "device": self.config["device"],
            "tun_address": self.config["tun_address"],
            "route": self.config["route"],
            "interface": self.config["interface"],
            "mtu": self.config["mtu"],
            "udp_timeout_seconds": self.config["udp_timeout_seconds"],
            "socks_port": self.socks_port,
            "helper_alive": self.alive(),
        }

    def _prepare_linux_device(self) -> None:
        """Create the Linux TUN device when the requested name is unused."""
        name = self.config["device_name"]
        if shutil.which("ip") is None:
            raise TunError("the ip command is required for Linux TUN setup")
        try:
            exists = subprocess.run(
                ["ip", "link", "show", "dev", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).returncode == 0
        except FileNotFoundError as exc:
            raise TunError("the ip command is required for Linux TUN setup") from exc
        except subprocess.TimeoutExpired as exc:
            raise TunError("timed out checking the Linux TUN device") from exc
        if exists:
            raise TunError(f"Linux TUN device already exists: {name}; choose a free name")
        _run_system_command(["ip", "tuntap", "add", "dev", name, "mode", "tun"])
        self._created_linux_device = True

    def _configure_interface(self, system: str) -> None:
        """Assign the TUN address, bring it up, and install the optional route."""
        address = ipaddress.ip_interface(self.config["tun_address"])
        name = self.config["device_name"]
        if system == "linux":
            _run_system_command(["ip", "addr", "replace", str(address), "dev", name])
            _run_system_command(["ip", "link", "set", "dev", name, "up"])
            if self.config["route"]:
                _run_system_command(["ip", "route", "replace", self.config["route"], "dev", name])
                self._route_configured = True
            return
        if system == "darwin":
            if shutil.which("ifconfig") is None:
                raise TunError("ifconfig is required for macOS TUN setup")
            _run_system_command(["ifconfig", name, str(address.ip), str(address.ip), "up"])
            if self.config["route"]:
                route = ipaddress.ip_network(self.config["route"], strict=False)
                _run_system_command(
                    [
                        "route",
                        "-n",
                        "add",
                        "-net",
                        str(route.network_address),
                        "-netmask",
                        str(route.netmask),
                        str(address.ip),
                    ]
                )
                self._route_configured = True

    def _remove_route(self, system: str) -> None:
        """Remove the route previously installed for this relay, if any."""
        if not self._route_configured or not self.config["route"]:
            return
        name = self.config["device_name"]
        if system == "linux":
            try:
                _run_system_command(["ip", "route", "del", self.config["route"], "dev", name])
            finally:
                self._route_configured = False
        elif system == "darwin":
            route = ipaddress.ip_network(self.config["route"], strict=False)
            try:
                _run_system_command(
                    ["route", "-n", "delete", "-net", str(route.network_address), "-netmask", str(route.netmask)]
                )
            finally:
                self._route_configured = False


class TunManager:
    """Thread-safe registry of server-side TUN relays."""

    def __init__(self, helper_path: str, socks_manager: Any, log_fn=None):
        self.helper_path = helper_path
        self.socks_manager = socks_manager
        self.log_fn = log_fn
        self.lock = threading.Lock()
        self.relays: dict[str, ServerTunRelay] = {}

    def _log(self, message: str, level: str = "INFO") -> None:
        """Forward lifecycle messages to the server logger when configured."""
        if self.log_fn:
            self.log_fn(message, level)

    def start(self, session_id: str, config: dict) -> ServerTunRelay:
        """Replace any existing relay for a session with a new TUN relay."""
        relay = ServerTunRelay(session_id, config, self.helper_path, self.socks_manager, self._log)
        with self.lock:
            existing = self.relays.pop(session_id, None)
        if existing:
            existing.stop()
        try:
            relay.start()
        except Exception:
            relay.stop()
            raise
        with self.lock:
            self.relays[session_id] = relay
        return relay

    def stop(self, session_id: str) -> bool:
        """Stop the relay for a session if one exists."""
        with self.lock:
            relay = self.relays.pop(session_id, None)
        if relay:
            relay.stop()
            return True
        return False

    def stop_all(self) -> None:
        """Stop all server-side TUN relays during server shutdown."""
        with self.lock:
            session_ids = list(self.relays)
        for session_id in session_ids:
            self.stop(session_id)

    def get(self, session_id: str) -> ServerTunRelay | None:
        """Return a session relay without changing its lifecycle."""
        with self.lock:
            return self.relays.get(session_id)

    def list_status(self) -> list[dict]:
        """Return safe status rows for all active TUN relays."""
        with self.lock:
            relays = list(self.relays.values())
        return [relay.status() for relay in relays]
