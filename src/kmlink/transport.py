"""Bounded, authenticated TLS transport for one KM Link peer.

The certificate is ephemeral. A manually exchanged 256-bit pairing key
authenticates both ends, including the TLS server certificate, before any
application message is delivered. Keys are never written to disk.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import re
import secrets
import select
import socket
import ssl
import struct
import tempfile
import threading
import time
from typing import Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


MAX_MESSAGE_BYTES = 256 * 1024
HEARTBEAT_INTERVAL = 1.0
PEER_TIMEOUT = 6.0
HANDSHAKE_TIMEOUT = 3.0
MAX_SEND_BATCH_FRAMES = 64
MAX_SEND_BATCH_BYTES = 64 * 1024
_INTERNAL = "_kmlink_transport"


class ProtocolError(ValueError):
    """A peer sent an invalid or unauthenticated frame."""



def _complete_handshake(
    sock: ssl.SSLSocket,
    stopped: Callable[[], bool] | threading.Event,
    timeout: float = HANDSHAKE_TIMEOUT,
) -> None:
    """Negotiate TLS without hiding cancellation inside a blocking SSL read.

    Closing a socket from another thread does not reliably interrupt a pending
    OpenSSL handshake on macOS. Poll readiness in short intervals instead, with
    one deadline for the entire handshake, then restore the caller's timeout.
    """
    is_stopped = stopped.is_set if isinstance(stopped, threading.Event) else stopped
    if is_stopped():
        raise ConnectionAbortedError("TLS handshake stopped")
    deadline = time.monotonic() + timeout
    previous_timeout = sock.gettimeout()
    try:
        sock.setblocking(False)
        while True:
            if is_stopped():
                raise ConnectionAbortedError("TLS handshake stopped")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("TLS handshake timed out")
            try:
                sock.do_handshake()
                return
            except ssl.SSLWantReadError:
                readable, writable = [sock], []
            except ssl.SSLWantWriteError:
                readable, writable = [], [sock]
            try:
                select.select(readable, writable, [], min(0.1, remaining))
            except (OSError, ValueError) as exc:
                if is_stopped():
                    raise ConnectionAbortedError("TLS handshake stopped") from exc
                raise
    finally:
        try:
            sock.settimeout(previous_timeout)
        except (OSError, ValueError):
            pass


def generate_pairing_key() -> str:
    """Return a copyable key with 256 bits of entropy."""
    return _b64(secrets.token_bytes(32))


def normalize_pairing_key(value: str) -> str:
    """Ignore whitespace, but retain '-' because it is base64url data."""
    compact = "".join(value.split())
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,86}={0,2}", compact):
        raise ValueError("配对密钥格式无效，请完整复制另一台电脑生成的密钥。")
    try:
        decoded = base64.b64decode(
            compact + "=" * (-len(compact) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("配对密钥格式无效。") from exc
    if not 32 <= len(decoded) <= 64 or _b64(decoded) != compact.rstrip("="):
        raise ValueError("配对密钥必须包含 32 至 64 个随机字节。")
    return _b64(decoded)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: object, size: int) -> bytes:
    if not isinstance(value, str) or len(value) > size * 2:
        raise ProtocolError("Invalid authentication data")
    try:
        result = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, base64.binascii.Error) as exc:
        raise ProtocolError("Invalid authentication data") from exc
    if len(result) != size:
        raise ProtocolError("Invalid authentication data")
    return result


def _encode(message: dict) -> bytes:
    if not isinstance(message, dict):
        raise ProtocolError("Message must be a JSON object")
    try:
        payload = json.dumps(
            message, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Message is not valid JSON") from exc
    if not 1 <= len(payload) <= MAX_MESSAGE_BYTES:
        raise ProtocolError("Message exceeds the 256 KiB limit")
    return struct.pack("!I", len(payload)) + payload


def _recv_exact(sock: socket.socket, length: int, stop: threading.Event) -> bytes:
    chunks = bytearray()
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT
    while len(chunks) < length:
        if stop.is_set():
            raise ConnectionAbortedError("Stopped")
        if time.monotonic() >= deadline:
            raise TimeoutError("Incomplete network frame")
        try:
            chunk = sock.recv(length - len(chunks))
        except socket.timeout:
            continue
        if not chunk:
            raise ConnectionError("Peer disconnected")
        chunks.extend(chunk)
    return bytes(chunks)


def _object_pairs(pairs: list[tuple[str, object]]) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ProtocolError("Duplicate JSON key")
        obj[key] = value
    return obj


def _read_frame(sock: socket.socket, stop: threading.Event) -> dict:
    size = struct.unpack("!I", _recv_exact(sock, 4, stop))[0]
    if not 1 <= size <= MAX_MESSAGE_BYTES:
        raise ProtocolError("Invalid network frame size")
    try:
        value = json.loads(
            _recv_exact(sock, size, stop).decode("utf-8"),
            parse_constant=lambda _: (_ for _ in ()).throw(ProtocolError("Nonfinite number")),
            object_pairs_hook=_object_pairs,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid JSON frame") from exc
    if not isinstance(value, dict):
        raise ProtocolError("Message must be a JSON object")
    return value


def _proof(key: bytes, role: bytes, transcript: bytes) -> str:
    return hmac.new(key, role + b"\0" + transcript, hashlib.sha256).hexdigest()


def _check_proof(supplied: object, expected: str) -> bool:
    return (
        isinstance(supplied, str)
        and re.fullmatch(r"[0-9a-f]{64}", supplied) is not None
        and hmac.compare_digest(supplied, expected)
    )


def _transcript(cert_digest: bytes, client_nonce: bytes, server_nonce: bytes) -> bytes:
    return b"KM Link pairing v1\0" + cert_digest + client_nonce + server_nonce


def _server_auth(sock: ssl.SSLSocket, key: bytes, digest: bytes, stop: threading.Event) -> None:
    hello = _read_frame(sock, stop)
    if hello.get("version") != 1:
        raise ProtocolError("Unsupported protocol version")
    client_nonce = _unb64(hello.get("nonce"), 32)
    server_nonce = secrets.token_bytes(32)
    transcript = _transcript(digest, client_nonce, server_nonce)
    sock.sendall(_encode({"version": 1, "nonce": _b64(server_nonce), "proof": _proof(key, b"server", transcript)}))
    finish = _read_frame(sock, stop)
    supplied = finish.get("proof")
    if not _check_proof(supplied, _proof(key, b"client", transcript)):
        raise ProtocolError("Pairing key does not match")
    sock.sendall(_encode({"version": 1, "ok": True}))


def _client_auth(sock: ssl.SSLSocket, key: bytes, stop: threading.Event) -> None:
    client_nonce = secrets.token_bytes(32)
    sock.sendall(_encode({"version": 1, "nonce": _b64(client_nonce)}))
    response = _read_frame(sock, stop)
    if response.get("version") != 1:
        raise ProtocolError("Unsupported protocol version")
    server_nonce = _unb64(response.get("nonce"), 32)
    digest = hashlib.sha256(sock.getpeercert(binary_form=True)).digest()
    transcript = _transcript(digest, client_nonce, server_nonce)
    supplied = response.get("proof")
    if not _check_proof(supplied, _proof(key, b"server", transcript)):
        raise ProtocolError("Pairing key does not match")
    sock.sendall(_encode({"proof": _proof(key, b"client", transcript)}))
    if _read_frame(sock, stop) != {"version": 1, "ok": True}:
        raise ProtocolError("Pairing was not acknowledged")


def _server_context(directory: str) -> tuple[ssl.SSLContext, bytes]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "KM Link ephemeral peer")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    cert_path, key_path = Path(directory) / "peer.pem", Path(directory) / "peer-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    # The temp directory is private on POSIX. Restrict the key file as well.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as key_file:
        key_file.write(private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    # OpenSSL has loaded both; no reason to retain the private key files.
    cert_path.unlink()
    key_path.unlink()
    return context, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).digest()


@dataclass
class _Run:
    stop: threading.Event = field(default_factory=threading.Event)
    connected: threading.Event = field(default_factory=threading.Event)
    outgoing: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1024))
    lock: threading.RLock = field(default_factory=threading.RLock)
    listener: socket.socket | None = None
    sock: socket.socket | None = None
    wake_read: socket.socket | None = None
    wake_write: socket.socket | None = None
    pending_frame: bytes | None = None
    thread: threading.Thread | None = None
    failure: str = ""


class Transport:
    """One asynchronous authenticated connection; callbacks run on a worker."""

    def __init__(self, on_message: Callable[[dict], None], on_status: Callable[[str, str], None]):
        self.on_message = on_message
        self.on_status = on_status
        self._run: _Run | None = None

    @property
    def is_connected(self) -> bool:
        run = self._run
        return bool(run and run.connected.is_set() and not run.stop.is_set())

    @property
    def listening_address(self) -> tuple | None:
        run = self._run
        if run and run.listener:
            try:
                return run.listener.getsockname()
            except OSError:
                pass
        return None

    def _status(self, run: _Run, state: str, detail: str) -> None:
        if self._run is run and not run.stop.is_set():
            try:
                self.on_status(state, detail)
            except Exception:
                # A UI callback must not leave hooks stuck or kill cleanup.
                pass

    def _start(self, target: Callable, host: str, port: int, pairing_key: str) -> None:
        normalized = normalize_pairing_key(pairing_key)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("端口必须为 1 至 65535。")
        if not isinstance(host, str) or not host.strip():
            raise ValueError("请输入电脑的 IP 地址或主机名。")
        key = base64.urlsafe_b64decode(normalized + "=" * (-len(normalized) % 4))
        self.stop()
        run = _Run()
        # A socketpair wakes the one TLS worker on every new outgoing burst.
        # It avoids polling at 100 Hz (and delaying a just-arrived mouse event
        # by another 10 ms), while still letting the same worker own all SSL IO.
        run.wake_read, run.wake_write = socket.socketpair()
        run.wake_read.setblocking(False)
        run.wake_write.setblocking(False)
        self._run = run
        run.thread = threading.Thread(
            target=target, args=(run, host.strip(), port, key),
            name="kmlink-transport", daemon=True,
        )
        try:
            run.thread.start()
        except Exception:
            self._run = None
            self._close_wakeup(run)
            raise

    def listen(self, host: str, port: int, pairing_key: str) -> None:
        self._start(self._listen, host, port, pairing_key)

    def connect(self, host: str, port: int, pairing_key: str) -> None:
        if port == 0:
            raise ValueError("连接端口必须为 1 至 65535。")
        self._start(self._connect, host, port, pairing_key)

    def send(self, message: dict) -> bool:
        run = self._run
        if not run or not run.connected.is_set() or run.stop.is_set():
            return False
        if not isinstance(message, dict) or _INTERNAL in message:
            return False
        try:
            encoded = _encode(message)
        except ProtocolError:
            return False
        try:
            run.outgoing.put_nowait(encoded)
            self._wake(run)
            return True
        except queue.Full:
            run.failure = "发送队列已满，连接已安全断开。"
            self._close_socket(run.sock)
            self._wake(run)
            return False

    @staticmethod
    def _wake(run: _Run) -> None:
        writer = run.wake_write
        if writer is not None:
            try:
                writer.send(b"\0")
            except (BlockingIOError, OSError):
                # A full pipe is already readable; a closed pipe belongs to a
                # session that is stopping. Neither may block an input hook.
                pass

    @staticmethod
    def _close_wakeup(run: _Run) -> None:
        with run.lock:
            sockets = run.wake_read, run.wake_write
            run.wake_read = run.wake_write = None
        for sock in sockets:
            if sock is not None:
                sock.close()

    @staticmethod
    def _outgoing_batch(run: _Run) -> bytes:
        """Batch ready frames without changing protocol framing or event order."""
        frames = []
        total = 0
        for _ in range(MAX_SEND_BATCH_FRAMES):
            frame = run.pending_frame
            run.pending_frame = None
            if frame is None:
                try:
                    frame = run.outgoing.get_nowait()
                except queue.Empty:
                    break
            if frames and total + len(frame) > MAX_SEND_BATCH_BYTES:
                run.pending_frame = frame
                break
            frames.append(frame)
            total += len(frame)
            # A legal single large frame must still be allowed through, but
            # cannot drag more frames into an unbounded write behind it.
            if total >= MAX_SEND_BATCH_BYTES:
                break
        return b"".join(frames)

    @staticmethod
    def _wait_for_input(run: _Run, sock: ssl.SSLSocket, timeout: float) -> bool:
        if sock.pending() > 0:
            return True
        reader = run.wake_read
        readable = select.select([sock, reader] if reader is not None else [sock], [], [], timeout)[0]
        if reader is not None and reader in readable:
            for _ in range(16):
                try:
                    if not reader.recv(4096):
                        break
                except BlockingIOError:
                    break
        return sock in readable

    @staticmethod
    def _close_socket(sock: socket.socket | None) -> None:
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def stop(self) -> None:
        run = self._run
        if not run:
            return
        self._run = None
        run.stop.set()
        run.connected.clear()
        self._wake(run)
        self._close_socket(run.listener)
        self._close_socket(run.sock)
        if run.thread and run.thread is not threading.current_thread():
            run.thread.join(timeout=0.8)
        self._close_wakeup(run)

    def _install_socket(self, run: _Run, sock: socket.socket) -> None:
        with run.lock:
            if run.stop.is_set():
                sock.close()
                raise ConnectionAbortedError("Stopped")
            run.sock = sock
            # Mouse/key frames are tiny. Avoid Nagle waiting for delayed ACKs
            # between successive TLS records on either side of the connection.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _listen(self, run: _Run, host: str, port: int, key: bytes) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="kmlink-tls-") as directory:
                context, digest = _server_context(directory)
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                run.listener = listener
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((host, port))
                listener.listen(4)
                listener.settimeout(0.25)
                self._status(run, "listening", f"正在等待连接，端口 {listener.getsockname()[1]}。")
                while not run.stop.is_set():
                    try:
                        raw, _address = listener.accept()
                    except socket.timeout:
                        continue
                    self._install_socket(run, raw)
                    raw.settimeout(HANDSHAKE_TIMEOUT)
                    try:
                        secure = context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
                        self._install_socket(run, secure)
                        _complete_handshake(secure, run.stop, HANDSHAKE_TIMEOUT)
                        secure.settimeout(0.25)
                        _server_auth(secure, key, digest, run.stop)
                    except (OSError, ValueError) as exc:
                        self._close_socket(run.sock)
                        run.sock = None
                        self._status(run, "listening", "配对未通过，继续等待正确的密钥。")
                        continue
                    self._session(run, secure)
                    break
        except (OSError, ValueError) as exc:
            self._status(run, "error", self._error_text(exc))
        finally:
            run.connected.clear()
            self._close_socket(run.sock)
            self._close_socket(run.listener)
            self._close_wakeup(run)

    def _connect(self, run: _Run, host: str, port: int, key: bytes) -> None:
        self._status(run, "connecting", f"正在连接 {host}:{port}…")
        try:
            raw = socket.create_connection((host, port), timeout=HANDSHAKE_TIMEOUT)
            self._install_socket(run, raw)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            # The PSK proof authenticates this exact certificate before data
            # is accepted, so a public CA / persistent certificate is not needed.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            secure = context.wrap_socket(raw, server_hostname=None, do_handshake_on_connect=False)
            self._install_socket(run, secure)
            _complete_handshake(secure, run.stop, HANDSHAKE_TIMEOUT)
            secure.settimeout(0.25)
            _client_auth(secure, key, run.stop)
            self._session(run, secure)
        except (OSError, ValueError) as exc:
            self._status(run, "error", self._error_text(exc))
        finally:
            run.connected.clear()
            self._close_socket(run.sock)
            self._close_wakeup(run)

    @staticmethod
    def _error_text(exc: Exception) -> str:
        if isinstance(exc, ProtocolError):
            return "配对或通信校验失败，请检查两端密钥并重新连接。"
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return "连接超时，请检查地址、防火墙及另一台电脑。"
        if isinstance(exc, ConnectionRefusedError):
            return "另一台电脑拒绝连接，请先点击「等待连接」。"
        return f"连接失败：{exc}"

    def _session(self, run: _Run, sock: ssl.SSLSocket) -> None:
        run.connected.set()
        self._status(run, "connected", "已建立加密连接。")
        last_received = time.monotonic()
        last_heartbeat = 0.0
        reason = "连接已断开。"
        try:
            while not run.stop.is_set():
                if run.failure:
                    raise ConnectionError(run.failure)
                # One ready batch becomes one TLS write, not one TLS record
                # and syscall for each high-frequency mouse event. The bounded
                # batch keeps reads, heartbeats and emergency input responsive.
                batch = self._outgoing_batch(run)
                if batch:
                    sock.sendall(batch)
                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    sock.sendall(_encode({_INTERNAL: "heartbeat"}))
                    last_heartbeat = now
                if now - last_received >= PEER_TIMEOUT:
                    raise TimeoutError("对端无响应，已收回控制。")
                timeout = max(0., min(HEARTBEAT_INTERVAL - (now - last_heartbeat),
                                     PEER_TIMEOUT - (now - last_received)))
                if run.pending_frame is not None or not run.outgoing.empty():
                    timeout = 0.
                ready = self._wait_for_input(run, sock, timeout)
                if not ready:
                    continue
                message = _read_frame(sock, run.stop)
                last_received = time.monotonic()
                if _INTERNAL in message:
                    if message != {_INTERNAL: "heartbeat"}:
                        raise ProtocolError("Invalid transport message")
                    continue
                if self._run is run and not run.stop.is_set():
                    self.on_message(message)
        except (OSError, ValueError) as exc:
            reason = run.failure or self._error_text(exc)
        except Exception:
            reason = "处理对端消息失败，连接已安全断开。"
        finally:
            run.connected.clear()
            self._close_socket(sock)
            self._status(run, "disconnected", reason)
