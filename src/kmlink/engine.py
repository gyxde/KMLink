"""Toolkit-independent session state and strictly validated input protocol."""
from __future__ import annotations

import math
import platform
import queue
import socket
import threading
import time
import uuid
from typing import Callable

from .transport import Transport
from .geometry import Bounds, DesktopLayout


SIDES = {"left", "right", "top", "bottom"}
OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
MAX_CLIPBOARD_BYTES = 64 * 1024
MAX_TEXT_BYTES = 4096
POINTER_READY_CAPABILITY = "pointer_ready_v1"


def number(value: object, limit: float = 4096) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
        raise ValueError("输入数值超出范围")
    return value


class Engine:
    def __init__(self, native, bounds: Bounds, changed: Callable[[str, str], None],
                 clipboard_write: Callable[[str], None], key_validator: Callable[[str], bool],
                 transport_factory=Transport, layout=None):
        self.native = native
        self.layout = layout or DesktopLayout([bounds])
        self.bounds = self.layout.bounds
        self.changed = changed
        self.clipboard_write = clipboard_write
        self.key_validator = key_validator
        self.transport_factory = transport_factory
        self.transport = None
        self.role = "receiver"
        self.side = "right"
        self.remote_bounds = None
        self.peer_name = ""
        self.connected = False
        self.remote_active = False
        self.receiving = False
        self.share_clipboard = False
        self.map_ctrl_cmd = False
        self._peer_platform = ""
        self._events = queue.Queue(maxsize=8192)
        self._overflow = threading.Event()
        self._generation = 0
        self._cooldown = 0.
        self._last_clipboard = None
        self._native_started = False
        self._mapped_keys = {}
        self._scroll_remainder = [0., 0.]
        self._pointer_position = None
        self._pending_position = None
        self._posted_position = None
        self._peer_pointer_ready = False
        self._entry_pending = None
        self._receive_entry = None
        self._pointer_check_at = 0.
        self._actual_pointer = None
        self._actual_motion_at = 0.
        self._last_remote_motion_at = 0.

    def enqueue(self, kind: str, value=None, generation=None):
        try:
            self._events.put_nowait((self._generation if generation is None else generation, kind, value))
        except queue.Full:
            self._overflow.set()

    def start(self, role: str, host: str, port: int, key: str, side: str):
        self.stop(announce=False)
        self.role, self.side = role, side
        generation = self._generation
        self.transport = self.transport_factory(
            lambda msg: self.enqueue("message", msg, generation),
            lambda state, detail: self.enqueue("status", (state, detail), generation))
        try:
            if role == "receiver":
                self.transport.listen(host, port, key)
            else:
                self.transport.connect(host, port, key)
        except Exception:
            self.stop(announce=False)
            raise

    def stop(self, announce=True):
        last_position = self._posted_position
        self._generation += 1
        self.connected = False
        self.remote_active = False
        self.receiving = False
        self.remote_bounds = None
        self._mapped_keys.clear()
        self._scroll_remainder = [0., 0.]
        self._pointer_position = self._pending_position = self._posted_position = None
        self._peer_pointer_ready = False
        self._entry_pending = self._receive_entry = None
        self._actual_pointer = None
        try:
            self.native.set_capturing(False)
            self.native.release_all(last_position)
        finally:
            if self._native_started:
                self.native.stop()
                self._native_started = False
            transport, self.transport = self.transport, None
            if transport:
                transport.stop()
        self._last_clipboard = None
        self._cooldown = time.monotonic() + .7
        if announce:
            self.changed("stopped", "已停止共享，本机键鼠已恢复")

    def poll(self):
        if self._overflow.is_set():
            self._overflow.clear()
            self.stop()
            self.changed("error", "输入处理繁忙，已安全断开，请重新连接")
        for _ in range(2048):
            try:
                generation, kind, value = self._events.get_nowait()
            except queue.Empty:
                break
            if generation != self._generation:
                continue
            try:
                if kind == "status":
                    self._status(*value)
                elif kind == "message":
                    self._message(value)
                elif kind == "input":
                    self._input(value)
                elif kind == "toggle":
                    if self.remote_active or self._entry_pending:
                        self.return_local()
                    elif self.role == "controller" and self.remote_bounds:
                        self.enter_remote(*self.native.position())
                elif kind == "emergency":
                    if self.role == "controller":
                        self.return_local()
                    else:
                        self.stop()
            except Exception as exc:
                self.stop()
                self.changed("error", str(exc)[:240])
        try:
            self._flush_motion()
            self._check_pointer_health()
        except Exception as exc:
            self.stop()
            self.changed("error", str(exc)[:240])

    @staticmethod
    def _near(first, second):
        return max(abs(first[i] - second[i]) for i in (0, 1)) <= 3

    def _check_pointer_health(self):
        now = time.monotonic()
        if self._entry_pending and now >= self._entry_pending["deadline"]:
            self.return_local()
            self.changed("notice", "对端鼠标未就绪，已保留本机控制，请重新尝试。")
        if not self.receiving or now < self._pointer_check_at:
            return
        self._pointer_check_at = now + .04
        actual = self.native.position()
        entry = self._receive_entry
        if entry:
            ready = self._near(actual, self._posted_position)
            # Older peers cannot wait for an ACK. Any observed motion after
            # their first move also proves the mouse path works.
            if not entry["id"] and self._last_remote_motion_at > entry["started"]:
                ready = ready or actual != entry["origin"]
            if ready:
                self._receive_entry = None
                self._actual_pointer = actual
                self._actual_motion_at = now
                if entry["id"]:
                    self._send({"type": "entered", "entry_id": entry["id"]})
            elif now >= entry["deadline"]:
                self._release_remote_pointer("鼠标未能进入目标桌面，已返回控制端。")
            elif now >= entry["retry"]:
                # SendInput accepting the event is not proof that Windows
                # applied it. Re-submit a bounded number of initial positions.
                self.native.move_to(*self._posted_position)
                entry["retry"] = now + .25
            return
        if actual != self._actual_pointer:
            self._actual_pointer = actual
            self._actual_motion_at = now
        elif (self._last_remote_motion_at > self._actual_motion_at
              and now - self._actual_motion_at >= .8
              and not self._near(actual, self._posted_position)):
            self._release_remote_pointer("目标桌面的鼠标移动被限制，已返回控制端。")

    def _release_remote_pointer(self, reason):
        position = self._posted_position
        self.receiving = False
        self._receive_entry = None
        self._pending_position = None
        self.native.release_all(position)
        self._pointer_position = self._posted_position = None
        self._mapped_keys.clear()
        rx, ry = self.bounds.ratio(*position)
        self._send({"type": "return", "rx": rx, "ry": ry, "reason": reason})
        self.changed("notice", reason)

    def _flush_motion(self):
        position, self._pending_position = self._pending_position, None
        if position is not None and position != self._posted_position:
            self.native.move_to(*position)
            self._posted_position = position

    def _send(self, message):
        if not self.transport or not self.transport.send(message):
            raise ConnectionError("连接已中断，已恢复本机控制")

    def _begin_capture(self):
        if getattr(self.native, "needs_restart", False):
            # A Windows low-level hook may have been silently removed. Rebuild
            # both hooks on the next explicit switch after returning locally.
            self.native.stop()
            self.native.start()
            self._native_started = True
        self.native.set_capturing(True)

    def _status(self, state, detail):
        if state == "connected":
            self.connected = True
            try:
                self.native.start()
                self._native_started = True
            except Exception:
                # start() may have installed only one of the two listeners.
                self.native.stop()
                raise
            self._send({"type": "hello", "version": 1, "role": self.role,
                        "name": socket.gethostname()[:80], "platform": platform.system(),
                        "bounds": self.bounds.wire(), "screens": [screen.wire() for screen in self.layout.screens],
                        "capabilities": ["text_input", POINTER_READY_CAPABILITY]})
            self.changed("handshake", "加密连接已建立，正在确认对端设备")
        elif state in {"error", "disconnected"}:
            self.stop(announce=False)
            self.changed(state, detail)
        else:
            self.changed(state, detail)

    def _message(self, msg):
        kind = msg.get("type")
        if kind != "move":
            # Keep clicks, keys and session boundaries after all preceding motion.
            self._flush_motion()
        if kind == "hello":
            if self.remote_bounds is not None or type(msg.get("version")) is not int or msg.get("version") != 1:
                raise ValueError("对端协议版本不匹配")
            expected_role = "receiver" if self.role == "controller" else "controller"
            if msg.get("role") != expected_role:
                raise ValueError("两端角色相同，请设置一端接收、另一端连接")
            self.remote_bounds = Bounds.parse(msg.get("bounds"))
            self.peer_name = str(msg.get("name", "另一台电脑"))[:80]
            self._peer_platform = str(msg.get("platform", ""))[:32]
            capabilities = msg.get("capabilities", [])
            if (not isinstance(capabilities, list) or len(capabilities) > 32
                    or any(not isinstance(item, str) or len(item) > 64 for item in capabilities)):
                raise ValueError("对端功能声明无效")
            self._peer_pointer_ready = POINTER_READY_CAPABILITY in capabilities
            self.changed("ready", f"已连接 {self.peer_name} · F8 切换控制")
            return
        if self.remote_bounds is None:
            raise ValueError("对端尚未完成设备确认")
        if kind == "clipboard":
            text = msg.get("text")
            if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
                raise ValueError("剪贴板内容超出文本限制")
            if self.share_clipboard:
                self._last_clipboard = text
                self.clipboard_write(text)
            return
        if self.role == "controller":
            if kind == "entered" and self._peer_pointer_ready:
                entry_id = msg.get("entry_id")
                if not isinstance(entry_id, str) or len(entry_id) != 32:
                    raise ValueError("无效的鼠标就绪确认")
                if self._entry_pending and entry_id == self._entry_pending["id"]:
                    try:
                        self._begin_capture()
                    except RuntimeError as exc:
                        self.return_local()
                        self.changed("notice", str(exc))
                        return
                    self._entry_pending = None
                    self.remote_active = True
                    self.changed("controlling", f"正在控制 {self.peer_name} · F8 或 Ctrl+Alt+Esc 返回")
                return  # Ignore a late ACK after cancellation or a retry.
            if kind != "return":
                raise ValueError("对端发送了无效的控制消息")
            rx, ry = number(msg.get("rx"), 1), number(msg.get("ry"), 1)
            if rx < 0 or ry < 0:
                raise ValueError("无效的返回坐标")
            if self.remote_active or self._entry_pending:
                self.return_local(rx, ry)
                if isinstance(msg.get("reason"), str):
                    self.changed("notice", msg["reason"][:160])
            return
        if kind == "enter":
            side = msg.get("side")
            if side not in SIDES:
                raise ValueError("无效的屏幕排列")
            rx, ry = number(msg.get("rx"), 1), number(msg.get("ry"), 1)
            if rx < 0 or ry < 0:
                raise ValueError("无效的进入坐标")
            self.native.release_all(self._posted_position)
            entry_id = msg.get("entry_id") if self._peer_pointer_ready else None
            if self._peer_pointer_ready and (not isinstance(entry_id, str)
                    or len(entry_id) != 32 or any(c not in "0123456789abcdef" for c in entry_id)):
                raise ValueError("无效的鼠标进入请求")
            now = time.monotonic()
            origin = self.native.position()
            self.side = side
            self.receiving = True
            self._pointer_position = self.layout.entry(side, rx, ry)
            self._pending_position = self._pointer_position
            self._flush_motion()
            self._receive_entry = {"id": entry_id, "origin": origin, "started": now,
                                   "deadline": now + 1., "retry": now + .1}
            self._pointer_check_at = 0.
            self._last_remote_motion_at = now
            self.changed("receiving", "对端正在控制此电脑 · Ctrl+Alt+Esc 停止")
            return
        if kind == "leave":
            self.receiving = False
            self._receive_entry = None
            self.native.release_all(self._posted_position)
            self._pointer_position = self._posted_position = None
            self._mapped_keys.clear()
            self.changed("ready", f"已连接 {self.peer_name} · 等待对端控制")
            return
        if kind not in {"move", "button", "scroll", "key", "text"}:
            raise ValueError("收到未知的控制消息")
        if not self.receiving:
            return  # in-flight input after returning across an edge
        if kind == "text":
            text = msg.get("text")
            if (not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES
                    or any(not ch.isprintable() and ch not in "\n\t" for ch in text)):
                raise ValueError("文本输入无效或超过 4 KiB 限制")
            self.native.type_text(text)
        elif kind == "move":
            self._last_remote_motion_at = time.monotonic()
            dx, dy = number(msg.get("dx")), number(msg.get("dy"))
            # Native injection is asynchronous on macOS and can be queued on
            # Windows. Reading the OS here loses deltas that have not landed yet.
            x, y = self._pointer_position
            desired = (x + dx, y + dy)
            # Project gaps in an irregular monitor layout onto a real screen.
            # Internal monitor borders remain traversable in the same session.
            x, y = self.layout.clamp(*desired)
            self._pointer_position = desired if self.layout.contains(*desired) else (x, y)
            self._pending_position = (x, y)
            crossed = ((self.side == "right" and dx < 0 and x <= self.bounds.x) or
                       (self.side == "left" and dx > 0 and x >= self.bounds.x + self.bounds.width - 1) or
                       (self.side == "bottom" and dy < 0 and y <= self.bounds.y) or
                       (self.side == "top" and dy > 0 and y >= self.bounds.y + self.bounds.height - 1))
            if crossed:
                self._flush_motion()
                self.receiving = False
                self._receive_entry = None
                self.native.release_all(self._posted_position)
                self._pointer_position = self._posted_position = None
                self._mapped_keys.clear()
                rx, ry = self.bounds.ratio(x, y)
                self._send({"type": "return", "rx": rx, "ry": ry})
                self.changed("ready", f"已连接 {self.peer_name} · 已返回对端")
        elif kind == "button":
            if msg.get("button") not in {"left", "right", "middle"} or type(msg.get("pressed")) is not bool:
                raise ValueError("无效的鼠标按键")
            self.native.button(msg["button"], msg["pressed"], self._posted_position)
        elif kind == "scroll":
            dx, dy = number(msg.get("dx"), 120), number(msg.get("dy"), 120)
            self._scroll_remainder[0] += dx
            self._scroll_remainder[1] += dy
            sx, sy = (math.trunc(v) for v in self._scroll_remainder)
            self._scroll_remainder[0] -= sx
            self._scroll_remainder[1] -= sy
            if sx or sy:
                self.native.scroll(sx, sy)
        else:
            key, pressed = msg.get("key"), msg.get("pressed")
            if not isinstance(key, str) or not self.key_validator(key) or type(pressed) is not bool:
                raise ValueError("无效的键盘按键")
            self.native.key(key, pressed)

    def _input(self, event):
        if self.role == "receiver" and self.receiving and event.get("type") == "local_move":
            # A real local mouse move supersedes earlier queued remote motion.
            self._pointer_position = self.layout.clamp(event["x"], event["y"])
            self._pending_position = None
            self._posted_position = self._pointer_position
            return
        if self.role != "controller" or not self.remote_bounds:
            return
        if event.get("type") == "local_move":
            if self.remote_active or time.monotonic() < self._cooldown:
                return
            x, y = event["x"], event["y"]
            if self.layout.hits_outer_edge(self.side, x, y):
                self.enter_remote(x, y)
        elif self.remote_active:
            outgoing = dict(event)
            if outgoing.get("type") == "key":
                original = outgoing["key"]
                if outgoing["pressed"]:
                    mapped = self._map_key(original)
                    self._mapped_keys[original] = mapped
                else:
                    mapped = self._mapped_keys.pop(original, self._map_key(original))
                outgoing["key"] = mapped
            self._send(outgoing)

    def _map_key(self, key):
        if (self.map_ctrl_cmd and self._peer_platform in {"Windows", "Darwin"}
                and platform.system() in {"Windows", "Darwin"}
                and self._peer_platform != platform.system()):
            for suffix in ("", "_l", "_r"):
                if key == "named:ctrl" + suffix:
                    return "named:cmd" + suffix
                if key == "named:cmd" + suffix:
                    return "named:ctrl" + suffix
        return key

    def enter_remote(self, x, y):
        if not self.connected or not self.remote_bounds or self.remote_active or self._entry_pending:
            return
        rx, ry = self.bounds.ratio(x, y)
        if self._peer_pointer_ready:
            entry_id = uuid.uuid4().hex
            self._entry_pending = {"id": entry_id, "deadline": time.monotonic() + 1.5}
            self._send({"type": "enter", "side": self.side, "rx": rx, "ry": ry,
                        "entry_id": entry_id})
            self.changed("handshake", "正在确认对端鼠标就绪；键鼠暂留本机。")
            return
        try:
            self._begin_capture()
        except RuntimeError as exc:
            self._cooldown = time.monotonic() + .7
            self.changed("notice", str(exc))
            return
        self.remote_active = True
        self._send({"type": "enter", "side": self.side, "rx": rx, "ry": ry})
        self.changed("controlling", f"正在控制 {self.peer_name} · F8 或 Ctrl+Alt+Esc 返回")

    def return_local(self, rx=None, ry=None):
        active = self.remote_active
        pending = self._entry_pending
        self._entry_pending = None
        self.remote_active = False
        self.native.set_capturing(False)
        self._mapped_keys.clear()
        self._cooldown = time.monotonic() + .7
        if (active or pending) and self.connected:
            self._send({"type": "leave"})
        if active and rx is not None and ry is not None:
            self.native.move_to(*self.layout.entry(OPPOSITE[self.side], rx, ry, inset=28))
        self.changed("ready", "已恢复本机控制 · F8 切到另一台电脑")

    def clipboard_changed(self, text: str):
        if not self.share_clipboard or not self.remote_bounds or text == self._last_clipboard:
            return
        self._last_clipboard = text
        if len(text.encode("utf-8")) <= MAX_CLIPBOARD_BYTES:
            try:
                self._send({"type": "clipboard", "text": text})
            except ConnectionError as exc:
                self.stop()
                self.changed("error", str(exc))
