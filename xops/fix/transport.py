"""Transports for the session layer: a blocking TCP socket with a reader thread (wall clock) and an in-process pair on a
virtual clock with configurable one-way latency and fault hooks (drop, duplicate, reorder, delay, silence)."""
from __future__ import annotations

import heapq
import socket
import threading
from typing import Callable


class SocketTransport:
    def __init__(self, sock: socket.socket):
        self.sock = sock; self.closed = False

    def send(self, raw: bytes):
        if not self.closed:
            try:
                self.sock.sendall(raw)
            except OSError:
                self.closed = True

    def reader(self, on_bytes: Callable[[bytes], None], on_close: Callable[[], None] | None = None) -> threading.Thread:
        def run():
            while not self.closed:
                try:
                    chunk = self.sock.recv(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    self.closed = True
                    if on_close:
                        on_close()
                    return
                on_bytes(chunk)
        t = threading.Thread(target=run, daemon=True); t.start(); return t

    def close(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def listen(port: int, host: str = "127.0.0.1") -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind((host, port)); s.listen(1); return s


def connect(port: int, host: str = "127.0.0.1", timeout: float = 5.0) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


class VirtualLink:
    """Two in-process endpoints joined by delay queues on a shared virtual clock.  `pump(now)` delivers everything due."""

    def __init__(self, latency_ab: float = 0.002, latency_ba: float = 0.002):
        self.latency = {"a": latency_ab, "b": latency_ba}
        self.queue: list[tuple[float, int, str, bytes]] = []
        self.n = 0
        self.receivers: dict[str, Callable[[bytes], None]] = {}
        self.faults = {"a": {}, "b": {}}   # side -> {drop: n, dup: n, delay: seconds, silent: bool, reorder: n}
        self.stats = {"delivered": 0, "dropped": 0, "duplicated": 0, "delayed": 0, "reordered": 0}
        self.now = 0.0

    def endpoint(self, side: str) -> "VirtualEndpoint":
        return VirtualEndpoint(self, side)

    def _enqueue(self, side: str, raw: bytes, now: float):
        f = self.faults[side]
        if f.get("silent"):
            self.stats["dropped"] += 1; return
        if f.get("drop", 0) > 0:
            f["drop"] -= 1; self.stats["dropped"] += 1; return
        delay = self.latency[side] + f.get("delay", 0.0)
        if f.get("delay", 0.0):
            self.stats["delayed"] += 1
        copies = 1
        if f.get("dup", 0) > 0:
            f["dup"] -= 1; copies = 2; self.stats["duplicated"] += 1
        for k in range(copies):
            order = self.n; self.n += 1
            if f.get("reorder", 0) > 0 and k == 0:
                f["reorder"] -= 1; delay += 5 * self.latency[side]; self.stats["reordered"] += 1
            heapq.heappush(self.queue, (now + delay, order, side, raw))

    def pump(self, now: float):
        self.now = now
        while self.queue and self.queue[0][0] <= now:
            _, _, side, raw = heapq.heappop(self.queue)
            dest = "b" if side == "a" else "a"
            if dest in self.receivers:
                self.receivers[dest](raw); self.stats["delivered"] += 1

    def next_due(self) -> float | None:
        return self.queue[0][0] if self.queue else None


class VirtualEndpoint:
    def __init__(self, link: VirtualLink, side: str):
        self.link, self.side = link, side

    def send(self, raw: bytes):
        self.link._enqueue(self.side, raw, self.link.now)

    def attach(self, on_bytes: Callable[[bytes], None]):
        self.link.receivers[self.side] = on_bytes
