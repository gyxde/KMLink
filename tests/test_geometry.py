"""Headless geometry and native display-query adapter tests."""
import ctypes
import math
import sys
import types
import unittest
from ctypes import wintypes
from unittest.mock import patch

from kmlink.geometry import Bounds, DesktopLayout, query_desktop_layout
from kmlink import geometry


class BoundsTests(unittest.TestCase):
    def test_wire_roundtrip_preserves_negative_origin(self):
        bounds = Bounds(-1920, -300, 3840, 1380)
        self.assertEqual(Bounds.parse(bounds.wire()), bounds)
        self.assertEqual(bounds.ratio(-1920, -300), (0., 0.))
        self.assertEqual(bounds.ratio(1919, 1079), (1., 1.))
        self.assertEqual(bounds.ratio(-9000, 9000), (0., 1.))

    def test_parse_rejects_invalid_wire_geometry(self):
        for invalid in (None, {}, {"x": False, "y": 0, "width": 100, "height": 100},
                        {"x": 0, "y": 0, "width": 63, "height": 100},
                        {"x": 131073, "y": 0, "width": 100, "height": 100}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                Bounds.parse(invalid)

    def test_entry_preserves_existing_side_convention(self):
        bounds = Bounds(-100, -200, 1000, 600)
        self.assertEqual(bounds.entry("right", .5, .5), (-84, 100))
        self.assertEqual(bounds.entry("left", .5, .5), (883, 100))
        self.assertEqual(bounds.entry("bottom", .5, .5), (400, -184))
        self.assertEqual(bounds.entry("top", .5, .5), (400, 383))


class DesktopLayoutTests(unittest.TestCase):
    def test_horizontal_monitors_only_transfer_at_outermost_edge(self):
        desktop = DesktopLayout([Bounds(0, 0, 1920, 1080), Bounds(1920, 0, 1920, 1080)])
        self.assertEqual(desktop.bounds, Bounds(0, 0, 3840, 1080))
        self.assertFalse(desktop.hits_outer_edge("right", 1919, 500))
        self.assertFalse(desktop.hits_outer_edge("left", 1920, 500))
        self.assertTrue(desktop.hits_outer_edge("right", 3839, 500))
        self.assertTrue(desktop.hits_outer_edge("right", 3838, 500))
        self.assertFalse(desktop.hits_outer_edge("right", 3837, 500))
        self.assertTrue(desktop.hits_outer_edge("left", 0, 500))

    def test_negative_monitors_keep_global_coordinates(self):
        desktop = DesktopLayout([Bounds(0, 0, 1920, 1080), Bounds(-2560, -200, 2560, 1440)])
        self.assertEqual(desktop.bounds, Bounds(-2560, -200, 4480, 1440))
        self.assertFalse(desktop.hits_outer_edge("right", -1, 500))
        self.assertFalse(desktop.hits_outer_edge("left", 0, 500))
        self.assertTrue(desktop.hits_outer_edge("left", -2560, 500))
        self.assertTrue(desktop.hits_outer_edge("right", 1919, 500))
        self.assertFalse(desktop.hits_outer_edge("right", 1919, -100))
        self.assertEqual(desktop.clamp(-3000, -300), (-2560, -200))

    def test_vertical_monitors_do_not_transfer_at_shared_edge(self):
        desktop = DesktopLayout([Bounds(0, -1200, 1920, 1200), Bounds(0, 0, 1920, 1080)])
        self.assertEqual(desktop.bounds, Bounds(0, -1200, 1920, 2280))
        self.assertFalse(desktop.hits_outer_edge("bottom", 1000, -1))
        self.assertFalse(desktop.hits_outer_edge("top", 1000, 0))
        self.assertTrue(desktop.hits_outer_edge("top", 1000, -1200))
        self.assertTrue(desktop.hits_outer_edge("bottom", 1000, 1079))

    def test_disjoint_gap_is_not_part_of_a_real_screen(self):
        desktop = DesktopLayout([Bounds(0, 0, 100, 100), Bounds(200, 200, 100, 100)])
        self.assertFalse(desktop.contains(150, 150))
        self.assertFalse(desktop.hits_outer_edge("right", 299, 150))
        self.assertFalse(desktop.hits_outer_edge("top", 150, 0))
        self.assertFalse(desktop.hits_outer_edge("right", 99, 50))
        self.assertTrue(desktop.hits_outer_edge("right", 299, 250))
        self.assertEqual(desktop.clamp(150, 150), (200, 200))
        self.assertEqual(desktop.clamp(120, 120), (99, 99))
        self.assertEqual(desktop.clamp(150, 30), (99, 30))

    def test_entry_is_clamped_to_real_screen_when_global_edge_has_gap(self):
        desktop = DesktopLayout([Bounds(0, 0, 100, 100), Bounds(200, 200, 100, 100)])
        self.assertEqual(desktop.entry("left", .5, 0), (99, 0))
        self.assertEqual(desktop.entry("right", .5, 1), (200, 299))
        self.assertEqual(desktop.entry("bottom", 1, .5), (299, 200))
        self.assertEqual(desktop.entry("top", 0, .5), (0, 99))

    def test_corner_guard_and_real_screen_boundaries(self):
        desktop = DesktopLayout([Bounds(0, 0, 100, 100)])
        for side, point in (("right", (99, 24)), ("right", (99, 75)),
                            ("left", (0, 0)), ("top", (24, 0)), ("bottom", (75, 99))):
            with self.subTest(side=side, point=point):
                self.assertFalse(desktop.hits_outer_edge(side, *point))
        self.assertTrue(desktop.hits_outer_edge("right", 99, 25))
        self.assertTrue(desktop.hits_outer_edge("top", 25, 0))
        self.assertFalse(desktop.hits_outer_edge("right", 100, 50))
        self.assertFalse(desktop.hits_outer_edge("left", -1, 50))
        self.assertFalse(desktop.hits_outer_edge("invalid", 99, 50))
        self.assertFalse(desktop.hits_outer_edge("right", 99, 50, margin=0))

    def test_clamp_rounds_to_a_valid_pixel_and_rejects_nonfinite(self):
        desktop = DesktopLayout([Bounds(-100, -100, 100, 100)])
        self.assertEqual(desktop.clamp(-.1, -.1), (-1, -1))
        self.assertEqual(desktop.clamp(-80.4, -60.6), (-80, -61))
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value), self.assertRaises(ValueError):
                desktop.clamp(value, 0)

    def test_mirrors_and_enumeration_order_have_stable_representation(self):
        a, b = Bounds(0, 0, 1920, 1080), Bounds(-1000, 0, 1000, 1000)
        self.assertEqual(DesktopLayout([a, b, a]).screens, DesktopLayout([b, a]).screens)

    def test_empty_and_invalid_screens_are_rejected(self):
        for screens in ([], [Bounds(0, 0, 0, 100)], [Bounds(False, 0, 100, 100)], [None]):
            with self.subTest(screens=screens), self.assertRaises(ValueError):
                DesktopLayout(screens)


