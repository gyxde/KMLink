"""A non-interactive cursor fallback for a hidden native receiver cursor.

The indicator uses Qt's global cursor coordinates rather than the input
protocol's physical pixel coordinates. Qt therefore handles per-screen scaling.
It never changes the operating system's cursor visibility counter or shape.
"""
from __future__ import annotations

import weakref
from collections.abc import Callable

from PySide6.QtCore import QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QCursor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QWidget


class _CursorOverlay(QWidget):
    """Output-only window; it cannot focus or intercept the remote click."""

    HOTSPOT = QPoint(3, 3)

    def __init__(self):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint
                 | Qt.WindowType.WindowTransparentForInput
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
        # An owned Windows tool window would disappear when KMLink is minimized.
        # The indicator owner explicitly closes this independent top-level window.
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(29, 36)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outline = QPolygonF([QPointF(3, 3), QPointF(3, 26), QPointF(9, 20),
                            QPointF(14, 31), QPointF(19, 28), QPointF(14, 18),
                            QPointF(23, 18)])
        painter.setPen(QPen(QColor("#111111"), 2.5, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawPolygon(outline)


class RemoteCursorIndicator(QObject):
    """Show a fallback only while receiving input and visibility is known hidden.

    ``visibility_query`` returns True, False, or None (unknown). Unknown states
    never add a second cursor. Call from the UI thread after polling the engine.
    """

    def __init__(self, owner: QWidget, visibility_query: Callable[[], bool | None],
                 position_query: Callable[[], QPoint] | None = None):
        super().__init__(owner)
        self._owner = weakref.ref(owner)
        self._visibility_query = visibility_query
        self._position_query = position_query or QCursor.pos
        self._widget: _CursorOverlay | None = None
        self._closed = False
        owner.destroyed.connect(self.close)

    def _hide(self):
        if self._widget is not None:
            try:
                self._widget.hide()
            except RuntimeError:
                self._widget = None  # Qt may already be tearing down its windows.

    def update_receiving(self, receiving: bool):
        if self._closed:
            return
        if not receiving:
            self._hide()
            return
        try:
            if self._visibility_query() is not False:
                self._hide()
                return
            position = self._position_query()
            if not isinstance(position, QPoint):
                self._hide()
                return
            if self._widget is None:
                self._widget = _CursorOverlay()
            self._widget.move(position - self._widget.HOTSPOT)
            if not self._widget.isVisible():
                self._widget.show()
        except Exception:
            # A fallback must never interrupt input or keep a stale pointer on
            # screen when the native state/desktop becomes unavailable.
            self._hide()

    def close(self, *_args):
        if self._closed:
            return
        self._closed = True
        owner = self._owner()
        if owner is not None:
            try:
                owner.destroyed.disconnect(self.close)
            except (RuntimeError, TypeError):
                pass
        widget, self._widget = self._widget, None
        if widget is not None:
            try:
                widget.hide()
                widget.close()
                widget.deleteLater()
            except RuntimeError:
                pass
