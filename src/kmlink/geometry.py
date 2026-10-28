"""Desktop coordinates shared by the input engine and native display discovery.

Rectangles use the same global coordinate space as native pointer events. Windows
callers must enable process DPI awareness before querying the desktop. Importing
this module does not load a GUI toolkit or any platform display framework.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Bounds:
    x: int
    y: int
    width: int
    height: int

    def wire(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def parse(cls, value: object) -> "Bounds":
        if not isinstance(value, dict):
            raise ValueError("无效的屏幕信息")
        parts = [value.get(k) for k in ("x", "y", "width", "height")]
        if any(type(v) is not int for v in parts):
            raise ValueError("无效的屏幕坐标")
        x, y, w, h = parts
        if not (64 <= w <= 65536 and 64 <= h <= 65536 and abs(x) <= 131072 and abs(y) <= 131072):
            raise ValueError("屏幕尺寸超出范围")
        return cls(x, y, w, h)

    def ratio(self, x: float, y: float) -> tuple[float, float]:
        return (max(0., min(1., (x - self.x) / (self.width - 1))),
                max(0., min(1., (y - self.y) / (self.height - 1))))

    def entry(self, side: str, rx: float, ry: float, inset: int = 16) -> tuple[int, int]:
        x = self.x + round(rx * (self.width - 1))
        y = self.y + round(ry * (self.height - 1))
        if side == "right":
            x = self.x + inset
        elif side == "left":
            x = self.x + self.width - 1 - inset
        elif side == "bottom":
            y = self.y + inset
        else:
            y = self.y + self.height - 1 - inset
        return x, y


class DesktopLayout:
    """A union rectangle plus the actual occupied areas of all local screens."""

    def __init__(self, screens: Iterable[Bounds]):
        screens = tuple(screens)
        if not screens:
            raise ValueError("未找到可用的显示器")
        for screen in screens:
            if (not isinstance(screen, Bounds)
                    or any(type(v) is not int for v in
                           (screen.x, screen.y, screen.width, screen.height))
                    or screen.width <= 0 or screen.height <= 0):
                raise ValueError("无效的显示器范围")
        # Mirrored displays can share a rectangle. Stable ordering also avoids
        # treating a native enumeration-order change as a topology change.
        self.screens = tuple(sorted(set(screens), key=lambda s: (s.x, s.y, s.width, s.height)))
        x = min(s.x for s in self.screens)
        y = min(s.y for s in self.screens)
        right = max(s.x + s.width for s in self.screens)
        bottom = max(s.y + s.height for s in self.screens)
        self.bounds = Bounds(x, y, right - x, bottom - y)

    def contains(self, x: float, y: float) -> bool:
        return any(s.x <= x < s.x + s.width and s.y <= y < s.y + s.height
                   for s in self.screens)

    def clamp(self, x: float, y: float) -> tuple[int, int]:
        """Return the nearest integer pointer position on a real screen."""
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("无效的鼠标坐标")
        candidates = ((max(s.x, min(s.x + s.width - 1, round(x))),
                       max(s.y, min(s.y + s.height - 1, round(y))))
                      for s in self.screens)
        return min(candidates, key=lambda p: ((p[0] - x) ** 2 + (p[1] - y) ** 2, p))

    def entry(self, side: str, rx: float, ry: float, inset: int = 16) -> tuple[int, int]:
        return self.clamp(*self.bounds.entry(side, rx, ry, inset))

    def hits_outer_edge(self, side: str, x: float, y: float,
                        margin: int = 2, corner_guard: int = 24) -> bool:
        """Only the overall desktop perimeter can transfer control.

        Internal monitor boundaries and empty areas inside the union never
        trigger a switch. Corner guard and the edge band preserve the original
        single-monitor behavior.
        """
        if margin <= 0 or corner_guard < 0 or not self.contains(x, y):
            return False
        b = self.bounds
        if side in {"left", "right"}:
            if not b.y + corner_guard < y < b.y + b.height - 1 - corner_guard:
                return False
            return x < b.x + margin if side == "left" else x >= b.x + b.width - margin
        if side in {"top", "bottom"}:
            if not b.x + corner_guard < x < b.x + b.width - 1 - corner_guard:
                return False
            return y < b.y + margin if side == "top" else y >= b.y + b.height - margin
        return False


def _windows_screens() -> tuple[Bounds, ...]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    screens = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    @callback_type
    def monitor(_monitor, _dc, rectangle, _data):
        r = rectangle.contents
        if r.right > r.left and r.bottom > r.top:
            screens.append(Bounds(r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    try:
        enum = user32.EnumDisplayMonitors
        enum.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), callback_type, wintypes.LPARAM]
        enum.restype = wintypes.BOOL
        if enum(None, None, monitor, 0) and screens:
            return tuple(screens)
    except (AttributeError, OSError, ValueError, ctypes.ArgumentError):
        pass
    # Unlike metrics 0/1, these four values describe the entire virtual desktop.
    metrics = user32.GetSystemMetrics
    metrics.argtypes = [ctypes.c_int]
    metrics.restype = ctypes.c_int
    x, y, width, height = (metrics(i) for i in (76, 77, 78, 79))
    if width <= 0 or height <= 0:
        raise RuntimeError("无法读取 Windows 显示器范围")
    return (Bounds(x, y, width, height),)


def _mac_screens() -> tuple[Bounds, ...]:
    import Quartz

    def bounds(display):
        r = Quartz.CGDisplayBounds(display)
        return Bounds(int(r.origin.x), int(r.origin.y), int(r.size.width), int(r.size.height))

    try:
        # PyObjC allocates the out array for None. Grow it when entirely filled
        # rather than silently dropping displays beyond a fixed-size buffer.
        capacity = 16
        while capacity <= 1024:
            error, displays, count = Quartz.CGGetActiveDisplayList(capacity, None, None)
            if error != Quartz.kCGErrorSuccess:
                break
            if count < capacity:
                screens = tuple(bounds(display) for display in displays[:count])
                if screens and all(s.width > 0 and s.height > 0 for s in screens):
                    return screens
                break
            capacity *= 2
    except (AttributeError, TypeError, ValueError, RuntimeError):
        pass
    main = bounds(Quartz.CGMainDisplayID())
    if main.width <= 0 or main.height <= 0:
        raise RuntimeError("无法读取 macOS 显示器范围")
    return (main,)


def _qt_screens() -> tuple[Bounds, ...]:
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance()
    if app is None:
        raise RuntimeError("尚未初始化桌面显示服务")
    screens = app.screens()
    if not screens:
        primary = app.primaryScreen()
        screens = [primary] if primary is not None else []
    rectangles = [screen.geometry() for screen in screens]
    return tuple(Bounds(r.x(), r.y(), r.width(), r.height()) for r in rectangles
                 if r.width() > 0 and r.height() > 0)


def query_desktop_layout() -> DesktopLayout:
    """Read current native monitor rectangles, never a guessed display size."""
    try:
        if sys.platform == "win32":
            screens = _windows_screens()
        elif sys.platform == "darwin":
            screens = _mac_screens()
        else:
            screens = _qt_screens()
        return DesktopLayout(screens)
    except (ImportError, AttributeError, OSError, TypeError, ValueError) as error:
        raise RuntimeError("无法读取当前桌面显示器范围") from error
