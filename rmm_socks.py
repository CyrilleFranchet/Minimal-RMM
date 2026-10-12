"""
SOCKS5 listener on the operator host; TCP and TUN UDP flows are relayed through
a pivot plugin.

This module owns the server-side listener, task queue, and relay framing used by
the public `/socks` channel and the private `/tun` channel. The agent-side
relay implementation is delivered separately as a pivot plugin; the main
beacon only reports the matching lifecycle flag. Current plugins use HTTP poll:

  - HTTP GET/POST /socks (current plugin transport)
  - HTTP GET/POST /tun (dedicated TUN plugin transport)
  - WebSocket GET /socks?... with Upgrade (available to compatible plugins)
"""

from __future__ import annotations

import base64
import json
import socket
import struct
import threading
import time
import uuid
from collections import deque
from typing import Any

DEFAULT_SOCKS_PORT = 1080
DEFAULT_BIND_HOST = "127.0.0.1"
GO_SOCKS_PLUGIN_FILE = "socks.dll"
SOCKS_CHUNK = 16384
CONNECT_TIMEOUT = 45.0
RELAY_IDLE_TIMEOUT = 300.0
# Cap sends per pull so JSON stays under common proxy frame limits (~1 MiB).
MAX_SENDS_PER_PULL = 32
# UDP payloads are larger than normal TCP chunks, so use a smaller batch cap.
MAX_UDP_SENDS_PER_PULL = 8
MAX_QUEUED_UDP_TASKS = 256


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _ub64(text: str) -> bytes:
    return base64.b64decode(text)


