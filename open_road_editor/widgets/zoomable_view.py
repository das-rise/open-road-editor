"""Zoomable / pannable QGraphicsView subclass."""

import math  # noqa: F401

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QGraphicsView

from open_road_editor.constants import DEFAULT_MIN_SCALE, MAX_ZOOM_SCALE, ZOOM_IN_FACTOR

# GridItem imported here (not in __init__.py to avoid circularity)
from open_road_editor.widgets.grid_item import GridItem  # noqa: E402


class ZoomableGraphicsView(QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        # Use NoDrag so Qt never forces an open-hand cursor onto the viewport.
        # Panning is implemented manually below: arrow cursor at rest,
        # closed-hand cursor only while actively click-dragging.
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        # Enable mouse tracking so the viewport (and our eventFilter) receives
        # QEvent.Type.MouseMove even when no button is pressed.  Without this, hover
        # highlighting for world-extent edges (and XODR/OSM segments) never fires
        # because Qt only delivers MouseMove when a button is held in NoDrag mode.
        self.viewport().setMouseTracking(True)
        self.setMouseTracking(True)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.zoom_changed_cb = None  # callback(scale: float) → set by parent
        self.viewport_changed_cb = None  # callback() → fired on scroll or zoom
        self._min_scale = DEFAULT_MIN_SCALE  # updated by fit_to_window
        self._pan_active = False
        self._pan_last_pos = None

    def _fire_viewport_changed(self):
        if self.viewport_changed_cb:
            try:
                self.viewport_changed_cb()
            except RuntimeError:
                # Qt C++ objects (e.g. QTimer) may be deleted during shutdown
                pass

    def mousePressEvent(self, event):
        """Start a manual pan on left-button press (NoDrag mode)."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._pan_active = True
            self._pan_last_pos = event.pos()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            # Do NOT call super() — that would let Qt dispatch to scene items.
            # The eventFilter already handled priority cases (edge drag, OSM dots);
            # if we reach here there is nothing in the scene to interact with.
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Pan the viewport while the left button is held."""
        if self._pan_active and self._pan_last_pos is not None:
            delta = event.pos() - self._pan_last_pos
            self._pan_last_pos = event.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
            self._fire_viewport_changed()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """End a manual pan on left-button release."""
        if event.button() == Qt.MouseButton.LeftButton and self._pan_active:
            self._pan_active = False
            self._pan_last_pos = None
            self.viewport().unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        scene = self.scene()
        if scene:
            for item in scene.items():
                if isinstance(item, GridItem):
                    item.update()
                    break
        self._fire_viewport_changed()

    def wheelEvent(self, event):
        zoom_in_factor = ZOOM_IN_FACTOR
        zoom_out_factor = 1 / zoom_in_factor
        current_scale = self.transform().m11()

        if event.angleDelta().y() > 0:
            if current_scale > MAX_ZOOM_SCALE:
                return
            zoom_factor = zoom_in_factor
        else:
            if current_scale <= self._min_scale:
                return
            # Don't zoom below the fit-to-window scale
            new_scale = current_scale * zoom_out_factor
            if new_scale < self._min_scale:
                zoom_factor = self._min_scale / current_scale
            else:
                zoom_factor = zoom_out_factor

        self.scale(zoom_factor, zoom_factor)
        new_scale = self.transform().m11()
        if self.zoom_changed_cb:
            self.zoom_changed_cb(new_scale)
        scene = self.scene()
        if scene:
            for item in scene.items():
                if isinstance(item, GridItem):
                    item.update()
                    break
        self._fire_viewport_changed()
