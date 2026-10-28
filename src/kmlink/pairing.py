"""Consent-based LAN invitations, protected by TLS and a comparison code.

Discovery metadata is not trusted identity. Users compare the six digits on
both devices before the recipient approves. Nonce commitments prevent either
side choosing its nonce after learning the other side's comparison material.
No keyboard-sharing key is generated or sent before approval.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
import select
import socket
import ssl
import struct
import tempfile
import threading
import time
import uuid
from typing import Callable

from .transport import _complete_handshake, _server_context, generate_pairing_key, normalize_pairing_key


PROTOCOL = "kmlink-invite/1"
MAX_FRAME = 8192
HANDSHAKE_TIMEOUT = 3.0
APPROVAL_TIMEOUT = 45.0
READY_TIMEOUT = 12.0


class InvitationError(ValueError):
    """A bounded invitation protocol failure, never including key material."""


class _Rejected(Exception):
    pass


def _text(value: object, field_name: str, maximum: int = 80) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= maximum
            or value != value.strip() or not value.isprintable()):
        raise InvitationError(f"无效的{field_name}")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
        raise InvitationError("无效的设备标识")
    return value


def _port(value: object, allow_zero: bool = False) -> int:
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= 65535:
        raise InvitationError("无效的连接端口")
    return value


def _identity(value: object) -> dict:
    if not isinstance(value, dict):
        raise InvitationError("缺少设备信息")
    return {"id": _identifier(value.get("id")),
            "name": _text(value.get("name"), "设备名称"),
            "system": _text(value.get("system"), "系统信息")}


def _nonce(value: object) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        raise InvitationError("无效的配对随机数")
    decoded = base64.urlsafe_b64decode(value + "=")
    if len(decoded) != 32 or _b64(decoded) != value:
        raise InvitationError("无效的配对随机数")
    return decoded


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _commit(value: bytes) -> str:
    return hashlib.sha256(b"KMLink invite nonce\0" + value).hexdigest()


def _commitment(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InvitationError("无效的配对承诺")
    return value


def comparison_code(digest: bytes, request: dict, challenge: dict,
                    client_nonce: bytes, server_nonce: bytes) -> str:
    transcript = json.dumps([request, challenge], sort_keys=True, ensure_ascii=False,
                            separators=(",", ":")).encode("utf-8")
    result = hashlib.sha256(b"KMLink numeric comparison v1\0" + digest
                            + client_nonce + server_nonce + transcript).digest()
    return f"{int.from_bytes(result[:8], 'big') % 1_000_000:06d}"


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise InvitationError("重复的协议字段")
        result[key] = value
    return result


def _frame(message: dict) -> bytes:
    try:
        data = json.dumps(message, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise InvitationError("无效的协议消息") from exc
    if not 1 <= len(data) <= MAX_FRAME:
        raise InvitationError("邀请消息过大")
    return struct.pack("!I", len(data)) + data


def _recv_exact(sock, length: int, stopped: Callable[[], bool], deadline: float) -> bytes:
    data = bytearray()
    while len(data) < length:
        if stopped():
            raise _Rejected("已取消请求")
        if time.monotonic() >= deadline:
            raise TimeoutError("邀请通信超时")
        try:
            part = sock.recv(length - len(data))
        except socket.timeout:
            continue
        if not part:
            raise _Rejected("对方已取消请求或关闭应用")
        data.extend(part)
    return bytes(data)


def _read(sock, stopped: Callable[[], bool]) -> dict:
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT
    length = struct.unpack("!I", _recv_exact(sock, 4, stopped, deadline))[0]
    if not 1 <= length <= MAX_FRAME:
        raise InvitationError("邀请消息大小无效")
    try:
        value = json.loads(_recv_exact(sock, length, stopped, deadline).decode("utf-8"),
                           object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(InvitationError("无效数值")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvitationError("无效的邀请消息") from exc
    if not isinstance(value, dict):
        raise InvitationError("邀请消息必须是对象")
    return value


def _close(sock) -> None:
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


@dataclass
class _Session:
    request_id: str
    mode: str
    outgoing: bool
    peer_ip: str
    peer: dict = field(default_factory=dict)
    sock: ssl.SSLSocket | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    decided: threading.Event = field(default_factory=threading.Event)
    ready: threading.Event = field(default_factory=threading.Event)
    approved: bool = False
    receiver_port: int = 0
    phase: str = "handshake"

    @property
    def local_role(self) -> str:
        requester_controls = self.mode == "control"
        return "controller" if requester_controls == self.outgoing else "receiver"

    def details(self) -> dict:
        return {"request_id": self.request_id, "mode": self.mode,
                "local_role": self.local_role, "peer_id": self.peer.get("id", ""),
                "peer_name": self.peer.get("name", self.peer_ip),
                "peer_system": self.peer.get("system", ""), "peer_ip": self.peer_ip}


@dataclass
class _Run:
    stop: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    pending: _Session | None = None
    listener: socket.socket | None = None
    thread: threading.Thread | None = None
    workers: set = field(default_factory=set)
    sockets: set = field(default_factory=set)
    slots: threading.BoundedSemaphore = field(default_factory=lambda: threading.BoundedSemaphore(4))
    port: int = 0
    available: bool = True


class PairingService:
    """One consent dialog at a time; callbacks run on background threads."""

    def __init__(self, device_id: str, name: str, system: str,
                 on_event: Callable[[str, dict], None]):
        self.identity = _identity({"id": device_id, "name": name, "system": system})
        self.on_event = on_event
        self._run: _Run | None = None

    @property
    def port(self) -> int:
        return self._run.port if self._run else 0

    def _emit(self, run: _Run, event: str, data: dict) -> None:
        if self._run is run and not run.stop.is_set():
            try:
                self.on_event(event, data)
            except Exception:
                # A failed consent UI must never become implicit approval.
                if run.pending:
                    run.pending.cancelled.set()

    def start(self, host: str = "0.0.0.0", port: int = 24882) -> None:
        _port(port, allow_zero=True)
        _text(host, "监听地址", 253)
        self.stop()
        run = _Run()
        self._run = run
        run.thread = threading.Thread(target=self._listen, args=(run, host, port),
                                      name="kmlink-invitations", daemon=True)
        run.thread.start()

    def set_available(self, available: bool) -> None:
        run = self._run
        if run:
            with run.lock:
                run.available = bool(available)

    def request(self, peer_id: str, host: str, port: int, mode: str) -> str:
        _identifier(peer_id)
        _text(host, "对端地址", 253)
        _port(port)
        if not isinstance(mode, str) or mode not in {"control", "be_controlled"}:
            raise ValueError("无效的控制方向")
        if peer_id == self.identity["id"]:
            raise ValueError("不能连接本机")
        run = self._run
        if run is None or run.stop.is_set() or run.port == 0:
            raise RuntimeError("设备发现服务尚未就绪")
        session = _Session(uuid.uuid4().hex, mode, True, host,
                           peer={"id": peer_id, "name": host, "system": ""})
        with run.lock:
            if run.pending is not None or not run.available:
                raise RuntimeError("已有连接或请求，请先停止共享")
            run.pending = session
        worker = threading.Thread(target=self._client, args=(run, session, host, port),
                                  name="kmlink-invite-outgoing", daemon=True)
        with run.lock:
            run.workers.add(worker)
        worker.start()
        return session.request_id

    def _pending(self, request_id: str) -> tuple[_Run | None, _Session | None]:
        run = self._run
        if run:
            with run.lock:
                item = run.pending
                if item and item.request_id == request_id:
                    return run, item
        return run, None

    def approve(self, request_id: str) -> None:
        run, session = self._pending(request_id)
        if session and not session.outgoing and session.phase == "approval":
            with run.lock:
                session.approved = True
                session.decided.set()

    def reject(self, request_id: str) -> None:
        run, session = self._pending(request_id)
        if session and not session.outgoing and session.phase == "approval":
            with run.lock:
                session.approved = False
                session.decided.set()

    def cancel(self, request_id: str) -> None:
        _, session = self._pending(request_id)
        if session:
            session.cancelled.set()
            _close(session.sock)

    def receiver_ready(self, request_id: str, port: int) -> None:
        _port(port)
        run, session = self._pending(request_id)
        if session and session.local_role == "receiver" and session.phase == "preparing":
            with run.lock:
                session.receiver_port = port
                session.ready.set()

    def stop(self) -> None:
        run, self._run = self._run, None
        if not run:
            return
        run.stop.set()
        with run.lock:
            if run.pending:
                run.pending.cancelled.set()
            sockets = list(run.sockets)
            workers = list(run.workers)
        _close(run.listener)
        for sock in sockets:
            _close(sock)
        if run.thread and run.thread is not threading.current_thread():
            run.thread.join(timeout=.6)
        deadline = time.monotonic() + .8
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=max(0, deadline - time.monotonic()))

    def _track(self, run: _Run, sock) -> None:
        with run.lock:
            if run.stop.is_set():
                _close(sock)
                raise _Rejected("已停止邀请服务")
            run.sockets.add(sock)

    def _finish(self, run: _Run, session: _Session | None, sock=None) -> None:
        _close(sock)
        with run.lock:
            run.sockets.discard(sock)
            if run.pending is session:
                run.pending = None
            run.workers.discard(threading.current_thread())

    def _listen(self, run: _Run, host: str, port: int) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="kmlink-invite-") as directory:
                context, digest = _server_context(directory)
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            run.listener = listener
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen(4)
            listener.settimeout(.2)
            run.port = listener.getsockname()[1]
            self._emit(run, "listening", {"port": run.port})
            while not run.stop.is_set():
                try:
                    raw, address = listener.accept()
                except socket.timeout:
                    continue
                if not run.slots.acquire(blocking=False):
                    _close(raw)
                    continue
                self._track(run, raw)
                worker = threading.Thread(target=self._server,
                                          args=(run, raw, address[0], context, digest),
                                          name="kmlink-invite-incoming", daemon=True)
                with run.lock:
                    run.workers.add(worker)
                worker.start()
        except (OSError, ValueError):
            self._emit(run, "error", {"request_id": "", "detail": "无法启动邀请服务，请检查端口或防火墙。"})
        finally:
            run.port = 0
            _close(run.listener)

    def _wait_remote(self, run: _Run, session: _Session, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if run.stop.is_set() or session.cancelled.is_set():
                raise _Rejected("已取消请求")
            if session.sock.pending() or select.select([session.sock], [], [], .1)[0]:
                return _read(session.sock, lambda: run.stop.is_set() or session.cancelled.is_set())
        raise TimeoutError("请求已超时，请重新申请")

    def _wait_local(self, run: _Run, session: _Session, event: threading.Event, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if run.stop.is_set() or session.cancelled.is_set():
                raise _Rejected("已取消请求")
            if event.is_set():
                return
            if session.sock.pending() or select.select([session.sock], [], [], .05)[0]:
                message = _read(session.sock, lambda: run.stop.is_set() or session.cancelled.is_set())
                if message.get("type") == "cancel":
                    raise _Rejected("对方已取消请求")
                raise InvitationError("在确认前收到了意外消息")
        raise TimeoutError("请求已超时，请重新申请")

    def _coordinate(self, run: _Run, session: _Session, key: str) -> None:
        session.phase = "preparing"
        details = session.details()
        if session.local_role == "receiver":
            self._emit(run, "prepare_receiver", {**details, "key": key})
            self._wait_local(run, session, session.ready, READY_TIMEOUT)
            session.sock.sendall(_frame({"type": "ready", "request_id": session.request_id,
                                         "port": session.receiver_port}))
            reply = self._wait_remote(run, session, READY_TIMEOUT)
            if reply != {"type": "handoff", "request_id": session.request_id}:
                raise InvitationError("无效的连接交接")
        else:
            ready = self._wait_remote(run, session, READY_TIMEOUT)
            if ready.get("type") != "ready" or ready.get("request_id") != session.request_id:
                raise InvitationError("对端未准备好接收控制")
            port = _port(ready.get("port"))
            self._emit(run, "connect_controller", {**details, "key": key,
                                                    "host": session.peer_ip, "port": port})
            if run.stop.is_set() or session.cancelled.is_set():
                raise _Rejected("已取消请求")
            session.sock.sendall(_frame({"type": "handoff", "request_id": session.request_id}))
        session.phase = "finished"
        self._emit(run, "finished", {**details, "detail": "请求已获同意，正在建立键鼠连接。"})

    def _failed(self, run: _Run, session: _Session | None, error: Exception) -> None:
        if session is None:
            return  # Unsolicited malformed traffic must not raise UI dialogs.
        if isinstance(error, _Rejected):
            event, detail = "rejected", str(error)
        elif isinstance(error, TimeoutError):
            event, detail = "rejected", "请求已超时，请重新申请。"
        elif session.cancelled.is_set():
            event, detail = "rejected", "已取消请求。"
        else:
            event, detail = "error", "邀请连接失败，请刷新设备后重试；配对数字必须两端一致。"
        self._emit(run, event, {**session.details(), "detail": detail})

    def _server(self, run: _Run, raw, peer_ip: str, context, digest: bytes) -> None:
        session, secure = None, None
        try:
            raw.settimeout(HANDSHAKE_TIMEOUT)
            secure = context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
            with run.lock:
                run.sockets.discard(raw)
            self._track(run, secure)
            _complete_handshake(secure, run.stop, HANDSHAKE_TIMEOUT)
            secure.settimeout(.2)
            request = _read(secure, run.stop.is_set)
            if request.get("type") != "request" or request.get("protocol") != PROTOCOL:
                raise InvitationError("协议不匹配")
            request_id = request.get("request_id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
                raise InvitationError("无效的请求标识")
            if request.get("target_id") != self.identity["id"]:
                raise InvitationError("设备标识不匹配")
            peer = _identity(request.get("device"))
            mode = request.get("mode")
            if (not isinstance(mode, str) or mode not in {"control", "be_controlled"}
                    or peer["id"] == self.identity["id"]):
                raise InvitationError("无效的控制方向")
            client_commit = _commitment(request.get("nonce_commit"))
            with run.lock:
                if run.pending is not None or not run.available:
                    secure.sendall(_frame({"type": "refused", "reason": "busy"}))
                    return
                session = _Session(request_id, mode, False, peer_ip, peer=peer, sock=secure)
                run.pending = session
            server_nonce = secrets.token_bytes(32)
            challenge = {"type": "challenge", "protocol": PROTOCOL,
                         "request_id": request_id, "device": self.identity,
                         "nonce_commit": _commit(server_nonce)}
            secure.sendall(_frame(challenge))
            reveal = _read(secure, lambda: run.stop.is_set() or session.cancelled.is_set())
            if reveal.get("type") != "reveal":
                raise InvitationError("缺少配对随机数")
            client_nonce = _nonce(reveal.get("nonce"))
            if not secrets.compare_digest(_commit(client_nonce), client_commit):
                raise InvitationError("配对承诺校验失败")
            secure.sendall(_frame({"type": "reveal", "nonce": _b64(server_nonce)}))
            code = comparison_code(digest, request, challenge, client_nonce, server_nonce)
            session.phase = "approval"
            self._emit(run, "incoming", {**session.details(), "code": code})
            self._wait_local(run, session, session.decided, APPROVAL_TIMEOUT)
            if not session.approved:
                secure.sendall(_frame({"type": "refused", "reason": "declined"}))
                raise _Rejected("已拒绝请求。")
            key = generate_pairing_key()
            secure.sendall(_frame({"type": "accepted", "request_id": request_id, "key": key}))
            self._coordinate(run, session, key)
        except (OSError, ValueError, _Rejected) as exc:
            self._failed(run, session, exc)
        finally:
            self._finish(run, session, secure or raw)
            run.slots.release()

    def _client(self, run: _Run, session: _Session, host: str, port: int) -> None:
        raw, secure = None, None
        try:
            raw = socket.create_connection((host, port), timeout=HANDSHAKE_TIMEOUT)
            self._track(run, raw)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            secure = context.wrap_socket(raw, do_handshake_on_connect=False)
            with run.lock:
                run.sockets.discard(raw)
            self._track(run, secure)
            session.sock = secure
            _complete_handshake(secure, lambda: run.stop.is_set() or session.cancelled.is_set(), HANDSHAKE_TIMEOUT)
            secure.settimeout(.2)
            session.peer_ip = secure.getpeername()[0]
            digest = hashlib.sha256(secure.getpeercert(binary_form=True)).digest()
            client_nonce = secrets.token_bytes(32)
            request = {"type": "request", "protocol": PROTOCOL,
                       "request_id": session.request_id, "target_id": session.peer["id"],
                       "device": self.identity, "mode": session.mode,
                       "nonce_commit": _commit(client_nonce)}
            secure.sendall(_frame(request))
            challenge = _read(secure, lambda: run.stop.is_set() or session.cancelled.is_set())
            if challenge.get("type") == "refused":
                raise _Rejected("对方正忙，请稍后重试。")
            if (challenge.get("type") != "challenge" or challenge.get("protocol") != PROTOCOL
                    or challenge.get("request_id") != session.request_id):
                raise InvitationError("无效的邀请响应")
            peer = _identity(challenge.get("device"))
            if peer["id"] != session.peer["id"]:
                raise InvitationError("对端身份与发现结果不符")
            session.peer = peer
            server_commit = _commitment(challenge.get("nonce_commit"))
            secure.sendall(_frame({"type": "reveal", "nonce": _b64(client_nonce)}))
            reveal = _read(secure, lambda: run.stop.is_set() or session.cancelled.is_set())
            if reveal.get("type") != "reveal":
                raise InvitationError("缺少对端随机数")
            server_nonce = _nonce(reveal.get("nonce"))
            if not secrets.compare_digest(_commit(server_nonce), server_commit):
                raise InvitationError("对端配对承诺不匹配")
            code = comparison_code(digest, request, challenge, client_nonce, server_nonce)
            session.phase = "approval"
            self._emit(run, "outgoing", {**session.details(), "code": code})
            response = self._wait_remote(run, session, APPROVAL_TIMEOUT)
            if response.get("type") == "refused":
                raise _Rejected("对方拒绝了请求。")
            if response.get("type") != "accepted" or response.get("request_id") != session.request_id:
                raise InvitationError("无效的确认响应")
            supplied_key = response.get("key")
            if not isinstance(supplied_key, str):
                raise InvitationError("无效的会话密钥")
            key = normalize_pairing_key(supplied_key)
            self._coordinate(run, session, key)
        except (OSError, ValueError, _Rejected) as exc:
            self._failed(run, session, exc)
        finally:
            self._finish(run, session, secure or raw)
