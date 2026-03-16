"""Coordinate-grid overlay QGraphicsItem."""
import math

from PyQt6.QtCore import Qt, QPointF, QRectF
from PyQt6.QtGui import QFont, QPen
from PyQt6.QtWidgets import QGraphicsItem

from open_road_editor.constants import (
    DEFAULT_GRID_COLOR,
    DEFAULT_GRID_LABEL_DIGITS,
    GRID_AXIS_COLOR,
    GRID_AXIS_PEN_WIDTH,
    GRID_CROSS_COLOR,
    GRID_CROSS_PEN_WIDTH,
    GRID_CROSS_RADIUS_PX,
    GRID_FONT_FAMILY,
    GRID_LABEL_PAD_X_H,
    GRID_LABEL_PAD_X_V,
    GRID_LABEL_PAD_Y_H,
    GRID_LABEL_PAD_Y_V,
    GRID_MAX_SPACING_PX,
    GRID_MIN_SPACING_PX,
    GRID_TARGET_SPACING_PX,
    GRID_ZOOM_SPACING_EXPONENT,
    MIN_FONT_SIZE,
    Z_GRID,
)


class GridItem(QGraphicsItem):
    def __init__(self, mpp, world_offset, rect, max_grid_lines=10):
        super().__init__()
        self.mpp = mpp
        self.world_offset = world_offset
        self.rect = rect
        self.max_grid_lines = max_grid_lines
        self.line_thickness = 2
        self.grid_color = DEFAULT_GRID_COLOR
        self.label_size = 12
        self.label_sig_digits = DEFAULT_GRID_LABEL_DIGITS
        self._last_spacing_meters: float | None = None
        self.setZValue(Z_GRID)
        # The grid is a pure visual overlay — it must never accept mouse or
        # hover events, otherwise Qt routes scene-hover dispatch through it
        # (it has the highest Z and a very large boundingRect) and resets the
        # viewport cursor, fighting our world-extent edge hover/drag cursors.
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)

    def boundingRect(self):
        # Return a very large rect so Qt does not clip grid painting
        # at the world-extent boundary — the grid should fill the viewport.
        return QRectF(-1e7, -1e7, 2e7, 2e7)

    def paint(self, painter, option, widget):
        transform = painter.worldTransform()
        scale = transform.m11()

        # Calculate visible area
        viewport_rect = painter.viewport()
        visible_rect = transform.inverted()[0].mapRect(QRectF(viewport_rect))

        # Determine grid spacing based on zoom
        # Target ~400 pixels spacing on screen (Further reduced density)
        pixels_per_meter = scale / self.mpp
        if pixels_per_meter <= 0:
            return

        spacing_meters = self._choose_grid_spacing_meters(pixels_per_meter)

        # Pen setup
        pen = QPen(self.grid_color)
        pen.setWidthF(float(self.line_thickness))
        pen.setCosmetic(True)
        painter.setPen(pen)

        # NOTE: The world-extent bounding box is drawn by separate
        # QGraphicsLineItem edges (_world_extent_edge_items) so it stays
        # visible and interactive independently of the grid layer.

        # Font setup
        font = QFont(GRID_FONT_FAMILY)
        font_size = max(float(MIN_FONT_SIZE), self.label_size)
        font.setPointSizeF(font_size)
        painter.setFont(font)

        # Use the full visible area — do NOT clamp to self.rect so the
        # grid extends across the entire viewport, not just the world extent.
        left = visible_rect.left()
        right = visible_rect.right()
        top = visible_rect.top()
        bottom = visible_rect.bottom()

        start_k = math.ceil((left * self.mpp + self.world_offset[0]) / spacing_meters)
        end_k = math.floor((right * self.mpp + self.world_offset[0]) / spacing_meters)

        # Formatting helper
        def fmt(val):
            digits = self.label_sig_digits
            if abs(val) >= 1000:
                return f"{val:.{digits}g}m"
            return f"{round(val, digits):g}m"

        text_pen = QPen(self.grid_color)
        painter.setPen(text_pen)

        # Dynamic padding based on font size
        x_pad_v = (
            font_size * GRID_LABEL_PAD_X_V
        )  # Vertical padding from line end (rotated X)
        x_pad_h = (
            font_size * GRID_LABEL_PAD_X_H
        )  # Horizontal padding from line (rotated Y)
        y_pad_h = font_size * GRID_LABEL_PAD_Y_H  # Horizontal padding from line start
        y_pad_v = font_size * GRID_LABEL_PAD_Y_V  # Vertical padding above line

        for k in range(start_k, end_k + 1):
            world_x = k * spacing_meters
            px = (world_x - self.world_offset[0]) / self.mpp

            if left <= px <= right:
                painter.drawLine(QPointF(px, top), QPointF(px, bottom))

                # Draw X labels at the BOTTOM
                # Align vertically to the left of the line
                text = fmt(world_x)

                painter.save()
                # Try to position near the bottom of the visible view
                painter.translate(px, bottom)
                painter.scale(1.0 / scale, 1.0 / scale)
                painter.rotate(-90)  # Vertical text running up

                # Draw text slightly offset from the line
                # Dynamic padding depending on font size
                painter.drawText(QPointF(x_pad_v, x_pad_h), text)
                painter.restore()

        # Horizontal lines (Y const)
        start_k_y = math.ceil((top * self.mpp + self.world_offset[1]) / spacing_meters)
        end_k_y = math.floor(
            (bottom * self.mpp + self.world_offset[1]) / spacing_meters
        )

        for k in range(start_k_y, end_k_y + 1):
            world_y = k * spacing_meters
            py = (world_y - self.world_offset[1]) / self.mpp

            if top <= py <= bottom:
                painter.drawLine(QPointF(left, py), QPointF(right, py))

                # Y labels: keep as is, maybe ensure visibility
                painter.save()
                painter.translate(left, py)
                painter.scale(1.0 / scale, 1.0 / scale)

                # Dynamic padding depending on font size
                painter.drawText(QPointF(y_pad_h, y_pad_v), fmt(world_y))
                painter.restore()

        # ── World-origin axes (red) ───────────────────────────────────────
        # Origin in pixel space: world (0,0) → (ox, oy)
        ox = -self.world_offset[0] / self.mpp
        oy = -self.world_offset[1] / self.mpp

        axis_pen = QPen(GRID_AXIS_COLOR)
        axis_pen.setWidthF(GRID_AXIS_PEN_WIDTH)
        axis_pen.setCosmetic(True)
        axis_pen.setStyle(Qt.PenStyle.SolidLine)

        # X-axis: vertical line at ox (constant world X = 0), running top→bottom
        if left <= ox <= right:
            painter.setPen(axis_pen)
            painter.drawLine(QPointF(ox, top), QPointF(ox, bottom))

        # Y-axis: horizontal line at oy (constant world Y = 0), running left→right
        if top <= oy <= bottom:
            painter.setPen(axis_pen)
            painter.drawLine(QPointF(left, oy), QPointF(right, oy))

        # If both axes are visible, draw a small cross at the exact origin
        if left <= ox <= right and top <= oy <= bottom:
            cross_r = GRID_CROSS_RADIUS_PX / scale  # fixed pixel radius
            cross_pen = QPen(GRID_CROSS_COLOR)
            cross_pen.setWidthF(GRID_CROSS_PEN_WIDTH)
            cross_pen.setCosmetic(True)
            painter.setPen(cross_pen)
            painter.drawLine(QPointF(ox - cross_r, oy), QPointF(ox + cross_r, oy))
            painter.drawLine(QPointF(ox, oy - cross_r), QPointF(ox, oy + cross_r))

    def _choose_grid_spacing_meters(self, pixels_per_meter: float) -> float:
        # Sub-linear response vs zoom keeps grid from getting too dense
        # when zooming in heavily (closer to ControlTower behavior).
        target_m = GRID_TARGET_SPACING_PX / (
            pixels_per_meter**GRID_ZOOM_SPACING_EXPONENT
        )
        exponent = math.floor(math.log10(max(target_m, 1e-9)))
        candidates = []
        for e in range(exponent - 2, exponent + 3):
            base = 10**e
            for m in (1.0, 2.0, 5.0):
                s = m * base
                if s > 0.0:
                    candidates.append(s)
        candidates = sorted(set(candidates))

        def _px(spacing_m: float) -> float:
            return spacing_m * pixels_per_meter

        in_band = [
            s
            for s in candidates
            if GRID_MIN_SPACING_PX <= _px(s) <= GRID_MAX_SPACING_PX
        ]
        pool = in_band if in_band else candidates
        spacing = min(pool, key=lambda s: abs(_px(s) - GRID_TARGET_SPACING_PX))
        spacing = max(spacing, 1.0)

        prev = self._last_spacing_meters
        if prev is not None and prev > 0.0:
            ratio = spacing / prev
            if 0.92 <= ratio <= 1.08:
                spacing = prev
        self._last_spacing_meters = spacing
        return spacing