class SessionSocksBridge:
    """Per-session SOCKS relay state (server-side listener + beacon task queue)."""

    def __init__(
        self,
        session_id: str,
        bind_host: str,
        port: int,
        on_log=None,
        udp_idle_timeout: float = RELAY_IDLE_TIMEOUT,
    ):
        self.session_id = session_id
        self.bind_host = bind_host
        self.port = port
        self.on_log = on_log or (lambda _msg, _level="INFO": None)
        self.udp_idle_timeout = max(1.0, float(udp_idle_timeout))
        self.lock = threading.Lock()
        self.task_queue: deque[dict[str, Any]] = deque()
        self.connect_events: dict[str, threading.Event] = {}
        self.connect_errors: dict[str, str] = {}
        self.tunnels: dict[str, dict[str, Any]] = {}
        self._pending_sends: dict[str, deque[dict[str, Any]]] = {}
        self._queued_udp_tasks = 0
        self._listener_sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._running = False
        self._udp_sock: socket.socket | None = None
        self._udp_control_sock: socket.socket | None = None
        self._udp_thread: threading.Thread | None = None
        self.udp_tunnels: dict[str, dict[str, Any]] = {}
        self._agent_ws: Any = None
        self._agent_ws_lock = threading.Lock()

    def log(self, msg: str, level: str = "INFO"):
        self.on_log(msg, level)

    def start(self) -> None:
        with self.lock:
            if self._running:
                return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind_host, self.port))
            sock.listen(64)
            sock.settimeout(1.0)
        except Exception:
            sock.close()
            raise

        accept_thread = threading.Thread(
            target=self._accept_loop, name=f"socks-{self.session_id[:8]}", daemon=True
        )
        with self.lock:
            if self._running:
                sock.close()
                return
            self._listener_sock = sock
            self._accept_thread = accept_thread
            self.port = int(sock.getsockname()[1])
            self._running = True
        try:
            accept_thread.start()
        except Exception:
            with self.lock:
                self._running = False
                if self._listener_sock is sock:
                    self._listener_sock = None
                if self._accept_thread is accept_thread:
                    self._accept_thread = None
            sock.close()
            raise
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
            self._queued_udp_tasks = 0
            udp_sock = self._udp_sock
            udp_control_sock = self._udp_control_sock
            self._udp_sock = None
            self._udp_control_sock = None
            self.udp_tunnels.clear()
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
        if udp_sock:
            try:
                udp_sock.close()
            except OSError:
                pass
        if udp_control_sock:
            try:
                udp_control_sock.close()
            except OSError:
                pass
        if self._udp_thread and self._udp_thread.is_alive():
            self._udp_thread.join(timeout=2.0)
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
            if task.get("op") == "udp_send":
                if self._queued_udp_tasks >= MAX_QUEUED_UDP_TASKS:
                    return
                self._queued_udp_tasks += 1
            if task.get("op") == "send":
                cid = task.get("id")
                tunnel = self.tunnels.get(cid) if cid else None
                if cid and (not tunnel or not tunnel.get("client")):
                    if cid not in self._pending_sends:
                        self._pending_sends[cid] = deque()
                    self._pending_sends[cid].append(task)
                    return
            self.task_queue.append(task)
        # Tasks are pull-only on the agent WS thread (no push) to keep one reply per pull.

    def _wake_agent_ws(self, task: dict[str, Any]) -> None:
        """Reserved; agent uses pull so frames are not stranded ahead of pull replies."""
        del task

    def _drain_task_queue(self) -> list[dict[str, Any]]:
        """Dequeue tasks, retaining control/close work until it is acknowledged."""
        with self.lock:
            outbound: list[dict[str, Any]] = []
            keep: deque[dict[str, Any]] = deque()
            deferred_sends: deque[dict[str, Any]] = deque()
            deferred_udp: deque[dict[str, Any]] = deque()
            send_count = 0
            udp_count = 0
            for task in self.task_queue:
                op = task.get("op")
                if op == "send":
                    if send_count < MAX_SENDS_PER_PULL:
                        outbound.append(task)
                        send_count += 1
                    else:
                        deferred_sends.append(task)
                elif op == "udp_send":
                    if udp_count < MAX_UDP_SENDS_PER_PULL:
                        outbound.append(task)
                        udp_count += 1
                        self._queued_udp_tasks = max(0, self._queued_udp_tasks - 1)
                    else:
                        deferred_udp.append(task)
                elif op in ("close", "connect", "udp_close"):
                    # Close is retained until the agent acknowledges it. This
                    # lets a later poll recover when the delivery response was
                    # lost in transit.
                    keep.append(task)
                else:
                    keep.append(task)
            self.task_queue = deque([*keep, *deferred_sends, *deferred_udp])
            # Persistent control tasks precede the current send batch.
            return list(keep) + outbound

    def fetch_tasks_for_pull(self) -> list[dict[str, Any]]:
        """Tasks for agent pull (WebSocket handler thread only)."""
        return self._drain_task_queue()

    def _drop_tasks(self, conn_id: str, op: str) -> None:
        with self.lock:
            kept = deque()
            removed_udp = 0
            for task in self.task_queue:
                if task.get("id") == conn_id and task.get("op") == op:
                    if op == "udp_send":
                        removed_udp += 1
                    continue
                kept.append(task)
            self.task_queue = kept
            if removed_udp:
                self._queued_udp_tasks = max(0, self._queued_udp_tasks - removed_udp)

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
                        self.tunnels[cid] = {
                            "id": cid,
                            "client_lock": threading.Lock(),
                            "pending_data": deque(),
                        }
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
                        with self.lock:
                            current = self.tunnels.get(cid)
                            if current is tunnel and not current.get("client"):
                                current.setdefault("pending_data", deque()).append(payload)
                        continue
                    client_lock = tunnel.get("client_lock")
                    try:
                        if client_lock:
                            with client_lock:
                                sock.sendall(payload)
                        else:
                            sock.sendall(payload)
                    except OSError:
                        self._close_tunnel(tunnel)
            elif op == "udp_data":
                data_b64 = resp.get("data_b64")
                host = str(resp.get("host") or "")
                try:
                    port = int(resp.get("port"))
                    payload = _ub64(data_b64 or "")
                except (TypeError, ValueError, OSError):
                    continue
                if host and 1 <= port <= 65535 and payload:
                    self._send_udp_datagram(str(cid), host, port, payload)
            elif op == "udp_closed":
                self._drop_tasks(str(cid), "udp_close")
            elif op in ("closed", "close"):
                self._drop_tasks(cid, "connect")
                self._drop_tasks(cid, "send")
                self._drop_tasks(cid, "close")
                with self.lock:
                    self._pending_sends.pop(cid, None)
                    tunnel = self.tunnels.pop(cid, None)
                if tunnel:
                    self._close_tunnel(tunnel, notify_client=False)

    def _close_tunnel(self, tunnel: dict[str, Any], *, notify_client: bool = True) -> None:
        sock = tunnel.get("client")
        client_lock = tunnel.get("client_lock")
        cid = tunnel.get("id")

        with self.lock:
            if cid and self.tunnels.get(cid) is tunnel:
                self.tunnels.pop(cid, None)
            if cid:
                self._pending_sends.pop(cid, None)
                self.task_queue = deque(
                    task
                    for task in self.task_queue
                    if not (
                        task.get("id") == cid
                        and task.get("op") in ("connect", "send", "close")
                    )
                )
                if notify_client and self._running:
                    self.task_queue.append({"op": "close", "id": cid})

        def _shutdown_client() -> None:
            if not sock:
                return
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

        if sock:
            if client_lock:
                with client_lock:
                    _shutdown_client()
            else:
                _shutdown_client()

    def _attach_client(self, tunnel: dict[str, Any], client_sock: socket.socket) -> bool:
        """Attach the local client and flush data received before the SOCKS reply."""
        client_lock = tunnel.get("client_lock")
        pending: list[bytes] = []
        failed = False
        with self.lock:
            if not self._running or self.tunnels.get(tunnel.get("id")) is not tunnel:
                return False
            # Keep the client absent while flushing the buffered data. This makes
            # concurrent agent responses append to the same FIFO instead of
            # overtaking the first bytes received from the remote target.
            pending_queue = tunnel.setdefault("pending_data", deque())
            pending = list(pending_queue)
            pending_queue.clear()
            try:
                if client_lock:
                    with client_lock:
                        for payload in pending:
                            client_sock.sendall(payload)
                else:
                    for payload in pending:
                        client_sock.sendall(payload)
                tunnel["client"] = client_sock
            except OSError:
                failed = True
        if failed:
            self._close_tunnel(tunnel)
            return False
        return True

    def _wait_connect(self, conn_id: str) -> bool:
        with self.lock:
            if conn_id in self.tunnels:
                return True
            err = self.connect_errors.get(conn_id)
            if err:
                self.connect_errors.pop(conn_id, None)
                self.log(f"SOCKS tunnel {conn_id[:8]} failed: {err}", "WARNING")
                return False
            ev = self.connect_events.get(conn_id)
            if ev is None:
                ev = threading.Event()
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
            command, target_host, target_port = self._socks5_read_command(client_sock)
            if command == 0x03:
                self._handle_udp_associate(client_sock)
                return
            if command != 0x01:
                self._socks5_reply(client_sock, rep=0x07)
                return
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
            if not self._attach_client(tunnel, client_sock):
                return
            self._relay_local_to_remote(tunnel)
        except Exception as exc:
            self.log(f"SOCKS client error: {exc}", "ERROR")
        finally:
            try:
                client_sock.close()
            except OSError:
                pass

    def _handle_udp_associate(self, control_sock: socket.socket) -> None:
        """Serve one SOCKS5 UDP association for a server-side TUN frontend."""
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp_sock.bind((self.bind_host, 0))
            udp_sock.settimeout(1.0)
            bind_host, bind_port = udp_sock.getsockname()[:2]
            with self.lock:
                if not self._running:
                    return
                old = self._udp_sock
                self._udp_sock = udp_sock
                self._udp_control_sock = control_sock
                self.udp_tunnels.clear()
            if old:
                try:
                    old.close()
                except OSError:
                    pass
            if not self._socks5_reply(control_sock, rep=0x00, bind=(bind_host, bind_port)):
                return
            thread = threading.Thread(
                target=self._udp_loop,
                args=(udp_sock,),
                name=f"socks-udp-{self.session_id[:8]}",
                daemon=True,
            )
            self._udp_thread = thread
            thread.start()
            control_sock.settimeout(1.0)
            while self._running:
                try:
                    if not control_sock.recv(1):
                        break
                except socket.timeout:
                    continue
                except OSError:
                    break
        finally:
            with self.lock:
                if self._udp_control_sock is control_sock:
                    self._udp_control_sock = None
                if self._udp_sock is udp_sock:
                    self._udp_sock = None
                    self.udp_tunnels.clear()
            try:
                udp_sock.close()
            except OSError:
                pass

    def _udp_loop(self, udp_sock: socket.socket) -> None:
        """Decode SOCKS5 UDP datagrams and queue them for the agent."""
        while self._running:
            try:
                packet, client_addr = udp_sock.recvfrom(65535)
            except socket.timeout:
                self._expire_udp_tunnels()
                continue
            except OSError:
                return
            decoded = self._decode_socks5_udp(packet)
            if not decoded:
                continue
            host, port, payload = decoded
            key = f"{host}:{port}"
            with self.lock:
                tunnel = self.udp_tunnels.get(key)
                if tunnel is None:
                    tunnel = {"id": uuid.uuid4().hex, "last_seen": time.monotonic()}
                    self.udp_tunnels[key] = tunnel
                tunnel["last_seen"] = time.monotonic()
                tunnel["client_addr"] = client_addr
                conn_id = tunnel["id"]
            self._enqueue_task(
                {
                    "op": "udp_send",
                    "id": conn_id,
                    "host": host,
                    "port": port,
                    "data_b64": _b64(payload),
                }
            )

    def _expire_udp_tunnels(self) -> None:
        """Expire idle UDP destinations and tell the agent to close them."""
        cutoff = time.monotonic() - self.udp_idle_timeout
        expired: list[str] = []
        with self.lock:
            for key, tunnel in list(self.udp_tunnels.items()):
                if tunnel.get("last_seen", 0) < cutoff:
                    expired.append(str(tunnel.get("id") or ""))
                    self.udp_tunnels.pop(key, None)
        for conn_id in expired:
            if conn_id:
                self._enqueue_task({"op": "udp_close", "id": conn_id})

    def _send_udp_datagram(self, conn_id: str, host: str, port: int, payload: bytes) -> None:
        """Wrap an agent UDP response and send it to the SOCKS client."""
        with self.lock:
            tunnel = next(
                (value for value in self.udp_tunnels.values() if value.get("id") == conn_id),
                None,
            )
            udp_sock = self._udp_sock
            client_addr = tunnel.get("client_addr") if tunnel else None
        if not udp_sock or not client_addr:
            return
        try:
            udp_sock.sendto(self._encode_socks5_udp(host, port, payload), client_addr)
        except OSError:
            pass

    @classmethod
    def _decode_socks5_udp(cls, packet: bytes) -> tuple[str, int, bytes] | None:
        """Decode one SOCKS5 UDP request into target host, port, and payload."""
        if len(packet) < 4 or packet[0:2] != b"\x00\x00" or packet[2] != 0:
            return None
        offset = 4
        atyp = packet[3]
        if atyp == 0x01:
            if len(packet) < offset + 4:
                return None
            host = socket.inet_ntoa(packet[offset:offset + 4])
            offset += 4
        elif atyp == 0x03:
            if len(packet) < offset + 1:
                return None
            length = packet[offset]
            offset += 1
            if length == 0 or len(packet) < offset + length:
                return None
            host_b = packet[offset:offset + length]
            try:
                host = host_b.decode("idna")
            except UnicodeError:
                host = host_b.decode("ascii", errors="replace")
            offset += length
        elif atyp == 0x04:
            if len(packet) < offset + 16:
                return None
            host = socket.inet_ntop(socket.AF_INET6, packet[offset:offset + 16])
            offset += 16
        else:
            return None
        if len(packet) < offset + 2:
            return None
        port = struct.unpack("!H", packet[offset:offset + 2])[0]
        payload = packet[offset + 2:]
        if not payload or not 1 <= port <= 65535:
            return None
        return host, port, payload

    @staticmethod
    def _encode_socks5_udp(host: str, port: int, payload: bytes) -> bytes:
        """Encode an agent UDP response as a SOCKS5 UDP datagram."""
        try:
            address = b"\x01" + socket.inet_pton(socket.AF_INET, host)
        except OSError:
            try:
                address = b"\x04" + socket.inet_pton(socket.AF_INET6, host)
            except OSError:
                encoded = host.encode("idna")[:255]
                address = b"\x03" + bytes([len(encoded)]) + encoded
        return b"\x00\x00\x00" + address + struct.pack("!H", port) + payload

    def _relay_local_to_remote(self, tunnel: dict[str, Any]) -> None:
        client_sock = tunnel["client"]
        conn_id = tunnel["id"]
        client_lock = tunnel.get("client_lock")
        client_sock.settimeout(1.0)
        try:
            while True:
                with self.lock:
                    if not self._running or conn_id not in self.tunnels:
                        break
                try:
                    if client_lock:
                        with client_lock:
                            data = client_sock.recv(SOCKS_CHUNK)
                    else:
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
    def _read_exact(client_sock: socket.socket, size: int) -> bytes | None:
        """Read exactly ``size`` bytes, tolerating normal TCP fragmentation."""
        data = bytearray()
        while len(data) < size:
            chunk = client_sock.recv(size - len(data))
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data)

    @classmethod
    def _socks5_handshake(cls, client_sock: socket.socket) -> bool:
        header = cls._read_exact(client_sock, 2)
        if header is None or header[0] != 0x05:
            return False
        nmethods = header[1]
        methods = cls._read_exact(client_sock, nmethods)
        if methods is None:
            return False
        if 0x00 not in methods:
            client_sock.sendall(b"\x05\xff")
            return False
        client_sock.sendall(b"\x05\x00")
        return True

    @classmethod
    def _socks5_read_command(cls, client_sock: socket.socket) -> tuple[int | None, str | None, int | None]:
        """Read a SOCKS5 command and its address tuple from a client."""
        req = cls._read_exact(client_sock, 4)
        if req is None or len(req) < 4 or req[0] != 0x05:
            return None, None, None
        command = req[1]
        atyp = req[3]
        if atyp == 0x01:
            raw = cls._read_exact(client_sock, 4)
            if raw is None:
                return command, None, None
            host = socket.inet_ntoa(raw)
        elif atyp == 0x03:
            n = cls._read_exact(client_sock, 1)
            if n is None:
                return command, None, None
            host_len = n[0]
            host_b = cls._read_exact(client_sock, host_len)
            if host_b is None:
                return command, None, None
            try:
                host = host_b.decode("idna")
            except UnicodeError:
                host = host_b.decode("ascii", errors="replace")
        elif atyp == 0x04:
            raw = cls._read_exact(client_sock, 16)
            if raw is None:
                return command, None, None
            host = socket.inet_ntop(socket.AF_INET6, raw)
        else:
            return command, None, None
        port_b = cls._read_exact(client_sock, 2)
        if port_b is None:
            return command, None, None
        port = struct.unpack("!H", port_b)[0]
        return command, host, port

    @classmethod
    def _socks5_read_request(cls, client_sock: socket.socket) -> tuple[str | None, int | None]:
        """Read a SOCKS5 CONNECT request for compatibility with older callers."""
        command, host, port = cls._socks5_read_command(client_sock)
        if command != 0x01:
            return None, None
        return host, port

    @staticmethod
    def _socks5_reply(
        client_sock: socket.socket,
        rep: int,
        bind: tuple[str, int] = ("0.0.0.0", 0),
    ) -> bool:
        """Send a SOCKS5 reply with an IPv4 or IPv6 bound address."""
        # VER REP RSV ATYP BND.ADDR BND.PORT.
        host, port = bind
        try:
            address = b"\x01" + socket.inet_pton(socket.AF_INET, host)
        except OSError:
            address = b"\x04" + socket.inet_pton(socket.AF_INET6, host)
        reply = struct.pack("!BBBB", 0x05, rep, 0x00, address[0]) + address[1:] + struct.pack("!H", port)
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

    def start(
        self,
        session_id: str,
        port: int = DEFAULT_SOCKS_PORT,
        bind_host: str = DEFAULT_BIND_HOST,
        udp_idle_timeout: float = RELAY_IDLE_TIMEOUT,
    ) -> SessionSocksBridge:
        with self.lock:
            existing = self.bridges.pop(session_id, None)
            if existing:
                existing.stop()
            bridge = SessionSocksBridge(
                session_id,
                bind_host,
                port,
                on_log=lambda m, lvl="INFO": self._log(m, lvl),
                udp_idle_timeout=udp_idle_timeout,
            )
            try:
                bridge.start()
            except Exception:
                # A failed bind must not leave an unusable bridge registered.
                bridge.stop()
                raise
            self.bridges[session_id] = bridge
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

    def list_relays(self) -> list[dict[str, Any]]:
        """Active SOCKS listeners and agent channel state (global operator view)."""
        with self.lock:
            bridges = list(self.bridges.values())
        relays: list[dict[str, Any]] = []
        for bridge in bridges:
            with bridge.lock:
                tunnels = len(bridge.tunnels) + len(bridge.udp_tunnels)
                running = bridge._running
            ws = bridge.agent_ws_connected()
            relays.append({
                "session_id": bridge.session_id,
                "bind_host": bridge.bind_host,
                "port": bridge.port,
                "socks_url": f"socks5://{bridge.bind_host}:{bridge.port}",
                "listener_running": running,
                "agent_websocket": ws,
                "agent_channel": "websocket" if ws else "http_or_pending",
                "active_tunnels": tunnels,
            })
        relays.sort(key=lambda r: (r["port"], r["session_id"]))
        return relays

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
