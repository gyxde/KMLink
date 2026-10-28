"""Application presence on a LAN, without sending pairing keys or trusting metadata.

Presence is deliberately unauthenticated. It is only a way to choose a peer;
the invitation/consent and encrypted control protocols establish a session.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import ipaddress
import json
import re
import socket
import sys
import threading
import time
from typing import Callable

from .network import local_ipv4_addresses


DISCOVERY_GROUP = "239.255.77.77"
DISCOVERY_PORT = 24881
ANNOUNCE_INTERVAL = 2.0
PEER_TTL = 7.0
MAX_PACKET_BYTES = 1400
MAX_PEERS = 64
MAX_ADDRESSES_PER_PEER = 8
_APP = "kmlink-discovery"
_ID = re.compile(r"[A-Za-z0-9_-]{8,80}\Z")


@dataclass(frozen=True)
class Peer:
    device_id: str
    name: str
    system: str
    ip: str
    port: int
    busy: bool
    last_seen: float


def _text(value: object) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 80
            and value == value.strip()
            and all(char.isprintable() and not 0xD800 <= ord(char) <= 0xDFFF for char in value))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _bad_constant(value):
    raise ValueError("Non-finite JSON number")


def _parse_packet(data: bytes, ip: str, now: float) -> tuple[str, str, Peer | None]:
    """Validate a datagram and derive the address from recvfrom(), never JSON."""
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_PACKET_BYTES:
        raise ValueError("Discovery packet too large")
    address = ipaddress.IPv4Address(ip)
    if address.is_multicast or address.is_unspecified or int(address) == 0xFFFFFFFF:
        raise ValueError("Invalid sender address")
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_bad_constant)
    if not isinstance(value, dict) or value.get("app") != _APP:
        raise ValueError("Not a KMLink announcement")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("Unsupported presence protocol")
    device_id = value.get("device_id")
    if not isinstance(device_id, str) or not _ID.fullmatch(device_id):
        raise ValueError("Invalid device identifier")
    kind = value.get("kind")
    common = {"app", "version", "kind", "device_id"}
    if kind in {"query", "bye"}:
        if set(value) != common:
            raise ValueError("Unexpected query fields")
        return kind, device_id, None
    if kind != "announce" or set(value) != common | {"name", "system", "port", "busy"}:
        raise ValueError("Unexpected announcement fields")
    if not _text(value["name"]) or not _text(value["system"]):
        raise ValueError("Invalid device description")
    port = value["port"]
    if type(port) is not int or not 1 <= port <= 65535 or type(value["busy"]) is not bool:
        raise ValueError("Invalid invitation endpoint")
    return kind, device_id, Peer(device_id, value["name"], value["system"],
                                 str(address), port, value["busy"], now)


def _encode(kind: str, device_id: str, name: str = "", system: str = "",
            port: int = 0, busy: bool = False) -> bytes:
    value = {"app": _APP, "version": 1, "kind": kind, "device_id": device_id}
    if kind == "announce":
        value.update(name=name, system=system, port=port, busy=busy)
    result = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    # Apply exactly the same schema to our own packets.
    _parse_packet(result, "127.0.0.1", time.monotonic())
    return result


class _Catalog:
    def __init__(self, local_id: str):
        self.local_id = local_id
        self.peers: dict[str, Peer] = {}
        self._endpoints: dict[str, dict[str, Peer]] = {}

    @staticmethod
    def _address_rank(ip: str) -> int:
        address = ipaddress.IPv4Address(ip)
        if any(address in network for network in (
                ipaddress.IPv4Network("10.0.0.0/8"), ipaddress.IPv4Network("172.16.0.0/12"),
                ipaddress.IPv4Network("192.168.0.0/16"))):
            return 0
        if address.is_link_local:
            return 1
        if address.is_global:
            return 2
        if address.is_loopback:
            return 4
        # Benchmark/TUN ranges (198.18/15), reserved, or documentation ranges
        # should not displace an address from the physical private network.
        return 3

    def _select(self, endpoints: dict[str, Peer], previous: Peer | None) -> Peer:
        rank = min(self._address_rank(ip) for ip in endpoints)
        if previous and previous.ip in endpoints and self._address_rank(previous.ip) == rank:
            return endpoints[previous.ip]
        return min(endpoints.values(), key=lambda peer: (self._address_rank(peer.ip), int(ipaddress.IPv4Address(peer.ip))))

    @staticmethod
    def _different(previous: Peer | None, selected: Peer | None) -> bool:
        if previous is None or selected is None:
            return previous != selected
        return replace(previous, last_seen=selected.last_seen) != selected

    def update(self, peer: Peer) -> bool:
        if peer.device_id == self.local_id:
            return False
        previous = self.peers.get(peer.device_id)
        if previous is None and len(self.peers) >= MAX_PEERS:
            return False
        endpoints = self._endpoints.setdefault(peer.device_id, {})
        for ip in list(endpoints):
            if peer.last_seen - endpoints[ip].last_seen >= PEER_TTL:
                del endpoints[ip]
        if peer.ip not in endpoints and len(endpoints) >= MAX_ADDRESSES_PER_PEER:
            # Prefer replacing the oldest least-useful candidate, so a later
            # real LAN announcement can still displace a full set of TUN IPs.
            worst_ip = max(endpoints, key=lambda ip: (self._address_rank(ip), -endpoints[ip].last_seen))
            if self._address_rank(peer.ip) > self._address_rank(worst_ip):
                selected = self._select(endpoints, previous)
                self.peers[peer.device_id] = selected
                return self._different(previous, selected)
            del endpoints[worst_ip]
        endpoints[peer.ip] = peer
        selected = self._select(endpoints, previous)
        self.peers[peer.device_id] = selected
        return self._different(previous, selected)

    def remove(self, device_id: str, ip: str) -> bool:
        endpoints = self._endpoints.get(device_id)
        if not endpoints or ip not in endpoints:
            return False
        previous = self.peers.get(device_id)
        del endpoints[ip]
        if not endpoints:
            del self._endpoints[device_id]
            self.peers.pop(device_id, None)
            return previous is not None
        selected = self._select(endpoints, previous)
        self.peers[device_id] = selected
        return self._different(previous, selected)

    def expire(self, now: float) -> bool:
        changed = False
        for device_id, endpoints in list(self._endpoints.items()):
            previous = self.peers.get(device_id)
            for ip, peer in list(endpoints.items()):
                if now - peer.last_seen >= PEER_TTL:
                    del endpoints[ip]
            if not endpoints:
                del self._endpoints[device_id]
                self.peers.pop(device_id, None)
                changed = True
            else:
                selected = self._select(endpoints, previous)
                self.peers[device_id] = selected
                changed = self._different(previous, selected) or changed
        return changed

    def snapshot(self) -> list[Peer]:
        return sorted(self.peers.values(), key=lambda peer: (peer.name.casefold(), peer.device_id, peer.ip))


class _RateLimit:
    """Bound both traffic per origin and memory used for untrusted senders."""
    def __init__(self):
        self.sources: dict[str, tuple[float, float]] = {}

    def allow(self, ip: str, now: float) -> bool:
        previous = self.sources.get(ip)
        if previous is None:
            if len(self.sources) >= 256:
                self.sources = {key: val for key, val in self.sources.items() if now - val[1] < 30}
                if len(self.sources) >= 256:
                    return False
            tokens, last = 24., now
        else:
            tokens, last = previous
            tokens = min(24., tokens + max(0., now - last) * 12.)
        allowed = tokens >= 1.
        self.sources[ip] = (tokens - 1. if allowed else tokens, now)
        return allowed


def _interface_addresses() -> list[str]:
    """Use each known IPv4 adapter; the default interface remains a fallback."""
    return ["0.0.0.0", *local_ipv4_addresses()[:16]]


@dataclass
class _Run:
    stop: threading.Event = field(default_factory=threading.Event)
    refresh: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    sock: socket.socket | None = None
    interfaces: list[str] = field(default_factory=list)
    last_replies: dict[str, float] = field(default_factory=dict)


class Discovery:
    """Periodic LAN presence; on_change and on_error run on a worker thread."""
    def __init__(self, device_id: str, name: str, system: str, port: int,
                 on_change: Callable[[list[Peer]], None],
                 on_error: Callable[[str], None] | None = None):
        # Also verifies packet size for long multi-byte device names.
        _encode("announce", device_id, name, system, port)
        self.device_id, self.name, self.system, self.port = device_id, name, system, port
        self.on_change = on_change
        self.on_error = on_error or (lambda message: None)
        self._busy = False
        self._lock = threading.RLock()
        self._catalog = _Catalog(device_id)
        self._run: _Run | None = None

    @property
    def peers(self) -> list[Peer]:
        with self._lock:
            return self._catalog.snapshot()

    @property
    def listening_address(self) -> tuple | None:
        run = self._run
        if run and run.sock:
            try:
                return run.sock.getsockname()
            except OSError:
                pass
        return None

    def start(self) -> None:
        with self._lock:
            if (self._run is not None and not self._run.stop.is_set()
                    and self._run.thread is not None and self._run.thread.is_alive()):
                return
            run = _Run()
            self._run = run
            self._catalog = _Catalog(self.device_id)
            run.thread = threading.Thread(target=self._work, args=(run,),
                                          name="kmlink-discovery", daemon=True)
            run.thread.start()

    def stop(self) -> None:
        with self._lock:
            run, self._run = self._run, None
        if run is None:
            return
        run.stop.set()
        if run.thread and run.thread is not threading.current_thread():
            run.thread.join(timeout=1.)
        # The receive loop normally wakes within 200 ms, announces departure,
        # and closes itself. Only force-close when cooperative shutdown stalls.
        if run.thread and run.thread.is_alive() and run.sock is not None:
            try:
                run.sock.close()
            except OSError:
                pass
        with self._lock:
            self._catalog = _Catalog(self.device_id)

    def refresh(self) -> None:
        run = self._run
        if run:
            run.refresh.set()

    def set_busy(self, busy: bool) -> None:
        if type(busy) is not bool:
            raise ValueError("Busy state must be boolean")
        with self._lock:
            changed = self._busy != busy
            self._busy = busy
        if changed:
            self.refresh()

    def _packet(self, kind: str) -> bytes:
        with self._lock:
            return _encode(kind, self.device_id, self.name, self.system, self.port, self._busy)

    def _emit(self, run: _Run) -> None:
        with self._lock:
            if self._run is not run or run.stop.is_set():
                return
            peers = self._catalog.snapshot()
        try:
            self.on_change(peers)
        except Exception:
            pass

    def _error(self, run: _Run, message: str) -> None:
        if self._run is run and not run.stop.is_set():
            try:
                self.on_error(message)
            except Exception:
                pass

    def _broadcast(self, run: _Run, data: bytes, closing: bool = False) -> None:
        for address in run.interfaces:
            if run.stop.is_set() and not closing:
                return
            try:
                run.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
                run.sock.sendto(data, (DISCOVERY_GROUP, DISCOVERY_PORT))
            except OSError:
                pass
        # Some access points disable multicast but still pass limited broadcast.
        try:
            run.sock.sendto(data, ("255.255.255.255", DISCOVERY_PORT))
        except OSError:
            pass

    def _work(self, run: _Run) -> None:
        sock = None
        try:
            run.interfaces = _interface_addresses()
            if run.stop.is_set():
                return
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sys.platform == "darwin" and hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            sock.bind(("", DISCOVERY_PORT))
            for address in run.interfaces:
                try:
                    membership = socket.inet_aton(DISCOVERY_GROUP) + socket.inet_aton(address)
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
                except OSError:
                    pass
            sock.settimeout(.2)
            run.sock = sock
            limiter = _RateLimit()
            next_announcement = 0.
            last_refresh = -1.
            run.refresh.set()
            while not run.stop.is_set() and self._run is run:
                now = time.monotonic()
                if run.refresh.is_set() and now - last_refresh >= .35:
                    run.refresh.clear()
                    last_refresh = now
                    self._broadcast(run, self._packet("query"))
                    next_announcement = 0.
                if now >= next_announcement:
                    self._broadcast(run, self._packet("announce"))
                    next_announcement = now + ANNOUNCE_INTERVAL
                with self._lock:
                    if self._run is not run or run.stop.is_set():
                        break
                    changed = self._catalog.expire(now)
                if changed:
                    self._emit(run)
                try:
                    # On Windows a short recv buffer raises WSAEMSGSIZE rather
                    # than returning truncated data. Read the bounded UDP
                    # maximum, then reject oversized presence packets below.
                    data, address = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                if not limiter.allow(address[0], now):
                    continue
                try:
                    kind, device_id, peer = _parse_packet(data, address[0], time.monotonic())
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    continue
                if device_id == self.device_id:
                    continue
                if kind == "query":
                    previous = run.last_replies.get(address[0], -1.)
                    if now - previous >= 1.:
                        if len(run.last_replies) >= 256:
                            run.last_replies = {key: value for key, value in run.last_replies.items() if now-value < 30}
                        if len(run.last_replies) < 256 or address[0] in run.last_replies:
                            run.last_replies[address[0]] = now
                            try:
                                sock.sendto(self._packet("announce"), address)
                            except OSError:
                                pass
                    continue
                with self._lock:
                    if self._run is not run or run.stop.is_set():
                        break
                    changed = (self._catalog.update(peer) if peer is not None
                               else self._catalog.remove(device_id, address[0]))
                if changed:
                    self._emit(run)
        except OSError as exc:
            self._error(run, f"局域网发现暂不可用：{exc}。仍可使用 IP 地址手动连接。")
        finally:
            if sock is not None:
                if run.stop.is_set():
                    try:
                        self._broadcast(run, self._packet("bye"), closing=True)
                    except OSError:
                        pass
                try:
                    sock.close()
                except OSError:
                    pass
            run.sock = None
