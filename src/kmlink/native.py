"""Platform input hooks. No listeners or OS imports are created at import time.

The hook thread only updates small state and calls queue-oriented callbacks.
Windows and macOS suppression is selective: ordinary local input is allowed
until ``set_capturing(True)`` succeeds. F8 is reserved while listeners run.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from typing import Callable


NAMED_KEYS = frozenset({
    "alt", "alt_l", "alt_r", "alt_gr", "backspace", "caps_lock",
    "cmd", "cmd_l", "cmd_r", "ctrl", "ctrl_l", "ctrl_r", "delete",
    "down", "end", "enter", "esc", "home", "insert", "left", "menu",
    "num_lock", "page_down", "page_up", "pause", "print_screen", "right",
    "scroll_lock", "shift", "shift_l", "shift_r", "space", "tab", "up",
    "media_play_pause", "media_stop", "media_volume_mute", "media_volume_down",
    "media_volume_up", "media_previous", "media_next",
} | {f"f{i}" for i in range(1, 25)})
BUTTONS = frozenset({"left", "right", "middle"})


class _WindowsMouse:
    """Lazy Win32 mouse input API with physical virtual-desktop coordinates."""

    def __init__(self):
        # Fixed-width types also keep mocked API tests correct on non-Windows
        # hosts, where C long is not necessarily the Win32 32-bit LONG type.
        long, dword, word = ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint16

        class Point(ctypes.Structure):
            _fields_ = [("x", long), ("y", long)]

        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", long), ("dy", long), ("mouseData", dword),
                        ("dwFlags", dword), ("time", dword),
                        ("dwExtraInfo", ctypes.c_size_t)]

        class KeyboardInput(ctypes.Structure):
            _fields_ = [("wVk", word), ("wScan", word), ("dwFlags", dword),
                        ("time", dword), ("dwExtraInfo", ctypes.c_size_t)]

        class HardwareInput(ctypes.Structure):
            _fields_ = [("uMsg", dword), ("wParamL", word), ("wParamH", word)]

        class InputUnion(ctypes.Union):
            _fields_ = [("mi", MouseInput), ("ki", KeyboardInput), ("hi", HardwareInput)]

        class Input(ctypes.Structure):
            _anonymous_ = ("payload",)
            _fields_ = [("type", dword), ("payload", InputUnion)]

        class CursorInfo(ctypes.Structure):
            _fields_ = [("cbSize", dword), ("flags", dword),
                        ("hCursor", ctypes.c_void_p), ("ptScreenPos", Point)]

        self.Input, self.MouseInput, self.CursorInfo = Input, MouseInput, CursorInfo
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.SendInput.argtypes = [dword, ctypes.POINTER(Input), ctypes.c_int]
        self.api.SendInput.restype = dword
        self.api.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.api.GetSystemMetrics.restype = ctypes.c_int
        self.api.GetCursorInfo.argtypes = [ctypes.POINTER(CursorInfo)]
        self.api.GetCursorInfo.restype = ctypes.c_int
        self.bounds = tuple(self.api.GetSystemMetrics(index) for index in (76, 77, 78, 79))
        if self.bounds[2] <= 0 or self.bounds[3] <= 0:
            raise RuntimeError("无法读取 Windows 虚拟桌面坐标。")

    @staticmethod
    def _absolute(value: int, origin: int, extent: int) -> int:
        # Aim at the pixel centre in Win32's 16-bit absolute coordinate space.
        # This reaches every pixel in normal desktop sizes, including negative
        # monitor origins, without applying pointer acceleration a second time.
        pixel = max(0, min(extent - 1, int(value) - origin))
        return min(65535, (pixel * 65536 + 32768) // extent)

    def move_to(self, x: int, y: int, tag: int) -> None:
        left, top, width, height = self.bounds
        event = self.Input()
        event.type = 0  # INPUT_MOUSE
        event.mi = self.MouseInput(
            self._absolute(x, left, width), self._absolute(y, top, height),
            0, 0x0001 | 0x4000 | 0x8000, 0, tag)  # MOVE | VIRTUALDESK | ABSOLUTE
        if self.api.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            raise RuntimeError("系统拒绝鼠标输入，请检查目标窗口的权限或停止共享后重试。")

    def cursor_visible(self) -> bool | None:
        info = self.CursorInfo(cbSize=ctypes.sizeof(self.CursorInfo))
        if not self.api.GetCursorInfo(ctypes.byref(info)):
            return None
        return bool(info.flags & 0x0001) and not bool(info.flags & 0x0002)


def validate_key_code(code: object) -> bool:
    """Only symbolic names and a single printable Unicode scalar cross the wire."""
    if not isinstance(code, str):
        return False
    if code.startswith("named:"):
        return code[6:] in NAMED_KEYS
    if code.startswith("char:"):
        char = code[5:]
        return (len(char) == 1 and char.isprintable()
                and not 0xD800 <= ord(char) <= 0xDFFF)
    return False


def encode_key(key: object) -> str | None:
    """Convert pynput keys without putting platform virtual-key codes on wire."""
    name = getattr(key, "name", None)
    if name in NAMED_KEYS:
        return "named:" + name
    char = getattr(key, "char", None)
    if isinstance(char, str) and len(char) == 1:
        # Windows Ctrl+A..Z may arrive as ASCII controls. The modifier itself
        # travels separately, so send the original letter, not U+0001..001A.
        if 1 <= ord(char) <= 26:
            char = chr(ord(char) + 96)
        code = "char:" + char
        return code if validate_key_code(code) else None
    return None


def enable_dpi_awareness() -> None:
    """Keep Windows hook, controller and UI screen coordinates in physical pixels."""
    if sys.platform != "win32":
        return
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def permissions_status() -> str:
    """Return an actionable problem, without prompting or modifying permissions."""
    if sys.platform == "win32":
        return ""
    if sys.platform != "darwin":
        return "此版本仅支持 Windows 和 macOS。"
    try:
        import Quartz
        import HIServices
        trusted = bool(HIServices.AXIsProcessTrusted())
        listen_check = getattr(Quartz, "CGPreflightListenEventAccess", None)
        listening = bool(listen_check()) if listen_check else trusted
    except ImportError:
        return "macOS 输入组件未安装，请重新安装项目依赖。"
    missing = []
    if not trusted:
        missing.append("辅助功能")
    if not listening:
        missing.append("输入监控")
    if missing:
        return ("请在「系统设置 → 隐私与安全性」开启 " + "、".join(missing)
                + " 权限，然后完全退出并重新打开程序。源码运行时请授权终端或 Python。")
    return ""


class NativeInput:
    """Capture local input or inject peer input, with an always-local escape."""

    def __init__(self, on_event: Callable[[dict], None],
                 on_toggle: Callable[[], None], on_emergency: Callable[[], None]):
        self.on_event = on_event
        self.on_toggle = on_toggle
        self.on_emergency = on_emergency
        self.capturing = False
        self._lock = threading.RLock()
        self._physical_keys: dict[object, str | None] = {}
        self._physical_buttons: set[str] = set()
        self._injected_keys: set[str] = set()
        self._injected_buttons: set[str] = set()
        self._listeners: list = []
        self._keyboard_listener = None
        self._mouse_listener = None
        self._keyboard = self._mouse = None
        self._anchor = (0, 0)
        self._restore = None
        self._tag = 0x4B4D4C4B
        self._probe_tag = 0x4B4D5052
        self._mouse_probe_sent = 0.
        self._mouse_probe_seen = 0.
        self._clicks: dict[str, tuple[float, tuple[int, int], int]] = {}
        self._darwin_key_suppress = False
        self._started = False
        self._stopping = threading.Event()
        self._monitor = None
        self._windows_mouse = None
        self._listener_failed = False

    @property
    def needs_restart(self) -> bool:
        return self._listener_failed

    def start(self) -> None:
        if self._started:
            return
        problem = permissions_status()
        if problem:
            raise RuntimeError(problem)
        from pynput import keyboard, mouse
        self._kb_module, self._mouse_module = keyboard, mouse
        self._keyboard, self._mouse = keyboard.Controller(), mouse.Controller()
        if sys.platform == "win32":
            self._windows_mouse = _WindowsMouse()
            self._keyboard_listener = keyboard.Listener(
                win32_event_filter=self._win_keyboard)
            self._mouse_listener = mouse.Listener(win32_event_filter=self._win_mouse)
        elif sys.platform == "darwin":
            import Quartz
            self._quartz = Quartz
            self._keyboard_listener = keyboard.Listener(
                on_press=lambda key, injected=False: self._mac_key(key, True, injected),
                on_release=lambda key, injected=False: self._mac_key(key, False, injected),
                darwin_intercept=self._mac_keyboard_intercept)
            self._mouse_listener = mouse.Listener(
                on_move=self._mac_move,
                on_click=self._mac_click,
                on_scroll=self._mac_scroll,
                darwin_intercept=self._mac_mouse_intercept)
            self._guard_mac_callbacks(self._keyboard_listener)
            self._guard_mac_callbacks(self._mouse_listener)
        else:
            raise RuntimeError("此版本仅支持 Windows 和 macOS。")
        self._listeners = [self._keyboard_listener, self._mouse_listener]
        try:
            for listener in self._listeners:
                listener.start()
                self._wait_until_ready(listener)
            self._started = True
            self._listener_failed = False
            self._stopping.clear()
            self._monitor = threading.Thread(target=self._monitor_listeners, daemon=True,
                                             name="KMLink input watchdog")
            self._monitor.start()
        except Exception:
            self.stop()
            raise

    @staticmethod
    def _wait_until_ready(listener, timeout: float = 2.0) -> None:
        """pynput.wait() has no timeout and can hang on failed native startup."""
        deadline = time.monotonic() + timeout
        with listener._condition:
            while not listener._ready and listener.is_alive():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                listener._condition.wait(timeout=min(0.05, remaining))
        if not listener.is_alive():
            try:
                listener.join(timeout=0)
            except Exception as error:
                raise RuntimeError(f"输入监听启动失败：{error}") from error
            raise RuntimeError("系统未允许启动输入监听，请检查辅助功能和输入监控权限。")
        if not listener._ready:
            raise RuntimeError("输入监听启动超时，请关闭其他键鼠拦截工具后重试。")

    def _monitor_listeners(self) -> None:
        while not self._stopping.wait(0.25):
            if (any(not listener.is_alive() for listener in self._listeners)
                    or not self._mouse_hook_healthy()):
                # Failure of one hook must not leave the other hook capturing.
                self.capturing = False
                self._listener_failed = True
                try:
                    self.on_emergency()
                except Exception:
                    pass
                return

    def _mouse_hook_healthy(self, now=None) -> bool:
        """Detect a silently removed Windows low-level mouse hook.

        Windows can remove WH_MOUSE_LL after a timeout while its Python thread
        remains alive. A tagged zero-distance input confirms actual callbacks;
        failure returns local control instead of leaving only the keyboard on
        the remote computer. The probe never changes the cursor coordinates.
        """
        if sys.platform != "win32" or not self.capturing:
            return True
        now = time.monotonic() if now is None else now
        if self._mouse_probe_sent > self._mouse_probe_seen:
            return now - self._mouse_probe_sent < 1.2
        if now - self._mouse_probe_sent < .5:
            return True
        try:
            backend = self._windows_mouse
            if backend is None:
                return False
            self._mouse_probe_sent = now
            backend.move_to(*self.position(), self._probe_tag)
        except Exception:
            return False
        return True

    def stop(self) -> None:
        self._stopping.set()
        if self._monitor is not None and self._monitor is not threading.current_thread():
            self._monitor.join(timeout=0.5)
        self._monitor = None
        self.set_capturing(False)
        self.release_all()
        for listener in self._listeners:
            try:
                listener.stop()
            except Exception:
                pass
        for listener in self._listeners:
            if listener is not threading.current_thread() and listener.is_alive():
                try:
                    listener.join(timeout=1.2)
                except Exception:
                    pass  # Native callback errors must not interrupt cleanup.
        self._listeners.clear()
        self._physical_keys.clear()
        self._physical_buttons.clear()
        self._windows_mouse = None
        self._started = False

    def _emit(self, event: dict) -> None:
        try:
            self.on_event(event)
        except Exception:
            # If the queue/consumer fails, relinquish the hook immediately.
            self.capturing = False
            try:
                self.on_emergency()
            except Exception:
                pass

    def set_capturing(self, enabled: bool) -> None:
        with self._lock:
            if not enabled:
                self.capturing = False
                self._mouse_probe_sent = self._mouse_probe_seen = 0.
                if self._restore and self._mouse:
                    self._warp_to(*self._restore)
                self._restore = None
                return
            if self.capturing:
                return
            if not self._started:
                raise RuntimeError("输入监听尚未启动。")
            if self._physical_keys or self._physical_buttons:
                raise RuntimeError("请先松开键盘按键和鼠标按钮，再切换到另一台电脑。")
            self._restore = self.position()
            try:
                requested = self._capture_center()
                self._warp_to(*requested)
                actual = self.position()
                if (sys.platform == "win32"
                        and max(abs(actual[i] - requested[i]) for i in (0, 1)) > 2):
                    raise RuntimeError("当前窗口限制了鼠标位置，请退出该窗口的鼠标锁定后重试。")
            except Exception:
                self.capturing = False
                try:
                    self._warp_to(*self._restore)
                finally:
                    self._restore = None
                raise
            self._anchor = actual
            self._mouse_probe_sent = self._mouse_probe_seen = 0.
            self.capturing = True

    def _capture_center(self) -> tuple[int, int]:
        if sys.platform == "win32":
            from ctypes import wintypes
            class MonitorInfo(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
            user32 = ctypes.windll.user32
            user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
            user32.MonitorFromPoint.restype = ctypes.c_void_p
            user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MonitorInfo)]
            monitor = user32.MonitorFromPoint(wintypes.POINT(*self.position()), 2)
            info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
            if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                rect = info.rcMonitor
                clip = wintypes.RECT()
                user32.GetClipCursor.argtypes = [ctypes.POINTER(wintypes.RECT)]
                user32.GetClipCursor.restype = wintypes.BOOL
                if user32.GetClipCursor(ctypes.byref(clip)):
                    # Respect other applications' confinement; do not clear it.
                    rect.left, rect.top = max(rect.left, clip.left), max(rect.top, clip.top)
                    rect.right, rect.bottom = min(rect.right, clip.right), min(rect.bottom, clip.bottom)
                    if rect.right - rect.left < 8 or rect.bottom - rect.top < 8:
                        raise RuntimeError("当前窗口锁定了鼠标，请先解除鼠标锁定再切换电脑。")
                return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        elif sys.platform == "darwin":
            q = self._quartz
            bounds = q.CGDisplayBounds(q.CGMainDisplayID())
            return (int(bounds.origin.x + bounds.size.width / 2),
                    int(bounds.origin.y + bounds.size.height / 2))
        return self.position()

    def position(self) -> tuple[int, int]:
        if self._mouse is None:
            raise RuntimeError("输入设备尚未初始化。")
        x, y = self._mouse.position
        return int(x), int(y)

    def move_to(self, x: int, y: int) -> None:
        if self._mouse is None:
            return
        if sys.platform == "darwin":
            q = self._quartz
            button = next(iter(self._injected_buttons), None)
            kind = {"left": q.kCGEventLeftMouseDragged,
                    "right": q.kCGEventRightMouseDragged,
                    "middle": q.kCGEventOtherMouseDragged}.get(button, q.kCGEventMouseMoved)
            number = {"left": 0, "right": 1, "middle": 2}.get(button, 0)
            self._mac_post(q.CGEventCreateMouseEvent(None, kind, (int(x), int(y)), number))
        elif sys.platform == "win32":
            if self._windows_mouse is None:
                self._windows_mouse = _WindowsMouse()
            self._windows_mouse.move_to(x, y, self._tag)
        else:
            self._mouse.position = (int(x), int(y))

    def _warp_to(self, x: int, y: int) -> None:
        """Position the capture anchor without asynchronous remote input."""
        if self._mouse is None:
            return
        if sys.platform == "darwin":
            # Posting an event here can be observed after capture is enabled,
            # accidentally forwarding the anchor jump as a large remote move.
            result = self._quartz.CGWarpMouseCursorPosition((int(x), int(y)))
            if result != self._quartz.kCGErrorSuccess:
                raise RuntimeError("无法定位捕获光标，请检查辅助功能权限。")
        else:
            self._mouse.position = (int(x), int(y))

    def cursor_visible(self) -> bool | None:
        """Read visibility without changing OS cursor counters or preferences."""
        if self._mouse is None:
            return None
        if sys.platform == "win32":
            if self._windows_mouse is None:
                self._windows_mouse = _WindowsMouse()
            return self._windows_mouse.cursor_visible()
        if sys.platform == "darwin":
            check = getattr(self._quartz, "CGCursorIsVisible", None)
            return bool(check()) if check else None
        return None

    def move_by(self, dx: int, dy: int) -> None:
        x, y = self.position()
        self.move_to(x + int(dx), y + int(dy))

    def button(self, name: str, pressed: bool, position=None) -> None:
        if name not in BUTTONS or type(pressed) is not bool:
            raise ValueError("不支持的鼠标按钮。")
        if self._mouse is None:
            return
        if sys.platform == "darwin":
            q = self._quartz
            kinds = {"left": (q.kCGEventLeftMouseDown, q.kCGEventLeftMouseUp, 0),
                     "right": (q.kCGEventRightMouseDown, q.kCGEventRightMouseUp, 1),
                     "middle": (q.kCGEventOtherMouseDown, q.kCGEventOtherMouseUp, 2)}
            down, up, number = kinds[name]
            # Quartz posts movement asynchronously. The session can supply its
            # current target so a following click cannot land at the old OS point.
            position = self.position() if position is None else (int(position[0]), int(position[1]))
            now = time.monotonic()
            last, old_pos, count = self._clicks.get(name, (0, position, 0))
            if pressed:
                interval = 0.5
                try:
                    from AppKit import NSEvent
                    interval = NSEvent.doubleClickInterval()
                except (ImportError, AttributeError):
                    pass
                nearby = abs(position[0] - old_pos[0]) <= 4 and abs(position[1] - old_pos[1]) <= 4
                count = count + 1 if now - last <= interval and nearby else 1
                self._clicks[name] = (now, position, count)
            event = q.CGEventCreateMouseEvent(None, down if pressed else up, position, number)
            q.CGEventSetIntegerValueField(event, q.kCGMouseEventClickState, max(1, count))
            self._mac_post(event)
        else:
            button = getattr(self._mouse_module.Button, name)
            (self._mouse.press if pressed else self._mouse.release)(button)
        (self._injected_buttons.add if pressed else self._injected_buttons.discard)(name)

    def scroll(self, dx: int, dy: int) -> None:
        if self._mouse is None:
            return
        if sys.platform == "darwin":
            q = self._quartz
            self._mac_post(q.CGEventCreateScrollWheelEvent(
                None, q.kCGScrollEventUnitLine, 2, int(dy), int(dx)))
        else:
            self._mouse.scroll(int(dx), int(dy))

    def key(self, code: str, pressed: bool) -> None:
        if not validate_key_code(code) or type(pressed) is not bool:
            raise ValueError("不支持的按键。")
        if self._keyboard is None:
            return
        if code.startswith("named:"):
            key = getattr(self._kb_module.Key, code[6:], None)
            if key is None:
                return  # E.g. Insert or F24 do not exist on macOS.
        else:
            key = self._kb_module.KeyCode.from_char(code[5:])
        if sys.platform == "darwin":
            # Keep pynput's modifier bookkeeping, but tag every Quartz event so
            # our own listeners do not consider it physical local input.
            controller = self._keyboard
            base_key = key.value if isinstance(key, self._kb_module.Key) else key
            controller._update_modifiers(base_key, pressed)
            with controller.modifiers as modifiers:
                event = base_key._event(modifiers, controller._mapping, pressed)
            self._mac_post(event)
        else:
            (self._keyboard.press if pressed else self._keyboard.release)(key)
        (self._injected_keys.add if pressed else self._injected_keys.discard)(code)

    def type_text(self, text: str) -> None:
        """Insert text from the paired mobile controller without held modifiers."""
        if self._keyboard is None:
            return
        self.release_all()
        for character in text:
            code = {"\n": "named:enter", "\t": "named:tab"}.get(character, "char:" + character)
            self.key(code, True)
            self.key(code, False)

    def release_all(self, position=None) -> None:
        for code in list(self._injected_keys):
            try:
                self.key(code, False)
            except Exception:
                pass
        for name in list(self._injected_buttons):
            try:
                self.button(name, False, position)
            except Exception:
                pass
        self._injected_keys.clear()
        self._injected_buttons.clear()

    def _physical_key(self, identity: object, code: str | None, pressed: bool) -> bool:
        """Record physical state and return whether this event is reserved."""
        with self._lock:
            if pressed:
                first = identity not in self._physical_keys
                self._physical_keys[identity] = code
            else:
                first = identity in self._physical_keys
                code = self._physical_keys.pop(identity, code)
            if code == "named:f8":
                if not pressed and first:
                    self.on_toggle()
                return True
            control = any(c and c.startswith("named:ctrl") for c in self._physical_keys.values())
            alt = any(c and c.startswith("named:alt") for c in self._physical_keys.values())
            if code == "named:esc" and control and alt:
                if pressed and first:
                    self.capturing = False
                    self.on_emergency()
                return True
            if self.capturing and code:
                self._emit({"type": "key", "key": code, "pressed": pressed})
            return False

    def _win_keyboard(self, msg, data):
        if data.flags & 0x12:  # injected or lower-integrity injected
            return False
        if msg not in (0x100, 0x101, 0x104, 0x105):
            return False
        try:
            key = self._keyboard_listener._event_to_key(msg, data.vkCode)
            if not getattr(key, "name", None):
                key = self._keyboard_listener.canonical(key)
            code = encode_key(key)
            was_capturing = self.capturing
            reserved = self._physical_key(int(data.vkCode), code, msg in (0x100, 0x104))
        except Exception:
            self.capturing = False
            return False
        if reserved or was_capturing:
            self._keyboard_listener.suppress_event()
        return False

    def _win_mouse(self, msg, data):
        if data.flags & 0x03:
            if getattr(data, "dwExtraInfo", 0) == self._probe_tag:
                self._mouse_probe_seen = time.monotonic()
            return False
        x, y = int(data.pt.x), int(data.pt.y)
        capturing = self.capturing
        if msg == 0x200:
            if capturing:
                dx, dy = x - self._anchor[0], y - self._anchor[1]
                if dx or dy:
                    self._emit({"type": "move", "dx": dx, "dy": dy})
            else:
                self._emit({"type": "local_move", "x": x, "y": y})
        elif msg in (0x201, 0x202, 0x204, 0x205, 0x207, 0x208):
            name, pressed = {0x201: ("left", True), 0x202: ("left", False),
                             0x204: ("right", True), 0x205: ("right", False),
                             0x207: ("middle", True), 0x208: ("middle", False)}[msg]
            with self._lock:
                (self._physical_buttons.add if pressed else self._physical_buttons.discard)(name)
            if capturing:
                self._emit({"type": "button", "button": name, "pressed": pressed})
        elif msg in (0x20A, 0x20E) and capturing:
            delta = ctypes.c_short(int(data.mouseData) >> 16).value / 120
            self._emit({"type": "scroll", "dx": delta if msg == 0x20E else 0,
                        "dy": delta if msg == 0x20A else 0})
        if capturing:
            self._mouse_listener.suppress_event()
        return False

    def _mac_post(self, event) -> None:
        q = self._quartz
        q.CGEventSetIntegerValueField(event, q.kCGEventSourceUserData, self._tag)
        q.CGEventPost(q.kCGHIDEventTap, event)

    def _guard_mac_callbacks(self, listener) -> None:
        # pynput 1.8 dispatches callbacks BEFORE darwin_intercept. Check our tag
        # at that earlier boundary as well: even a backend reporting
        # injected=False must never update physical key state for our events.
        original = listener._handle_message

        def handle(proxy, event_type, event, refcon, injected):
            q = self._quartz
            if q.CGEventGetIntegerValueField(event, q.kCGEventSourceUserData) != self._tag:
                return original(proxy, event_type, event, refcon, injected)

        listener._handle_message = handle

    def _mac_key(self, key, pressed, injected=False):
        self._darwin_key_suppress = False
        if injected:
            return
        value = getattr(key, "value", key)
        identity = getattr(value, "vk", None)
        code = encode_key(key)
        if not getattr(key, "name", None) and identity is not None:
            # Translate without modifiers so Shift+1 becomes the physical 1,
            # with Shift transmitted separately. Keep non-US layouts intact.
            try:
                from pynput._util.darwin import keycode_to_string
                char = keycode_to_string(self._keyboard_listener._context, identity)
                if validate_key_code("char:" + char):
                    code = "char:" + char
            except (AttributeError, TypeError, ValueError):
                pass
        was_capturing = self.capturing
        self._darwin_key_suppress = self._physical_key(identity if identity is not None else code, code, pressed) or was_capturing

    def _mac_keyboard_intercept(self, event_type, event):
        q = self._quartz
        if q.CGEventGetIntegerValueField(event, q.kCGEventSourceUserData) == self._tag:
            return event
        return None if self._darwin_key_suppress else event

    def _mac_move(self, x, y, injected=False):
        if not injected and not self.capturing:
            self._emit({"type": "local_move", "x": int(x), "y": int(y)})

    def _mac_click(self, x, y, button, pressed, injected=False):
        if injected or button.name not in BUTTONS:
            return
        with self._lock:
            (self._physical_buttons.add if pressed else self._physical_buttons.discard)(button.name)
        if self.capturing:
            self._emit({"type": "button", "button": button.name, "pressed": bool(pressed)})

    def _mac_scroll(self, x, y, dx, dy, injected=False):
        if not injected and self.capturing:
            self._emit({"type": "scroll", "dx": dx, "dy": dy})

    def _mac_mouse_intercept(self, event_type, event):
        q = self._quartz
        if q.CGEventGetIntegerValueField(event, q.kCGEventSourceUserData) == self._tag:
            return event
        if not self.capturing:
            return event
        if event_type in (q.kCGEventMouseMoved, q.kCGEventLeftMouseDragged,
                          q.kCGEventRightMouseDragged, q.kCGEventOtherMouseDragged):
            dx = q.CGEventGetIntegerValueField(event, q.kCGMouseEventDeltaX)
            dy = q.CGEventGetIntegerValueField(event, q.kCGMouseEventDeltaY)
            if dx or dy:
                self._emit({"type": "move", "dx": int(dx), "dy": int(dy)})
        return None
