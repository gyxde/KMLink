"""Native pointer contracts without moving the test machine's real cursor."""
import ctypes
import sys
import types
import unittest
from unittest.mock import patch

from kmlink.native import NativeInput, _WindowsMouse


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class WindowsPointerTests(unittest.TestCase):
    def backend(self, bounds=(-1920, -200, 4480, 1640), sent=1, visible=1):
        calls = []
        holder = {}

        def send(count, pointer, size):
            value = ctypes.cast(pointer, ctypes.POINTER(holder['backend'].Input)).contents
            calls.append((count, size, value.type, value.mi.dx, value.mi.dy,
                          value.mi.dwFlags, value.mi.dwExtraInfo))
            return sent

        def visibility(pointer):
            info = ctypes.cast(pointer, ctypes.POINTER(holder['backend'].CursorInfo)).contents
            self.assertEqual(info.cbSize, ctypes.sizeof(holder['backend'].CursorInfo))
            info.flags = visible
            return visible is not None

        api = types.SimpleNamespace(
            GetSystemMetrics=Function(lambda i: dict(zip((76, 77, 78, 79), bounds))[i]),
            SendInput=Function(send), GetCursorInfo=Function(visibility))
        with patch.object(ctypes, 'WinDLL', return_value=api, create=True):
            backend = _WindowsMouse()
        holder['backend'] = backend
        return backend, calls

    def test_send_input_uses_virtual_desktop_tag_and_pixel_centres(self):
        backend, calls = self.backend()
        for x, y in ((-1920, -200), (-100, 400), (0, 0), (2559, 1439)):
            backend.move_to(x, y, 0x4B4D4C4B)
            count, size, kind, nx, ny, flags, tag = calls[-1]
            self.assertEqual((count, kind, flags, tag), (1, 0, 0xC001, 0x4B4D4C4B))
            self.assertEqual(size, 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
            self.assertEqual(nx * 4480 // 65536 - 1920, x)
            self.assertEqual(ny * 1640 // 65536 - 200, y)

    def test_absolute_coordinates_clamp_to_virtual_desktop(self):
        backend, calls = self.backend()
        backend.move_to(-99999, 99999, 7)
        nx, ny = calls[-1][3:5]
        self.assertEqual(nx * 4480 // 65536 - 1920, -1920)
        self.assertEqual(ny * 1640 // 65536 - 200, 1439)

    def test_failed_input_is_reported_instead_of_silent_cursor_stall(self):
        backend, _ = self.backend(sent=0)
        with self.assertRaisesRegex(RuntimeError, '拒绝鼠标输入'):
            backend.move_to(10, 20, 7)

    def test_invalid_virtual_desktop_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, '虚拟桌面'):
            self.backend(bounds=(0, 0, 0, 1080))

    def test_cursor_visibility_reads_global_state_without_changing_counters(self):
        for flags, expected in ((0, False), (1, True), (2, False), (3, False)):
            with self.subTest(flags=flags):
                backend, _ = self.backend(visible=flags)
                self.assertEqual(backend.cursor_visible(), expected)


class NativePointerTests(unittest.TestCase):
    def build(self):
        return NativeInput(lambda event: None, lambda: None, lambda: None)

    def test_windows_remote_move_uses_input_but_capture_uses_synchronous_warp(self):
        native = self.build()
        native._started = True
        native._mouse = types.SimpleNamespace(position=(50, 60))
        native._capture_center = lambda: (500, 300)
        sent = []
        native._windows_mouse = types.SimpleNamespace(move_to=lambda *args: sent.append(args))
        with patch.object(sys, 'platform', 'win32'):
            native.move_to(-100, 200)
            self.assertEqual(sent, [(-100, 200, native._tag)])
            self.assertEqual(native._mouse.position, (50, 60))
            native.set_capturing(True)
            self.assertEqual(native._mouse.position, (500, 300))
            self.assertEqual(len(sent), 1)
            native.set_capturing(False)
            self.assertEqual(native._mouse.position, (50, 60))

    def test_windows_capture_forwards_each_suppressed_motion_relative_to_anchor(self):
        events = []
        native = NativeInput(events.append, lambda: None, lambda: None)
        native.capturing = True
        native._anchor = (500, 300)
        suppressed = []
        native._mouse_listener = types.SimpleNamespace(suppress_event=lambda: suppressed.append(True))
        for x, y in ((502, 303), (501, 299), (500, 300)):
            native._win_mouse(0x200, types.SimpleNamespace(flags=0, pt=types.SimpleNamespace(x=x, y=y)))
        self.assertEqual(events, [{'type': 'move', 'dx': 2, 'dy': 3},
                                  {'type': 'move', 'dx': 1, 'dy': -1}])
        self.assertEqual(suppressed, [True, True, True])

    def test_windows_injected_move_is_never_forwarded_back_to_peer(self):
        native = NativeInput(lambda event: self.fail('Injected move echoed'), lambda: None, lambda: None)
        native.capturing = True
        native._win_mouse(0x200, types.SimpleNamespace(flags=1))

    def test_mac_capture_warp_does_not_post_an_anchor_movement_event(self):
        native = self.build()
        native._started = True
        native._mouse = types.SimpleNamespace(position=(50, 60))
        native._capture_center = lambda: (500, 300)
        warps = []
        native._quartz = types.SimpleNamespace(
            kCGErrorSuccess=0, CGWarpMouseCursorPosition=lambda p: warps.append(p) or 0)
        with patch.object(sys, 'platform', 'darwin'):
            native.set_capturing(True)
            native.set_capturing(False)
        self.assertEqual(warps, [(500, 300), (50, 60)])

    def test_mac_click_uses_supplied_target_without_reading_stale_os_position(self):
        native = self.build()
        native._mouse = object()
        native.position = lambda: self.fail('A following click must use the session target')
        posts = []
        native._quartz = types.SimpleNamespace(
            kCGEventLeftMouseDown=1, kCGEventLeftMouseUp=2,
            kCGEventRightMouseDown=3, kCGEventRightMouseUp=4,
            kCGEventOtherMouseDown=5, kCGEventOtherMouseUp=6,
            kCGMouseEventClickState=7,
            CGEventCreateMouseEvent=lambda source, kind, position, button: (kind, position, button),
            CGEventSetIntegerValueField=lambda *args: None)
        native._mac_post = posts.append
        with patch.object(sys, 'platform', 'darwin'):
            native.button('left', True, (-700, 400))
            native.button('left', False, (-700, 400))
        self.assertEqual(posts, [(1, (-700, 400), 0), (2, (-700, 400), 0)])

    def test_visibility_is_unknown_before_native_devices_start(self):
        self.assertIsNone(self.build().cursor_visible())

    def test_visibility_uses_platform_api(self):
        native = self.build()
        native._mouse = object()
        native._windows_mouse = types.SimpleNamespace(cursor_visible=lambda: False)
        native._quartz = types.SimpleNamespace(CGCursorIsVisible=lambda: True)
        with patch.object(sys, 'platform', 'win32'):
            self.assertFalse(native.cursor_visible())
        with patch.object(sys, 'platform', 'darwin'):
            self.assertTrue(native.cursor_visible())


if __name__ == '__main__':
    unittest.main()
