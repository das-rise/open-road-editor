"""OSM way/node editing mixin."""

import copy
import io
import math
import os
import tempfile
import threading
import xml.etree.ElementTree as ET

from PyQt6.QtCore import (
    QPointF,
    QRectF,
    Qt,
    QTimer,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPolygonItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from open_road_editor.constants import *  # noqa: F401,F403
from open_road_editor.constants import _OSM_HIGHWAY_DEFAULT, _OSM_HIGHWAY_STYLES  # noqa: F401
from open_road_editor.utils.coords import _tmerc_forward_wgs84, _tmerc_inverse_wgs84
from open_road_editor.widgets import OSMWayPathItem


_OSM_SIGN_HEADING_OFFSET_TAG = 'direction'
_OSM_NODE_SIGN_HIGHWAY_VALUES = {
    'traffic_signals',
    'give_way',
    'stop',
    'crossing',
    'street_lamp',
    'bus_stop',
}
_OSM_NODE_SIGN_TAG_KEYS = {
    'traffic_sign',
    'traffic_signals',
    'maxspeed',
}


class _OsmMixin:
    """Mixin — see viewer/main.py for the assembled class."""

    def on_osm_path_changed(self, text: str):
        path = text.strip()
        new_path = (
            path if os.path.isfile(path) and path.lower().endswith(('.osm', '.xml')) else None
        )
        if new_path != self.osm_path:
            self.osm_path = new_path
            self._osm_content = None
            if not self._restoring_project_payload:
                self._suppress_auto_fit = False
            if self.osm_path:
                try:
                    with open(self.osm_path, 'r', encoding='utf-8') as f:
                        self._osm_content = f.read()
                except Exception as e:
                    print(f'Failed to read OSM content: {e}')
                if not self._restoring_project_payload:
                    self.town_name = os.path.splitext(os.path.basename(self.osm_path))[0]
                    self._refresh_window_title()
                    self._mark_osm_dirty_after_load = True
                    generated_xodr = self._convert_osm_to_xodr(self.osm_path)
                    if generated_xodr:
                        self._suppress_next_xodr_title_update = True
                        self.edit_xodr.setText(generated_xodr)
                        self.check_opendrive.setChecked(True)
                        self._arrange_import_layers(
                            show_xodr=True, show_osm=True, osm_first=True, reset_objects=True
                        )
                else:
                    self._mark_osm_dirty_after_load = False

            self._clear_osm_items()
            if self.osm_path:
                if not self.check_osm.isChecked():
                    if not self._restoring_project_payload:
                        self.check_osm.setChecked(True)  # triggers refresh via update_visibility
                else:
                    self.refresh_osm()
            else:
                self._mark_osm_dirty_after_load = False
                self.check_osm.setChecked(False)
        self.update_visibility()

    def refresh_osm(self):
        if not self.osm_path:
            self.lbl_osm_status.setText('')
            return
        self._osm_loading = True
        self.lbl_osm_status.setText('Loading...')

        path = self.osm_path
        content = self._compose_current_osm_content()
        if content is None:
            content = self._osm_content

        temp_osm_path = None
        if content:
            try:
                fd, temp_osm_path = tempfile.mkstemp(suffix='.osm', prefix='refresh_osm_')
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(content)
                path = temp_osm_path
            except Exception as exc:
                print(f'refresh_osm temp file error: {exc}')
                temp_osm_path = None

        def run():
            try:
                ways, signs, tree = self._parse_osm(path)
                self.osm_ways_ready.emit((ways, signs, tree, content if temp_osm_path else None))
            except Exception as exc:
                print(f'refresh_osm error: {exc}')
                self.osm_ways_ready.emit(([], [], None))
            finally:
                if temp_osm_path:
                    try:
                        os.unlink(temp_osm_path)
                    except OSError:
                        pass

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _parse_osm(path: str):
        """Parse an OSM XML file.

        Returns ``(ways, signs, tree)`` where *ways* is a list of
        ``(highway, [(lat, lon)…], tags_dict, way_id, [(node_id, lat, lon)…])``
        tuples, *signs* is a list of ``(lat, lon, tags_dict, node_id)`` tuples,
        and *tree* is the parsed :class:`ET.ElementTree`.
        """
        tree = ET.parse(path)
        root = tree.getroot()

        detail_tags = {
            'traffic_sign',
            'maxspeed',
            'natural',  # for tree
            'amenity',  # for parking
            'barrier',  # for guard_rail
            'traffic_signals',
        }
        detail_highway_values = {
            'traffic_signals',
            'give_way',
            'stop',
            'crossing',
            'street_lamp',
            'bus_stop',
            'turning_circle',
        }

        # Build node-id → (lat, lon, tags) index
        nodes: dict = {}
        signs: list = []
        for node in root.iter('node'):
            nid = node.get('id')
            tags: dict = {}
            is_interesting = False
            for tag in node.iter('tag'):
                k = tag.get('k', '')
                v = tag.get('v', '')
                if k:
                    tags[k] = v
                    if k in detail_tags or (k == 'highway' and v in detail_highway_values):
                        is_interesting = True
            try:
                lat, lon = float(node.get('lat')), float(node.get('lon', '0'))
                nodes[nid] = (lat, lon)
                if is_interesting:
                    signs.append((lat, lon, tags, nid))
            except (TypeError, ValueError):
                pass

        # Collect ways that carry a highway tag
        ways = []
        for way in root.iter('way'):
            tags = {}
            for tag in way.iter('tag'):
                k = tag.get('k', '')
                v = tag.get('v', '')
                if k:
                    tags[k] = v
            highway = tags.get('highway')
            if not highway:
                continue
            way_id = way.get('id', '')
            node_refs: list = []  # [(node_id, lat, lon), …]
            coords: list = []  # [(lat, lon), …]
            for nd in way.iter('nd'):
                ref = nd.get('ref')
                if ref in nodes:
                    lat, lon = nodes[ref]
                    node_refs.append((ref, lat, lon))
                    coords.append((lat, lon))
            if len(coords) >= 2:
                ways.append((highway, coords, tags, way_id, node_refs))

        # Compute road bearing for give_way nodes so the triangle can be oriented
        # bearing_by_nid maps node_id → angle in degrees (0=north, clockwise)
        bearing_by_nid: dict = {}
        give_way_nids = {nid for _, _, stags, nid in signs if stags.get('highway') == 'give_way'}
        if give_way_nids:
            for _, _, _, _, node_refs in ways:
                for idx, (ref, _lat, _lon) in enumerate(node_refs):
                    if ref not in give_way_nids:
                        continue
                    # Use the segment arriving at this node (prev → cur)
                    if idx > 0:
                        prev_lat, prev_lon = node_refs[idx - 1][1], node_refs[idx - 1][2]
                        cur_lat, cur_lon = _lat, _lon
                    elif idx < len(node_refs) - 1:
                        # node is first; use departure direction (cur → next)
                        prev_lat, prev_lon = _lat, _lon
                        cur_lat, cur_lon = node_refs[idx + 1][1], node_refs[idx + 1][2]
                    else:
                        continue
                    d_lat = cur_lat - prev_lat
                    d_lon = cur_lon - prev_lon
                    # atan2 in screen-space: lon → x (right), lat → y (up, but screen y is down)
                    angle_deg = math.degrees(math.atan2(d_lon, d_lat))
                    bearing_by_nid[ref] = angle_deg  # last encountered way wins

        # Attach bearing (or None) as 5th element to each sign
        signs = [
            (
                lat,
                lon,
                stags,
                nid,
                (bearing_by_nid.get(nid) + _OsmMixin._osm_sign_heading_offset_deg(stags))
                if bearing_by_nid.get(nid) is not None
                else None,
            )
            for lat, lon, stags, nid in signs
        ]
        return ways, signs, tree

    def _on_osm_ways_ready(self, result) -> None:
        """Main-thread slot: convert parsed OSM ways into QGraphicsPathItems in the scene."""
        self._osm_loading = False
        self._clear_osm_items()
        mark_dirty_after_load = self._mark_osm_dirty_after_load
        self._mark_osm_dirty_after_load = False

        if isinstance(result, tuple) and len(result) >= 3:
            ways, signs, tree = result[:3]
            if len(result) >= 4 and result[3] is not None:
                self._osm_content = str(result[3])
        else:
            ways, signs, tree = result if result else [], [], None

        if not ways or not self.map_ctx:
            self.lbl_osm_status.setText('No roads found' if ways is not None else '')
            if mark_dirty_after_load:
                self._mark_osm_dirty()
            return

        self._osm_original_tree = tree
        self._osm_edits.clear()
        self._osm_node_tag_edits.clear()
        self._osm_created_ways.clear()
        self._osm_deleted_way_ids.clear()
        self._osm_deleted_node_ids.clear()

        # Only reset the dirty flag if this is a fresh project/OSM load.
        # For a refresh (mark_dirty_after_load is False), keep the dirty flag
        # if it was already set (meaning there were pending edits).
        if mark_dirty_after_load or not self._osm_dirty:
            self._reset_osm_dirty()
        max_way_id = 0
        max_node_id = 0
        for _highway, _coords, _tags, way_id, node_refs in ways:
            try:
                wid = int(str(way_id))
                if wid > max_way_id:
                    max_way_id = wid
            except Exception:
                pass
            for nid, _lat, _lon in node_refs:
                try:
                    nid_i = int(str(nid))
                    if nid_i > max_node_id:
                        max_node_id = nid_i
                except Exception:
                    pass
        self._osm_next_way_id = max(1, max_way_id + 1)
        self._osm_next_node_id = max(1, max_node_id + 1)

        ref_lat = self.map_ctx.earth_ref_lat
        ref_lon = self.map_ctx.earth_ref_lon
        x0 = getattr(self.map_ctx, 'proj_false_easting', 0.0)
        y0 = getattr(self.map_ctx, 'proj_false_northing', 0.0)
        k0 = getattr(self.map_ctx, 'proj_scale_factor', 1.0)
        mpp = self.map_ctx.mpp
        min_x = self.map_ctx.world_bounds[0]
        min_y = self.map_ctx.world_bounds[2]

        def latlon_to_scene(lat: float, lon: float):
            x_tm, y_tm = _tmerc_forward_wgs84(lat, lon, ref_lat, ref_lon, k0, x0, y0)
            carla_y = -y_tm
            return (x_tm - min_x) / mpp, (carla_y - min_y) / mpp

        # Store the projection closure for later use by node-drag
        self._osm_latlon_to_scene = latlon_to_scene

        path_items: list = []
        for highway, coords, tags, way_id, node_refs in ways:
            rgba, width = _OSM_HIGHWAY_STYLES.get(highway, _OSM_HIGHWAY_DEFAULT)
            scene_coords: list = []
            path = QPainterPath()
            for i, (lat, lon) in enumerate(coords):
                px, py = latlon_to_scene(lat, lon)
                scene_coords.append((px, py))
                if i == 0:
                    path.moveTo(px, py)
                else:
                    path.lineTo(px, py)
            pen = QPen(QColor(*rgba))
            pen.setWidthF(float(width) * OSM_LINE_THICKNESS)
            pen.setCosmetic(True)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            item = OSMWayPathItem(path)
            item.setPen(pen)
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            item.set_way_scene_coords(scene_coords)
            item.set_direction_mode(self._osm_direction_mode_from_tags(tags))
            path_items.append(item)
            # meta: (highway, tags, base_pen, scene_coords, latlon_coords, way_id, node_refs)
            self._osm_item_meta[item] = (
                highway,
                dict(tags),
                QPen(pen),
                scene_coords,
                list(coords),
                way_id,
                list(node_refs),
            )

        # Draw signs (non-interactable)
        for lat, lon, tags, nid, bearing in signs:
            px, py = latlon_to_scene(lat, lon)

            # Determine color based on tag type
            hw = tags.get('highway', '')
            if hw == 'give_way':
                # Yield sign: equilateral triangle, white fill, red border.
                # The triangle apex points *down* (inverted), which is the conventional shape.
                # By default, it faces against the direction of travel (oncoming traffic).
                s = 6.0  # half-width of the base
                h = s * math.sqrt(3)  # height of equilateral triangle

                # Build inverted triangle in local coords (apex pointing down/south)
                apex = QPointF(0.0, h * 2 / 3)
                tl = QPointF(-s, -h / 3)
                tr = QPointF(s, -h / 3)

                # Rotate each point by the road bearing.
                # Since apex is down (South), a bearing of 0 (North) keeps it pointing South
                # (facing the driver arriving at the junction).
                rot_deg = bearing if bearing is not None else 0.0
                rot_rad = math.radians(rot_deg)
                cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

                def _rot(pt: QPointF) -> QPointF:
                    return QPointF(
                        px + pt.x() * cos_r - pt.y() * sin_r,
                        py + pt.x() * sin_r + pt.y() * cos_r,
                    )

                poly = QPolygonF([_rot(apex), _rot(tl), _rot(tr)])
                item = QGraphicsPolygonItem(poly)
                item.setBrush(QBrush(QColor(255, 255, 255)))
                item.setPen(QPen(QColor(220, 0, 0), 1.5))
                item.setZValue(Z_OSM_LAYER + 1)
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
                item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
                item.setAcceptHoverEvents(False)
                item.setData(0, 'osm_sign')
                item.setData(1, tags)
                item.setData(2, str(nid))
                item.setData(3, QPen(item.pen()))
                self._osm_sign_item_positions[item] = (px, py)
                path_items.append(item)
                continue

            r = 3.0
            item = QGraphicsEllipseItem(px - r, py - r, 2 * r, 2 * r)

            if hw == 'traffic_signals' or 'traffic_signals' in tags:
                color = QColor(0, 200, 0)  # Green  — traffic light
            elif hw == 'stop':
                color = QColor(220, 0, 0)  # Red    — stop sign
            elif hw == 'crossing':
                color = QColor(0, 180, 255)  # Blue   — pedestrian crossing
            elif hw == 'street_lamp':
                color = QColor(255, 230, 100)  # Light yellow — lamp
            elif hw in ('bus_stop', 'turning_circle'):
                color = QColor(180, 0, 220)  # Purple — bus stop / turning circle
            elif 'maxspeed' in tags:
                color = QColor(255, 255, 255)  # White  — speed limit
            elif 'traffic_sign' in tags:
                color = QColor(255, 200, 0)  # Amber  — generic traffic sign
            elif 'natural' in tags:
                color = QColor(0, 128, 0)  # Dark green — tree
            else:
                color = QColor(200, 200, 200)  # Grey   — other details

            item.setBrush(QBrush(color))
            item.setPen(QPen(Qt.GlobalColor.black, 0.5))
            item.setZValue(Z_OSM_LAYER + 1)
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
            item.setAcceptHoverEvents(False)
            item.setData(0, 'osm_sign')
            item.setData(1, tags)
            item.setData(2, str(nid))
            item.setData(3, QPen(item.pen()))
            self._osm_sign_item_positions[item] = (px, py)
            path_items.append(item)

        if not path_items:
            self.lbl_osm_status.setText('No roads found')
            return

        group = self.scene.createItemGroup(path_items)
        group.setZValue(Z_OSM_LAYER)  # above opendrive_item, below grid
        self._osm_vector_group = group
        self._rebuild_osm_connectivity()
        self._draw_osm_bounds_rect()
        self._apply_osm_layer_style()
        self.lbl_osm_status.setText('Loaded')
        if mark_dirty_after_load:
            self._mark_osm_dirty()

    def _clear_osm_items(self) -> None:
        """Remove the OSM vector group from the scene, if present."""
        self._remove_osm_node_dots()
        self._remove_osm_selected_direction_arrows()
        self._osm_hover_item = None
        self._osm_selected_item = None
        self._osm_sign_item_positions.clear()
        self._osm_selected_sign_item = None
        self._osm_selected_sign_node_id = None
        self._osm_selected_node_index = None
        self._osm_dragging_dot = None
        self._osm_dragging_way_item = None
        self._osm_way_drag_last_scene = None
        self._osm_way_drag_had_motion = False
        self._osm_drag_start_scene = None
        self._osm_drag_start_latlon = None
        self._osm_props_group.setVisible(False)
        self._osm_item_meta.clear()
        self._osm_way_connectivity.clear()
        self._osm_edits.clear()
        self._osm_node_tag_edits.clear()
        self._osm_created_ways.clear()
        self._osm_deleted_way_ids.clear()
        self._osm_deleted_node_ids.clear()
        self._osm_relation_edit_mode = {'preceding': False, 'succeeding': False}
        self._osm_relation_draft = {'preceding': None, 'succeeding': None}
        self._osm_tags_edit_mode = False
        self._osm_node_tags_edit_mode = False
        self._osm_relation_hover_map = {}
        self._osm_relation_pick_mode = None
        self._osm_suppress_next_click_select = False
        self._clear_osm_multi_selection()
        self._stop_osm_blink()
        self._reset_osm_dirty()
        self._osm_undo_stack.clear()
        self._osm_redo_stack.clear()
        self._osm_next_node_id = 1
        self._osm_next_way_id = 1
        self._osm_original_tree = None
        if hasattr(self, '_osm_node_props_group'):
            self._osm_node_props_group.setVisible(False)
        self._update_osm_reverse_sign_button()
        self._update_osm_export_btn()
        if self._osm_bounds_rect_item is not None:
            self.scene.removeItem(self._osm_bounds_rect_item)
            self._osm_bounds_rect_item = None
        if self._osm_vector_group is not None:
            for item in list(self._osm_vector_group.childItems()):
                self.scene.removeItem(item)
            self.scene.removeItem(self._osm_vector_group)
            self._osm_vector_group = None
        self.lbl_osm_status.setText('')

    def _apply_osm_layer_style(
        self, opacity: float | None = None, visible: bool | None = None
    ) -> None:
        op = self.spin_osm_alpha.value() if opacity is None else float(opacity)
        vis = self.check_osm.isChecked() if visible is None else bool(visible)
        render_visible = vis and op > 0.0
        show_objects = getattr(self, 'check_osm_objects', None)
        show_objects_checked = show_objects.isChecked() if show_objects is not None else True
        if self._osm_vector_group is not None:
            self._osm_vector_group.setVisible(render_visible)
            self._osm_vector_group.setOpacity(op)
            for _item in self._osm_vector_group.childItems():
                if _item.data(0) == 'osm_sign':
                    _item.setVisible(render_visible and show_objects_checked)
                else:
                    _item.setVisible(render_visible)
        if self._osm_bounds_rect_item is not None:
            self._osm_bounds_rect_item.setVisible(render_visible)

    def _on_osm_thickness_changed(self, value: float) -> None:
        """Deprecated: OSM thickness is fixed by OSM_LINE_THICKNESS."""
        self.view.viewport().update()

    def _on_osm_opacity_changed(self, value: float) -> None:
        self._apply_osm_layer_style(opacity=value)
        self.view.viewport().update()
        if self._osm_vector_group is not None:
            if not self.check_osm.isChecked():
                self.lbl_osm_status.setText('')
            elif value <= 0.0:
                self.lbl_osm_status.setText('Loaded (Hidden)')
            else:
                self.lbl_osm_status.setText('Loaded')

    # ── OSM hover / selection / editing ──────────────────────────────

    _OSM_NODE_DOT_RADIUS = 5.0  # cosmetic px radius for node markers

    def _osm_highlight_item(self, item, color: QColor) -> None:
        """Apply a highlight pen to an OSM path item."""
        meta = self._osm_item_meta.get(item)
        if not meta:
            return
        pen = QPen(color)
        pen.setWidthF(meta[2].widthF() + OSM_HIGHLIGHT_PEN_EXTRA_WIDTH)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        item.setPen(pen)

    def _osm_show_props(self, item) -> None:
        """Populate the editable properties panel from an OSM item's tags."""
        meta = self._osm_item_meta.get(item)
        if not meta:
            return
        if hasattr(self, 'btn_osm_props_edit_mode'):
            want_checked = bool(self._osm_tags_edit_mode)
            if self.btn_osm_props_edit_mode.isChecked() != want_checked:
                blocked = self.btn_osm_props_edit_mode.blockSignals(True)
                self.btn_osm_props_edit_mode.setChecked(want_checked)
                self.btn_osm_props_edit_mode.blockSignals(blocked)
            self._set_mode_toggle_visual(self.btn_osm_props_edit_mode, want_checked)
            self._position_osm_props_edit_mode_button()
        self._osm_props_group.setVisible(True)
        tags = meta[1]
        self._populate_osm_tag_editor(tags, item)

    def _show_selected_osm_node_props(self) -> None:
        if not hasattr(self, '_osm_node_props_group'):
            return
        node_id = self._osm_selected_node_id()
        if hasattr(self, 'btn_osm_node_props_edit_mode'):
            want_checked = bool(self._osm_node_tags_edit_mode)
            if self.btn_osm_node_props_edit_mode.isChecked() != want_checked:
                blocked = self.btn_osm_node_props_edit_mode.blockSignals(True)
                self.btn_osm_node_props_edit_mode.setChecked(want_checked)
                self.btn_osm_node_props_edit_mode.blockSignals(blocked)
            self._set_mode_toggle_visual(self.btn_osm_node_props_edit_mode, want_checked)
            self._position_osm_node_props_edit_mode_button()
        if not node_id:
            self._osm_node_props_group.setVisible(False)
            return
        self._osm_node_props_group.setVisible(True)
        self._populate_osm_node_tag_editor(node_id, self._osm_current_node_tags(node_id))

    def _clear_osm_multi_selection(self) -> None:
        items = list(getattr(self, '_osm_multi_selected_items', set()) or [])
        for it in items:
            meta = self._osm_item_meta.get(it)
            if meta:
                it.setPen(meta[2])
        self._osm_multi_selected_items = set()

    def _set_osm_multi_selection(self, items: list) -> None:
        self._clear_osm_multi_selection()
        chosen = set(items or [])
        for it in chosen:
            if it in self._osm_item_meta:
                self._osm_highlight_item(it, OSM_SELECTION_COLOR)
        self._osm_multi_selected_items = chosen

    def _select_osm_segments_by_rect(
        self, rect: QRectF, append: bool = False, subtract: bool = False
    ) -> int:
        rect = QRectF(rect).normalized()
        if rect.width() <= 1.0 or rect.height() <= 1.0:
            if append or subtract:
                current = set(getattr(self, '_osm_multi_selected_items', set()) or [])
                if self._osm_selected_item is not None:
                    current.add(self._osm_selected_item)
                return len(current)
            return 0
        hit_path = QPainterPath()
        hit_path.addRect(rect)
        hits = []
        for it in list(self._osm_item_meta.keys()):
            try:
                scene_path = it.mapToScene(it.path())
            except Exception:
                continue
            if scene_path.intersects(hit_path):
                hits.append(it)
        if append or subtract:
            chosen = set(getattr(self, '_osm_multi_selected_items', set()) or [])
            if self._osm_selected_item is not None:
                chosen.add(self._osm_selected_item)
            if subtract:
                chosen.difference_update(hits)
            else:
                chosen.update(hits)
            self._select_osm_item(None)
            self._set_osm_multi_selection(list(chosen))
            return len(chosen)
        self._select_osm_item(None)
        self._set_osm_multi_selection(hits)
        return len(hits)

    def _find_osm_item_by_way_id(self, way_id: str):
        w = str(way_id)
        for item, meta in self._osm_item_meta.items():
            if str(meta[5]) == w:
                return item
        return None

    def _select_osm_item(self, item, center_view: bool = False) -> None:
        """Select and highlight an OSM segment, update properties and optional viewport focus."""
        self._clear_osm_multi_selection()
        self._stop_osm_blink()
        self._osm_dragging_way_item = None
        self._osm_way_drag_last_scene = None
        self._osm_way_drag_had_motion = False
        # Clear any sign-node selection when a segment is being selected/deselected
        if self._osm_selected_sign_item is not None:
            orig_pen = self._osm_selected_sign_item.data(3)
            if orig_pen is not None:
                self._osm_selected_sign_item.setPen(orig_pen)
            self._osm_selected_sign_item = None
            self._osm_selected_sign_node_id = None
        prev = self._osm_selected_item
        if prev is not item:
            self._osm_selected_node_index = None
            self._osm_selected_dot = None
        if prev is not item:
            if prev is not None and self._osm_tags_edit_mode:
                self._on_osm_tag_edited(prev)
                self._osm_tags_edit_mode = False
            if self._osm_node_tags_edit_mode:
                self._commit_selected_osm_node_tag_edits()
                self._osm_node_tags_edit_mode = False
            if prev is not None:
                for rel in ('preceding', 'succeeding'):
                    if bool(self._osm_relation_edit_mode.get(rel, False)):
                        self._commit_relation_edit(prev, rel, reselection=False)
                        self._osm_relation_edit_mode[rel] = False
                        self._osm_relation_draft[rel] = None
                self._osm_relation_pick_mode = None
        if prev is not None and prev is not item and prev is not self._osm_hover_item:
            meta_prev = self._osm_item_meta.get(prev)
            if meta_prev:
                prev.setPen(meta_prev[2])
        self._remove_osm_node_dots()
        self._remove_osm_selected_direction_arrows()
        self._osm_selected_item = item
        if item is None:
            self._osm_props_group.setVisible(False)
            if hasattr(self, '_osm_node_props_group'):
                self._osm_node_props_group.setVisible(False)
            self._update_osm_reverse_sign_button()
            return
        self._osm_highlight_item(item, OSM_SELECTION_COLOR)
        self._osm_show_props(item)
        self._show_osm_node_dots(item)
        self._show_osm_selected_direction_arrows(item)
        if center_view:
            try:
                br = item.path().boundingRect()
                center = item.mapToScene(br.center())
                self.view.centerOn(center)
            except Exception:
                pass

    def _rebuild_osm_connectivity(self) -> None:
        """Recompute predecessor/successor IDs from shared endpoint node refs."""
        endpoint_refs = {}  # node_id -> [(way_id, 'start'|'end')]
        for meta in self._osm_item_meta.values():
            way_id = str(meta[5])
            node_refs = meta[6]
            if not node_refs:
                continue
            start_nid = str(node_refs[0][0])
            end_nid = str(node_refs[-1][0])
            endpoint_refs.setdefault(start_nid, []).append((way_id, 'start'))
            endpoint_refs.setdefault(end_nid, []).append((way_id, 'end'))

        connectivity = {}
        for meta in self._osm_item_meta.values():
            way_id = str(meta[5])
            node_refs = meta[6]
            if not node_refs:
                connectivity[way_id] = ([], [])
                continue
            start_nid = str(node_refs[0][0])
            end_nid = str(node_refs[-1][0])
            preceding = []
            succeeding = []
            for other_id, _side in endpoint_refs.get(start_nid, []):
                if other_id != way_id and other_id not in preceding:
                    preceding.append(other_id)
            for other_id, _side in endpoint_refs.get(end_nid, []):
                if other_id != way_id and other_id not in succeeding:
                    succeeding.append(other_id)
            connectivity[way_id] = (preceding, succeeding)
        self._osm_way_connectivity = connectivity

    def _make_segment_id_links(self, ids: list, clickable: bool = True):
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        if not ids:
            row.addWidget(QLabel('-'))
            row.addStretch()
            return container
        for idx, sid in enumerate(ids):
            if clickable:
                btn = QPushButton(str(sid))
                btn.setFlat(True)
                btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                btn.setStyleSheet('QPushButton { color: #0078d4; text-decoration: underline; }')
                btn.clicked.connect(
                    lambda _checked=False, _sid=str(sid): self._on_segment_id_clicked(_sid)
                )
                row.addWidget(btn)
            else:
                row.addWidget(QLabel(str(sid)))
            if idx < len(ids) - 1:
                row.addWidget(QLabel(','))
        row.addStretch()
        return container

    def _on_segment_id_clicked(self, way_id: str) -> None:
        item = self._find_osm_item_by_way_id(way_id)
        if item is None:
            self._show_project_status(f'Segment {way_id} not available')
            return
        self._select_osm_item(item, center_view=True)

    def _on_osm_blink_tick(self) -> None:
        item = self._osm_blink_item
        if item is None or item is self._osm_selected_item:
            self._stop_osm_blink()
            return
        meta = self._osm_item_meta.get(item)
        if not meta:
            self._stop_osm_blink()
            return
        if self._osm_blink_on:
            item.setPen(meta[2])
        else:
            self._osm_highlight_item(item, OSM_HOVER_COLOR)
        self._osm_blink_on = not self._osm_blink_on

    def _stop_osm_blink(self) -> None:
        if (
            self._osm_blink_item is not None
            and self._osm_blink_item is not self._osm_selected_item
        ):
            meta = self._osm_item_meta.get(self._osm_blink_item)
            if meta:
                self._osm_blink_item.setPen(meta[2])
        self._osm_blink_item = None
        self._osm_blink_on = False
        if self._osm_blink_timer.isActive():
            self._osm_blink_timer.stop()

    def _start_osm_blink_by_way_id(self, way_id: str) -> None:
        item = self._find_osm_item_by_way_id(way_id)
        if item is None or item is self._osm_selected_item:
            self._stop_osm_blink()
            return
        if self._osm_blink_item is not item:
            self._stop_osm_blink()
            self._osm_blink_item = item
            self._osm_blink_on = False
        if not self._osm_blink_timer.isActive():
            self._osm_blink_timer.start()

    def _toggle_relation_edit_mode(self, item, relation: str) -> None:
        if not self._osm_edit_enabled():
            self._show_project_status('Enable OSM Edit mode first')
            return
        enabled = bool(self._osm_relation_edit_mode.get(relation, False))
        if enabled:
            self._commit_relation_edit(item, relation)
            self._osm_relation_edit_mode[relation] = False
            self._osm_relation_draft[relation] = None
        else:
            meta = self._osm_item_meta.get(item)
            way_id = str(meta[5]) if meta else ''
            ids = list(
                self._osm_way_connectivity.get(way_id, ([], []))[
                    0 if relation == 'preceding' else 1
                ]
            )
            self._osm_relation_draft[relation] = ids
            self._osm_relation_edit_mode[relation] = True
        self._osm_relation_pick_mode = None
        self._osm_show_props(item)

    def _cancel_relation_edit_mode(self, item, relation: str) -> None:
        self._osm_relation_edit_mode[relation] = False
        self._osm_relation_draft[relation] = None
        if self._osm_relation_pick_mode == relation:
            self._osm_relation_pick_mode = None
        self._osm_show_props(item)

    def _toggle_osm_tags_edit_mode(self, item) -> None:
        if item is None:
            return
        if not self._osm_edit_enabled():
            self._show_project_status('Enable OSM Edit mode first')
            return
        if self._osm_tags_edit_mode:
            self._on_osm_tag_edited(item)
            self._osm_tags_edit_mode = False
        else:
            self._osm_tags_edit_mode = True
        self._osm_show_props(item)

    def _cancel_osm_tags_edit_mode(self, item) -> None:
        if item is None:
            return
        self._osm_tags_edit_mode = False
        self._osm_show_props(item)

    def _relation_endpoint_index(self, meta, relation: str) -> int:
        return 0 if relation == 'preceding' else (len(meta[6]) - 1)

    def _detach_relation_by_way_id(self, item, relation: str, target_id: str) -> bool:
        meta = self._osm_item_meta.get(item)
        target_item = self._find_osm_item_by_way_id(target_id)
        tmeta = self._osm_item_meta.get(target_item) if target_item is not None else None
        if not meta or not tmeta:
            return False
        idx = self._relation_endpoint_index(meta, relation)
        if idx < 0 or idx >= len(meta[6]):
            return False
        anchor_nid = str(meta[6][idx][0])
        ti_candidates = [0, len(tmeta[6]) - 1] if tmeta[6] else []
        ti = None
        for c in ti_candidates:
            if str(tmeta[6][c][0]) == anchor_nid:
                ti = c
                break
        if ti is None:
            return False
        _nid_old, lat, lon = tmeta[6][ti]
        sx, sy = tmeta[3][ti]
        new_nid = str(self._osm_next_node_id)
        self._osm_next_node_id += 1
        tmeta[6][ti] = (new_nid, lat, lon)
        tmeta[4][ti] = (lat, lon)
        tmeta[3][ti] = (sx, sy)
        self._rebuild_osm_path(target_item)
        self._osm_persist_node_edit(target_item)
        return True

    def _attach_relation_by_way_id(self, item, relation: str, target_id: str) -> bool:
        meta = self._osm_item_meta.get(item)
        target_item = self._find_osm_item_by_way_id(target_id)
        if target_item is None or target_item is item:
            return False
        tmeta = self._osm_item_meta.get(target_item)
        if not meta or not tmeta or not tmeta[6]:
            return False
        idx = self._relation_endpoint_index(meta, relation)
        if idx < 0 or idx >= len(meta[6]):
            return False
        anchor_nid, anchor_lat, anchor_lon = meta[6][idx]
        anchor_sx, anchor_sy = meta[3][idx]
        endpoint_indices = [0, len(tmeta[6]) - 1]
        best_idx = endpoint_indices[0]
        best_d2 = float('inf')
        for c in endpoint_indices:
            tsx, tsy = tmeta[3][c]
            d2 = (anchor_sx - tsx) ** 2 + (anchor_sy - tsy) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_idx = c
        tmeta[6][best_idx] = (str(anchor_nid), float(anchor_lat), float(anchor_lon))
        tmeta[4][best_idx] = (float(anchor_lat), float(anchor_lon))
        tmeta[3][best_idx] = (anchor_sx, anchor_sy)
        self._rebuild_osm_path(target_item)
        self._osm_persist_node_edit(target_item)
        return True

    def _on_relation_remove_clicked(self, item, relation: str, rid: str) -> None:
        if not self._osm_edit_enabled():
            return
        draft = self._osm_relation_draft.get(relation)
        if draft is None:
            return
        rid_s = str(rid)
        self._osm_relation_draft[relation] = [x for x in draft if str(x) != rid_s]
        self._osm_show_props(item)

    def _on_relation_id_edited(self, item, relation: str, old_id: str, new_id: str) -> None:
        if not self._osm_edit_enabled():
            return
        old_id = str(old_id or '').strip()
        new_id = self._extract_first_segment_id(new_id)
        draft = self._osm_relation_draft.get(relation)
        if draft is None:
            return
        updated = []
        for val in draft:
            sval = str(val)
            if sval == old_id:
                if new_id:
                    updated.append(new_id)
            else:
                updated.append(sval)
        dedup = []
        seen = set()
        for v in updated:
            if v and v not in seen:
                seen.add(v)
                dedup.append(v)
        self._osm_relation_draft[relation] = dedup
        self._osm_show_props(item)

    def _on_relation_add_clicked(self, relation: str) -> None:
        if not self._osm_edit_enabled():
            self._show_project_status('Enable OSM Edit mode first')
            return
        self._osm_relation_pick_mode = relation
        self._show_project_status(
            f'Pick mode: click a segment on the map to add as {relation} relation'
        )

    def _commit_relation_edit(self, item, relation: str, reselection: bool = True) -> None:
        meta = self._osm_item_meta.get(item)
        if not meta:
            return
        way_id = str(meta[5])
        current = list(
            self._osm_way_connectivity.get(way_id, ([], []))[0 if relation == 'preceding' else 1]
        )
        desired_raw = self._osm_relation_draft.get(relation) or []
        desired = []
        seen = set()
        for rid in desired_raw:
            rid_s = self._extract_first_segment_id(rid)
            if rid_s and rid_s != way_id and rid_s not in seen:
                seen.add(rid_s)
                desired.append(rid_s)
        for rid in list(current):
            if rid not in desired:
                self._detach_relation_by_way_id(item, relation, rid)
        for rid in desired:
            if rid not in current:
                self._attach_relation_by_way_id(item, relation, rid)
        if reselection:
            self._select_osm_item(item)

    def _make_relation_table(self, item, relation: str, ids: list):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        editable = bool(
            self._osm_edit_enabled() and self._osm_relation_edit_mode.get(relation, False)
        )
        if editable:
            draft_ids = self._osm_relation_draft.get(relation)
            source_ids = list(draft_ids if draft_ids is not None else ids)
        else:
            source_ids = list(ids)
        if not source_ids and not editable:
            source_ids = ['']
        for rid in source_ids:
            rid_s = str(rid)
            row_w = QWidget()
            row = QHBoxLayout(row_w)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            edit = QLineEdit(rid_s)
            edit.setReadOnly(not editable)
            if rid_s:
                edit.installEventFilter(self)
                self._osm_relation_hover_map[edit] = rid_s
                edit.setToolTip('Hover to blink segment')
            if not editable:
                if rid_s:
                    edit.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                    edit.setToolTip('Hover to blink segment, click to select segment')
                    edit.mousePressEvent = (
                        lambda _ev, _rid=rid_s: self._on_segment_id_clicked(_rid)  # type: ignore[attr-defined]
                    )
            else:
                edit.editingFinished.connect(
                    lambda _it=item, _r=relation, _old=rid_s, _e=edit: self._on_relation_id_edited(
                        _it, _r, _old, _e.text()
                    )
                )
            x_btn = QPushButton('✕')
            x_btn.setFixedWidth(TAG_ROW_HEIGHT)
            x_btn.setFixedHeight(TAG_ROW_HEIGHT)
            x_btn.setVisible(editable)
            x_btn.clicked.connect(
                lambda _checked=False, _it=item, _r=relation, _e=edit: (
                    self._on_relation_remove_clicked(_it, _r, _e.text())
                )
            )
            row.addWidget(edit)
            row.addWidget(x_btn)
            layout.addWidget(row_w)
        if editable:
            add_btn = QPushButton('+')
            add_btn.setFixedHeight(TAG_ROW_HEIGHT)
            add_btn.clicked.connect(
                lambda _checked=False, _rel=relation: self._on_relation_add_clicked(_rel)
            )
            layout.addWidget(add_btn)
        return container

    def _extract_first_segment_id(self, text: str) -> str:
        parts = [p.strip() for p in str(text or '').replace(';', ',').split(',') if p.strip()]
        return parts[0] if parts else ''

    def _on_osm_tag_splitter_moved(self, pos: int, _index: int) -> None:
        """Keep key/value column widths synchronized across Segment Properties rows."""
        min_width = int(getattr(self, '_osm_tag_auto_key_col_width_min', 60))
        width = max(min_width, int(pos))
        self._osm_tag_key_col_width = width
        splitters = list(getattr(self, '_osm_tag_splitters', []) or [])
        if not splitters:
            return
        for splitter in splitters:
            if splitter is None:
                continue
            blocked = splitter.blockSignals(True)
            splitter.setSizes([width, 100000])
            splitter.blockSignals(blocked)

    # ── Editable tag grid ─────────────────────────────────────────────

    def _populate_osm_tag_editor(self, tags: dict, item) -> None:
        """Build editable key/value rows inside the properties scroll area."""
        # Clear previous contents
        container = self._osm_tag_editor_widget
        layout = container.layout()
        if layout is not None:
            while layout.count():
                child = layout.takeAt(0)
                w = child.widget()
                if w:
                    w.deleteLater()
        else:
            layout = QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

        self._osm_tag_rows: list = []  # [(key_edit, val_edit), …]
        self._osm_tag_splitters: list = []
        self._stop_osm_blink()
        self._osm_relation_hover_map = {}

        meta = self._osm_item_meta.get(item)
        way_id = str(meta[5]) if meta else '-'
        edits_enabled = self._osm_edit_enabled()

        props_editing = bool(edits_enabled and self._osm_tags_edit_mode)

        # Segment ID is shown as a read-only, non-removable row in the same
        # key/value style as tags.
        id_row = QHBoxLayout()
        id_row.setContentsMargins(0, 0, 0, 0)
        id_row.setSpacing(2)
        id_key_edit = QLineEdit('id')
        id_key_edit.setFixedHeight(TAG_ROW_HEIGHT)
        id_key_edit.setMinimumWidth(60)
        id_key_edit.setReadOnly(True)
        id_val_edit = QLineEdit(way_id)
        id_val_edit.setFixedHeight(TAG_ROW_HEIGHT)
        id_val_edit.setMinimumWidth(80)
        id_val_edit.setReadOnly(True)
        id_splitter = QSplitter(Qt.Orientation.Horizontal)
        id_splitter.setChildrenCollapsible(False)
        id_splitter.addWidget(id_key_edit)
        id_splitter.addWidget(id_val_edit)
        id_splitter.setStretchFactor(0, 0)
        id_splitter.setStretchFactor(1, 1)
        id_splitter.splitterMoved.connect(self._on_osm_tag_splitter_moved)
        self._osm_tag_splitters.append(id_splitter)
        id_del_btn = QPushButton('✕')
        id_del_btn.setFixedWidth(TAG_ROW_HEIGHT)
        id_del_btn.setFixedHeight(TAG_ROW_HEIGHT)
        id_del_btn.setVisible(False)
        id_row.addWidget(id_splitter, 1)
        id_row.addWidget(id_del_btn)
        id_widget = QWidget()
        id_widget.setContentsMargins(0, 0, 0, 0)
        id_widget.setLayout(id_row)
        layout.addWidget(id_widget)

        for k, v in tags.items():
            if str(k) == 'id':
                continue
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            key_edit = QLineEdit(k)
            key_edit.setFixedHeight(TAG_ROW_HEIGHT)
            key_edit.setMinimumWidth(60)
            key_edit.setPlaceholderText('key')
            key_edit.setReadOnly(not props_editing)
            val_edit = QLineEdit(v)
            val_edit.setFixedHeight(TAG_ROW_HEIGHT)
            val_edit.setMinimumWidth(80)
            val_edit.setPlaceholderText('value')
            val_edit.setReadOnly(not props_editing)
            row_splitter = QSplitter(Qt.Orientation.Horizontal)
            row_splitter.setChildrenCollapsible(False)
            row_splitter.addWidget(key_edit)
            row_splitter.addWidget(val_edit)
            row_splitter.setStretchFactor(0, 0)
            row_splitter.setStretchFactor(1, 1)
            row_splitter.splitterMoved.connect(self._on_osm_tag_splitter_moved)
            self._osm_tag_splitters.append(row_splitter)
            del_btn = QPushButton('✕')
            del_btn.setFixedWidth(TAG_ROW_HEIGHT)
            del_btn.setFixedHeight(TAG_ROW_HEIGHT)
            del_btn.setToolTip('Remove tag')
            del_btn.setVisible(props_editing)
            row.addWidget(row_splitter, 1)
            row.addWidget(del_btn)
            w = QWidget()
            w.setContentsMargins(0, 0, 0, 0)
            w.setLayout(row)
            layout.addWidget(w)
            self._osm_tag_rows.append((key_edit, val_edit))
            del_btn.clicked.connect(
                lambda checked, _w=w, _it=item: self._on_osm_tag_delete(_w, _it)
            )

        # "Add tag" button
        add_btn = QPushButton('+ Add tag')
        add_btn.setFixedHeight(TAG_ROW_HEIGHT)
        add_btn.clicked.connect(lambda: self._on_osm_tag_add(item))
        add_btn.setVisible(props_editing)
        layout.addWidget(add_btn)
        layout.addStretch()

        key_texts = ['id'] + [str(k) for k in tags.keys() if str(k) != 'id']
        fm = self._osm_tag_editor_widget.fontMetrics()
        widest_px = max((fm.horizontalAdvance(t) for t in key_texts), default=0)
        auto_width = max(60, int(widest_px + 18))
        self._osm_tag_auto_key_col_width_min = auto_width
        stored_width = int(getattr(self, '_osm_tag_key_col_width', TAG_KEY_FIELD_WIDTH))
        col_width = max(auto_width, stored_width)
        self._on_osm_tag_splitter_moved(col_width, 0)

    def _on_osm_tag_edited(self, item) -> None:
        """Called when a tag key or value is edited — persist to edit state."""
        if not self._osm_edit_enabled():
            return
        new_tags: dict = {}
        for key_edit, val_edit in self._osm_tag_rows:
            k = key_edit.text().strip()
            v = val_edit.text().strip()
            if k:
                new_tags[k] = v
        meta = self._osm_item_meta.get(item)
        if not meta:
            return
        new_pen = self._osm_pen_for_way(new_tags)
        item.setPen(new_pen)
        # Update in-memory meta
        self._osm_item_meta[item] = (
            new_tags.get('highway', meta[0]),
            new_tags,
            QPen(new_pen),
            meta[3],
            meta[4],
            meta[5],
            meta[6],
        )
        self._rebuild_osm_path(item)
        # Record edit
        way_id = meta[5]
        if way_id in self._osm_created_ways:
            self._osm_created_ways[way_id]['tags'] = dict(new_tags)
        else:
            edit = self._osm_edits.setdefault(way_id, {})
            edit['tags'] = dict(new_tags)
        self._mark_osm_dirty()
        self._update_osm_export_btn()
        # After editing tags, schedule an OpenDRIVE refresh from current OSM
        try:
            if hasattr(self, '_schedule_auto_xodr_refresh'):
                self._schedule_auto_xodr_refresh()
        except Exception:
            pass

    def _on_osm_tag_delete(self, row_widget, item) -> None:
        """Remove a tag row and update."""
        if not self._osm_edit_enabled():
            return
        row_widget.deleteLater()
        # Find and remove from _osm_tag_rows
        layout = self._osm_tag_editor_widget.layout()
        new_rows = []
        for key_edit, val_edit in self._osm_tag_rows:
            if key_edit.parent() is not row_widget and val_edit.parent() is not row_widget:
                new_rows.append((key_edit, val_edit))
        self._osm_tag_rows = new_rows
        if not self._osm_tags_edit_mode:
            QTimer.singleShot(0, lambda: self._on_osm_tag_edited(item))

    def _on_osm_tag_add(self, item) -> None:
        """Add a blank tag row."""
        if not self._osm_edit_enabled():
            return
        tags = {}
        for key_edit, val_edit in self._osm_tag_rows:
            k = key_edit.text().strip()
            v = val_edit.text().strip()
            if k:
                tags[k] = v
        tags[''] = ''
        self._populate_osm_tag_editor(tags, item)

    # ── Node dot markers ──────────────────────────────────────────────

    def _apply_osm_node_dot_style(self, dot, selected: bool) -> None:
        """Update node-dot styling for selected vs unselected state."""
        if selected:
            dot.setPen(QPen(OSM_SELECTION_COLOR, OSM_NODE_DOT_OUTLINE_WIDTH + 0.8))
            dot.setBrush(QBrush(QColor(255, 235, 80, 240)))
        else:
            dot.setPen(QPen(OSM_NODE_DOT_OUTLINE_COLOR, OSM_NODE_DOT_OUTLINE_WIDTH))
            dot.setBrush(QBrush(OSM_NODE_DOT_FILL_COLOR))

    def _set_osm_selected_node_index(self, node_index: int | None, dot=None) -> None:
        """Select a node on the current segment, with or without a visible edit dot."""
        prev_node_id = self._osm_selected_node_id()
        item = self._osm_selected_item
        meta = self._osm_item_meta.get(item) if item is not None else None
        if meta is None or node_index is None or not (0 <= int(node_index) < len(meta[6])):
            self._osm_selected_node_index = None
            self._osm_selected_dot = None
        else:
            self._osm_selected_node_index = int(node_index)
            if dot in self._osm_dot_to_index and self._osm_dot_to_index.get(dot) == int(
                node_index
            ):
                self._osm_selected_dot = dot
            else:
                self._osm_selected_dot = None
                for existing_dot, existing_idx in self._osm_dot_to_index.items():
                    if existing_idx == int(node_index):
                        self._osm_selected_dot = existing_dot
                        break
        new_node_id = self._osm_selected_node_id()
        if self._osm_node_tags_edit_mode and prev_node_id and prev_node_id != new_node_id:
            self._on_osm_node_tag_edited(prev_node_id)
        for existing_dot in self._osm_node_dots:
            self._apply_osm_node_dot_style(existing_dot, existing_dot is self._osm_selected_dot)
        self._update_osm_reverse_sign_button()
        self._show_selected_osm_node_props()

    def _set_osm_selected_dot(self, dot) -> None:
        """Select a node dot on the current segment and restyle markers."""
        node_index = self._osm_dot_to_index.get(dot) if dot in self._osm_dot_to_index else None
        self._set_osm_selected_node_index(node_index, dot=dot)

    @staticmethod
    def _osm_sign_heading_offset_deg(tags: dict) -> float:
        if tags is None:
            return 0.0
        # Check standard tags first
        val = tags.get(_OSM_SIGN_HEADING_OFFSET_TAG) or tags.get('traffic_sign:direction')

        # Handle standard OSM relative directions for signs on ways
        if isinstance(val, str):
            v_lower = val.strip().lower()
            if v_lower == 'forward':
                return 0.0
            if v_lower == 'backward':
                return 180.0

        try:
            return float(str(val).strip() or '0')
        except (TypeError, ValueError):
            return 0.0

    def _osm_selected_node_id(self) -> str | None:
        item = self._osm_selected_item
        node_index = self._osm_selected_node_index
        if item is not None and node_index is not None:
            meta = self._osm_item_meta.get(item)
            if meta:
                idx = int(node_index)
                if 0 <= idx < len(meta[6]):
                    return str(meta[6][idx][0])
        # Fall back to selected sign node (locked-mode node selection)
        return getattr(self, '_osm_selected_sign_node_id', None)

    def _osm_original_node_tags(self, node_id: str | None) -> dict:
        nid = str(node_id or '')
        if not nid:
            return {}
        tree = self._osm_original_tree
        if tree is None:
            return {}
        node_el = tree.getroot().find(f"node[@id='{nid}']")
        if node_el is None:
            return {}
        tags = {}
        for tag_el in node_el.findall('tag'):
            k = tag_el.get('k', '')
            v = tag_el.get('v', '')
            if k:
                tags[k] = v
        return tags

    def _osm_current_node_tags(self, node_id: str | None) -> dict:
        nid = str(node_id or '')
        if not nid:
            return {}
        if nid in self._osm_node_tag_edits:
            return dict(self._osm_node_tag_edits[nid])
        return self._osm_original_node_tags(nid)

    def _set_osm_node_tags(self, node_id: str | None, tags: dict) -> bool:
        nid = str(node_id or '').strip()
        if not nid:
            return False
        normalized = {}
        for key, value in dict(tags or {}).items():
            key_s = str(key or '').strip()
            if not key_s:
                continue
            normalized[key_s] = str(value or '').strip()
        original = self._osm_original_node_tags(nid)
        if normalized == original:
            self._osm_node_tag_edits.pop(nid, None)
        else:
            self._osm_node_tag_edits[nid] = normalized
        composed_osm = self._compose_current_osm_content()
        if composed_osm is not None:
            self._osm_content = composed_osm
        self._mark_osm_dirty()
        self._update_osm_export_btn()
        self._update_osm_reverse_sign_button()
        try:
            if hasattr(self, '_schedule_auto_xodr_refresh'):
                self._schedule_auto_xodr_refresh()
        except Exception:
            pass
        return True

    @staticmethod
    def _osm_node_has_sign(tags: dict) -> bool:
        hw = str((tags or {}).get('highway', '')).strip().lower()
        if hw in _OSM_NODE_SIGN_HIGHWAY_VALUES:
            return True
        return bool((tags or {}).get('traffic_sign') or (tags or {}).get('maxspeed'))

    def _update_osm_reverse_sign_button(self) -> None:
        if not hasattr(self, 'btn_reverse_osm_sign'):
            return
        node_id = self._osm_selected_node_id()
        tags = self._osm_current_node_tags(node_id)
        should_show = bool(node_id and self._osm_node_has_sign(tags))
        self.btn_reverse_osm_sign.setVisible(should_show)
        self.btn_reverse_osm_sign.setEnabled(should_show)

    def _reverse_selected_osm_sign(self) -> None:
        node_id = self._osm_selected_node_id()
        if not node_id:
            return
        tags = self._osm_current_node_tags(node_id)
        if not self._osm_node_has_sign(tags):
            return

        new_tags = dict(tags)
        # Determine if we should use forward/backward strings or numeric degrees.
        # We prefer strings if the current value is a string or if it's a cardinal 0/180 flip.
        current_val = tags.get(_OSM_SIGN_HEADING_OFFSET_TAG) or tags.get('traffic_sign:direction')
        use_strings = isinstance(current_val, str) and current_val.strip().lower() in (
            'forward',
            'backward',
        )

        current_offset = self._osm_sign_heading_offset_deg(tags)
        new_offset = (current_offset + 180.0) % 360.0

        # When reversing, always use the new standard tag and clear alternatives
        new_tags.pop('traffic_sign:direction', None)

        if (
            use_strings
            or math.isclose(new_offset, 0.0, abs_tol=1e-6)
            or math.isclose(new_offset, 180.0, abs_tol=1e-6)
        ):
            if math.isclose(new_offset, 0.0, abs_tol=1e-6):
                new_tags[_OSM_SIGN_HEADING_OFFSET_TAG] = 'forward'
            else:
                new_tags[_OSM_SIGN_HEADING_OFFSET_TAG] = 'backward'
        else:
            if math.isclose(new_offset, round(new_offset), abs_tol=1e-6):
                new_tags[_OSM_SIGN_HEADING_OFFSET_TAG] = str(int(round(new_offset)))
            else:
                new_tags[_OSM_SIGN_HEADING_OFFSET_TAG] = f'{new_offset:.6f}'

        self._set_osm_node_tags(node_id, new_tags)
        self._show_selected_osm_node_props()
        self._update_osm_reverse_sign_button()
        self._show_project_status(f'Sign {node_id} reversed by 180 degrees')
        self.refresh_all_layers()

    def _commit_selected_osm_node_tag_edits(self) -> None:
        node_id = self._osm_selected_node_id()
        if node_id:
            self._on_osm_node_tag_edited(node_id)

    def _populate_osm_node_tag_editor(self, node_id: str, tags: dict) -> None:
        container = self._osm_node_tag_editor_widget
        layout = container.layout()
        if layout is not None:
            while layout.count():
                child = layout.takeAt(0)
                w = child.widget()
                if w:
                    w.deleteLater()
        else:
            layout = QVBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

        self._osm_node_tag_rows = []
        props_editing = bool(self._osm_edit_enabled() and self._osm_node_tags_edit_mode)

        def _add_row(key: str, value: str, *, deletable: bool) -> None:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            key_edit = QLineEdit(key)
            key_edit.setFixedHeight(TAG_ROW_HEIGHT)
            key_edit.setMinimumWidth(60)
            key_edit.setPlaceholderText('key')
            key_edit.setReadOnly(not props_editing or not deletable)
            val_edit = QLineEdit(value)
            val_edit.setFixedHeight(TAG_ROW_HEIGHT)
            val_edit.setMinimumWidth(80)
            val_edit.setPlaceholderText('value')
            val_edit.setReadOnly(not props_editing or not deletable)
            row.addWidget(key_edit, 1)
            row.addWidget(val_edit, 1)
            del_btn = QPushButton('✕')
            del_btn.setFixedWidth(TAG_ROW_HEIGHT)
            del_btn.setFixedHeight(TAG_ROW_HEIGHT)
            del_btn.setToolTip('Remove tag')
            del_btn.setVisible(props_editing and deletable)
            row.addWidget(del_btn)
            row_widget = QWidget()
            row_widget.setContentsMargins(0, 0, 0, 0)
            row_widget.setLayout(row)
            layout.addWidget(row_widget)
            self._osm_node_tag_rows.append((row_widget, key_edit, val_edit))
            if props_editing and deletable:
                key_edit.editingFinished.connect(
                    lambda _nid=node_id: self._on_osm_node_tag_edited(_nid)
                )
                val_edit.editingFinished.connect(
                    lambda _nid=node_id: self._on_osm_node_tag_edited(_nid)
                )
                del_btn.clicked.connect(
                    lambda _checked=False, _row=row_widget, _nid=node_id: (
                        self._on_osm_node_tag_delete(_row, _nid)
                    )
                )

        _add_row('id', str(node_id), deletable=False)
        for key, value in tags.items():
            if str(key) == 'id':
                continue
            _add_row(str(key), str(value), deletable=True)

        add_btn = QPushButton('+ Add tag')
        add_btn.setFixedHeight(TAG_ROW_HEIGHT)
        add_btn.setVisible(props_editing)
        add_btn.clicked.connect(
            lambda _checked=False, _nid=node_id: self._on_osm_node_tag_add(_nid)
        )
        layout.addWidget(add_btn)
        layout.addStretch()

        has_sign = self._osm_node_has_sign(tags)
        if hasattr(self, 'btn_osm_node_add_sign'):
            self.btn_osm_node_add_sign.setVisible(props_editing)
            self.btn_osm_node_add_sign.setEnabled(props_editing and not has_sign)
        if hasattr(self, 'btn_osm_node_remove_sign'):
            self.btn_osm_node_remove_sign.setVisible(props_editing)
            self.btn_osm_node_remove_sign.setEnabled(props_editing and has_sign)

    def _on_osm_node_tag_edited(self, node_id: str | None) -> None:
        if not self._osm_edit_enabled():
            return
        new_tags = {}
        for _row_widget, key_edit, val_edit in self._osm_node_tag_rows:
            key = key_edit.text().strip()
            value = val_edit.text().strip()
            if key and key != 'id':
                new_tags[key] = value
        if self._set_osm_node_tags(node_id, new_tags):
            self._show_selected_osm_node_props()

    def _on_osm_node_tag_delete(self, row_widget, node_id: str | None) -> None:
        if not self._osm_edit_enabled():
            return
        row_widget.deleteLater()
        self._osm_node_tag_rows = [
            row for row in self._osm_node_tag_rows if row[0] is not row_widget
        ]
        self._on_osm_node_tag_edited(node_id)

    def _on_osm_node_tag_add(self, node_id: str | None) -> None:
        if not self._osm_edit_enabled():
            return
        tags = self._osm_current_node_tags(node_id)
        suffix = 1
        blank_key = ''
        while blank_key in tags:
            suffix += 1
            blank_key = f'new_tag_{suffix}'
        tags[blank_key] = ''
        self._populate_osm_node_tag_editor(str(node_id or ''), tags)

    def _add_sign_info_to_selected_osm_node(self) -> None:
        if not self._osm_edit_enabled():
            self._show_project_status('Enable OSM Edit mode first')
            return
        node_id = self._osm_selected_node_id()
        if not node_id:
            self._show_project_status('Select an OSM node first')
            return
        tags = self._osm_current_node_tags(node_id)
        if self._osm_node_has_sign(tags):
            self._show_project_status(f'Node {node_id} already has sign information')
            return
        new_tags = dict(tags)
        new_tags['traffic_sign'] = str(new_tags.get('traffic_sign') or 'unknown')
        self._set_osm_node_tags(node_id, new_tags)
        self._show_selected_osm_node_props()
        self._show_project_status(f'Added sign information to node {node_id}')

    def _remove_sign_info_from_selected_osm_node(self) -> None:
        if not self._osm_edit_enabled():
            self._show_project_status('Enable OSM Edit mode first')
            return
        node_id = self._osm_selected_node_id()
        if not node_id:
            self._show_project_status('Select an OSM node first')
            return
        tags = self._osm_current_node_tags(node_id)
        if not self._osm_node_has_sign(tags):
            self._show_project_status(f'Node {node_id} has no sign information to remove')
            return
        new_tags = dict(tags)
        for key in _OSM_NODE_SIGN_TAG_KEYS:
            new_tags.pop(key, None)
        if str(new_tags.get('highway', '')).strip().lower() in _OSM_NODE_SIGN_HIGHWAY_VALUES:
            new_tags.pop('highway', None)
        new_tags.pop(_OSM_SIGN_HEADING_OFFSET_TAG, None)
        new_tags.pop('traffic_sign:direction', None)
        self._set_osm_node_tags(node_id, new_tags)
        self._show_selected_osm_node_props()
        self._show_project_status(f'Removed sign information from node {node_id}')

    def _show_osm_node_dots(self, item) -> None:
        """Place draggable dot markers on each node of the selected OSM segment."""
        selected_index = self._osm_selected_node_index if item is self._osm_selected_item else None
        self._remove_osm_node_dots()
        meta = self._osm_item_meta.get(item)
        if not meta:
            return
        scene_coords = meta[3]
        r = self._OSM_NODE_DOT_RADIUS
        edit_enabled = self._osm_edit_enabled()
        for idx, (sx, sy) in enumerate(scene_coords):
            dot = QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
            dot.setPos(sx, sy)
            dot.setZValue(Z_OSM_NODE_DOTS)  # above everything
            dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
            dot.setCursor(
                QCursor(
                    Qt.CursorShape.CrossCursor
                    if edit_enabled
                    else Qt.CursorShape.PointingHandCursor
                )
            )
            dot.setAcceptedMouseButtons(
                Qt.MouseButton.LeftButton if edit_enabled else Qt.MouseButton.NoButton
            )
            # Mark as cosmetic so it doesn't scale with zoom
            dot.setFlags(dot.flags() | QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            self.scene.addItem(dot)
            self._osm_node_dots.append(dot)
            self._osm_dot_to_index[dot] = idx
            self._apply_osm_node_dot_style(dot, False)

        if selected_index is None:
            self._set_osm_selected_node_index(None)
            return
        selected_dot = None
        for dot, idx in self._osm_dot_to_index.items():
            if idx == selected_index:
                selected_dot = dot
                break
        self._set_osm_selected_node_index(selected_index, dot=selected_dot)

    def _show_osm_selected_direction_arrows(self, item) -> None:
        self._remove_osm_selected_direction_arrows()
        meta = self._osm_item_meta.get(item)
        if not meta:
            return
        coords = meta[3]
        if len(coords) < 2:
            return
        direction_mode = self._osm_direction_mode_from_tags(meta[1])
        half_len = OSM_DIRECTION_ARROW_LENGTH_PX * 0.75
        half_w = OSM_DIRECTION_ARROW_WIDTH_PX * 0.75
        poly = QPolygonF(
            [
                QPointF(half_len, 0.0),
                QPointF(-half_len, half_w),
                QPointF(-half_len, -half_w),
            ]
        )
        for i in range(len(coords) - 1):
            x0, y0 = coords[i]
            x1, y1 = coords[i + 1]
            dx = x1 - x0
            dy = y1 - y0
            seg_len = math.hypot(dx, dy)
            if seg_len < 1e-6:
                continue
            mx = (x0 + x1) * 0.5
            my = (y0 + y1) * 0.5
            nx = -dy / seg_len
            ny = dx / seg_len
            off = OSM_DIRECTION_BIDIR_OFFSET_PX

            def _add_arrow(dir_sign: float, normal_sign: float) -> None:
                angle_deg = math.degrees(math.atan2(dy * dir_sign, dx * dir_sign))
                arrow = QGraphicsPolygonItem(poly)
                arrow.setPos(mx + nx * off * normal_sign, my + ny * off * normal_sign)
                arrow.setRotation(angle_deg)
                arrow.setPen(QPen(OSM_SELECTED_ARROW_OUTLINE_COLOR, 1.0))
                arrow.setBrush(QBrush(OSM_SELECTED_ARROW_FILL_COLOR))
                arrow.setZValue(Z_OSM_SELECTED_ARROWS)
                arrow.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
                arrow.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                arrow.setAcceptHoverEvents(False)
                self.scene.addItem(arrow)
                self._osm_selected_arrows.append(arrow)

            if direction_mode == 'both':
                _add_arrow(1.0, 1.0)
                _add_arrow(-1.0, -1.0)
            elif direction_mode == 'reverse':
                _add_arrow(-1.0, 0.0)
            else:
                _add_arrow(1.0, 0.0)

    def _remove_osm_selected_direction_arrows(self) -> None:
        for arrow in self._osm_selected_arrows:
            self.scene.removeItem(arrow)
        self._osm_selected_arrows.clear()

    def _remove_osm_node_dots(self) -> None:
        """Remove all node dot markers from the scene."""
        for dot in self._osm_node_dots:
            self.scene.removeItem(dot)
        self._osm_node_dots.clear()
        self._osm_dot_to_index.clear()
        self._osm_selected_dot = None
        self._osm_dragging_dot = None
        if hasattr(self, '_osm_node_props_group'):
            self._osm_node_props_group.setVisible(False)
        self._update_osm_reverse_sign_button()

    def _osm_selected_item_node_index_at(self, scene_pos, max_pick_px: float = 14.0):
        """Return the nearest node index on the selected segment within tolerance."""
        item = self._osm_selected_item
        if item is None:
            return None
        meta = self._osm_item_meta.get(item)
        if not meta:
            return None
        coords = meta[3]
        if not coords:
            return None
        px = float(scene_pos.x())
        py = float(scene_pos.y())
        tolerance = self._osm_pick_tolerance_scene(max_pick_px)
        tolerance_sq = tolerance * tolerance
        best_index = None
        best_dist_sq = tolerance_sq
        for idx, (sx, sy) in enumerate(coords):
            dx = px - float(sx)
            dy = py - float(sy)
            dist_sq = dx * dx + dy * dy
            if dist_sq <= best_dist_sq:
                best_dist_sq = dist_sq
                best_index = idx
        return best_index

    def _rebuild_osm_path(self, item, rebuild_arrows: bool = True) -> None:
        """Rebuild the QPainterPath for an OSM item from its (possibly edited) scene coords."""
        meta = self._osm_item_meta.get(item)
        if not meta:
            return
        scene_coords = meta[3]
        path = QPainterPath()
        for i, (sx, sy) in enumerate(scene_coords):
            if i == 0:
                path.moveTo(sx, sy)
            else:
                path.lineTo(sx, sy)
        item.setPath(path)
        if rebuild_arrows and isinstance(item, OSMWayPathItem):
            item.set_way_scene_coords(scene_coords)
            item.set_direction_mode(self._osm_direction_mode_from_tags(meta[1]))
        if item is self._osm_selected_item:
            self._show_osm_selected_direction_arrows(item)

    def _osm_pen_for_way(self, tags: dict) -> QPen:
        highway = str(tags.get('highway', ''))
        rgba, width = _OSM_HIGHWAY_STYLES.get(highway, _OSM_HIGHWAY_DEFAULT)
        pen = QPen(QColor(*rgba))
        pen.setWidthF(float(width) * OSM_LINE_THICKNESS)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return pen

    def _osm_direction_mode_from_tags(self, tags: dict) -> str:
        oneway = str(tags.get('oneway', '')).strip().lower()
        if oneway in ('-1', 'reverse'):
            return 'reverse'
        if oneway in ('yes', 'true', '1'):
            return 'forward'

        def _to_int(v):
            try:
                return int(float(str(v).strip()))
            except Exception:
                return 0

        lanes_fwd = _to_int(tags.get('lanes:forward', 0))
        lanes_bwd = _to_int(tags.get('lanes:backward', 0))
        if lanes_fwd > 0 and lanes_bwd > 0:
            return 'both'
        lanes_total = _to_int(tags.get('lanes', 0))
        if lanes_total >= 2:
            return 'both'
        return 'forward'

    def _osm_record_new_way(self, way_id: str, tags: dict, node_refs: list) -> None:
        way_tags = dict(tags or {})
        if not str(way_tags.get('highway', '')).strip():
            way_tags['highway'] = 'residential'
        self._osm_created_ways[str(way_id)] = {
            'tags': way_tags,
            'node_coords': [(str(nid), float(lat), float(lon)) for nid, lat, lon in node_refs],
        }
        self._mark_osm_dirty()
        self._update_osm_export_btn()

    def _on_osm_split_way(self, scene_pos) -> bool:
        """Split the selected way at a clicked interior node (Shift+Right-click)."""
        if not self._osm_edit_enabled():
            return False
        item = self._osm_selected_item
        if item is None:
            return False
        meta = self._osm_item_meta.get(item)
        if not meta:
            return False

        dot = self._osm_dot_at(scene_pos)
        if dot is None:
            return False
        split_idx = int(self._osm_dot_to_index.get(dot, -1))
        node_refs = list(meta[6])
        if split_idx <= 0 or split_idx >= len(node_refs) - 1:
            return False  # must split on an interior node

        first_refs = node_refs[: split_idx + 1]
        second_refs = node_refs[split_idx:]
        first_scene = list(meta[3][: split_idx + 1])
        second_scene = list(meta[3][split_idx:])
        first_latlon = list(meta[4][: split_idx + 1])
        second_latlon = list(meta[4][split_idx:])

        first_meta = (
            meta[0],
            dict(meta[1]),
            QPen(meta[2]),
            first_scene,
            first_latlon,
            meta[5],
            first_refs,
        )
        self._osm_item_meta[item] = first_meta
        self._rebuild_osm_path(item)
        self._osm_persist_node_edit(item)

        new_way_id = str(self._osm_next_way_id)
        self._osm_next_way_id += 1
        new_pen = self._osm_pen_for_way(meta[1])
        path = QPainterPath()
        for i, (sx, sy) in enumerate(second_scene):
            if i == 0:
                path.moveTo(sx, sy)
            else:
                path.lineTo(sx, sy)
        new_item = OSMWayPathItem(path)
        new_item.setPen(new_pen)
        new_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        new_item.set_way_scene_coords(second_scene)
        new_item.set_direction_mode(self._osm_direction_mode_from_tags(meta[1]))
        if self._osm_vector_group is not None:
            new_item.setParentItem(self._osm_vector_group)
        else:
            self.scene.addItem(new_item)

        self._osm_item_meta[new_item] = (
            meta[0],
            dict(meta[1]),
            QPen(new_pen),
            second_scene,
            second_latlon,
            new_way_id,
            second_refs,
        )
        new_tags = dict(meta[1] or {})
        if not str(new_tags.get('highway', '')).strip():
            new_tags['highway'] = str(meta[0] or 'residential')
        self._osm_record_new_way(new_way_id, new_tags, second_refs)

        self._rebuild_osm_connectivity()
        self._show_osm_node_dots(item)
        self._show_project_status('OSM way split at selected node')
        return True

    def _scene_to_latlon(self, sx: float, sy: float):
        """Inverse of latlon_to_scene: scene coords → (lat, lon).

        Uses the stored projection parameters.  This is the inverse of the
        ellipsoidal TM forward used during OSM loading.
        """
        if not self.map_ctx:
            return 0.0, 0.0
        mpp = self.map_ctx.mpp
        min_x = self.map_ctx.world_bounds[0]
        min_y = self.map_ctx.world_bounds[2]
        # Recover CARLA (world) coords
        x_tm = sx * mpp + min_x
        carla_y = sy * mpp + min_y
        y_tm = -carla_y  # invert the CARLA Y-negation
        # Inverse TM
        ref_lat = self.map_ctx.earth_ref_lat
        ref_lon = self.map_ctx.earth_ref_lon
        x0 = getattr(self.map_ctx, 'proj_false_easting', 0.0)
        y0 = getattr(self.map_ctx, 'proj_false_northing', 0.0)
        k0 = getattr(self.map_ctx, 'proj_scale_factor', 1.0)
        lat, lon = _tmerc_inverse_wgs84(x_tm, y_tm, ref_lat, ref_lon, k0, x0, y0)
        return lat, lon

    def _osm_pick_tolerance_scene(self, pixels: float = 10.0) -> float:
        """Convert a viewport-pixel hit tolerance to scene units."""
        scale = float(self.view.transform().m11() or 1.0)
        return max(float(pixels) / max(scale, 1e-6), 1.0)

    @staticmethod
    def _point_to_segment_distance_sq(
        px: float,
        py: float,
        ax: float,
        ay: float,
        bx: float,
        by: float,
    ) -> float:
        """Return squared distance from point P to line segment AB."""
        abx = bx - ax
        aby = by - ay
        apx = px - ax
        apy = py - ay
        ab_len_sq = abx * abx + aby * aby
        if ab_len_sq <= 1e-12:
            return apx * apx + apy * apy
        t = max(0.0, min(1.0, (apx * abx + apy * aby) / ab_len_sq))
        cx = ax + t * abx
        cy = ay + t * aby
        dx = px - cx
        dy = py - cy
        return dx * dx + dy * dy

    def _osm_nearest_way_item_at(self, scene_pos, exclude_item=None, max_pick_px: float = 10.0):
        """Return the nearest OSM way to scene_pos within a screen-space tolerance."""
        if not self._osm_item_meta:
            return None

        px = float(scene_pos.x())
        py = float(scene_pos.y())
        tolerance = self._osm_pick_tolerance_scene(max_pick_px)
        tolerance_sq = tolerance * tolerance
        best_item = None
        best_dist_sq = tolerance_sq

        for item, meta in self._osm_item_meta.items():
            if item is exclude_item:
                continue

            scene_coords = meta[3] if len(meta) > 3 else None
            if not scene_coords:
                continue

            min_x = min(pt[0] for pt in scene_coords) - tolerance
            max_x = max(pt[0] for pt in scene_coords) + tolerance
            min_y = min(pt[1] for pt in scene_coords) - tolerance
            max_y = max(pt[1] for pt in scene_coords) + tolerance
            if px < min_x or px > max_x or py < min_y or py > max_y:
                continue

            if len(scene_coords) == 1:
                dx = px - float(scene_coords[0][0])
                dy = py - float(scene_coords[0][1])
                dist_sq = dx * dx + dy * dy
            else:
                dist_sq = min(
                    self._point_to_segment_distance_sq(
                        px,
                        py,
                        float(scene_coords[idx][0]),
                        float(scene_coords[idx][1]),
                        float(scene_coords[idx + 1][0]),
                        float(scene_coords[idx + 1][1]),
                    )
                    for idx in range(len(scene_coords) - 1)
                )

            if dist_sq <= best_dist_sq:
                best_dist_sq = dist_sq
                best_item = item

        return best_item

    # ── Hover ─────────────────────────────────────────────────────────

    def _update_osm_hover(self, scene_pos) -> None:
        """Highlight the OSM road segment under the cursor and show its tags."""
        # Don't update hover while dragging a node
        if self._osm_dragging_dot is not None or self._osm_dragging_way_item is not None:
            return
        if not self.check_osm.isChecked() or self.spin_osm_alpha.value() <= 0.0:
            self._clear_osm_hover()
            return
        if not self._osm_item_meta:
            return
        hit_item = self._osm_nearest_way_item_at(scene_pos)
        if hit_item is self._osm_hover_item:
            return  # nothing changed
        # Restore previous hover item (but not if it is the selected item)
        self._clear_osm_hover(restore_selected=False)
        self._osm_hover_item = hit_item
        if hit_item is None:
            return
        # Highlight hovered item (yellow for hover)
        if hit_item is not self._osm_selected_item and hit_item not in getattr(
            self, '_osm_multi_selected_items', set()
        ):
            self._osm_highlight_item(hit_item, OSM_HOVER_COLOR)

    def _clear_osm_hover(self, restore_selected: bool = True) -> None:
        """Restore base pen for the hovered OSM item (leave selected item alone)."""
        if (
            self._osm_hover_item is not None
            and self._osm_hover_item is not self._osm_selected_item
            and self._osm_hover_item not in getattr(self, '_osm_multi_selected_items', set())
        ):
            meta = self._osm_item_meta.get(self._osm_hover_item)
            if meta:
                self._osm_hover_item.setPen(meta[2])
        self._osm_hover_item = None

    # ── Click / selection ─────────────────────────────────────────────

    def _osm_sign_node_at(self, scene_pos, max_pick_px: float = 14.0):
        """Return the sign node item nearest to scene_pos within tolerance, or None."""
        if not self._osm_sign_item_positions:
            return None
        tolerance = self._osm_pick_tolerance_scene(max_pick_px)
        tolerance_sq = tolerance * tolerance
        px = float(scene_pos.x())
        py = float(scene_pos.y())
        best_item = None
        best_dist_sq = tolerance_sq
        for item, (sx, sy) in self._osm_sign_item_positions.items():
            if not item.isVisible():
                continue
            dx = px - sx
            dy = py - sy
            dist_sq = dx * dx + dy * dy
            if dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best_item = item
        return best_item

    def _select_osm_sign_item(self, item) -> None:
        """Select or deselect a sign node item, showing its tags read-only."""
        prev = self._osm_selected_sign_item
        if prev is not None and prev is not item:
            orig_pen = prev.data(3)
            if orig_pen is not None:
                prev.setPen(orig_pen)
        self._osm_selected_sign_item = item
        if item is None:
            self._osm_selected_sign_node_id = None
            if hasattr(self, '_osm_node_props_group'):
                self._osm_node_props_group.setVisible(False)
            return
        nid = item.data(2)
        self._osm_selected_sign_node_id = str(nid) if nid is not None else None
        highlight_pen = QPen(QColor(255, 165, 0))
        highlight_pen.setWidthF(2.5)
        highlight_pen.setCosmetic(True)
        item.setPen(highlight_pen)
        self._show_selected_osm_node_props()

    def _delete_selected_osm_sign_node(self) -> bool:
        """Delete the currently selected standalone/sign node (ghost node) via keyboard."""
        if not self._osm_edit_enabled():
            return False
        item = getattr(self, '_osm_selected_sign_item', None)
        if item is None:
            return False
        nid = item.data(2)
        self._osm_sign_item_positions.pop(item, None)
        self.scene.removeItem(item)
        self._select_osm_sign_item(None)
        if nid is not None:
            self._osm_deleted_node_ids.add(str(nid))
            self._osm_node_tag_edits.pop(str(nid), None)
        self._mark_osm_dirty()
        if hasattr(self, '_schedule_auto_xodr_refresh'):
            self._schedule_auto_xodr_refresh()
        return True

    def _on_osm_click(self, scene_pos) -> None:
        """Select / deselect an OSM road segment or sign node on click."""
        if not self.check_osm.isChecked() or self.spin_osm_alpha.value() <= 0.0:
            return
        if not self._osm_item_meta and not self._osm_sign_item_positions:
            return

        # Sign nodes take priority: check them before segments
        sign_hit = self._osm_sign_node_at(scene_pos)
        if sign_hit is not None:
            if sign_hit is self._osm_selected_sign_item:
                # Toggle off — deselect sign
                self._select_osm_sign_item(None)
                return
            self._select_osm_item(None)  # deselect any segment
            self._select_osm_sign_item(sign_hit)
            return

        # No sign hit — clear any sign selection and handle way segments
        if self._osm_selected_sign_item is not None:
            self._select_osm_sign_item(None)

        node_hit_index = self._osm_selected_item_node_index_at(scene_pos)
        if node_hit_index is not None:
            if self._osm_selected_node_index == node_hit_index:
                self._set_osm_selected_node_index(None)
            else:
                self._set_osm_selected_node_index(node_hit_index)
            return

        if not self._osm_item_meta:
            return
        hit_item = self._osm_nearest_way_item_at(scene_pos)
        prev = self._osm_selected_item
        if hit_item is prev:
            # Toggle off — deselect
            self._select_osm_item(None)
            return
        self._select_osm_item(hit_item)

    # ── Node dragging ─────────────────────────────────────────────────

    def _osm_dot_at(self, scene_pos) -> 'QGraphicsEllipseItem | None':
        """Return the node-dot item nearest to *scene_pos* in screen space, or None.

        Using scene.items() is unreliable for ItemIgnoresTransformations dots because
        Qt requires the view's deviceTransform to compute the correct screen-space hit
        area; without it the hit region is the raw local rect in scene units (10×10 at
        any zoom), which mis-selects the wrong dot at zoom ≠ 1.  We instead project
        both the click and every dot centre into viewport pixels and pick the closest
        one within the dot's screen radius.
        """
        if not self._osm_dot_to_index:
            return None
        view_pos = self.view.mapFromScene(scene_pos)
        vx, vy = float(view_pos.x()), float(view_pos.y())
        r = self._OSM_NODE_DOT_RADIUS
        best_dot = None
        best_dist_sq = r * r  # must be within the visual dot radius
        for dot in self._osm_dot_to_index:
            dp = self.view.mapFromScene(dot.pos())
            dx = vx - float(dp.x())
            dy = vy - float(dp.y())
            dist_sq = dx * dx + dy * dy
            if dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best_dot = dot
        return best_dot

    def _osm_way_item_at(self, scene_pos, exclude_item=None):
        """Return OSM way item under scene_pos, optionally excluding one item."""
        return self._osm_nearest_way_item_at(scene_pos, exclude_item=exclude_item)

    def _on_osm_node_press(self, scene_pos, ctrl_pressed: bool = False) -> bool:
        """Select a node dot, and start dragging only when Ctrl is held."""
        if not self._osm_edit_enabled():
            return False
        dot = self._osm_dot_at(scene_pos)
        if dot is None:
            return False
        self._set_osm_selected_dot(dot)
        if not bool(ctrl_pressed):
            self._osm_dragging_dot = None
            self._osm_drag_start_scene = None
            self._osm_drag_start_latlon = None
            return True

        self._osm_dragging_dot = dot
        # Snapshot starting position for undo
        idx = self._osm_dot_to_index[dot]
        item = self._osm_selected_item
        if item is not None:
            meta = self._osm_item_meta.get(item)
            if meta:
                self._osm_drag_start_scene = meta[3][idx]
                self._osm_drag_start_latlon = meta[4][idx]
        self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
        return True

    def _on_osm_node_move(self, scene_pos) -> bool:
        """Move the dragged node dot.  Returns True if consumed."""
        if not self._osm_edit_enabled():
            return False
        dot = self._osm_dragging_dot
        if dot is None:
            return False
        dot.setPos(scene_pos)
        # Live-update the path
        idx = self._osm_dot_to_index[dot]
        item = self._osm_selected_item
        if item is None:
            return True
        meta = self._osm_item_meta.get(item)
        if meta:
            meta[3][idx] = (scene_pos.x(), scene_pos.y())
            self._rebuild_osm_path(item, rebuild_arrows=False)
            nid = str(meta[6][idx][0])
            self._osm_propagate_shared_node_scene(
                nid,
                scene_pos.x(),
                scene_pos.y(),
                source_item=item,
                source_idx=idx,
            )
        return True

    def _on_osm_node_release(self, scene_pos) -> bool:
        """Finish dragging a node dot.  Returns True if consumed."""
        if not self._osm_edit_enabled():
            return False
        dot = self._osm_dragging_dot
        if dot is None:
            return False
        self._osm_dragging_dot = None
        self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
        self._set_osm_selected_dot(dot)
        idx = self._osm_dot_to_index[dot]
        item = self._osm_selected_item
        if item is None:
            self._osm_drag_start_scene = None
            self._osm_drag_start_latlon = None
            return True
        meta = self._osm_item_meta.get(item)
        if not meta:
            self._osm_drag_start_scene = None
            self._osm_drag_start_latlon = None
            return True
        # Convert scene pos → lat/lon and persist
        sx, sy = scene_pos.x(), scene_pos.y()
        meta[3][idx] = (sx, sy)
        lat, lon = self._scene_to_latlon(sx, sy)
        meta[4][idx] = (lat, lon)
        # Update node_refs lat/lon
        nid, _, _ = meta[6][idx]
        meta[6][idx] = (nid, lat, lon)
        self._rebuild_osm_path(item)
        self._osm_propagate_shared_node_all(
            str(nid),
            sx,
            sy,
            lat,
            lon,
            source_item=item,
            source_idx=idx,
            persist=True,
        )
        # Push undo action (clear redo on new action)
        if self._osm_drag_start_scene is not None:
            self._osm_undo_stack.append(
                {
                    'item': item,
                    'idx': idx,
                    'old_scene': self._osm_drag_start_scene,
                    'new_scene': (sx, sy),
                    'old_latlon': self._osm_drag_start_latlon,
                    'new_latlon': (lat, lon),
                }
            )
            self._osm_redo_stack.clear()
        self._osm_drag_start_scene = None
        self._osm_drag_start_latlon = None
        # Record edit
        way_id = meta[5]
        edit = self._osm_edits.setdefault(way_id, {})
        edit['node_coords'] = [(nid, lat, lon) for nid, lat, lon in meta[6]]
        self._mark_osm_dirty()
        self._update_osm_export_btn()
        self._schedule_auto_xodr_refresh()
        return True

    def _is_roundabout_osm_item(self, item) -> bool:
        """Return True when *item* is tagged as a roundabout in OSM metadata."""
        meta = self._osm_item_meta.get(item)
        if not meta:
            return False
        tags = meta[1] or {}
        junction = str(tags.get('junction', '')).strip().lower()
        if junction == 'roundabout':
            return True
        highway = str(tags.get('highway', '')).strip().lower()
        return highway == 'mini_roundabout'

    def _on_osm_way_press(self, scene_pos, ctrl_pressed: bool = False) -> bool:
        """Start dragging a selected roundabout way. Returns True if consumed."""
        if not self._osm_edit_enabled():
            return False
        if not bool(ctrl_pressed):
            return False
        if self._osm_dragging_dot is not None:
            return False
        item = self._osm_selected_item
        if item is None or not self._is_roundabout_osm_item(item):
            return False
        hit_item = self._osm_way_item_at(scene_pos)
        if hit_item is not item:
            return False
        self._osm_dragging_way_item = item
        self._osm_way_drag_last_scene = QPointF(scene_pos)
        self._osm_way_drag_had_motion = False
        self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
        return True

    def _on_osm_way_move(self, scene_pos) -> bool:
        """Move the currently dragged roundabout way. Returns True if consumed."""
        if not self._osm_edit_enabled():
            return False
        item = self._osm_dragging_way_item
        last_scene = self._osm_way_drag_last_scene
        if item is None or last_scene is None:
            return False
        meta = self._osm_item_meta.get(item)
        if not meta:
            return False

        dx = float(scene_pos.x() - last_scene.x())
        dy = float(scene_pos.y() - last_scene.y())
        self._osm_way_drag_last_scene = QPointF(scene_pos)
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return True
        self._osm_way_drag_had_motion = True

        node_scene = meta[3]
        for idx in range(len(node_scene)):
            sx, sy = node_scene[idx]
            node_scene[idx] = (float(sx) + dx, float(sy) + dy)

        self._rebuild_osm_path(item, rebuild_arrows=False)

        node_idx_by_id: dict[str, int] = {}
        for idx, (nid, _lat, _lon) in enumerate(meta[6]):
            key = str(nid)
            if key not in node_idx_by_id:
                node_idx_by_id[key] = idx
        for nid, idx in node_idx_by_id.items():
            sx, sy = node_scene[idx]
            self._osm_propagate_shared_node_scene(
                nid,
                sx,
                sy,
                source_item=item,
                source_idx=idx,
            )

        if item is self._osm_selected_item:
            for idx, dot in enumerate(self._osm_node_dots):
                if idx < len(node_scene):
                    sx, sy = node_scene[idx]
                    dot.setPos(sx, sy)
        return True

    def _on_osm_way_release(self, scene_pos) -> bool:
        """Finish dragging the selected roundabout way. Returns True if consumed."""
        if not self._osm_edit_enabled():
            return False
        item = self._osm_dragging_way_item
        if item is None:
            return False

        # Snap to final cursor position before committing.
        self._on_osm_way_move(scene_pos)

        self._osm_dragging_way_item = None
        self._osm_way_drag_last_scene = None

        if not self._osm_way_drag_had_motion:
            self._osm_way_drag_had_motion = False
            return True
        self._osm_way_drag_had_motion = False

        meta = self._osm_item_meta.get(item)
        if not meta:
            return True

        node_scene = meta[3]
        node_refs = meta[6]
        for idx in range(len(node_scene)):
            sx, sy = node_scene[idx]
            lat, lon = self._scene_to_latlon(sx, sy)
            meta[4][idx] = (lat, lon)
            nid = node_refs[idx][0]
            node_refs[idx] = (str(nid), lat, lon)

        node_state_by_id: dict[str, tuple[float, float, float, float, int]] = {}
        for idx, (nid, lat, lon) in enumerate(node_refs):
            key = str(nid)
            if key in node_state_by_id:
                continue
            sx, sy = node_scene[idx]
            node_state_by_id[key] = (sx, sy, float(lat), float(lon), idx)

        for nid, (sx, sy, lat, lon, idx) in node_state_by_id.items():
            self._osm_propagate_shared_node_all(
                nid,
                sx,
                sy,
                lat,
                lon,
                source_item=item,
                source_idx=idx,
                persist=True,
            )

        self._rebuild_osm_path(item)
        if item is self._osm_selected_item:
            self._show_osm_node_dots(item)
        self._osm_persist_node_edit(item)
        self._schedule_auto_xodr_refresh()
        return True

    def _on_osm_way_nudge(self, dx_scene: float, dy_scene: float) -> bool:
        """Translate selected roundabout way by a scene-space delta."""
        if not self._osm_edit_enabled():
            return False
        item = self._osm_selected_item
        if item is None or not self._is_roundabout_osm_item(item):
            return False
        dx = float(dx_scene)
        dy = float(dy_scene)
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return False
        meta = self._osm_item_meta.get(item)
        if not meta:
            return False

        node_scene = meta[3]
        for idx in range(len(node_scene)):
            sx, sy = node_scene[idx]
            node_scene[idx] = (float(sx) + dx, float(sy) + dy)

        self._rebuild_osm_path(item, rebuild_arrows=False)

        node_idx_by_id: dict[str, int] = {}
        for idx, (nid, _lat, _lon) in enumerate(meta[6]):
            key = str(nid)
            if key not in node_idx_by_id:
                node_idx_by_id[key] = idx
        for nid, idx in node_idx_by_id.items():
            sx, sy = node_scene[idx]
            self._osm_propagate_shared_node_scene(
                nid,
                sx,
                sy,
                source_item=item,
                source_idx=idx,
            )

        node_refs = meta[6]
        for idx in range(len(node_scene)):
            sx, sy = node_scene[idx]
            lat, lon = self._scene_to_latlon(sx, sy)
            meta[4][idx] = (lat, lon)
            nid = node_refs[idx][0]
            node_refs[idx] = (str(nid), lat, lon)

        node_state_by_id: dict[str, tuple[float, float, float, float, int]] = {}
        for idx, (nid, lat, lon) in enumerate(node_refs):
            key = str(nid)
            if key in node_state_by_id:
                continue
            sx, sy = node_scene[idx]
            node_state_by_id[key] = (sx, sy, float(lat), float(lon), idx)

        for nid, (sx, sy, lat, lon, idx) in node_state_by_id.items():
            self._osm_propagate_shared_node_all(
                nid,
                sx,
                sy,
                lat,
                lon,
                source_item=item,
                source_idx=idx,
                persist=True,
            )

        self._rebuild_osm_path(item)
        if item is self._osm_selected_item:
            self._show_osm_node_dots(item)
        self._osm_persist_node_edit(item)
        return True

    def _osm_propagate_shared_node_scene(
        self,
        node_id: str,
        sx: float,
        sy: float,
        source_item=None,
        source_idx: int | None = None,
    ) -> list:
        touched = []
        nid_s = str(node_id)
        for it, m in self._osm_item_meta.items():
            refs = m[6]
            changed = False
            for j, (nid, _lat, _lon) in enumerate(refs):
                if str(nid) != nid_s:
                    continue
                if it is source_item and source_idx is not None and j == source_idx:
                    continue
                m[3][j] = (float(sx), float(sy))
                changed = True
            if changed:
                self._rebuild_osm_path(it, rebuild_arrows=False)
                touched.append(it)
        return touched

    def _osm_propagate_shared_node_all(
        self,
        node_id: str,
        sx: float,
        sy: float,
        lat: float,
        lon: float,
        source_item=None,
        source_idx: int | None = None,
        persist: bool = False,
    ) -> list:
        touched = []
        nid_s = str(node_id)
        for it, m in self._osm_item_meta.items():
            refs = m[6]
            changed = False
            for j, (nid, _lat_old, _lon_old) in enumerate(refs):
                if str(nid) != nid_s:
                    continue
                if it is source_item and source_idx is not None and j == source_idx:
                    continue
                m[3][j] = (float(sx), float(sy))
                m[4][j] = (float(lat), float(lon))
                m[6][j] = (str(nid), float(lat), float(lon))
                changed = True
            if changed:
                self._rebuild_osm_path(it)
                if persist:
                    self._osm_persist_node_edit(it)
                touched.append(it)
        return touched

    def _resolve_stitch_tags(self, tags_a: dict, tags_b: dict) -> dict | None:
        if dict(tags_a) == dict(tags_b):
            return dict(tags_a)
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setWindowTitle('Stitch Roads: Tag Conflict')
        msg.setText('The two roads have different tags. Which tags should be kept?')
        diff_keys = sorted(set(tags_a.keys()) | set(tags_b.keys()))
        diff_lines = []
        for k in diff_keys:
            va = str(tags_a.get(k, ''))
            vb = str(tags_b.get(k, ''))
            if va != vb:
                diff_lines.append(f'{k}: selected="{va}" | target="{vb}"')
        if diff_lines:
            msg.setInformativeText(
                'Differences:\n'
                + '\n'.join(diff_lines[:12])
                + ('\n...' if len(diff_lines) > 12 else '')
            )
            msg.setDetailedText('\n'.join(diff_lines))
        btn_keep_a = msg.addButton('Keep Selected Road Tags', QMessageBox.ButtonRole.AcceptRole)
        btn_keep_b = msg.addButton('Keep Target Road Tags', QMessageBox.ButtonRole.AcceptRole)
        msg.addButton(QMessageBox.StandardButton.Cancel)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is btn_keep_a:
            return dict(tags_a)
        if clicked is btn_keep_b:
            return dict(tags_b)
        return None

    def _build_merged_way(self, meta_a, meta_b, ai: int, bi: int):
        refs_a, refs_b = list(meta_a[6]), list(meta_b[6])
        scene_a, scene_b = list(meta_a[3]), list(meta_b[3])
        latlon_a, latlon_b = list(meta_a[4]), list(meta_b[4])
        a_last = len(refs_a) - 1
        b_last = len(refs_b) - 1

        if ai == a_last and bi == 0:
            return (
                refs_a + refs_b[1:],
                scene_a + scene_b[1:],
                latlon_a + latlon_b[1:],
            )
        if ai == 0 and bi == b_last:
            return (
                refs_b + refs_a[1:],
                scene_b + scene_a[1:],
                latlon_b + latlon_a[1:],
            )
        if ai == 0 and bi == 0:
            return (
                list(reversed(refs_b)) + refs_a[1:],
                list(reversed(scene_b)) + scene_a[1:],
                list(reversed(latlon_b)) + latlon_a[1:],
            )
        if ai == a_last and bi == b_last:
            return (
                refs_a + list(reversed(refs_b[:-1])),
                scene_a + list(reversed(scene_b[:-1])),
                latlon_a + list(reversed(latlon_b[:-1])),
            )
        return None

    def _persist_osm_tags_edit(self, item, tags: dict) -> None:
        meta = self._osm_item_meta.get(item)
        if not meta:
            return
        way_id = meta[5]
        if way_id in self._osm_created_ways:
            self._osm_created_ways[way_id]['tags'] = dict(tags)
        else:
            edit = self._osm_edits.setdefault(way_id, {})
            edit['tags'] = dict(tags)
        self._mark_osm_dirty()
        self._update_osm_export_btn()

    def _on_osm_stitch_way(self, scene_pos) -> bool:
        """Stitch selected way with nearest road endpoint by merging two ways."""
        if not self._osm_edit_enabled():
            return False
        item_a = self._osm_selected_item
        if item_a is None:
            return False

        meta_a = self._osm_item_meta.get(item_a)
        if not meta_a:
            return False
        if len(meta_a[6]) < 2:
            return False

        # Prefer explicit selected-way node-dot click.
        dot = self._osm_dot_at(scene_pos)
        if dot is not None:
            ai = int(self._osm_dot_to_index.get(dot, -1))
            if ai < 0 or ai >= len(meta_a[3]):
                return False
        else:
            # Fallback: nearest selected-way node, but only within a zoom-aware tolerance.
            scale = self.view.transform().m11() or 1.0
            pick_tol_scene = OSM_STITCH_MAX_DIST_PX / scale
            pick_tol2 = pick_tol_scene * pick_tol_scene
            ai = -1
            best_node_d2 = float('inf')
            for i, (sx, sy) in enumerate(meta_a[3]):
                d2 = (sx - scene_pos.x()) ** 2 + (sy - scene_pos.y()) ** 2
                if d2 < best_node_d2:
                    best_node_d2 = d2
                    ai = i
            if ai < 0 or best_node_d2 > pick_tol2:
                self._show_project_status('Stitch: Alt+Right-click on a node of the selected road')
                return False

        a_sx, a_sy = meta_a[3][ai]
        a_lat, a_lon = meta_a[4][ai]
        a_nid = str(meta_a[6][ai][0])

        # Find nearest endpoint on any other way.
        best_item_b = None
        best_bi = -1
        best_d2 = float('inf')
        for item_b, meta_b in self._osm_item_meta.items():
            if item_b is item_a or len(meta_b[6]) < 2:
                continue
            for bi in (0, len(meta_b[3]) - 1):
                bx, by = meta_b[3][bi]
                d2 = (a_sx - bx) ** 2 + (a_sy - by) ** 2
                if d2 < best_d2:
                    best_d2 = d2
                    best_item_b = item_b
                    best_bi = bi

        if best_item_b is None or best_bi < 0:
            return False

        # Distance gate in viewport pixels (converted to scene units), avoids accidental long-range stitches.
        scale = self.view.transform().m11() or 1.0
        max_dist_scene = OSM_STITCH_MAX_DIST_PX / scale
        max_d2 = max_dist_scene * max_dist_scene
        if best_d2 > max_d2:
            self._show_project_status('Stitch skipped: endpoints are too far apart')
            return False

        item_b = best_item_b
        bi = best_bi
        meta_b = self._osm_item_meta.get(item_b)
        if not meta_b:
            return False

        # Merge requires selected endpoint.
        if ai not in (0, len(meta_a[6]) - 1):
            self._show_project_status('Stitch requires selecting an endpoint node')
            return False

        # Snap target endpoint to selected endpoint before merge.
        meta_b[3][bi] = (a_sx, a_sy)
        meta_b[4][bi] = (a_lat, a_lon)
        meta_b[6][bi] = (a_nid, a_lat, a_lon)

        merged_tags = self._resolve_stitch_tags(meta_a[1], meta_b[1])
        if merged_tags is None:
            self._show_project_status('Stitch cancelled')
            return False

        merged = self._build_merged_way(meta_a, meta_b, ai, bi)
        if merged is None:
            self._show_project_status('Stitch failed: unsupported endpoint configuration')
            return False
        merged_refs, merged_scene, merged_latlon = merged

        new_pen = self._osm_pen_for_way(merged_tags)
        self._osm_item_meta[item_a] = (
            merged_tags.get('highway', meta_a[0]),
            dict(merged_tags),
            QPen(new_pen),
            merged_scene,
            merged_latlon,
            meta_a[5],
            merged_refs,
        )
        item_a.setPen(new_pen)
        self._rebuild_osm_path(item_a)
        self._osm_persist_node_edit(item_a)
        self._persist_osm_tags_edit(item_a, merged_tags)

        removed_way_id = str(meta_b[5])
        if removed_way_id in self._osm_created_ways:
            self._osm_created_ways.pop(removed_way_id, None)
        else:
            self._osm_deleted_way_ids.add(removed_way_id)
            self._osm_edits.pop(removed_way_id, None)
        self._mark_osm_dirty()
        self._update_osm_export_btn()

        self._remove_osm_node_dots()
        self._osm_item_meta.pop(item_b, None)
        self.scene.removeItem(item_b)
        self._rebuild_osm_connectivity()
        self._select_osm_item(item_a, center_view=False)
        self._show_project_status('Roads stitched and merged')
        return True

    # ── Add / delete nodes ─────────────────────────────────────────

    def _on_osm_add_node(self, scene_pos) -> bool:
        """Right-click on the selected segment inserts a new node at the closest edge."""
        if not self._osm_edit_enabled():
            return False
        item = self._osm_selected_item
        if item is None:
            return False
        meta = self._osm_item_meta.get(item)
        if not meta:
            return False
        coords = meta[3]  # list of (sx, sy)
        if len(coords) < 2:
            return False
        px, py = scene_pos.x(), scene_pos.y()
        # Find the segment edge nearest to the click
        best_dist = float('inf')
        best_insert = 1
        best_proj = (px, py)
        for i in range(len(coords) - 1):
            ax, ay = coords[i]
            bx, by = coords[i + 1]
            dx, dy = bx - ax, by - ay
            len_sq = dx * dx + dy * dy
            if len_sq < 1e-12:
                t = 0.0
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
            proj_x = ax + t * dx
            proj_y = ay + t * dy
            dist = (proj_x - px) ** 2 + (proj_y - py) ** 2
            if dist < best_dist:
                best_dist = dist
                best_insert = i + 1
                best_proj = (proj_x, proj_y)
        sx, sy = best_proj
        lat, lon = self._scene_to_latlon(sx, sy)
        # Generate a new unique node id
        nid = str(self._osm_next_node_id)
        self._osm_next_node_id += 1
        # Insert into meta lists
        meta[3].insert(best_insert, (sx, sy))
        meta[4].insert(best_insert, (lat, lon))
        meta[6].insert(best_insert, (nid, lat, lon))
        self._rebuild_osm_path(item)
        # Re-show dots to keep indices in sync
        self._show_osm_node_dots(item)
        # Undo entry
        self._osm_undo_stack.append(
            {
                'type': 'add',
                'item': item,
                'idx': best_insert,
                'scene': (sx, sy),
                'latlon': (lat, lon),
                'nref': (nid, lat, lon),
            }
        )
        self._osm_redo_stack.clear()
        self._osm_persist_node_edit(item)
        return True

    def _on_osm_delete_node(self, scene_pos) -> bool:
        """Ctrl+right-click on a node dot deletes it (min 2 nodes)."""
        if not self._osm_edit_enabled():
            return False
        dot = self._osm_dot_at(scene_pos)
        if dot is None:
            return False
        item = self._osm_selected_item
        if item is None:
            return False
        meta = self._osm_item_meta.get(item)
        if not meta:
            return False
        if len(meta[3]) <= 2:
            return False  # can't delete below 2 nodes
        idx = self._osm_dot_to_index[dot]
        return self._delete_osm_node_by_index(item, meta, idx)

    def _delete_selected_osm_node(self) -> bool:
        """Delete the currently selected node (via keyboard shortcut)."""
        if not self._osm_edit_enabled():
            return False
        if self._osm_selected_node_index is None:
            return False
        item = self._osm_selected_item
        if item is None:
            return False
        meta = self._osm_item_meta.get(item)
        if not meta:
            return False
        if len(meta[3]) <= 2:
            return False
        idx = int(self._osm_selected_node_index)
        if not (0 <= idx < len(meta[6])):
            return False
        return self._delete_osm_node_by_index(item, meta, idx)

    def _delete_osm_node_by_index(self, item, meta, idx: int) -> bool:
        """Remove node at *idx* from *item*, push undo, persist."""
        removed_scene = meta[3].pop(idx)
        removed_latlon = meta[4].pop(idx)
        removed_nref = meta[6].pop(idx)
        self._osm_node_tag_edits.pop(str(removed_nref[0]), None)
        self._rebuild_osm_path(item)
        self._show_osm_node_dots(item)
        self._set_osm_selected_node_index(None)
        # Undo entry
        self._osm_undo_stack.append(
            {
                'type': 'delete',
                'item': item,
                'idx': idx,
                'scene': removed_scene,
                'latlon': removed_latlon,
                'nref': removed_nref,
            }
        )
        self._osm_redo_stack.clear()
        self._osm_persist_node_edit(item)
        return True

    def _delete_selected_osm_segment(self) -> bool:
        """Delete currently selected OSM segment in edit mode."""
        """Delete selected OSM segment(s) in edit mode."""
        if not self._osm_edit_enabled():
            return False
        selected_items = list(getattr(self, '_osm_multi_selected_items', set()) or [])
        if not selected_items and self._osm_selected_item is not None:
            selected_items = [self._osm_selected_item]
        if not selected_items:
            return False
        removed_ids: list[str] = []
        self._remove_osm_node_dots()
        self._remove_osm_selected_direction_arrows()
        for item in selected_items:
            meta = self._osm_item_meta.get(item)
            if not meta:
                continue
            removed_way_id = str(meta[5])
            removed_ids.append(removed_way_id)
            if removed_way_id in self._osm_created_ways:
                self._osm_created_ways.pop(removed_way_id, None)
            else:
                self._osm_deleted_way_ids.add(removed_way_id)
                self._osm_edits.pop(removed_way_id, None)
            self._osm_item_meta.pop(item, None)
            self.scene.removeItem(item)
        self._clear_osm_multi_selection()
        self._osm_selected_item = None
        self._osm_props_group.setVisible(False)
        self._rebuild_osm_connectivity()
        self._mark_osm_dirty()
        self._update_osm_export_btn()
        self._schedule_auto_xodr_refresh()
        if len(removed_ids) == 1:
            self._show_project_status(f'Segment {removed_ids[0]} deleted')
        else:
            self._show_project_status(f'{len(removed_ids)} segments deleted')
        return True

    def _osm_persist_node_edit(self, item) -> None:
        """Persist the current node list of *item* to ``_osm_edits``."""
        meta = self._osm_item_meta.get(item)
        if not meta:
            return
        way_id = meta[5]
        if way_id in self._osm_created_ways:
            self._osm_created_ways[way_id]['node_coords'] = list(meta[6])
        else:
            edit = self._osm_edits.setdefault(way_id, {})
            edit['node_coords'] = list(meta[6])
        self._rebuild_osm_connectivity()
        self._mark_osm_dirty()
        self._update_osm_export_btn()

    # ── Undo / redo helpers ───────────────────────────────────────────

    def _osm_apply_move(self, item, idx: int, scene_xy: tuple, latlon: tuple) -> None:
        """Apply a node move to *item* at node *idx* (shared by undo/redo)."""
        meta = self._osm_item_meta.get(item)
        if not meta:
            return
        meta[3][idx] = scene_xy
        meta[4][idx] = latlon
        nid = meta[6][idx][0]
        meta[6][idx] = (nid, latlon[0], latlon[1])
        self._rebuild_osm_path(item)
        self._osm_propagate_shared_node_all(
            str(nid),
            scene_xy[0],
            scene_xy[1],
            latlon[0],
            latlon[1],
            source_item=item,
            source_idx=idx,
            persist=True,
        )
        # Move dot marker if it exists
        if idx < len(self._osm_node_dots):
            self._osm_node_dots[idx].setPos(scene_xy[0], scene_xy[1])
        self._osm_persist_node_edit(item)

    def _osm_apply_insert(self, item, idx: int, scene_xy, latlon, nref) -> None:
        """Insert a node at *idx* (used by redo-add and undo-delete)."""
        meta = self._osm_item_meta.get(item)
        if not meta:
            return
        meta[3].insert(idx, scene_xy)
        meta[4].insert(idx, latlon)
        meta[6].insert(idx, nref)
        self._rebuild_osm_path(item)
        if item is self._osm_selected_item:
            self._show_osm_node_dots(item)
        self._osm_persist_node_edit(item)

    def _osm_apply_remove(self, item, idx: int) -> tuple:
        """Remove node at *idx*, return (scene, latlon, nref) for undo."""
        meta = self._osm_item_meta.get(item)
        if not meta:
            return (0, 0), (0, 0), ('', 0, 0)
        sc = meta[3].pop(idx)
        ll = meta[4].pop(idx)
        nr = meta[6].pop(idx)
        self._rebuild_osm_path(item)
        if item is self._osm_selected_item:
            self._show_osm_node_dots(item)
        self._osm_persist_node_edit(item)
        return sc, ll, nr

    def _osm_undo_move(self) -> None:
        """Undo the last node action (move / add / delete)."""
        if not self._osm_edit_enabled():
            return
        if not self._osm_undo_stack:
            return
        action = self._osm_undo_stack.pop()
        self._osm_redo_stack.append(action)
        atype = action.get('type', 'move')
        if atype == 'move':
            self._osm_apply_move(
                action['item'],
                action['idx'],
                action['old_scene'],
                action['old_latlon'],
            )
        elif atype == 'add':
            # Undo add → remove the inserted node
            self._osm_apply_remove(action['item'], action['idx'])
        elif atype == 'delete':
            # Undo delete → re-insert the removed node
            self._osm_apply_insert(
                action['item'],
                action['idx'],
                action['scene'],
                action['latlon'],
                action['nref'],
            )

    def _osm_redo_move(self) -> None:
        """Redo the last undone node action (move / add / delete)."""
        if not self._osm_edit_enabled():
            return
        if not self._osm_redo_stack:
            return
        action = self._osm_redo_stack.pop()
        self._osm_undo_stack.append(action)
        atype = action.get('type', 'move')
        if atype == 'move':
            self._osm_apply_move(
                action['item'],
                action['idx'],
                action['new_scene'],
                action['new_latlon'],
            )
        elif atype == 'add':
            # Redo add → re-insert
            self._osm_apply_insert(
                action['item'],
                action['idx'],
                action['scene'],
                action['latlon'],
                action['nref'],
            )
        elif atype == 'delete':
            # Redo delete → remove again
            self._osm_apply_remove(action['item'], action['idx'])

    # ── Export edited OSM ─────────────────────────────────────────────

    def _update_osm_export_btn(self) -> None:
        if not hasattr(self, 'btn_export_osm_bundle'):
            return
        has_osm = bool(self._compose_current_osm_content() or self._osm_content)
        has_xodr = bool(self._xodr_content or (self.xodr_path and os.path.isfile(self.xodr_path)))
        self.btn_export_osm_bundle.setEnabled(bool(has_osm and has_xodr))

    def _update_xodr_export_btn(self) -> None:
        has_xodr = bool(self._xodr_content or (self.xodr_path and os.path.isfile(self.xodr_path)))
        if hasattr(self, 'btn_export_xodr'):
            self.btn_export_xodr.setEnabled(has_xodr)
        if hasattr(self, 'action_export_opendrive'):
            self.action_export_opendrive.setEnabled(has_xodr)

    def _update_xodr_delete_btn(self) -> None:
        pass  # xodr edit mode removed

    def _scene_clip_rect(self) -> tuple[float, float, float, float]:
        if not self.map_ctx:
            return (0.0, 0.0, 0.0, 0.0)
        return (
            0.0,
            float(self.map_ctx.width_in_pixels),
            0.0,
            float(self.map_ctx.height_in_pixels),
        )

    def _segment_intersects_rect(
        self,
        p0: tuple[float, float],
        p1: tuple[float, float],
        rect: tuple[float, float, float, float],
    ) -> bool:
        xmin, xmax, ymin, ymax = rect
        x0, y0 = p0
        x1, y1 = p1

        INSIDE, LEFT, RIGHT, BOTTOM, TOP = 0, 1, 2, 4, 8

        def _code(x: float, y: float) -> int:
            code = INSIDE
            if x < xmin:
                code |= LEFT
            elif x > xmax:
                code |= RIGHT
            if y < ymin:
                code |= BOTTOM
            elif y > ymax:
                code |= TOP
            return code

        c0 = _code(x0, y0)
        c1 = _code(x1, y1)
        while True:
            if not (c0 | c1):
                return True
            if c0 & c1:
                return False
            out = c0 or c1
            if out & TOP:
                x = x0 + (x1 - x0) * (ymax - y0) / (y1 - y0 if abs(y1 - y0) > 1e-12 else 1.0)
                y = ymax
            elif out & BOTTOM:
                x = x0 + (x1 - x0) * (ymin - y0) / (y1 - y0 if abs(y1 - y0) > 1e-12 else 1.0)
                y = ymin
            elif out & RIGHT:
                y = y0 + (y1 - y0) * (xmax - x0) / (x1 - x0 if abs(x1 - x0) > 1e-12 else 1.0)
                x = xmax
            else:
                y = y0 + (y1 - y0) * (xmin - x0) / (x1 - x0 if abs(x1 - x0) > 1e-12 else 1.0)
                x = xmin
            if out == c0:
                x0, y0 = x, y
                c0 = _code(x0, y0)
            else:
                x1, y1 = x, y
                c1 = _code(x1, y1)

    def _clip_polyline_to_rect(
        self, points: list[tuple[float, float]], rect: tuple[float, float, float, float]
    ) -> list[list[tuple[float, float]]]:
        if len(points) < 2:
            return []
        xmin, xmax, ymin, ymax = rect

        def _inside(p: tuple[float, float]) -> bool:
            return xmin <= p[0] <= xmax and ymin <= p[1] <= ymax

        clipped_parts: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for i in range(len(points) - 1):
            p0 = points[i]
            p1 = points[i + 1]
            if not self._segment_intersects_rect(p0, p1, rect):
                if len(current) >= 2:
                    clipped_parts.append(current)
                current = []
                continue
            seg = [p0, p1]
            # Iterative Cohen-Sutherland result endpoints.
            x0, y0 = seg[0]
            x1, y1 = seg[1]
            INSIDE, LEFT, RIGHT, BOTTOM, TOP = 0, 1, 2, 4, 8

            def _code(x: float, y: float) -> int:
                code = INSIDE
                if x < xmin:
                    code |= LEFT
                elif x > xmax:
                    code |= RIGHT
                if y < ymin:
                    code |= BOTTOM
                elif y > ymax:
                    code |= TOP
                return code

            c0 = _code(x0, y0)
            c1 = _code(x1, y1)
            accept = False
            while True:
                if not (c0 | c1):
                    accept = True
                    break
                if c0 & c1:
                    break
                out = c0 or c1
                if out & TOP:
                    x = x0 + (x1 - x0) * (ymax - y0) / (y1 - y0 if abs(y1 - y0) > 1e-12 else 1.0)
                    y = ymax
                elif out & BOTTOM:
                    x = x0 + (x1 - x0) * (ymin - y0) / (y1 - y0 if abs(y1 - y0) > 1e-12 else 1.0)
                    y = ymin
                elif out & RIGHT:
                    y = y0 + (y1 - y0) * (xmax - x0) / (x1 - x0 if abs(x1 - x0) > 1e-12 else 1.0)
                    x = xmax
                else:
                    y = y0 + (y1 - y0) * (xmin - x0) / (x1 - x0 if abs(x1 - x0) > 1e-12 else 1.0)
                    x = xmin
                if out == c0:
                    x0, y0 = x, y
                    c0 = _code(x0, y0)
                else:
                    x1, y1 = x, y
                    c1 = _code(x1, y1)
            if not accept:
                if len(current) >= 2:
                    clipped_parts.append(current)
                current = []
                continue
            seg_clipped = [(float(x0), float(y0)), (float(x1), float(y1))]
            if not current:
                current = [seg_clipped[0], seg_clipped[1]]
            else:
                if (
                    math.hypot(
                        current[-1][0] - seg_clipped[0][0],
                        current[-1][1] - seg_clipped[0][1],
                    )
                    > 1e-6
                ):
                    if len(current) >= 2:
                        clipped_parts.append(current)
                    current = [seg_clipped[0], seg_clipped[1]]
                else:
                    current.append(seg_clipped[1])
            if not _inside(p1):
                if len(current) >= 2:
                    clipped_parts.append(current)
                current = []
        if len(current) >= 2:
            clipped_parts.append(current)
        return clipped_parts

    def _clip_polygon_to_rect(
        self, points: list[tuple[float, float]], rect: tuple[float, float, float, float]
    ) -> list[tuple[float, float]]:
        if len(points) < 3:
            return []
        xmin, xmax, ymin, ymax = rect

        ring = list(points)
        if (
            len(ring) >= 2
            and math.hypot(ring[0][0] - ring[-1][0], ring[0][1] - ring[-1][1]) <= 1e-6
        ):
            ring = ring[:-1]
        if len(ring) < 3:
            return []

        def _clip_against_edge(
            vertices: list[tuple[float, float]],
            inside_fn,
            intersect_fn,
        ) -> list[tuple[float, float]]:
            if not vertices:
                return []
            clipped: list[tuple[float, float]] = []
            prev = vertices[-1]
            prev_inside = inside_fn(prev)
            for cur in vertices:
                cur_inside = inside_fn(cur)
                if cur_inside:
                    if not prev_inside:
                        clipped.append(intersect_fn(prev, cur))
                    clipped.append(cur)
                elif prev_inside:
                    clipped.append(intersect_fn(prev, cur))
                prev = cur
                prev_inside = cur_inside
            return clipped

        def _intersect_vertical(
            p0: tuple[float, float], p1: tuple[float, float], x_edge: float
        ) -> tuple[float, float]:
            x0, y0 = p0
            x1, y1 = p1
            if math.isclose(x0, x1, abs_tol=1e-12):
                return (float(x_edge), float(y0))
            t = (x_edge - x0) / (x1 - x0)
            return (float(x_edge), float(y0 + t * (y1 - y0)))

        def _intersect_horizontal(
            p0: tuple[float, float], p1: tuple[float, float], y_edge: float
        ) -> tuple[float, float]:
            x0, y0 = p0
            x1, y1 = p1
            if math.isclose(y0, y1, abs_tol=1e-12):
                return (float(x0), float(y_edge))
            t = (y_edge - y0) / (y1 - y0)
            return (float(x0 + t * (x1 - x0)), float(y_edge))

        clipped = ring
        clipped = _clip_against_edge(
            clipped,
            lambda p: p[0] >= xmin,
            lambda p0, p1: _intersect_vertical(p0, p1, xmin),
        )
        clipped = _clip_against_edge(
            clipped,
            lambda p: p[0] <= xmax,
            lambda p0, p1: _intersect_vertical(p0, p1, xmax),
        )
        clipped = _clip_against_edge(
            clipped,
            lambda p: p[1] >= ymin,
            lambda p0, p1: _intersect_horizontal(p0, p1, ymin),
        )
        clipped = _clip_against_edge(
            clipped,
            lambda p: p[1] <= ymax,
            lambda p0, p1: _intersect_horizontal(p0, p1, ymax),
        )
        if len(clipped) < 3:
            return []

        deduped: list[tuple[float, float]] = []
        for point in clipped:
            if (
                deduped
                and math.hypot(deduped[-1][0] - point[0], deduped[-1][1] - point[1]) <= 1e-6
            ):
                continue
            deduped.append((float(point[0]), float(point[1])))
        if (
            len(deduped) >= 2
            and math.hypot(deduped[0][0] - deduped[-1][0], deduped[0][1] - deduped[-1][1]) <= 1e-6
        ):
            deduped.pop()
        if len(deduped) < 3:
            return []
        deduped.append(deduped[0])
        return deduped

    def _clip_osm_content_to_world_bounds(self, osm_content: str) -> str:
        if not osm_content or not self.map_ctx:
            return osm_content
        try:
            tree = ET.ElementTree(ET.fromstring(osm_content))
            root = tree.getroot()
            rect = self._scene_clip_rect()

            # Identify nodes with "extra details" that should be preserved.
            detail_tags = {
                'traffic_sign',
                'maxspeed',
                'natural',  # for tree
                'amenity',  # for parking
                'barrier',  # for guard_rail (though usually ways)
                'traffic_signals',
            }
            detail_highway_values = {
                'traffic_signals',
                'give_way',
                'stop',
                'crossing',
                'street_lamp',
                'bus_stop',
                'turning_circle',
            }

            def _is_interesting_node(node_el: ET.Element) -> bool:
                for tag in node_el.findall('tag'):
                    k = tag.get('k', '')
                    v = tag.get('v', '')
                    if k in detail_tags:
                        return True
                    if k == 'highway' and v in detail_highway_values:
                        return True
                return False

            original_nodes: dict[str, ET.Element] = {}
            node_scene: dict[str, tuple[float, float]] = {}
            for node in root.iter('node'):
                try:
                    nid = str(node.get('id', ''))
                    original_nodes[nid] = node
                    lat = float(node.get('lat', '0'))
                    lon = float(node.get('lon', '0'))
                    sx, sy = self._osm_latlon_to_scene(lat, lon)  # type: ignore[misc]
                    node_scene[nid] = (float(sx), float(sy))
                except Exception:
                    continue

            xmin, xmax, ymin, ymax = rect

            def _is_in_bounds(sx: float, sy: float) -> bool:
                return xmin <= sx <= xmax and ymin <= sy <= ymax

            used_node_ids: set[str] = set()
            new_ways: list[ET.Element] = []
            next_node_id = -1
            next_way_id = -1

            # Map from (lat, lon) string to new node element to avoid duplicates
            generated_nodes: dict[str, ET.Element] = {}

            original_ways = list(root.findall('way'))

            # Clear original elements to rebuild
            for node in list(root.findall('node')):
                root.remove(node)
            for way in list(root.findall('way')):
                root.remove(way)

            for way in original_ways:
                refs = [str(nd.get('ref', '')) for nd in way.findall('nd')]
                points = [node_scene[r] for r in refs if r in node_scene]
                if len(points) < 2:
                    continue

                is_closed_way = (
                    len(refs) >= 4
                    and refs[0] == refs[-1]
                    and math.hypot(points[0][0] - points[-1][0], points[0][1] - points[-1][1])
                    <= 1e-6
                )
                if is_closed_way:
                    clipped_polygon = self._clip_polygon_to_rect(points, rect)
                    clipped_parts = [clipped_polygon] if clipped_polygon else []
                else:
                    clipped_parts = self._clip_polyline_to_rect(points, rect)
                if not clipped_parts:
                    continue

                tags = [copy.deepcopy(tag) for tag in way.findall('tag')]
                for idx, part in enumerate(clipped_parts):
                    min_points = 4 if is_closed_way else 2
                    if len(part) < min_points:
                        continue
                    new_way = ET.Element('way')
                    if idx == 0:
                        new_way.set('id', way.get('id', str(next_way_id)))
                    else:
                        new_way.set('id', str(next_way_id))
                        next_way_id -= 1
                    new_way.set('version', way.get('version', '1'))
                    new_way.set('visible', way.get('visible', 'true'))

                    for sx, sy in part:
                        lat, lon = self._scene_to_latlon(float(sx), float(sy))
                        # Use high precision for coordinate matching
                        key = f'{lat:.9f}:{lon:.9f}'
                        node_el = generated_nodes.get(key)
                        if node_el is None:
                            node_el = ET.Element('node')
                            node_el.set('id', str(next_node_id))
                            node_el.set('visible', 'true')
                            node_el.set('version', '1')
                            node_el.set('lat', f'{lat:.9f}')
                            node_el.set('lon', f'{lon:.9f}')

                            # Check if this coordinate matches an original interesting node
                            # This is slightly simplified: if multiple nodes match, we pick one.
                            for nid, (osx, osy) in node_scene.items():
                                if math.isclose(osx, sx, abs_tol=1e-6) and math.isclose(
                                    osy, sy, abs_tol=1e-6
                                ):
                                    orig_node = original_nodes[nid]
                                    if _is_interesting_node(orig_node):
                                        for otag in orig_node.findall('tag'):
                                            node_el.append(copy.deepcopy(otag))
                                        used_node_ids.add(nid)
                                    break

                            generated_nodes[key] = node_el
                            next_node_id -= 1

                        nd = ET.SubElement(new_way, 'nd')
                        nd.set('ref', node_el.get('id', ''))

                    for tag in tags:
                        new_way.append(copy.deepcopy(tag))
                    new_ways.append(new_way)

            # Preserve standalone interesting nodes that are within bounds
            for nid, node in original_nodes.items():
                if nid in used_node_ids:
                    continue
                if not _is_interesting_node(node):
                    continue
                osx, osy = node_scene.get(nid, (float('nan'), float('nan')))
                if not _is_in_bounds(osx, osy):
                    continue

                # Add this standalone node
                new_node = copy.deepcopy(node)
                # Keep original ID for standalone nodes if possible, or use next_node_id
                # Actually, using original ID is safer for standalone nodes as long as it doesn't collide
                # with our generated negative IDs.
                root.append(new_node)

            # Add generated nodes and ways
            for node_el in generated_nodes.values():
                root.append(node_el)
            for way_el in new_ways:
                root.append(way_el)

            buf = io.StringIO()
            tree.write(buf, encoding='unicode', xml_declaration=True)
            return buf.getvalue()
        except Exception:
            return osm_content

    def _clip_xodr_content_to_world_bounds(self, xodr_content: str, invert_y: bool = True) -> str:
        if not xodr_content or not self.map_ctx:
            return xodr_content
        try:
            tree = ET.ElementTree(ET.fromstring(xodr_content))
            root = tree.getroot()
            wb = self.map_ctx.world_bounds
            xmin, xmax, carla_ymin, carla_ymax = [float(v) for v in wb]
            if invert_y:
                # Road geometry Y is in CARLA frame (negated OpenDRIVE Y).
                # World bounds are also in CARLA frame — use directly.
                clip_ymin = -carla_ymax
                clip_ymax = -carla_ymin
                geom_ymin = clip_ymin
                geom_ymax = clip_ymax
            else:
                # Road geometry Y is in OpenDRIVE frame (positive northing).
                # World bounds are in CARLA frame; convert to OpenDRIVE frame for the
                # intersection test (carla_y = -opendrive_y → opendrive_y = -carla_y).
                geom_ymin = -carla_ymax
                geom_ymax = -carla_ymin
                # Keep clip_ymin/max in CARLA frame for the header calculation below.
                clip_ymin = carla_ymin
                clip_ymax = carla_ymax

            def _bbox_intersects(
                a: tuple[float, float, float, float],
                b: tuple[float, float, float, float],
            ) -> bool:
                return not (a[1] < b[0] or a[0] > b[1] or a[3] < b[2] or a[2] > b[3])

            kept_road_ids: set[str] = set()
            for road in list(root.findall('road')):
                min_x, max_x = float('inf'), float('-inf')
                min_y, max_y = float('inf'), float('-inf')
                plan_view = road.find('planView')
                if plan_view is not None:
                    for geom in plan_view.findall('geometry'):
                        x = float(geom.get('x', '0'))
                        y = float(geom.get('y', '0'))
                        length = float(geom.get('length', '0'))
                        min_x = min(min_x, x - length)
                        max_x = max(max_x, x + length)
                        min_y = min(min_y, y - length)
                        max_y = max(max_y, y + length)
                if min_x == float('inf'):
                    root.remove(road)
                    continue
                if _bbox_intersects(
                    (min_x, max_x, min_y, max_y), (xmin, xmax, geom_ymin, geom_ymax)
                ):
                    kept_road_ids.add(str(road.get('id', '')))
                else:
                    root.remove(road)

            referenced_junction_ids: set[str] = set()
            for road in root.findall('road'):
                junction_id = str(road.get('junction', '-1'))
                if junction_id not in ('', '-1'):
                    referenced_junction_ids.add(junction_id)
            for junction in list(root.findall('junction')):
                jid = str(junction.get('id', ''))
                if jid not in referenced_junction_ids:
                    root.remove(junction)
            for controller in list(root.findall('controller')):
                root.remove(controller)

            header = root.find('header')
            if header is not None:
                header.set('west', f'{xmin:.6f}')
                header.set('east', f'{xmax:.6f}')
                if invert_y:
                    # clip_y values are in OpenDRIVE frame already
                    header.set('south', f'{clip_ymin:.6f}')
                    header.set('north', f'{clip_ymax:.6f}')
                else:
                    # clip_y values are in CARLA/viewer frame; convert back to OpenDRIVE frame
                    header.set('south', f'{-clip_ymax:.6f}')
                    header.set('north', f'{-clip_ymin:.6f}')

            buf = io.StringIO()
            tree.write(buf, encoding='unicode', xml_declaration=True)
            return buf.getvalue()
        except Exception:
            return xodr_content

    def _mark_osm_dirty(self) -> None:
        self._osm_dirty = True

    def _reset_osm_dirty(self) -> None:
        self._osm_dirty = False

    def _mark_xodr_project_dirty(self, reset_bake_state: bool = True) -> None:
        self._xodr_project_dirty = True

    def _reset_xodr_project_dirty(self) -> None:
        self._xodr_project_dirty = False