class DisplayQueryTests(unittest.TestCase):
    def test_platform_selection_without_importing_a_gui(self):
        for platform, helper in (("win32", "_windows_screens"), ("darwin", "_mac_screens"),
                                 ("linux", "_qt_screens")):
            with self.subTest(platform=platform), patch.object(sys, "platform", platform), \
                    patch.object(geometry, helper, return_value=(Bounds(-400, 0, 400, 300),)):
                self.assertEqual(query_desktop_layout().bounds, Bounds(-400, 0, 400, 300))

    def test_query_failure_does_not_invent_display_dimensions(self):
        with patch.object(sys, "platform", "win32"), \
                patch.object(geometry, "_windows_screens", side_effect=OSError("unavailable")):
            with self.assertRaises(RuntimeError):
                query_desktop_layout()

    def test_windows_enumeration_keeps_negative_coordinates(self):
        def enumerate_monitors(_dc, _clip, callback, data):
            for values in ((-1920, -100, 0, 980), (0, 0, 2560, 1440)):
                rect = wintypes.RECT(*values)
                callback(None, None, ctypes.pointer(rect), data)
            return 1

        user32 = types.SimpleNamespace(EnumDisplayMonitors=enumerate_monitors)
        with patch.object(ctypes, "WinDLL", return_value=user32, create=True), \
                patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True):
            screens = geometry._windows_screens()
        self.assertEqual(screens, (Bounds(-1920, -100, 1920, 1080), Bounds(0, 0, 2560, 1440)))

    def test_windows_fallback_uses_virtual_desktop_metrics(self):
        requested = []

        def metrics(index):
            requested.append(index)
            return {76: -1920, 77: -200, 78: 4480, 79: 1640}[index]

        user32 = types.SimpleNamespace(EnumDisplayMonitors=lambda *args: 0,
                                       GetSystemMetrics=metrics)
        with patch.object(ctypes, "WinDLL", return_value=user32, create=True), \
                patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True):
            self.assertEqual(geometry._windows_screens(), (Bounds(-1920, -200, 4480, 1640),))
        self.assertEqual(requested, [76, 77, 78, 79])

    @staticmethod
    def rectangle(x, y, width, height):
        return types.SimpleNamespace(origin=types.SimpleNamespace(x=x, y=y),
                                     size=types.SimpleNamespace(width=width, height=height))

    def test_mac_active_displays_use_global_quartz_coordinates(self):
        quartz = types.SimpleNamespace(
            kCGErrorSuccess=0,
            CGGetActiveDisplayList=lambda *args: (0, [7, 9], 2),
            CGDisplayBounds=lambda display: self.rectangle(*{
                7: (0, 0, 1728, 1117), 9: (-1920, -50, 1920, 1080)}[display]))
        with patch.dict(sys.modules, {"Quartz": quartz}):
            self.assertEqual(geometry._mac_screens(),
                             (Bounds(0, 0, 1728, 1117), Bounds(-1920, -50, 1920, 1080)))

    def test_mac_failed_enumeration_falls_back_to_actual_main_display(self):
        quartz = types.SimpleNamespace(
            kCGErrorSuccess=0, CGGetActiveDisplayList=lambda *args: (1, [], 0),
            CGMainDisplayID=lambda: 7, CGDisplayBounds=lambda _: self.rectangle(0, 0, 1512, 982))
        with patch.dict(sys.modules, {"Quartz": quartz}):
            self.assertEqual(geometry._mac_screens(), (Bounds(0, 0, 1512, 982),))


if __name__ == "__main__":
    unittest.main()
