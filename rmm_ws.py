"""
Minimal WebSocket support (stdlib only) for operator event streaming.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
from typing import Any, Callable, Optional, Set


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept_key(sec_key: str) -> str:
    digest = hashlib.sha1((sec_key + WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def _get_header(headers, name: str) -> str:
    """Case-insensitive header lookup (dict or http.client HTTPMessage)."""
    if headers is None:
        return ""
    try:
        val = headers.get(name)
        if val:
            return val.split(",")[0].strip()
    except (TypeError, AttributeError):
        pass
    if isinstance(headers, dict):
        name_l = name.lower()
        for key, val in headers.items():
            if str(key).lower() == name_l and val:
                return str(val).split(",")[0].strip()
    return ""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def _read_frame(sock: socket.socket) -> tuple[int, bytes]:
    hdr = _recv_exact(sock, 2)
    fin = (hdr[0] & 0x80) != 0
    opcode = hdr[0] & 0x0F
    masked = (hdr[1] & 0x80) != 0
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length) if length else b""
    if masked and mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    """Server-to-client frame (unmasked per RFC 6455)."""
    fin_opcode = 0x80 | (opcode & 0x0F)
    length = len(payload)
    header = bytearray([fin_opcode])
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    sock.sendall(header + payload)


class WebSocketConnection:
    """Server-side WebSocket over an already-connected TCP socket."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.sock.settimeout(300.0)
        self._closed = False
        self._io_lock = threading.Lock()

    @classmethod
    def from_http_request(
        cls,
        sock: socket.socket,
        headers,
        path: str = "",
    ) -> Optional["WebSocketConnection"]:
        key = _get_header(headers, "Sec-WebSocket-Key")
        if not key:
            return None
        version = _get_header(headers, "Sec-WebSocket-Version")
        if version != "13":
            return None
        accept = _ws_accept_key(key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        )
        sock.sendall(response.encode())
        return cls(sock)

    def send_json(self, data: Any) -> None:
        if self._closed:
            return
        try:
            with self._io_lock:
                _send_frame(self.sock, 0x1, json.dumps(data).encode("utf-8"))
        except OSError:
            self._closed = True

    def recv_json(self) -> Optional[Any]:
        if self._closed:
            return None
        try:
            with self._io_lock:
                while True:
                    opcode, payload = _read_frame(self.sock)
                    if opcode == 0x8:
                        self._closed = True
                        return None
                    if opcode == 0x9:
                        _send_frame(self.sock, 0xA, payload)
                        continue
                    if opcode == 0x1:
                        return json.loads(payload.decode("utf-8"))
        except socket.timeout:
            return {"op": "_timeout"}
        except (OSError, json.JSONDecodeError, ConnectionError):
            self._closed = True
            return None

    def close(self) -> None:
        if self._closed:
            return
        try:
            _send_frame(self.sock, 0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


class OperatorEventHub:
    """Broadcast operator events to subscribed WebSocket clients."""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients: Set[WebSocketConnection] = set()
        self._filters: dict = {}  # id(conn) -> session_id or None

    def add(self, ws: WebSocketConnection, session_filter: Optional[str] = None) -> None:
        with self._lock:
            self._clients.add(ws)
            self._filters[id(ws)] = session_filter

    def remove(self, ws: WebSocketConnection) -> None:
        with self._lock:
            self._clients.discard(ws)
            self._filters.pop(id(ws), None)

    def set_filter(self, ws: WebSocketConnection, session_id: Optional[str]) -> None:
        with self._lock:
            if ws in self._clients:
                self._filters[id(ws)] = session_id

    def _snapshot(self):
        with self._lock:
            return [(ws, self._filters.get(id(ws))) for ws in list(self._clients)]

    def broadcast(self, message: dict, session_id: Optional[str] = None) -> None:
        dead = []
        for ws, filt in self._snapshot():
            if filt and session_id and filt != session_id:
                continue
            try:
                ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws)
            ws.close()

    def broadcast_event(self, session_id: str, event: dict) -> None:
        self.broadcast(
            {"op": "event", "session_id": session_id, "event": event},
            session_id=session_id,
        )

    def broadcast_sessions(self, sessions: list) -> None:
        self.broadcast({"op": "sessions", "sessions": sessions})
