"""Cursor fallback lifecycle and output-only window behavior; no OS injection."""
import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
    from PySide6.QtWidgets import QApplication, QWidget
    from kmlink.cursor import RemoteCursorIndicator, _CursorOverlay
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "Qt is not installed")
class CursorIndicatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.owner = QWidget()
        self.visibility = Mock(return_value=False)
        self.position = Mock(return_value=QPoint(-1200, 150))
        self.indicator = RemoteCursorIndicator(self.owner, self.visibility, self.position)

    def tearDown(self):
        self.indicator.close()
        if self.owner is not None:
            self.owner.close()
            self.owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_inactive_receiver_never_queries_or_creates_overlay(self):
        self.indicator.update_receiving(False)
        self.visibility.assert_not_called()
        self.position.assert_not_called()
        self.assertIsNone(self.indicator._widget)

    def test_only_known_hidden_native_cursor_enables_fallback(self):
        for state in (True, None):
            self.visibility.return_value = state
            self.indicator.update_receiving(True)
            self.assertIsNone(self.indicator._widget)
        self.visibility.return_value = False
        self.indicator.update_receiving(True)
        widget = self.indicator._widget
        self.assertTrue(widget.isVisible())
        self.assertEqual(widget.pos() + widget.HOTSPOT, QPoint(-1200, 150))
        for state in (True, None):
            self.visibility.return_value = state
            self.indicator.update_receiving(True)
            self.assertFalse(widget.isVisible())

    def test_uses_qt_global_position_directly_across_displays(self):
        self.indicator.update_receiving(True)
        widget = self.indicator._widget
        self.position.return_value = QPoint(2380, -400)
        self.indicator.update_receiving(True)
        self.assertIs(self.indicator._widget, widget)
        self.assertEqual(widget.pos() + widget.HOTSPOT, QPoint(2380, -400))

    def test_overlay_cannot_intercept_clicks_or_focus(self):
        self.indicator.update_receiving(True)
        widget = self.indicator._widget
        for flag in (Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
                     Qt.WindowType.WindowTransparentForInput, Qt.WindowType.WindowDoesNotAcceptFocus):
            self.assertTrue(widget.windowFlags() & flag)
        for attribute in (Qt.WidgetAttribute.WA_TranslucentBackground,
                          Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          Qt.WidgetAttribute.WA_ShowWithoutActivating,
                          Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow):
            self.assertTrue(widget.testAttribute(attribute))
        self.assertEqual(widget.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertIsNone(widget.parentWidget())

    def test_returning_local_hides_cursor_without_recreating_it(self):
        self.indicator.update_receiving(True)
        widget = self.indicator._widget
        self.indicator.update_receiving(False)
        self.assertFalse(widget.isVisible())
        self.indicator.update_receiving(True)
        self.assertIs(self.indicator._widget, widget)
        self.assertTrue(widget.isVisible())

    def test_arrow_is_visible_on_light_and_dark_backgrounds(self):
        self.indicator.update_receiving(True)
        image = self.indicator._widget.grab().toImage()
        scale = image.devicePixelRatio()
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
        self.assertEqual(image.pixelColor(round(6 * scale), round(12 * scale)).red(), 255)
        self.assertLess(image.pixelColor(round(3 * scale), round(12 * scale)).red(), 40)

    def test_unknown_state_or_position_failure_hides_stale_cursor(self):
        for probe in (self.visibility, self.position):
            self.indicator.update_receiving(True)
            widget = self.indicator._widget
            probe.side_effect = RuntimeError("desktop unavailable")
            self.indicator.update_receiving(True)
            self.assertFalse(widget.isVisible())
            probe.side_effect = None

    def test_close_is_idempotent_and_never_recreates_cursor(self):
        self.indicator.update_receiving(True)
        self.indicator.close()
        self.indicator.close()
        self.indicator.update_receiving(True)
        self.assertIsNone(self.indicator._widget)

    def test_destroying_owner_closes_independent_overlay(self):
        self.indicator.update_receiving(True)
        widget = self.indicator._widget
        self.owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.owner = None
        self.assertTrue(self.indicator._closed)
        self.assertIsNone(self.indicator._widget)
        # The overlay itself uses deferred deletion too.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
