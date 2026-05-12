"""OpenDRIVE display, editing and OSM→XODR conversion mixin."""

import copy
import math
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
import threading

# osm_to_xodr uses loguru with a default stderr sink — suppress it.
from loguru import logger as _osm_to_xodr_logger
from osm_to_xodr.config import AppSettings, NetconvertSettings
from osm_to_xodr.converter import convert_osm_to_xodr

_osm_to_xodr_logger.disable("osm_to_xodr")

from PIL import Image
from PyQt6.QtCore import (
    QPointF,
    Qt,
    QTimer,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsItemGroup,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QStyle,
    QVBoxLayout,
)

from open_road_editor.constants import *  # noqa: F401,F403
from open_road_editor.constants import _XODR_LANE_COLOR_DEFAULT, _XODR_LANE_COLORS, XODR_MARKING_LINE_WIDTH_PX  # noqa: F401
from orbit.gui.graphics.signal_graphics import (
    create_signal_pixmap,
    create_orientation_indicator,
)
from orbit.models.signal import SignalType

opendrive_renderer_bindings_py = importlib.import_module(
    "open_road_editor.viewer._xodr_renderer"
)


class _XodrMixin:
    """Mixin — see viewer/main.py for the assembled class."""

    @staticmethod
    def _xodr_visual_angle_from_heading(hdg_radians: float) -> float:
        """Convert a road/lane heading in radians to viewer display angle."""
        return math.degrees(float(hdg_radians))

    @staticmethod
    def _xodr_pixmap_rotation_from_visual_angle(visual_angle: float) -> float:
        """Convert a desired display angle to QGraphicsItem rotation degrees.

        The placeholder give-way pixmap points downward by default, so rotate it
        until its apex faces *against* the travel direction (facing the driver).
        """
        # Previously was 270.0 (pointing along travel), now 90.0 (pointing against).
        rotation = 90.0 - float(visual_angle)
        while rotation < 0.0:
            rotation += 360.0
        while rotation >= 360.0:
            rotation -= 360.0
        return rotation

    def pil_to_qpixmap(self, im):
        if im is None:
            return QPixmap()
        im = im.convert("RGBA")
        data = im.tobytes("raw", "RGBA")
        qim = QImage(data, im.size[0], im.size[1], QImage.Format.Format_RGBA8888)
        return QPixmap.fromImage(qim)

    def _selected_xodr_centerline_marking(self) -> str:
        combo = getattr(self, "combo_xodr_centerline_marking", None)
        if combo is not None:
            data = combo.currentData()
            if data:
                return str(data)
        return str(
            self.settings.value("opendrive_centerline_marking", "WhiteBroken")
            or "WhiteBroken"
        )

    def _on_xodr_centerline_marking_changed(self, _index: int = 0) -> None:
        style = self._selected_xodr_centerline_marking()
        self.settings.setValue("opendrive_centerline_marking", style)
        if (
            self.xodr_path
            and getattr(self, "check_opendrive", None)
            and self.check_opendrive.isChecked()
        ):
            self._show_project_status("OpenDRIVE centerline style updated")
            self.refresh_opendrive()

    def _on_xodr_signals_ready(self, signals) -> None:
        """Main-thread slot: render signals in the scene."""
        self._clear_xodr_signal_items()

        if not signals or not self.map_ctx:
            return

        min_x = self.map_ctx.world_bounds[0]
        min_y = self.map_ctx.world_bounds[2]
        mpp = self.map_ctx.mpp

        for sig in signals:
            scene_x = (sig["x"] - min_x) / mpp
            scene_y = (sig["y"] - min_y) / mpp

            # Map string type to SignalType enum if possible
            stype = SignalType.CUSTOM
            try:
                # Basic mapping for known types
                if "speed" in sig["type"].lower():
                    stype = SignalType.SPEED_LIMIT
                elif "yield" in sig["type"].lower() or "206" in sig["type"]:
                    stype = SignalType.GIVE_WAY
                elif "stop" in sig["type"].lower() or "201" in sig["type"]:
                    stype = SignalType.STOP
            except:
                pass

            value = None
            if stype == SignalType.SPEED_LIMIT:
                try:
                    value = int(sig["value"])
                except:
                    value = 50

            pixmap = create_signal_pixmap(stype, value, size=32)

            # Use QGraphicsItemGroup style similar to Orbit
            group = QGraphicsItemGroup()

            pix_item = QGraphicsPixmapItem(pixmap)
            pix_item.setOffset(-pixmap.width() / 2, -pixmap.height() / 2)

            # The signal's visual heading is fully encoded in hOffset
            # (including any orientation-based 180° flip baked in by the
            # postprocessor), so we just add it to the road heading.
            travel_hdg = self._xodr_visual_angle_from_heading(sig.get("hdg", 0.0))
            signal_visual_angle = travel_hdg + math.degrees(
                float(sig.get("h_offset", 0.0) or 0.0)
            )

            if stype == SignalType.GIVE_WAY:
                pix_item.setRotation(
                    self._xodr_pixmap_rotation_from_visual_angle(signal_visual_angle)
                )
            group.addToGroup(pix_item)

            # Orientation indicator
            # signal_visual_angle already accounts for road heading and orientation (+/-).
            # It represents the travel direction for which the signal is valid.
            # We add a 180 degree offset so the arrow points towards the driver / against travel,
            # matching the requested visual style.
            vis_angle = (signal_visual_angle + 180.0) % 360.0

            path = create_orientation_indicator(vis_angle, length=20)
            path_item = QGraphicsPathItem(path)
            path_item.setPen(QPen(QColor(0, 0, 200, 180), 2))
            group.addToGroup(path_item)

            group.setPos(scene_x, scene_y)
            group.setZValue(3.0)

            self.scene.addItem(group)
            self._xodr_vector_signal_items.append(group)

        # Ensure signals pick up current layer/object visibility settings
        self._apply_opendrive_layer_style()

    def toggle_sidebar(self):
        self._sidebar_visible = not self._sidebar_visible
        if self._sidebar_visible:
            # Restore sidebar: grow right pane back to saved width
            total = self.splitter.width()
            saved = getattr(self, "_sidebar_saved_width", SIDEBAR_DEFAULT_SAVED_WIDTH)
            map_w = max(
                100, total - saved - TOGGLE_STRIP_WIDTH
            )  # toggle strip + handle
            self.sidebar.setVisible(True)
            self.splitter.setSizes([map_w, saved + TOGGLE_STRIP_WIDTH])
            self.toggle_btn.setText("▶")
        else:
            # Save current right-pane width then collapse to toggle strip only
            sizes = self.splitter.sizes()
            self._sidebar_saved_width = (
                max(SIDEBAR_MIN_WIDTH, sizes[1] - TOGGLE_STRIP_WIDTH)
                if len(sizes) > 1
                else SIDEBAR_DEFAULT_SAVED_WIDTH
            )
            self.sidebar.setVisible(False)
            self.splitter.setSizes(
                [self.splitter.width() - TOGGLE_STRIP_WIDTH, TOGGLE_STRIP_WIDTH]
            )
            self.toggle_btn.setText("◀")

    def refresh_opendrive(self):
        if not self.xodr_path:
            self.opendrive_loading = False
            if self.btn_browse_xodr:
                self.btn_browse_xodr.setIcon(
                    self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
                )
            if not self.check_opendrive.isChecked():
                self.lbl_opendrive_status.setText("")
            return

        self.opendrive_loading = True
        self.lbl_opendrive_status.setText("Loading...")
        self.spinner_timer.start(SPINNER_TIMER_INTERVAL_MS)
        self.btn_browse_xodr.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload)
        )

        def fetch():
            # Vector path: build resolution-independent QPainterPath items
            self.fetch_local_opendrive_vector(self.xodr_path)

        threading.Thread(target=fetch, daemon=True).start()

    def _on_redraw_lane_markings(self) -> None:
        """Recompute exterior road-boundary marks for the current xodr file and redraw."""
        if not self.xodr_path:
            return

        xodr_path = self.xodr_path
        centerline_style = self._selected_xodr_centerline_marking()
        self._show_project_status("Redrawing lane markings…")
        self.lbl_opendrive_status.setText("Redrawing…")
        btn = getattr(self, "btn_redraw_lane_markings", None)
        if btn:
            btn.setEnabled(False)

        def run():
            try:
                renderer = opendrive_renderer_bindings_py.OpenDriveRenderer()
                if renderer.load_map(xodr_path):
                    renderer.rewrite_exterior_road_marks(
                        xodr_path,
                        centerline_style=centerline_style,
                    )
            except Exception as exc:
                print(f"_on_redraw_lane_markings: rewrite failed: {exc}")
            # Reload the (possibly modified) file in the viewer
            self.fetch_local_opendrive_vector(xodr_path)

        def _re_enable():
            if btn:
                btn.setEnabled(True)
            self.lbl_opendrive_status.setText("Loaded")
            self._xodr_dirty = True

        # Re-enable the button once polygons are ready (one-shot connection)
        self.xodr_polygons_ready.connect(_re_enable, Qt.ConnectionType.SingleShotConnection)

        threading.Thread(target=run, daemon=True).start()

    def fetch_local_opendrive(self, xodr_path, on_progress):
        # Local rendering logic
        try:
            renderer = opendrive_renderer_bindings_py.OpenDriveRenderer()
            if not renderer.load_map(xodr_path):
                print("Failed to load map locally")
                on_progress(None, -1, -1)
                return

            min_x = self.map_ctx.world_bounds[0]
            max_x = self.map_ctx.world_bounds[1]
            min_y = self.map_ctx.world_bounds[2]
            max_y = self.map_ctx.world_bounds[3]
            width_meters = max_x - min_x
            height_meters = max_y - min_y

            if width_meters <= 0 or height_meters <= 0:
                print("Invalid map_ctx dimensions for local render")
                on_progress(None, -1, -1)
                return

            # Use the MPP calculated from arguments/metadata to match server resolution
            render_mpp = self.map_ctx.mpp

            # Check for reasonable image size to prevent memory exhaustion
            w_px = int(width_meters / render_mpp)
            h_px = int(height_meters / render_mpp)

            # Warn if image is going to be massive
            if w_px * h_px > MAX_RENDER_DIMENSION * MAX_RENDER_DIMENSION:
                print(
                    f"Warning: Rendered map will be huge ({w_px}x{h_px}). Capping resolution."
                )
                max_dim = MAX_RENDER_DIMENSION
                scale_factor = max(w_px / max_dim, h_px / max_dim)
                render_mpp *= scale_factor
                w_px = int(width_meters / render_mpp)
                h_px = int(height_meters / render_mpp)

            # For the local renderer, we need to pass arguments.
            # We must check signature of opendrive_renderer_bindings_py.OpenDriveRenderer.render
            # Usually: min_x, min_y, width, height, mpp, r, g, b, signals, objects...
            # The current bindings (checked via grep) seem to take:
            # render(min_x, min_y, width_px, height_px, mpp, ...)

            # Bindings often expect float for geometry and int for dimensions
            road_layer_arr = renderer.render(
                min_x,
                min_y,
                w_px,
                h_px,
                render_mpp,
                46,
                52,
                54,  # road color
                True,
                True,  # signals, objects
            )

            # Convert to PIL
            if road_layer_arr is None:
                print("Renderer returned None")
                on_progress(None, -1, -1)
                return

            # Array shape is (height, width, 4) - BGRA usually from libOpenDRIVE?
            # Actually let's assume RGBA from typical bindings usage or check
            # In server.py: Image.fromarray(road_layer_arr, 'RGBA')
            img = Image.fromarray(road_layer_arr, "RGBA")

            # Resize to match full mapper bounds if needed, but since we are replacing tiles
            # we just show this one big image.
            # However, the OpenDriveViewer expects tiles or handles one big image?
            # on_opendrive_refreshed takes (image, count, total)
            # and sets the pixmap.
            # If we pass one big image, it will set it to the item.
            # BUT the item needs to be positioned correctly if it's not covering 0,0 to W,H in scene coords?
            # The scene is sized to `mapper.width_in_pixels` which is based on `min_meters_per_pixel` (zoom=max).
            # If our local render has different resolution, we must scale it or adjust item transform.

            # Creating a QPixmap from this image and letting the viewer handle it.
            # The viewer code: self.opendrive_item.setPixmap(...)
            # The item functions as background.
            # If resolution differs from scene resolution, it will look small/large.
            # We must scale the image to match `mapper.width_in_pixels` x `height_in_pixels`
            # or set item scale.

            target_w = self.map_ctx.width_in_pixels
            target_h = self.map_ctx.height_in_pixels
            if w_px != target_w or h_px != target_h:
                img = img.resize((target_w, target_h), Image.NEAREST)

            on_progress(img, 1, 1)
            on_progress(None, -1, -1)  # Signal complete

        except Exception as e:
            print(f"Local rendering failed: {e}")
            on_progress(None, -1, -1)

    # ------------------------------------------------------------------
    # Vector OpenDRIVE rendering (QPainterPath-based)
    # ------------------------------------------------------------------

    def fetch_local_opendrive_vector(self, xodr_path: str) -> None:
        """Background thread: load XODR and emit lane polygons (no rasterisation)."""
        centerline_style = self._selected_xodr_centerline_marking()

        def run():
            try:
                renderer = opendrive_renderer_bindings_py.OpenDriveRenderer()
                if not renderer.load_map(xodr_path):
                    print("fetch_local_opendrive_vector: failed to load map")
                    self.opendrive_refreshed.emit(None, -1, -1)
                    return
                # s_step=1.0 m gives smooth curves without excessive memory use
                polygons = renderer.get_lane_polygons(XODR_POLYGON_STEP_M)
                # Compute exterior boundary polylines (road marking lines).
                # These replace per-polygon seam markings with a clean outer contour.
                try:
                    marking_polylines = renderer.get_road_marking_polylines(
                        s_step=XODR_POLYGON_STEP_M
                    )
                except Exception as _exc:
                    print(f"get_road_marking_polylines warning: {_exc}")
                    marking_polylines = []
                try:
                    center_marking_polylines = (
                        renderer.get_bidirectional_center_marking_polylines(
                            centerline_style=centerline_style,
                            s_step=XODR_POLYGON_STEP_M,
                        )
                    )
                except Exception as _exc:
                    print(f"get_bidirectional_center_marking_polylines warning: {_exc}")
                    center_marking_polylines = []
                signals = renderer.get_signals()
                self.xodr_polygons_ready.emit(
                    {
                        "polygons": polygons,
                        "marking_polylines": marking_polylines,
                        "center_marking_polylines": center_marking_polylines,
                    }
                )
                self.xodr_signals_ready.emit(signals)
            except Exception as exc:
                print(f"fetch_local_opendrive_vector error: {exc}")
                self.opendrive_refreshed.emit(None, -1, -1)

        threading.Thread(target=run, daemon=True).start()

    def _on_xodr_polygons_ready(self, polygons) -> None:
        """Main-thread slot: convert LanePolygons to QGraphicsPathItems in the scene."""
        self._clear_xodr_vector_items(clear_signals=False)

        marking_polylines = []
        center_marking_polylines = []
        if isinstance(polygons, dict):
            marking_polylines = list(polygons.get("marking_polylines") or [])
            center_marking_polylines = list(
                polygons.get("center_marking_polylines") or []
            )
            polygons = list(polygons.get("polygons") or [])

        if not polygons or not self.map_ctx:
            self.opendrive_refreshed.emit(None, -1, -1)
            return

        min_x = self.map_ctx.world_bounds[0]
        min_y = self.map_ctx.world_bounds[2]
        mpp = self.map_ctx.mpp

        path_items: list = []
        self._xodr_lane_points_scene.clear()
        records: list = []
        for poly in polygons:
            pts = list(getattr(poly, "points", []) or [])
            if len(pts) < 3:
                continue
            lane_key = str(getattr(poly, "lane_key", "") or "")
            predecessor_key = str(getattr(poly, "predecessor_key", "") or "")
            successor_key = str(getattr(poly, "successor_key", "") or "")
            lane_type = str(getattr(poly, "lane_type", "") or "")
            scene_pts = [((wx - min_x) / mpp, (wy - min_y) / mpp) for wx, wy in pts]
            records.append(
                {
                    "lane_key": lane_key,
                    "topology_lane_key": lane_key,
                    "predecessor_key": predecessor_key,
                    "successor_key": successor_key,
                    "lane_type": lane_type,
                    "scene_points": scene_pts,
                    "world_points": pts,
                    "synthetic": False,
                }
            )

        lane_key_counts: dict[str, int] = {}
        for rec in records:
            topo_key = str(rec.get("topology_lane_key") or "")
            if topo_key:
                lane_key_counts[topo_key] = lane_key_counts.get(topo_key, 0) + 1

        seen_instance_keys: set[str] = set()
        for rec in records:
            topo_key = str(rec.get("topology_lane_key") or "")
            instance_key = topo_key
            if topo_key and lane_key_counts.get(topo_key, 0) > 1:
                instance_key = self._xodr_duplicate_instance_lane_key(
                    topo_key, rec.get("world_points") or []
                )
                suffix = 2
                unique_instance_key = instance_key
                while unique_instance_key in seen_instance_keys:
                    unique_instance_key = f"{instance_key}:{suffix}"
                    suffix += 1
                instance_key = unique_instance_key
            rec["lane_key"] = instance_key
            if instance_key:
                seen_instance_keys.add(instance_key)

        for rec in records:
            self._append_xodr_record_item(path_items, rec)

        self._rebuild_xodr_connectivity_maps()

        if not path_items:
            self.opendrive_refreshed.emit(None, -1, -1)
            return

        group = self.scene.createItemGroup(path_items)
        group.setZValue(self.opendrive_item.zValue())
        self._xodr_vector_group = group
        self._xodr_is_vector = True

        # Draw exterior road-boundary marking lines on top of lane polygons.
        # These replace the per-polygon seam pen as the visual lane marking,
        # ensuring no lines appear inside roundabout or junction patch areas.
        self._build_road_marking_items(marking_polylines, min_x, min_y, mpp, group)
        self._build_center_marking_items(
            center_marking_polylines, min_x, min_y, mpp, group
        )

        self._apply_opendrive_layer_style()

        # Trigger the normal "loading complete" flow
        self.opendrive_refreshed.emit(None, -1, -1)

    @staticmethod
    def _xodr_pen_from_brush(brush: QBrush) -> QPen:
        c = QColor(brush.color())
        # Keep fill translucency but make edge coverage nearly opaque to hide
        # residual raster seams between adjacent lane polygons.
        c.setAlpha(max(c.alpha(), 230))
        pen = QPen(c)
        pen.setCosmetic(True)
        pen.setWidthF(XODR_SEAM_PEN_WIDTH_PX)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        return pen

    def _build_road_marking_items(
        self,
        marking_polylines: list,
        min_x: float,
        min_y: float,
        mpp: float,
        group,
    ) -> None:
        """Draw exterior road-boundary marking lines as white cosmetic paths.

        Each polyline in *marking_polylines* is a list of world-coordinate
        (x, y) tuples representing the exterior contour of the road-surface
        union.  Items are added as children of *group* so they inherit the
        group's transform and visibility.
        """
        marking_pen = QPen(QColor(255, 255, 255, 220))
        marking_pen.setCosmetic(True)
        marking_pen.setWidthF(XODR_MARKING_LINE_WIDTH_PX)
        marking_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        marking_pen.setCapStyle(Qt.PenCapStyle.RoundCap)

        for polyline in marking_polylines:
            if len(polyline) < 2:
                continue
            path = QPainterPath()
            sx0, sy0 = ((polyline[0][0] - min_x) / mpp, (polyline[0][1] - min_y) / mpp)
            path.moveTo(sx0, sy0)
            for wx, wy in polyline[1:]:
                path.lineTo((wx - min_x) / mpp, (wy - min_y) / mpp)
            item = QGraphicsPathItem(path)
            item.setPen(marking_pen)
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            item.setZValue(0.5)  # above lane fills, below signals
            group.addToGroup(item)

    def _build_center_marking_items(
        self,
        center_marking_polylines: list,
        min_x: float,
        min_y: float,
        mpp: float,
        group,
    ) -> None:
        """Draw bidirectional centerline markings on top of lane polygons."""
        pens = {
            "white": QPen(QColor(255, 255, 255, 230)),
            "yellow": QPen(QColor(255, 214, 0, 230)),
        }
        for pen in pens.values():
            pen.setCosmetic(True)
            pen.setWidthF(XODR_MARKING_LINE_WIDTH_PX)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)

        for record in center_marking_polylines:
            if isinstance(record, dict):
                polyline = list(record.get("points") or [])
                color_name = str(record.get("color") or "white").lower()
            else:
                polyline = list(record or [])
                color_name = "white"
            if len(polyline) < 2:
                continue

            path = QPainterPath()
            sx0, sy0 = (
                (polyline[0][0] - min_x) / mpp,
                (polyline[0][1] - min_y) / mpp,
            )
            path.moveTo(sx0, sy0)
            for wx, wy in polyline[1:]:
                path.lineTo((wx - min_x) / mpp, (wy - min_y) / mpp)

            item = QGraphicsPathItem(path)
            item.setPen(pens.get(color_name, pens["white"]))
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            item.setZValue(0.55)
            group.addToGroup(item)

    def _set_xodr_item_fill(self, item, brush: QBrush) -> None:
        item.setBrush(brush)
        item.setPen(self._xodr_pen_from_brush(brush))

    def _parse_lane_key_for_orbit(self, lane_key: str) -> tuple[str, str, int]:
        """Parse lane_key into (road_id, section_s_str, lane_id) for ORBIT."""
        text = str(lane_key or "").strip()
        # Drop transient slice/instance suffixes used only in viewer state.
        # Examples:
        #   road/0.0/-1::slice:0
        #   road/0.0/-1@@abc123
        #   road/0.0/-1:2
        base_text = text
        if "::slice:" in base_text:
            base_text = base_text.split("::slice:", 1)[0]
        if "@@" in base_text:
            base_text = base_text.split("@@", 1)[0]
        if ":" in base_text:
            base_text = base_text.split(":", 1)[0]

        parts = base_text.split("/")
        if len(parts) == 3:
            road_id, section_s, lane_id_str = [p.strip() for p in parts]
            if road_id and section_s and lane_id_str:
                try:
                    lane_id = int(lane_id_str)
                except (ValueError, TypeError):
                    lane_id = 0
                try:
                    section_s = f"{float(section_s):.6f}"
                except Exception:
                    pass
                return road_id, section_s, lane_id

        return text, "0.0", 0

    def _append_xodr_record_item(self, path_items: list, rec: dict) -> None:
        scene_pts = rec.get("scene_points") or []
        if len(scene_pts) < 3:
            return

        lane_key = str(rec.get("lane_key") or "")
        lane_type = str(rec.get("lane_type") or "")
        predecessor_key = str(rec.get("predecessor_key") or "")
        successor_key = str(rec.get("successor_key") or "")
        is_synthetic = bool(rec.get("synthetic"))

        base_color = _XODR_LANE_COLORS.get(lane_type, _XODR_LANE_COLOR_DEFAULT)
        brush = QBrush(QColor(*base_color))

        poly = QPolygonF([QPointF(sx, sy) for sx, sy in scene_pts])
        path = QPainterPath()
        path.addPolygon(poly)
        item = QGraphicsPathItem(path)
        item.setAcceptHoverEvents(True)

        # For road-surface lane types the exterior boundary is drawn separately
        # as explicit marking lines.  Use a no-border pen so that seam artefacts
        # (dark lines between adjacent road patches) do not appear inside
        # roundabouts or junction areas.
        from open_road_editor.viewer._xodr_renderer import _ROAD_SURFACE_TYPES
        if lane_type in _ROAD_SURFACE_TYPES:
            item.setBrush(brush)
            item.setPen(QPen(Qt.PenStyle.NoPen))
        else:
            self._set_xodr_item_fill(item, brush)

        topo_key = str(rec.get("topology_lane_key") or lane_key)
        self._xodr_item_meta[item] = (
            lane_key,
            predecessor_key,
            successor_key,
            brush,
            topo_key,
        )
        self._xodr_lane_key_to_item[lane_key] = item
        self._xodr_topology_key_to_items.setdefault(topo_key, []).append(item)

        if not is_synthetic and lane_key:
            self._xodr_lane_points_scene[lane_key] = scene_pts

        path_items.append(item)

    @staticmethod
    def _xodr_duplicate_instance_lane_key(
        base_lane_key: str, world_points: list
    ) -> str:
        if not base_lane_key:
            return ""
        sample_points = list(world_points or [])
        if len(sample_points) > 8:
            sample_points = sample_points[:4] + sample_points[-4:]
        payload = "|".join(f"{float(x):.3f},{float(y):.3f}" for x, y in sample_points)
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
        return f"{base_lane_key}@@{digest}"

    @staticmethod
    def _xodr_meta_topology_key(meta) -> str:
        if not meta:
            return ""
        if len(meta) >= 5:
            return str(meta[4] or "")
        return str(meta[0] or "")

    def _xodr_items_for_topology_key(self, topology_key: str) -> list:
        topology_key = str(topology_key or "")
        if not topology_key:
            return []
        return list(self._xodr_topology_key_to_items.get(topology_key) or [])

    def _xodr_item_for_lane_key_or_topology_key(self, lane_key: str):
        lane_key = str(lane_key or "")
        if not lane_key:
            return None
        item = self._xodr_lane_key_to_item.get(lane_key)
        if item is not None:
            return item
        cands = self._xodr_items_for_topology_key(lane_key)
        if len(cands) == 1:
            return cands[0]
        return None

    def _rebuild_xodr_connectivity_maps(self) -> None:
        self._xodr_pred_back.clear()
        self._xodr_succ_back.clear()
        for item, meta in self._xodr_item_meta.items():
            lane_key = str(meta[0] or "")
            pred_key = str(meta[1] or "")
            succ_key = str(meta[2] or "")
            if not lane_key or lane_key not in self._xodr_lane_key_to_item:
                continue
            if pred_key:
                self._xodr_pred_back.setdefault(pred_key, []).append(item)
            if succ_key:
                self._xodr_succ_back.setdefault(succ_key, []).append(item)

    def _clear_xodr_vector_items(self, clear_signals: bool = True) -> None:
        """Remove the vector OpenDRIVE item group from the scene, if present."""
        if self._xodr_vector_group is not None:
            # Remove every child path item from the scene explicitly *before*
            # touching the group.  Qt's destroyItemGroup() reparents children to
            # the scene as top-level items, restoring their own visible=True state
            # and leaving permanent orphan items on screen.  Removing children
            # directly avoids that leak.
            for item in list(self._xodr_vector_group.childItems()):
                self.scene.removeItem(item)
            self.scene.removeItem(self._xodr_vector_group)
            self._xodr_vector_group = None
        self._xodr_item_meta.clear()
        self._xodr_lane_key_to_item.clear()
        self._xodr_topology_key_to_items.clear()
        self._xodr_pred_back.clear()
        self._xodr_succ_back.clear()
        self._xodr_lane_points_scene.clear()
        self._clear_xodr_signal_items()
        self._xodr_is_vector = False

    def _clear_xodr_signal_items(self) -> None:
        """Remove vector signal markers from the scene."""
        items = getattr(self, "_xodr_vector_signal_items", [])
        for item in items:
            try:
                self.scene.removeItem(item)
            except:
                pass
        items.clear()

    # ------------------------------------------------------------------
    # OSM file layer
    # ------------------------------------------------------------------

    def browse_osm(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select OSM File",
            "",
            "OpenStreetMap Files (*.osm *.xml);;All Files (*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if file_path:
            if self.xodr_path:
                reply = QMessageBox.question(
                    self,
                    "Confirm Import",
                    "An OpenDRIVE file is already loaded. Importing OSM will clear it. Continue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.No:
                    return
                self.edit_xodr.setText("")
            self.edit_osm.setText(file_path)

    def _normalize_osm2xodr_settings(self, raw_settings) -> dict:
        merged = copy.deepcopy(DEFAULT_OSM2XODR_SETTINGS)
        parsed = None
        if isinstance(raw_settings, dict):
            parsed = raw_settings
        elif isinstance(raw_settings, str):
            try:
                obj = json.loads(raw_settings)
                if isinstance(obj, dict):
                    parsed = obj
            except Exception:
                parsed = None
        if isinstance(parsed, dict):
            for section, options in OSM2XODR_SCHEMA:
                src_section = parsed.get(section)
                if not isinstance(src_section, dict):
                    continue
                for key, _, _ in options:
                    if key in src_section:
                        merged[section][key] = str(src_section[key]).strip()

        # Migrate the historic broken default used by the viewer. This preserves
        # explicitly customized values while preventing old projects/settings
        # from silently reintroducing the roundabout sign regression.
        junctions = merged.get("junctions", {})
        if str(junctions.get("junction_join_dist", "")).strip() in {"2", "2.0"}:
            junctions["junction_join_dist"] = "10.0"
        return merged

    @staticmethod
    def _as_bool_string(value) -> str:
        return (
            "true"
            if str(value).strip().lower() in ("1", "true", "yes", "on")
            else "false"
        )

    def _build_netconvert_settings(self) -> NetconvertSettings:
        """Build a NetconvertSettings instance from the current UI settings dict."""
        self._osm2xodr_settings = self._normalize_osm2xodr_settings(
            self._osm2xodr_settings
        )
        flat: dict = {}
        for section, options in OSM2XODR_SCHEMA:
            for key, field_type, default in options:
                raw = self._osm2xodr_settings.get(section, {}).get(key, default)
                try:
                    if field_type == "bool":
                        flat[key] = str(raw).strip().lower() in (
                            "1",
                            "true",
                            "yes",
                            "on",
                        )
                    elif field_type == "float":
                        flat[key] = float(raw)
                    elif field_type == "int":
                        flat[key] = int(float(raw))
                    else:
                        flat[key] = str(raw)
                except Exception:
                    pass  # leave field at NetconvertSettings default
        return NetconvertSettings(**flat)

    def _call_netconvert_conversion(
        self, osm_path: str, xodr_path: str
    ) -> tuple[bool, str]:
        """Run netconvert via osm-to-xodr's subprocess wrapper using our scene projection.

        Returns (success, error_message).
        """
        lat = self.spin_origin_lat.value()
        lon = self.spin_origin_lon.value()
        proj_str = f"+proj=tmerc +lat_0={lat:.6f} +lon_0={lon:.6f}"
        nc = self._build_netconvert_settings()
        osm_file = Path(osm_path).resolve()
        output_file = Path(xodr_path).resolve()

        # Use the unified converter which includes OSMSignalExtractor and signal merging
        app_settings = AppSettings(
            verbose=True,
            keep_intermediate=True,
        )

        # Override netconvert settings with current ORE values
        # Note: ORE uses its own projection string for better accuracy in the scene
        # We'll stick to ORE's direct call if we need custom projection,
        # but let's see if we can use the converter with these overrides.

        result = convert_osm_to_xodr(
            osm_file,
            output_file,
            netconvert_settings=nc,
            app_settings=app_settings,
            projection=proj_str,
        )

        if not result.success:
            return False, result.error or "Unknown error"

        return True, ""

    def _open_osm2xodr_settings_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("OSM to OpenDRIVE Conversion Settings")
        dialog.resize(760, 520)
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel(
                "These settings are passed to osm-to-xodr (SUMO netconvert) for OSM import."
            )
        )
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        editors = {}
        for section, options in OSM2XODR_SCHEMA:
            section_lbl = QLabel(f"<b>{section}</b>")
            form.addRow(section_lbl, QLabel(""))
            for key, field_type, default in options:
                current = self._osm2xodr_settings.get(section, {}).get(key, default)
                if field_type == "bool":
                    widget = QCheckBox()
                    widget.setChecked(self._as_bool_string(current) == "true")
                elif field_type == "int":
                    widget = QSpinBox()
                    widget.setRange(0, 9999)
                    try:
                        widget.setValue(int(float(current)))
                    except Exception:
                        widget.setValue(int(float(default)))
                elif field_type == "float":
                    widget = QDoubleSpinBox()
                    widget.setDecimals(6)
                    widget.setRange(0.0, 100.0)
                    widget.setSingleStep(0.05)
                    try:
                        widget.setValue(float(current))
                    except Exception:
                        widget.setValue(float(default))
                else:
                    widget = QLineEdit(str(current))
                editors[(section, key, field_type)] = widget
                form.addRow(f"{key}:", widget)
        layout.addLayout(form)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_defaults = button_box.addButton(
            "Restore Defaults", QDialogButtonBox.ButtonRole.ResetRole
        )

        def _restore_defaults():
            for section, options in OSM2XODR_SCHEMA:
                for key, field_type, default in options:
                    widget = editors[(section, key, field_type)]
                    if field_type == "bool":
                        widget.setChecked(default == "true")
                    elif field_type == "int":
                        widget.setValue(int(float(default)))
                    elif field_type == "float":
                        widget.setValue(float(default))
                    else:
                        widget.setText(default)

        btn_defaults.clicked.connect(_restore_defaults)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        layout.addWidget(button_box)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        updated = copy.deepcopy(DEFAULT_OSM2XODR_SETTINGS)
        for section, options in OSM2XODR_SCHEMA:
            for key, field_type, default in options:
                widget = editors[(section, key, field_type)]
                if field_type == "bool":
                    updated[section][key] = "true" if widget.isChecked() else "false"
                elif field_type == "int":
                    updated[section][key] = str(int(widget.value()))
                elif field_type == "float":
                    value = widget.value()
                    updated[section][key] = ("%.6f" % value).rstrip("0").rstrip(".")
                else:
                    text = widget.text().strip()
                    updated[section][key] = text if text else default

        self._osm2xodr_settings = updated
        self._show_project_status("Updated OSM conversion settings")

    def _convert_osm_to_xodr(self, osm_path: str) -> str | None:
        try:
            centerline_style = self._selected_xodr_centerline_marking()
            xodr_fd, xodr_path = tempfile.mkstemp(suffix=".xodr", prefix="osm2xodr_")
            os.close(xodr_fd)
            success, err = self._call_netconvert_conversion(osm_path, xodr_path)
            if success:
                # Re-add exterior road marks geometrically (strip was done in
                # the conversion pipeline via strip_all_road_marks).
                try:
                    renderer = opendrive_renderer_bindings_py.OpenDriveRenderer()
                    if renderer.load_map(xodr_path):
                        renderer.rewrite_exterior_road_marks(
                            xodr_path,
                            centerline_style=centerline_style,
                        )
                except Exception as _exc:
                    print(f"rewrite_exterior_road_marks warning: {_exc}")
                return xodr_path
            QMessageBox.warning(self, "OSM Conversion Failed", err or "Unknown error")
            return None
        except Exception as exc:
            QMessageBox.warning(
                self, "OSM Conversion Failed", f"Conversion error:\n{exc}"
            )
            return None

    def _flush_pending_osm_panel_edits(self) -> None:
        sel = self._osm_selected_item
        if sel is None:
            return
        # Ensure selected geometry edits are synced from scene coords to
        # lat/lon node refs before save/refresh compose.
        meta = self._osm_item_meta.get(sel)
        if meta and len(meta[3]) == len(meta[6]):
            updated_refs = []
            updated_latlon = []
            for i, (nid, _lat_old, _lon_old) in enumerate(meta[6]):
                sx, sy = meta[3][i]
                lat, lon = self._scene_to_latlon(float(sx), float(sy))
                updated_refs.append((str(nid), float(lat), float(lon)))
                updated_latlon.append((float(lat), float(lon)))
            meta[6][:] = updated_refs
            meta[4][:] = updated_latlon
            way_id = str(meta[5])
            if way_id in self._osm_created_ways:
                self._osm_created_ways[way_id]["node_coords"] = list(updated_refs)
            else:
                edit = self._osm_edits.setdefault(way_id, {})
                edit["node_coords"] = list(updated_refs)
        if self._osm_tags_edit_mode:
            self._on_osm_tag_edited(sel)
            self._osm_tags_edit_mode = False
        for rel in ("preceding", "succeeding"):
            if bool(self._osm_relation_edit_mode.get(rel, False)):
                self._commit_relation_edit(sel, rel, reselection=False)
                self._osm_relation_edit_mode[rel] = False
                self._osm_relation_draft[rel] = None
        if self._osm_relation_pick_mode is not None:
            self._osm_relation_pick_mode = None
        self._osm_show_props(sel)

    def _schedule_auto_xodr_refresh(self) -> None:
        """Restart the debounce timer that auto-refreshes XODR after an OSM drag."""
        if (
            not getattr(self, "check_opendrive", None)
            or not self.check_opendrive.isChecked()
        ):
            return
        if not getattr(self, "_auto_xodr_refresh_timer", None):
            self._auto_xodr_refresh_timer = QTimer(self)
            self._auto_xodr_refresh_timer.setSingleShot(True)
            self._auto_xodr_refresh_timer.timeout.connect(self._do_auto_xodr_refresh)
        # Bump generation so any in-flight conversion thread discards its result.
        self._auto_xodr_gen = getattr(self, "_auto_xodr_gen", 0) + 1
        self._show_project_status("OpenDRIVE refresh pending…")
        self._set_xodr_layer_dimmed(True)
        self._auto_xodr_refresh_timer.start(400)

    def _do_auto_xodr_refresh(self) -> None:
        """Compose current OSM content and run osm-to-xodr in a background thread."""
        self._flush_pending_osm_panel_edits()
        osm_content = self._compose_current_osm_content()
        if osm_content is None:
            osm_content = self._osm_content
        if not osm_content:
            return
        # Clip to world bounds so conversion processes only the visible area.
        osm_content = self._clip_osm_content_to_world_bounds(osm_content)
        try:
            fd, temp_osm_path = tempfile.mkstemp(suffix=".osm", prefix="osm2xodr_auto_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(osm_content)
            xodr_fd, xodr_path = tempfile.mkstemp(
                suffix=".xodr", prefix="osm2xodr_auto_"
            )
            os.close(xodr_fd)
        except Exception:
            return
        self._show_project_status("Converting OSM → OpenDRIVE…")
        my_gen = getattr(self, "_auto_xodr_gen", 0)
        centerline_style = self._selected_xodr_centerline_marking()

        def run():
            try:
                success, _err = self._call_netconvert_conversion(
                    temp_osm_path, xodr_path
                )
                if my_gen != getattr(self, "_auto_xodr_gen", 0):
                    return  # superseded by a later drag
                if success:
                    # Re-add lane markings geometrically: only the exterior road
                    # boundary gets a roadMark so internal roundabout/junction
                    # patch edges stay unmarked in the xodr file as well.
                    try:
                        renderer = opendrive_renderer_bindings_py.OpenDriveRenderer()
                        if renderer.load_map(xodr_path):
                            renderer.rewrite_exterior_road_marks(
                                xodr_path,
                                centerline_style=centerline_style,
                            )
                    except Exception as _exc:
                        print(f"rewrite_exterior_road_marks warning: {_exc}")
                self.xodr_auto_converted.emit(xodr_path if success else "")
            except Exception:
                if my_gen == getattr(self, "_auto_xodr_gen", 0):
                    self.xodr_auto_converted.emit("")

        threading.Thread(target=run, daemon=True).start()

    def _set_xodr_layer_dimmed(self, dimmed: bool) -> None:
        """Lower/restore opacity of the XODR vector layer to signal refresh state."""
        opacity = 0.25 if dimmed else 1.0
        if self._xodr_vector_group is not None:
            self._xodr_vector_group.setOpacity(opacity)

    def _on_xodr_auto_converted(self, xodr_path: str) -> None:
        """Main-thread slot: apply background OSM→XODR result and re-render."""
        self._set_xodr_layer_dimmed(False)
        if not xodr_path:
            self._show_project_status("OpenDRIVE auto-refresh failed")
            return
        self._suppress_next_xodr_title_update = True
        self.edit_xodr.setText(xodr_path)
        self._arrange_import_layers(show_xodr=True, show_osm=True, osm_first=True)
        self._show_project_status("OpenDRIVE auto-refreshed")
        self.refresh_opendrive()

    def refresh_all_layers(self) -> None:
        """Refresh all visible layers: re-render OSM signs, re-convert OSM\u2192XODR, re-fetch tiles."""
        # Always retry background tiles
        self._retry_failed_tiles()

        if not self.osm_path:
            # No OSM loaded \u2014 just reload the XODR from file
            if self.xodr_path and self.check_opendrive.isChecked():
                self.refresh_opendrive()
            return

        self._flush_pending_osm_panel_edits()
        osm_content = self._compose_current_osm_content()
        if osm_content is None:
            osm_content = self._osm_content
        if not osm_content:
            return

        try:
            fd, temp_osm_path = tempfile.mkstemp(suffix=".osm", prefix="refresh_all_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(osm_content)
        except Exception as exc:
            self._show_project_status(f"Refresh failed: {exc}")
            return

        # Re-render OSM layer from the composed (edited) content so that moved
        # give_way / signal nodes appear at their new positions.
        if self.check_osm.isChecked():
            self._osm_loading = True
            self.lbl_osm_status.setText("Loading...")

            def _run_osm(path=temp_osm_path, content=osm_content):
                try:
                    ways, signs, tree = self._parse_osm(path)
                    self.osm_ways_ready.emit((ways, signs, tree, content))
                except Exception:
                    self.osm_ways_ready.emit(([], [], None, None))

            threading.Thread(target=_run_osm, daemon=True).start()

        # Re-convert OSM \u2192 XODR and refresh the OpenDRIVE layer
        generated_xodr = self._convert_osm_to_xodr(temp_osm_path)
        if generated_xodr:
            self._suppress_next_xodr_title_update = True
            self.edit_xodr.setText(generated_xodr)
            if self.check_opendrive.isChecked():
                self.refresh_opendrive()
            self._show_project_status("All layers refreshed")

    def _refresh_opendrive_from_osm(self) -> None:
        if not self.osm_path:
            QMessageBox.information(
                self,
                "No OSM Loaded",
                "Load an OSM file first to regenerate OpenDRIVE.",
            )
            return

        # Ensure conversion uses latest in-memory OSM edits.
        self._flush_pending_osm_panel_edits()

        osm_content = self._compose_current_osm_content()
        if osm_content is None:
            osm_content = self._osm_content
        if not osm_content:
            QMessageBox.warning(
                self,
                "OSM Conversion Failed",
                "No OSM content is available for conversion.",
            )
            return

        try:
            fd, temp_osm_path = tempfile.mkstemp(
                suffix=".osm", prefix="osm2xodr_input_"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(osm_content)
        except Exception as exc:
            QMessageBox.warning(
                self, "OSM Conversion Failed", f"Could not prepare OSM input:\n{exc}"
            )
            return

        generated_xodr = self._convert_osm_to_xodr(temp_osm_path)
        if not generated_xodr:
            return

        self._suppress_next_xodr_title_update = True
        self.edit_xodr.setText(generated_xodr)
        self.check_opendrive.setChecked(True)
        self._arrange_import_layers(show_xodr=True, show_osm=True, osm_first=True)
        self._show_project_status("OpenDRIVE refreshed from current OSM")

    def _export_osm_only(self) -> None:
        self._flush_pending_osm_panel_edits()
        osm_content = self._compose_current_osm_content()
        if osm_content is None:
            osm_content = self._osm_content
        if not osm_content:
            QMessageBox.warning(
                self,
                "Export Failed",
                "No OSM content is available to export.",
            )
            return

        osm_content = self._clip_osm_content_to_world_bounds(osm_content)

        default_dir = ""
        base_name = ""
        if self.project_file_path:
            default_dir = os.path.dirname(self.project_file_path)
            base_name = os.path.splitext(os.path.basename(self.project_file_path))[0]
        elif self.osm_path:
            default_dir = os.path.dirname(self.osm_path)
            base_name = os.path.splitext(os.path.basename(self.osm_path))[0]
        elif self.town_name:
            base_name = self.town_name

        if not default_dir:
            default_dir = os.path.expanduser("~")
        if not base_name:
            base_name = "exported_project"

        default_path = os.path.join(default_dir, f"{base_name}.osm")
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export OSM",
            default_path,
            "OSM Files (*.osm);;All Files (*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not file_path:
            return
        if not os.path.splitext(file_path)[1]:
            file_path += ".osm"

        try:
            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write(osm_content)
                if not osm_content.endswith("\n"):
                    handle.write("\n")
            self._show_project_status(f"Exported OSM: {os.path.basename(file_path)}")
        except Exception as exc:
            QMessageBox.warning(self, "Export Failed", f"Could not export OSM:\n{exc}")

    def _export_xodr_file(self) -> None:
        default_dir = ""
        base_name = ""
        if self.project_file_path:
            default_dir = os.path.dirname(self.project_file_path)
            base_name = os.path.splitext(os.path.basename(self.project_file_path))[0]
        else:
            preferred_dir = str(self._preferred_project_save_dir or "").strip()
            if preferred_dir and os.path.isdir(preferred_dir):
                default_dir = preferred_dir
            else:
                default_dir = os.path.expanduser("~")
            if self.town_name:
                base_name = self.town_name

        if not base_name:
            base_name = "exported_opendrive"
        default_path = os.path.join(default_dir, f"{base_name}.xodr")

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export OpenDRIVE",
            default_path,
            "OpenDRIVE Files (*.xodr);;All Files (*)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not file_path:
            return
        if not os.path.splitext(file_path)[1]:
            file_path += ".xodr"

        # Export should mirror the currently loaded OpenDRIVE content exactly.
        xodr_content = self._current_xodr_content()
        if not xodr_content:
            QMessageBox.warning(
                self,
                "Export Failed",
                "No OpenDRIVE content is available to export.",
            )
            return

        if self._project_storage_mode() != "ore":
            xodr_content = self._clip_xodr_content_to_world_bounds(
                xodr_content, invert_y=False
            )

        try:
            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write(xodr_content)
                if not xodr_content.endswith("\n"):
                    handle.write("\n")
            self._show_project_status(
                f"Exported OpenDRIVE: {os.path.basename(file_path)}"
            )
        except Exception as exc:
            QMessageBox.warning(
                self, "Export Failed", f"Could not export OpenDRIVE:\n{exc}"
            )
