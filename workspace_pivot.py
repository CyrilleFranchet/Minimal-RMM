"""Workspace-local SOCKS and TUN data-plane relays.

The central RMM server keeps the authenticated agent transport. This module
runs in an operator's Exegol container, owns the local SOCKS listener and TUN
device, and forwards the existing JSON task protocol through the workspace
broker endpoints.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from rmm_socks import DEFAULT_BIND_HOST, DEFAULT_SOCKS_PORT, SocksManager
from rmm_tun import ServerTunRelay, TunError, normalize_tun_config


class WorkspacePivotManager:
    """Own workspace-local SOCKS/TUN listeners and bridge tasks upstream."""

    def __init__(
        self,
        helper_path: str,
        submit_tasks: Callable[[str, str, list[dict[str, Any]]], bool],
        fetch_responses: Callable[[str, str], list[dict[str, Any]]],
        log_fn: Callable[[str, str], None] | None = None,
    ):
        self.helper_path = helper_path
        self.submit_tasks = submit_tasks
        self.fetch_responses = fetch_responses
        self.log_fn = log_fn or (lambda _message, _level="INFO": None)
        self.socks = SocksManager(log_fn=self.log_fn)
        self.tun_transport = SocksManager(log_fn=self.log_fn)
        self.tun_relays: dict[str, ServerTunRelay] = {}
        self._lock = threading.RLock()
        self._running = True
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._sync_loop,
            name="workspace-pivot-sync",
            daemon=True,
        )
        self._thread.start()

    def log(self, message: str, level: str = "INFO") -> None:
        """Send a safe lifecycle message to the workspace service logger."""
        self.log_fn(message, level)

    def start_socks(
        self,
        session_id: str,
        port: int = DEFAULT_SOCKS_PORT,
        bind_host: str = DEFAULT_BIND_HOST,
    ) -> dict[str, Any]:
        """Start the workspace-local public SOCKS5 listener."""
        with self._lock:
            if not self._running:
                raise RuntimeError("workspace pivots are stopped")
            bridge = self.socks.start(session_id, port=port, bind_host=bind_host)
        return {
            "session_id": session_id,
            "bind_host": bridge.bind_host,
            "port": bridge.port,
            "socks_url": f"socks5://{bridge.bind_host}:{bridge.port}",
            "listener_running": True,
            "workspace_local": True,
        }

    def stop_socks(self, session_id: str) -> bool:
        """Stop the workspace-local public SOCKS5 listener."""
        return self.socks.stop(session_id)

    def socks_active(self, session_id: str) -> bool:
        """Return whether a local public SOCKS listener is active."""
        return self.socks.get(session_id) is not None

    def list_socks_status(self) -> list[dict[str, Any]]:
        """Return the local public SOCKS inventory."""
        rows = self.socks.list_relays()
        for row in rows:
            row["workspace_local"] = True
        return rows

    def start_tun(self, session_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """Start a local TUN helper and route target traffic through the broker."""
        with self._lock:
            if not self._running:
                raise TunError("workspace pivots are stopped")
            normalized = normalize_tun_config(config)
            if self.tun_active(session_id):
                raise TunError("TUN relay is already active")
            relay = ServerTunRelay(
                session_id,
                normalized,
                self.helper_path,
                self.tun_transport,
                log_fn=self.log_fn,
            )
            try:
                relay.start()
            except BaseException:
                relay.stop()
                raise
            self.tun_relays[session_id] = relay
        status = relay.status()
        status["workspace_local"] = True
        return status

    def stop_tun(self, session_id: str) -> bool:
        """Stop a local TUN helper and remove its workspace routes."""
        with self._lock:
            relay = self.tun_relays.pop(session_id, None)
        if not relay:
            return False
        relay.stop()
        return True

    def tun_active(self, session_id: str) -> bool:
        """Return whether a local TUN helper is active."""
        with self._lock:
            relay = self.tun_relays.get(session_id)
        return bool(relay and relay.alive())

    def tun_config(self, session_id: str) -> dict[str, Any] | None:
        """Return the local TUN status for one session."""
        with self._lock:
            relay = self.tun_relays.get(session_id)
        if not relay:
            return None
        status = relay.status()
        status["workspace_local"] = True
        return status

    def list_tun_status(self) -> list[dict[str, Any]]:
        """Return the local TUN inventory."""
        with self._lock:
            relays = list(self.tun_relays.values())
        rows = []
        for relay in relays:
            row = relay.status()
            row["workspace_local"] = True
            rows.append(row)
        return rows

    def sync_once(self) -> None:
        """Exchange pending task and response batches for every local pivot."""
        for channel, manager in (("socks", self.socks), ("tun", self.tun_transport)):
            for row in manager.list_relays():
                if not self._running:
                    return
                session_id = str(row.get("session_id") or "")
                if not session_id:
                    continue
                tasks = manager.poll_tasks(session_id)
                if tasks:
                    if not self.submit_tasks(session_id, channel, tasks):
                        manager.requeue_tasks(session_id, tasks)
                if not self._running:
                    return
                responses = self.fetch_responses(session_id, channel)
                if responses:
                    manager.submit_agent_responses(session_id, responses)

    def _sync_loop(self) -> None:
        """Keep the workspace data plane moving without blocking HTTP handlers."""
        while self._running:
            try:
                self.sync_once()
            except Exception as exc:
                if self._running:
                    self.log(f"Workspace pivot sync failed: {exc}", "WARNING")
            self._stop_event.wait(0.025)

    def stop_all(self) -> None:
        """Stop all local pivots and their background task bridge."""
        with self._lock:
            self._running = False
            self._stop_event.set()
            relays = list(self.tun_relays.values())
            self.tun_relays.clear()
        actions = [self.socks.stop_all, *(relay.stop for relay in relays), self.tun_transport.stop_all]
        first_error = None
        for action in actions:
            try:
                action()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join()
        if first_error is not None:
            raise first_error
