"""OSM polyline QGraphicsPathItem with direction-arrow paint."""

import math

from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QBrush, QColor, QPen, QPolygonF
from PyQt6.QtGui import QPainterPath  # noqa: F401
from PyQt6.QtWidgets import QGraphicsPathItem

from open_road_editor.constants import (
    OSM_DIRECTION_ARROW_COLOR,
    OSM_DIRECTION_ARROW_LENGTH_PX,
    OSM_DIRECTION_ARROW_WIDTH_PX,
    OSM_DIRECTION_BIDIR_OFFSET_PX,
)


class OSMWayPathItem(QGraphicsPathItem):
    """OSM polyline item that also paints direction arrows on the road geometry."""

    def __init__(self, path=None, parent=None):
        super().__init__(path or QPainterPath(), parent)
        self._way_scene_coords: list[tuple[float, float]] = []
        self._show_direction_arrows = True
        self._direction_mode = 'forward'  # 'forward' | 'reverse' | 'both'

    def set_way_scene_coords(self, coords: list) -> None:
        self._way_scene_coords = [(float(x), float(y)) for x, y in coords]
        self.update()

    def set_direction_mode(self, mode: str) -> None:
        if mode not in ('forward', 'reverse', 'both'):
            mode = 'forward'
        self._direction_mode = mode
        self.update()

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        coords = self._way_scene_coords
        if not coords:
            return
        scale = painter.worldTransform().m11() or 1.0
        if self._show_direction_arrows and len(coords) >= 2:
            half_len = (OSM_DIRECTION_ARROW_LENGTH_PX / scale) * 0.5
            half_w = (OSM_DIRECTION_ARROW_WIDTH_PX / scale) * 0.5
            color = QColor(self.pen().color())
            if not color.isValid():
                color = QColor(OSM_DIRECTION_ARROW_COLOR)
            color.setAlpha(OSM_DIRECTION_ARROW_COLOR.alpha())
            painter.save()
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            painter.setBrush(QBrush(color))
            for i in range(len(coords) - 1):
                x0, y0 = coords[i]
                x1, y1 = coords[i + 1]
                dx = x1 - x0
                dy = y1 - y0
                seg_len = math.hypot(dx, dy)
                if seg_len < 1e-6:
                    continue
                ux = dx / seg_len
                uy = dy / seg_len
                nx = -uy
                ny = ux
                mx = (x0 + x1) * 0.5
                my = (y0 + y1) * 0.5
                off = OSM_DIRECTION_BIDIR_OFFSET_PX / scale

                def _draw_arrow(dir_sign: float, normal_sign: float) -> None:
                    cx = mx + nx * off * normal_sign
                    cy = my + ny * off * normal_sign
                    tip = QPointF(cx + ux * half_len * dir_sign, cy + uy * half_len * dir_sign)
                    left = QPointF(
                        cx - ux * half_len * dir_sign + nx * half_w,
                        cy - uy * half_len * dir_sign + ny * half_w,
                    )
                    right = QPointF(
                        cx - ux * half_len * dir_sign - nx * half_w,
                        cy - uy * half_len * dir_sign - ny * half_w,
                    )
                    painter.drawPolygon(QPolygonF([tip, left, right]))

                if self._direction_mode == 'both':
                    _draw_arrow(1.0, 1.0)
                    _draw_arrow(-1.0, -1.0)
                elif self._direction_mode == 'reverse':
                    _draw_arrow(-1.0, 0.0)
                else:
                    _draw_arrow(1.0, 0.0)
            painter.restore()
