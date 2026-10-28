"""Capture setup and hook-health failures fail back to the local desktop."""
import ctypes
import sys
import types
import unittest
from unittest.mock import patch

from kmlink.native import NativeInput


class ApiFunction:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class NativeReliabilityTests(unittest.TestCase):
    def native(self):
        value = NativeInput(lambda _: None, lambda: None, lambda: None)
        value._started = True
        value._mouse = types.SimpleNamespace(position=(50, 60))
        value._capture_center = lambda: (500, 300)
        return value

    def test_capture_anchor_uses_confirmed_position_not_requested_position(self):
        native = self.native()
        native._warp_to = lambda *point: setattr(native._mouse, "position", (501, 301))
        with patch.object(sys, "platform", "win32"):
            native.set_capturing(True)
        self.assertEqual(native._anchor, (501, 301))
        self.assertTrue(native.capturing)

    def test_clipped_or_failed_warp_never_enables_capture(self):
        native = self.native()
        native._warp_to = lambda *_: None
        with patch.object(sys, "platform", "win32"):
            with self.assertRaisesRegex(RuntimeError, "限制"):
                native.set_capturing(True)
        self.assertFalse(native.capturing)
        self.assertIsNone(native._restore)

    def test_capture_centre_respects_existing_clip_rectangle(self):
        native = self.native()
        del native._capture_center
        api = types.SimpleNamespace()
        api.MonitorFromPoint = ApiFunction(lambda *_: 1)
        def monitor(_handle, pointer):
            value = ctypes.cast(pointer, api.GetMonitorInfoW.argtypes[1]).contents
            value.rcMonitor.left, value.rcMonitor.top = 0, 0
            value.rcMonitor.right, value.rcMonitor.bottom = 1920, 1080
            return 1
        api.GetMonitorInfoW = ApiFunction(monitor)
        def clip(pointer):
            value = ctypes.cast(pointer, api.GetClipCursor.argtypes[0]).contents
            value.left, value.top, value.right, value.bottom = 100, 200, 500, 600
            return 1
        api.GetClipCursor = ApiFunction(clip)
        with patch.object(sys, "platform", "win32"), \
                patch.object(ctypes, "windll", types.SimpleNamespace(user32=api), create=True):
            self.assertEqual(native._capture_center(), (300, 400))

    def test_tagged_probe_confirms_actual_callback_without_forwarding(self):
        native = self.native()
        native.capturing = True
        posted, forwarded = [], []
        native.on_event = forwarded.append
        native._windows_mouse = types.SimpleNamespace(move_to=lambda *args: posted.append(args))
        with patch.object(sys, "platform", "win32"):
            self.assertTrue(native._mouse_hook_healthy(10.))
            self.assertEqual(posted, [(50, 60, native._probe_tag)])
            with patch("kmlink.native.time.monotonic", return_value=10.1):
                native._win_mouse(0x200, types.SimpleNamespace(flags=1, dwExtraInfo=native._probe_tag))
            self.assertTrue(native._mouse_hook_healthy(10.7))
        self.assertEqual(forwarded, [])

    def test_missing_probe_callback_detects_silent_hook_removal(self):
        native = self.native()
        native.capturing = True
        native._windows_mouse = types.SimpleNamespace(move_to=lambda *_: None)
        with patch.object(sys, "platform", "win32"):
            self.assertTrue(native._mouse_hook_healthy(10.))
            self.assertTrue(native._mouse_hook_healthy(10.8))
            self.assertFalse(native._mouse_hook_healthy(11.3))

    def test_failed_probe_returns_control_even_when_listener_thread_still_lives(self):
        native = self.native()
        native.capturing = True
        native._listeners = [types.SimpleNamespace(is_alive=lambda: True)]
        native._mouse_hook_healthy = lambda: False
        native._stopping = types.SimpleNamespace(wait=lambda _: False)
        recovered = []
        native.on_emergency = lambda: recovered.append(True)
        native._monitor_listeners()
        self.assertFalse(native.capturing)
        self.assertTrue(native.needs_restart)
        self.assertEqual(recovered, [True])


if __name__ == "__main__":
    unittest.main()
