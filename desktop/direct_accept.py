"""Direct-mode channel pairing for the host.

A viewer opens four TCP connections to the host (video, then input, control, audio), finishing the
TLS handshake on each before opening the next. The host listens on four ports, and nothing on the
wire says which input connection belongs to which video connection: the pairing has always been
"the next one that arrives".

The original host accepted one viewer at a time to keep that pairing trivially correct, so a viewer
that opened the video channel and left made the whole accept loop wait up to HANDSHAKE_TIMEOUT for
channels that would never come, and the next viewer timed out behind it.

This module keeps the pairing rule but removes the global wait:

  * the three secondary ports are drained by listener threads into per-source-address queues, so
    waiting for one viewer's channels never blocks the video listener or a viewer at another address;
  * a per-address gate lets only one setup per source address run at a time (two viewers behind the
    same NAT would otherwise be indistinguishable), and stale queued sockets for that address are
    thrown away before a new setup starts;
  * while a setup waits for the next channel it watches the viewer's video connection, and gives up
    at once if the viewer has closed it, instead of sitting out the timeout.

No third-party imports, so it can be tested without a display or the capture/input libraries.
"""
import collections
import select
import socket
import threading
import time
from contextlib import contextmanager

POLL_SECONDS = 0.25          # how often a waiting setup looks at its video connection
LISTENER_TICK = 1.0          # how often a listener thread wakes to drop stale sockets
MAX_QUEUED_PER_ADDRESS = 6   # sockets waiting for a setup, per source address and channel
STALE_SECONDS = 15.0         # a queued socket nobody claimed in this long is closed


class ChannelPairer:
    """Accepts on the secondary channel listeners and hands sockets to whichever setup asks."""

    def __init__(self, servers: dict, log=print, stale_seconds: float = STALE_SECONDS):
        # servers: {"input": listening socket, "control": ..., "audio": ...}
        self._log = log
        self._stale = stale_seconds
        self._cond = threading.Condition()
        self._queues = {label: collections.defaultdict(collections.deque) for label in servers}
        self._gates = {}                 # address -> [Lock, refcount]
        self._gates_lock = threading.Lock()
        self._closed = False
        self._threads = []
        for label, server in servers.items():
            t = threading.Thread(target=self._listen, args=(label, server), daemon=True,
                                 name=f"pair-listen-{label}")
            t.start()
            self._threads.append(t)

    # ------------------------------------------------------------------ listeners

    def _listen(self, label: str, server: socket.socket) -> None:
        try:
            server.settimeout(LISTENER_TICK)
        except OSError:
            return                          # already closed: host is shutting down
        while not self._closed:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                self._drop_stale()
                continue
            except OSError:
                return                      # listener closed: host is shutting down
            if self._closed:
                conn.close()                # accepted just as we were closing; nobody will claim it
                return
            self._log(f"[host] [{label}] viewer connected from {addr}")
            with self._cond:
                queue = self._queues[label][addr[0]]
                if len(queue) >= MAX_QUEUED_PER_ADDRESS:
                    conn.close()
                    continue
                queue.append((conn, time.monotonic()))
                self._cond.notify_all()
            self._drop_stale()

    def _drop_stale(self) -> None:
        cutoff = time.monotonic() - self._stale
        dead = []
        with self._cond:
            for per_label in self._queues.values():
                for address in list(per_label):
                    queue = per_label[address]
                    while queue and queue[0][1] < cutoff:
                        dead.append(queue.popleft()[0])
                    if not queue:
                        del per_label[address]
        for conn in dead:
            _close(conn)

    def close(self) -> None:
        self._closed = True
        dead = []
        with self._cond:
            for per_label in self._queues.values():
                for queue in per_label.values():
                    dead.extend(conn for conn, _ in queue)
                per_label.clear()
            self._cond.notify_all()
        for conn in dead:
            _close(conn)

    # ------------------------------------------------------------------ one viewer's setup

    @contextmanager
    def gate(self, address: str):
        """One setup at a time per source address. Different addresses never wait for each other."""
        with self._gates_lock:
            entry = self._gates.setdefault(address, [threading.Lock(), 0])
            entry[1] += 1
        entry[0].acquire()
        try:
            self._discard(address)          # anything queued before this setup began is stale
            yield
        finally:
            self._discard(address)          # and anything left over belongs to a setup that ended
            entry[0].release()
            with self._gates_lock:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._gates[address]

    def _discard(self, address: str) -> None:
        dead = []
        with self._cond:
            for per_label in self._queues.values():
                queue = per_label.pop(address, None)
                if queue:
                    dead.extend(conn for conn, _ in queue)
        for conn in dead:
            _close(conn)

    def take(self, label: str, address: str, timeout: float, watch=None) -> socket.socket:
        """The next raw socket that arrives on `label` from `address`.

        `watch` is the viewer's established video connection. Nothing legitimate can arrive on it
        while the viewer is still opening its other channels (it only sends its login request once
        all four are up), so if it becomes readable the viewer has closed it or is misbehaving, and
        the wait ends immediately with ConnectionAbortedError.

        Raises socket.timeout (an OSError) if nothing arrives in `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while True:
            with self._cond:
                queue = self._queues[label].get(address)
                if queue:
                    conn, _ = queue.popleft()
                    if not queue:
                        del self._queues[label][address]
                    return conn
                if self._closed:
                    raise ConnectionAbortedError("host is shutting down")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout(f"no {label} channel from {address} within {timeout:g}s")
                self._cond.wait(min(POLL_SECONDS, remaining))
            if watch is not None and _peer_gone(watch):
                raise ConnectionAbortedError(f"viewer {address} closed its video channel "
                                             f"before opening {label}")


def _peer_gone(conn) -> bool:
    try:
        readable, _, _ = select.select([conn], [], [], 0)
    except (OSError, ValueError):
        return True
    return bool(readable)


def _close(conn) -> None:
    try:
        conn.close()
    except OSError:
        pass
