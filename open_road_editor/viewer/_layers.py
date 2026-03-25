"""Layer management, world-extent, event-filter and zoom mixin."""

import copy
import io
import json
import math
import os
import threading
import time
from urllib import request as urllib_request
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image
from PyQt6.QtCore import (
    QEvent,
    QPointF,
    QRectF,
    Qt,
    QTimer,
)
from PyQt6.QtGui import (
    QBrush,
    QCursor,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QColorDialog,
    QFileDialog,
    QGraphicsRectItem,
    QGraphicsView,
    QMessageBox,
    QStyle,
)

from open_road_editor.constants import *  # noqa: F401,F403
from open_road_editor.utils.map_context import MapContext


class _LayersMixin:
    """Mixin — see viewer/main.py for the assembled class."""

    def _compose_current_osm_content(self) -> str | None:
        """Compose current OSM XML including in-memory edits and split-created ways."""
        if not self._osm_original_tree:
            return self._osm_content
        if (
            not self._osm_edits
            and not self._osm_node_tag_edits
            and not self._osm_created_ways
            and not self._osm_deleted_way_ids
            and not self._osm_deleted_node_ids
        ):
            return self._osm_content

        tree = copy.deepcopy(self._osm_original_tree)
        root = tree.getroot()
        node_elems = {node_el.get('id'): node_el for node_el in root.iter('node')}

        def _first_child_index(tag_name: str):
            for i, child in enumerate(list(root)):
                if child.tag == tag_name:
                    return i
            return None

        def _insert_node_ordered(node_el) -> None:
            first_way_idx = _first_child_index('way')
            first_rel_idx = _first_child_index('relation')
            insert_idx = first_way_idx if first_way_idx is not None else first_rel_idx
            if insert_idx is None:
                root.append(node_el)
            else:
                root.insert(insert_idx, node_el)

        def _insert_way_ordered(way_el) -> None:
            first_rel_idx = _first_child_index('relation')
            if first_rel_idx is None:
                root.append(way_el)
            else:
                root.insert(first_rel_idx, way_el)

        for way_el in list(root.iter('way')):
            wid = way_el.get('id', '')
            if wid in self._osm_deleted_way_ids:
                root.remove(way_el)
                continue
            if wid not in self._osm_edits:
                continue
            edit = self._osm_edits[wid]
            if 'tags' in edit:
                for tag_el in list(way_el.findall('tag')):
                    way_el.remove(tag_el)
                for k, v in edit['tags'].items():
                    if k:
                        t = ET.SubElement(way_el, 'tag')
                        t.set('k', str(k))
                        t.set('v', str(v))
            if 'node_coords' in edit:
                edited_refs = edit['node_coords']
                edited_nids = [nid for nid, _, _ in edited_refs]
                for nid, lat, lon in edited_refs:
                    if nid not in node_elems:
                        new_node = ET.Element('node')
                        new_node.set('id', str(nid))
                        new_node.set('visible', 'true')
                        new_node.set('lat', f'{lat:.9f}')
                        new_node.set('lon', f'{lon:.9f}')
                        _insert_node_ordered(new_node)
                        node_elems[nid] = new_node
                    else:
                        node_elems[nid].set('lat', f'{lat:.9f}')
                        node_elems[nid].set('lon', f'{lon:.9f}')
                for nd_el in list(way_el.findall('nd')):
                    way_el.remove(nd_el)
                first_tag = way_el.find('tag')
                for nid in edited_nids:
                    nd_new = ET.Element('nd')
                    nd_new.set('ref', str(nid))
                    if first_tag is not None:
                        way_el.insert(list(way_el).index(first_tag), nd_new)
                    else:
                        way_el.append(nd_new)

        for new_wid, new_way in self._osm_created_ways.items():
            tags = new_way.get('tags', {})
            if not str(tags.get('highway', '')).strip():
                tags = dict(tags)
                tags['highway'] = 'residential'
            edited_refs = new_way.get('node_coords', [])
            if len(edited_refs) < 2:
                continue
            way_el = ET.Element('way')
            way_el.set('id', str(new_wid))
            way_el.set('visible', 'true')
            for nid, lat, lon in edited_refs:
                if nid not in node_elems:
                    new_node = ET.Element('node')
                    new_node.set('id', str(nid))
                    new_node.set('visible', 'true')
                    new_node.set('lat', f'{lat:.9f}')
                    new_node.set('lon', f'{lon:.9f}')
                    _insert_node_ordered(new_node)
                    node_elems[nid] = new_node
                else:
                    node_elems[nid].set('lat', f'{lat:.9f}')
                    node_elems[nid].set('lon', f'{lon:.9f}')
                nd_new = ET.SubElement(way_el, 'nd')
                nd_new.set('ref', str(nid))
            for k, v in tags.items():
                if k:
                    t = ET.SubElement(way_el, 'tag')
                    t.set('k', str(k))
                    t.set('v', str(v))
            _insert_way_ordered(way_el)

        for nid in self._osm_deleted_node_ids:
            node_el = node_elems.get(str(nid))
            if node_el is not None:
                root.remove(node_el)
                node_elems.pop(str(nid), None)

        for nid, tags in self._osm_node_tag_edits.items():
            node_el = node_elems.get(str(nid))
            if node_el is None:
                continue
            for tag_el in list(node_el.findall('tag')):
                node_el.remove(tag_el)
            for k, v in tags.items():
                if k:
                    t = ET.SubElement(node_el, 'tag')
                    t.set('k', str(k))
                    t.set('v', str(v))

        ET.indent(tree, space='  ')
        buf = io.StringIO()
        tree.write(buf, encoding='unicode', xml_declaration=True)
        return buf.getvalue()

    def on_opendrive_refreshed(self, image, count, total):
        if count == -1:
            self.opendrive_loading = False
            self.btn_browse_xodr.setIcon(
                self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
            )
            if not self.check_opendrive.isChecked():
                self.lbl_opendrive_status.setText('')
            elif self.spin_opendrive_alpha.value() <= 0.0:
                self.lbl_opendrive_status.setText('Loaded (Hidden)')
            else:
                self.lbl_opendrive_status.setText('Loaded')
            if not self._carla_bev_loading and not self._esri_loading:
                self.spinner_timer.stop()
            if getattr(self, '_fit_after_opendrive_load', False):
                self._fit_after_opendrive_load = False
                QTimer.singleShot(FIT_AFTER_LOAD_DELAY_MS, self._apply_load_view)
            return
        if image:
            self.opendrive_item.setPixmap(self.pil_to_qpixmap(image))
            self.opendrive_item.setPos(0, 0)
            self.update_visibility()

    def browse_xodr(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            'Select OpenDRIVE File',
            '',
            'OpenDRIVE Files (*.xodr);;All Files (*)',
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if file_path:
            if self.osm_path:
                reply = QMessageBox.question(
                    self,
                    'Confirm Import',
                    'An OSM file is already loaded. Importing OpenDRIVE will clear it. Continue?',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.No:
                    return
                self.edit_osm.setText('')
            self.edit_xodr.setText(file_path)

    def fit_world_extent_to_xodr(self):
        """Update world extent (and projection) to match the loaded XODR file."""
        path = self.xodr_path
        if not path or not os.path.isfile(path) or not self.map_ctx:
            return

        # Check if update is needed
        new_georef = MapContext.parse_xodr_georef(path)
        new_bounds = MapContext.parse_xodr_bounds(path)

        if not new_bounds:
            return

        # Calculate target MPP and geo params
        target_mpp = self.map_ctx.mpp
        target_ref_lat = self.map_ctx.earth_ref_lat
        target_ref_lon = self.map_ctx.earth_ref_lon
        target_x0 = getattr(self.map_ctx, 'proj_false_easting', 0.0)
        target_y0 = getattr(self.map_ctx, 'proj_false_northing', 0.0)
        target_k0 = getattr(self.map_ctx, 'proj_scale_factor', 1.0)

        if new_georef:
            target_ref_lat, target_ref_lon, target_x0, target_y0, target_k0 = new_georef
            target_mpp = (EARTH_CIRCUMFERENCE * math.cos(math.radians(target_ref_lat))) / (
                (2**self.map_ctx.tile_max_zoom_level) * TILE_SIZE
            )

        current_bounds = self.map_ctx.world_bounds
        bounds_match = len(current_bounds) == 4 and all(
            abs(c - n) < 1e-3 for c, n in zip(current_bounds, new_bounds)
        )

        geo_match = (
            abs(self.map_ctx.mpp - target_mpp) < 1e-6
            and abs(self.map_ctx.earth_ref_lat - target_ref_lat) < 1e-6
            and abs(self.map_ctx.earth_ref_lon - target_ref_lon) < 1e-6
            and abs(getattr(self.map_ctx, 'proj_false_easting', 0.0) - target_x0) < 1e-3
            and abs(getattr(self.map_ctx, 'proj_false_northing', 0.0) - target_y0) < 1e-3
            and abs(getattr(self.map_ctx, 'proj_scale_factor', 1.0) - target_k0) < 1e-6
        )

        if bounds_match and geo_match:
            print('World extent already matches OpenDRIVE. Skipping update.')
            return

        # Capture old state
        old_offset = list(self.map_ctx.world_offset)
        old_w = self.map_ctx.width_in_pixels
        old_h = self.map_ctx.height_in_pixels
        old_mpp = self.map_ctx.mpp

        # Capture viewport center in WORLD coordinates so we can restore it exactly
        view_center = self.view.viewport().rect().center()
        scene_center = self.view.mapToScene(view_center)
        world_center_x = old_offset[0] + scene_center.x() * old_mpp
        world_center_y = old_offset[1] + scene_center.y() * old_mpp

        # Recompute mpp from the file's geoReference
        georef = MapContext.parse_xodr_georef(path)
        if georef:
            ref_lat, ref_lon, x0, y0, k0 = georef
            new_mpp = (EARTH_CIRCUMFERENCE * math.cos(math.radians(ref_lat))) / (
                (2**self.map_ctx.tile_max_zoom_level) * TILE_SIZE
            )
            self.map_ctx.mpp = new_mpp
            self.map_ctx.min_meters_per_pixel = new_mpp
            self.map_ctx.earth_ref_lat = ref_lat
            self.map_ctx.earth_ref_lon = ref_lon
            self.map_ctx.proj_false_easting = x0
            self.map_ctx.proj_false_northing = y0
            self.map_ctx.proj_scale_factor = k0
            print(
                f'XODR geoReference: lat={ref_lat}, lon={ref_lon}, x0={x0}, y0={y0}, k0={k0}, mpp={new_mpp:.6f}'
            )

        print(f'Parsing bounds from XODR for extent fit: {path}')
        bounds = MapContext.parse_xodr_bounds(path)

        if bounds:
            # Update mapper with new bounds
            self.map_ctx.world_bounds = bounds
            self.map_ctx.world_offset = [bounds[0], bounds[2]]
            w = bounds[1] - bounds[0]
            h = bounds[3] - bounds[2]

            # Update size in pixels
            self.map_ctx.width_in_pixels = int(math.ceil(w / self.map_ctx.mpp))
            self.map_ctx.height_in_pixels = int(math.ceil(h / self.map_ctx.mpp))

            print(
                f'New bounds: {bounds}, Size: {self.map_ctx.width_in_pixels}x{self.map_ctx.height_in_pixels}'
            )

            # Update scene rect
            if self.scene:
                rect = QRectF(0, 0, self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels)
                self.scene.setSceneRect(rect)
                self._sync_scene_rect()

            # Update grid item
            if self.grid_item:
                self.grid_item.mpp = self.map_ctx.mpp
                self.grid_item.rect = QRectF(
                    0, 0, self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels
                )
                self.grid_item.world_offset = self.map_ctx.world_offset
                self.grid_item.update()

            # Compensate viewport scroll so the geographic center stays consistent
            new_mpp = self.map_ctx.mpp
            new_offset = self.map_ctx.world_offset

            new_scene_x = (world_center_x - new_offset[0]) / new_mpp
            new_scene_y = (world_center_y - new_offset[1]) / new_mpp
            self.view.centerOn(new_scene_x, new_scene_y)

            # Calculate scene shift for vector overlays
            dx_scene = (old_offset[0] - new_offset[0]) / new_mpp
            dy_scene = (old_offset[1] - new_offset[1]) / new_mpp

            # Redraw CARLA bounds rect immediately so it matches new coordinates
            if self._carla_bev_server_meta is not None:
                self._carla_bev_draw_bounds_rect(self._carla_bev_server_meta)

            # If MPP changed significantly, we must full-refresh.
            # If MPP is consistent, we can just shift existing tiles.
            mpp_changed = abs(new_mpp - old_mpp) > 1e-6
            if mpp_changed:
                if self._esri_pix_data is not None or self._esri_loading:
                    self.stop_esri_refresh()
                if self._carla_bev_pix_data is not None or self._carla_bev_loading:
                    self.stop_carla_bev_refresh()
                if self.opendrive_item:
                    self.opendrive_item.setPixmap(QPixmap())
                self._clear_xodr_vector_items()
                self._clear_osm_items()
            else:
                # Temporarily shift vector overlays to align with new origin until
                # the async refresh replaces them.
                if self.check_opendrive.isChecked():
                    if self.opendrive_item:
                        self.opendrive_item.setPos(dx_scene, dy_scene)
                    if self._xodr_vector_group:
                        self._xodr_vector_group.setPos(dx_scene, dy_scene)

                if self.check_osm.isChecked() and self._osm_vector_group:
                    self._osm_vector_group.setPos(dx_scene, dy_scene)

                self._flush_tile_resize(old_offset, old_w, old_h)

        # Sync World Extent spinboxes
        self._update_world_extent_spinboxes()

        # Draw bounding-box overlays
        self._draw_xodr_bounds_rect()
        self._draw_osm_bounds_rect()
        self._draw_world_extent_rect()

        # Trigger refresh
        self.update_visibility()

    def fit_world_extent_to_osm(self):
        """Update world extent to match the loaded OSM file geometry."""
        path = self.osm_path
        if not path or not os.path.isfile(path) or not self.map_ctx:
            return

        try:
            ways, _tree = self._parse_osm(path)
        except Exception as exc:
            print(f'Failed to parse OSM bounds: {exc}')
            return
        if not ways:
            return

        from open_road_editor.utils.coords import _tmerc_forward_wgs84

        ref_lat = float(self.map_ctx.earth_ref_lat)
        ref_lon = float(self.map_ctx.earth_ref_lon)
        x0 = float(getattr(self.map_ctx, 'proj_false_easting', 0.0))
        y0 = float(getattr(self.map_ctx, 'proj_false_northing', 0.0))
        k0 = float(getattr(self.map_ctx, 'proj_scale_factor', 1.0))

        min_x, max_x = float('inf'), float('-inf')
        min_y, max_y = float('inf'), float('-inf')
        for _highway, coords, _tags, _way_id, _node_refs in ways:
            for lat, lon in coords:
                try:
                    x_tm, y_tm = _tmerc_forward_wgs84(
                        float(lat), float(lon), ref_lat, ref_lon, k0, x0, y0
                    )
                except Exception:
                    continue
                carla_y = -float(y_tm)
                min_x = min(min_x, float(x_tm))
                max_x = max(max_x, float(x_tm))
                min_y = min(min_y, carla_y)
                max_y = max(max_y, carla_y)

        if min_x == float('inf'):
            return

        # OSM fit should be exact; adding OpenDRIVE margin leaves a visible gap
        # between the world bounds (green) and OSM bounds (red) overlays.
        bounds = [min_x, max_x, min_y, max_y]

        current_bounds = self.map_ctx.world_bounds
        bounds_match = len(current_bounds) == 4 and all(
            abs(c - n) < 1e-3 for c, n in zip(current_bounds, bounds)
        )
        if bounds_match:
            print('World extent already matches OSM. Skipping update.')
            return

        old_offset = list(self.map_ctx.world_offset)
        old_w = self.map_ctx.width_in_pixels
        old_h = self.map_ctx.height_in_pixels
        old_mpp = self.map_ctx.mpp

        view_center = self.view.viewport().rect().center()
        scene_center = self.view.mapToScene(view_center)
        world_center_x = old_offset[0] + scene_center.x() * old_mpp
        world_center_y = old_offset[1] + scene_center.y() * old_mpp

        self.map_ctx.world_bounds = bounds
        self.map_ctx.world_offset = [bounds[0], bounds[2]]
        w = bounds[1] - bounds[0]
        h = bounds[3] - bounds[2]
        self.map_ctx.width_in_pixels = int(math.ceil(w / self.map_ctx.mpp))
        self.map_ctx.height_in_pixels = int(math.ceil(h / self.map_ctx.mpp))

        if self.scene:
            rect = QRectF(0, 0, self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels)
            self.scene.setSceneRect(rect)
            self._sync_scene_rect()

        if self.grid_item:
            self.grid_item.mpp = self.map_ctx.mpp
            self.grid_item.rect = QRectF(
                0, 0, self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels
            )
            self.grid_item.world_offset = self.map_ctx.world_offset
            self.grid_item.update()

        new_mpp = self.map_ctx.mpp
        new_offset = self.map_ctx.world_offset
        new_scene_x = (world_center_x - new_offset[0]) / new_mpp
        new_scene_y = (world_center_y - new_offset[1]) / new_mpp
        self.view.centerOn(new_scene_x, new_scene_y)

        dx_scene = (old_offset[0] - new_offset[0]) / new_mpp
        dy_scene = (old_offset[1] - new_offset[1]) / new_mpp

        if self._carla_bev_server_meta is not None:
            self._carla_bev_draw_bounds_rect(self._carla_bev_server_meta)

        # OSM fit keeps projection/mpp unchanged; shift overlays until refresh settles.
        if self.check_opendrive.isChecked():
            if self.opendrive_item:
                self.opendrive_item.setPos(dx_scene, dy_scene)
            if self._xodr_vector_group:
                self._xodr_vector_group.setPos(dx_scene, dy_scene)
        if self.check_osm.isChecked() and self._osm_vector_group:
            self._osm_vector_group.setPos(dx_scene, dy_scene)
        self._flush_tile_resize(old_offset, old_w, old_h)

        self._update_world_extent_spinboxes()
        self._draw_xodr_bounds_rect()
        self._draw_osm_bounds_rect()
        self._draw_world_extent_rect()
        self.update_visibility()

    def fit_world_extent_to_carla(self):
        """Update world extent to match the CARLA server's reported bounds."""
        if not self.map_ctx:
            return

        ip = (
            self.node.tcp_server_ip
            if self.node
            else getattr(self, 'server_ip', DEFAULT_SERVER_HOST)
        )
        port = (
            self.node.tcp_server_port
            if self.node
            else getattr(self, 'server_port', DEFAULT_SERVER_PORT)
        )
        url = f'http://{ip}:{port}/metadata'

        try:
            req = urllib_request.Request(url, headers={'User-Agent': HTTP_USER_AGENT})
            with urllib_request.urlopen(req, timeout=1.0) as resp:
                meta = json.loads(resp.read().decode())
        except Exception:
            self._show_project_status('Cannot connect to CARLA server')
            return

        new_bounds = meta.get('world_bounds')
        if not new_bounds or len(new_bounds) < 4:
            self._show_project_status('Server returned invalid bounds')
            return

        new_mpp = meta.get('mpp')
        if new_mpp is None:
            # Fallback: keep current MPP or calculate from ref_lat if provided
            ref_lat = meta.get('ref_lat')
            if ref_lat is not None:
                new_mpp = (EARTH_CIRCUMFERENCE * math.cos(math.radians(ref_lat))) / (
                    (2**self.map_ctx.tile_max_zoom_level) * TILE_SIZE
                )
            else:
                new_mpp = self.map_ctx.mpp

        # Check if match
        current_bounds = self.map_ctx.world_bounds
        bounds_match = len(current_bounds) == 4 and all(
            abs(c - n) < 1e-3 for c, n in zip(current_bounds, new_bounds)
        )
        mpp_match = abs(self.map_ctx.mpp - new_mpp) < 1e-6

        if bounds_match and mpp_match:
            print('World extent already matches CARLA. Skipping update.')
            return

        # Capture old state
        old_offset = list(self.map_ctx.world_offset)
        old_w = self.map_ctx.width_in_pixels
        old_h = self.map_ctx.height_in_pixels
        old_mpp = self.map_ctx.mpp

        # Capture viewport center
        view_center = self.view.viewport().rect().center()
        scene_center = self.view.mapToScene(view_center)
        world_center_x = old_offset[0] + scene_center.x() * old_mpp
        world_center_y = old_offset[1] + scene_center.y() * old_mpp

        # Update map_ctx
        self.map_ctx.world_bounds = new_bounds
        self.map_ctx.world_offset = [new_bounds[0], new_bounds[2]]
        self.map_ctx.mpp = new_mpp
        self.map_ctx.min_meters_per_pixel = new_mpp

        # Update geo ref if provided
        if 'ref_lat' in meta:
            self.map_ctx.earth_ref_lat = meta['ref_lat']
        if 'ref_lon' in meta:
            self.map_ctx.earth_ref_lon = meta['ref_lon']

        w = new_bounds[1] - new_bounds[0]
        h = new_bounds[3] - new_bounds[2]
        self.map_ctx.width_in_pixels = int(math.ceil(w / new_mpp))
        self.map_ctx.height_in_pixels = int(math.ceil(h / new_mpp))

        # Update scene/grid
        if self.scene:
            rect = QRectF(0, 0, self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels)
            self.scene.setSceneRect(rect)
            self._sync_scene_rect()

        if self.grid_item:
            self.grid_item.mpp = self.map_ctx.mpp
            self.grid_item.rect = QRectF(
                0, 0, self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels
            )
            self.grid_item.world_offset = self.map_ctx.world_offset
            self.grid_item.update()

        # Restore viewport
        new_offset = self.map_ctx.world_offset
        new_scene_x = (world_center_x - new_offset[0]) / new_mpp
        new_scene_y = (world_center_y - new_offset[1]) / new_mpp
        self.view.centerOn(new_scene_x, new_scene_y)

        # Calc scene shift for vector overlays
        dx_scene = (old_offset[0] - new_offset[0]) / new_mpp
        dy_scene = (old_offset[1] - new_offset[1]) / new_mpp

        if self._carla_bev_server_meta is not None:
            self._carla_bev_draw_bounds_rect(self._carla_bev_server_meta)

        mpp_changed = abs(new_mpp - old_mpp) > 1e-6
        if mpp_changed:
            if self._esri_pix_data is not None or self._esri_loading:
                self.stop_esri_refresh()
            if self._carla_bev_pix_data is not None or self._carla_bev_loading:
                self.stop_carla_bev_refresh()
            if self.opendrive_item:
                self.opendrive_item.setPixmap(QPixmap())
            self._clear_xodr_vector_items()
            self._clear_osm_items()
        else:
            # Temporarily shift vector overlays
            if self.check_opendrive.isChecked():
                if self.opendrive_item:
                    self.opendrive_item.setPos(dx_scene, dy_scene)
                if self._xodr_vector_group:
                    self._xodr_vector_group.setPos(dx_scene, dy_scene)
            if self.check_osm.isChecked() and self._osm_vector_group:
                self._osm_vector_group.setPos(dx_scene, dy_scene)
            self._flush_tile_resize(old_offset, old_w, old_h)

        self._update_world_extent_spinboxes()
        if self.xodr_path:
            self._draw_xodr_bounds_rect()
        self._draw_osm_bounds_rect()
        self._draw_world_extent_rect()
        self.update_visibility()

    def on_xodr_path_changed(self, text):
        path = text.strip()
        # If path changed, clear old data
        if path != (self.xodr_path or ''):
            self.xodr_path = path
            self._xodr_content = None
            if not self._restoring_project_payload:
                self._suppress_auto_fit = False
            # Don't fit automatically
            # self._fit_after_opendrive_load = True

            if os.path.isfile(path) and path.lower().endswith('.xodr'):
                try:
                    with open(self.xodr_path, 'r', encoding='utf-8') as f:
                        self._xodr_content = f.read()
                except Exception as e:
                    print(f'Failed to read XODR content: {e}')

                # Reset rendered item (raster and vector)
                if self.opendrive_item:
                    self.opendrive_item.setPixmap(QPixmap())
                self._clear_xodr_vector_items()

                # ONLY parse bounds for the overlay rect, DO NOT update map_ctx
                # print(f'Parsing bounds from new XODR (overlay only): {path}')

                # Update window title unless this path change is part of project restore
                programmatic_update = self._suppress_next_xodr_title_update
                if self._suppress_next_xodr_title_update:
                    self._suppress_next_xodr_title_update = False
                elif not self._restoring_project_payload:
                    self.town_name = os.path.basename(path).replace('.xodr', '')
                    self._refresh_window_title()

                # Draw bounding-box overlays for the XODR extent
                # This uses map_ctx to project the bounds, so it will show where the
                # XODR lands in the CURRENT world extent.
                self._draw_xodr_bounds_rect()

                # Enable checkbox if not already enabled.
                # For programmatic updates (e.g. refresh_all_layers) preserve the
                # user's current visibility — the caller is responsible for refreshing.
                if not self.check_opendrive.isChecked():
                    if not programmatic_update:
                        self.check_opendrive.setChecked(True)
                else:
                    if not programmatic_update:
                        # Trigger refresh explicitly since path changed
                        self.refresh_opendrive()

                # Reset saved grid state so grid auto-enables for the new map
                # self._grid_saved_state = True
            else:
                self.xodr_path = None

        self.update_visibility()

    # ── Incremental tile-buffer resize helpers ──────────────────────────

    def _resize_tile_buffer(
        self,
        pix_data,
        pix_lock,
        fetch_lock,
        fetched_tiles_attr,
        fetching_tiles_attr,
        epoch_attr,
        tile_pixel_region_fn,
        old_offset,
        old_w,
        old_h,
        item,
        fetch_visible_fn,
    ):
        """Resize a tile pixel buffer after a world-extent change.

        Shifts existing content to its correct new position, invalidates
        tiles that were partially clipped at the old edges, and triggers
        fetch_visible to load only the newly exposed tiles.

        Returns ``(new_buffer, dx, dy)`` where *dx*/*dy* is the pixel shift.
        """
        mpp = self.map_ctx.mpp
        new_w = self.map_ctx.width_in_pixels
        new_h = self.map_ctx.height_in_pixels
        new_offset = self.map_ctx.world_offset

        # Cancel in-flight fetches (they target old pixel coordinates)
        setattr(self, epoch_attr, getattr(self, epoch_attr) + 1)
        with fetch_lock:
            setattr(self, fetching_tiles_attr, set())

        # Pixel shift: how much old content moves in the new buffer
        dx = int(round((old_offset[0] - new_offset[0]) / mpp))
        dy = int(round((old_offset[1] - new_offset[1]) / mpp))

        with pix_lock:
            old_data = pix_data
            new_data = np.full((new_h, new_w, 4), list(TILE_PLACEHOLDER_BG_COLOR), dtype=np.uint8)
            # Copy overlapping region from old buffer into new buffer
            src_x0 = max(0, -dx)
            src_x1 = min(old_w, new_w - dx)
            src_y0 = max(0, -dy)
            src_y1 = min(old_h, new_h - dy)
            dst_x0 = max(0, dx)
            dst_y0 = max(0, dy)
            if src_x1 > src_x0 and src_y1 > src_y0:
                h = src_y1 - src_y0
                w = src_x1 - src_x0
                new_data[dst_y0 : dst_y0 + h, dst_x0 : dst_x0 + w] = old_data[
                    src_y0:src_y1, src_x0:src_x1
                ]
            return new_data, dx, dy

    def _invalidate_edge_tiles(
        self, fetched_tiles, tile_pixel_region_fn, old_w, old_h, zoom, dx, dy
    ):
        """Remove tiles from *fetched_tiles* that were clipped at the old
        buffer edges so they get properly re-painted at the new size.

        *dx*/*dy* is the pixel shift applied when copying old content into
        the new buffer (old_offset - new_offset) / mpp.
        """
        to_remove = set()
        for tx, ty in fetched_tiles:
            region = tile_pixel_region_fn(tx, ty, zoom)
            if region is None:
                # Tile is now entirely outside the new buffer — drop it
                to_remove.add((tx, ty))
                continue
            y0, x0, mask, _, _ = region
            rh, rw = mask.shape
            # Compute where this tile sat in the OLD buffer
            old_x0 = x0 - dx
            old_y0 = y0 - dy
            # If it overlapped any edge of the old buffer, it was clipped
            if old_x0 <= 0 or old_y0 <= 0 or old_x0 + rw >= old_w or old_y0 + rh >= old_h:
                to_remove.add((tx, ty))
        return fetched_tiles - to_remove

    # ──────────────────────────────────────────────────────────────────────

    def _on_world_extent_changed(self, _value=None):
        """Push World Extent spinbox values into map_ctx and update the viewport."""
        if not self.map_ctx:
            return

        # Capture old state before updating map_ctx
        old_offset = list(self.map_ctx.world_offset)
        old_w = self.map_ctx.width_in_pixels
        old_h = self.map_ctx.height_in_pixels
        old_mpp = float(self.map_ctx.mpp)

        ref_lat = self.spin_origin_lat.value()
        ref_lon = self.spin_origin_lon.value()
        self.map_ctx.earth_ref_lat = ref_lat
        self.map_ctx.earth_ref_lon = ref_lon
        new_mpp = (EARTH_CIRCUMFERENCE * math.cos(math.radians(ref_lat))) / (
            (2**self.map_ctx.tile_max_zoom_level) * TILE_SIZE
        )
        self.map_ctx.mpp = new_mpp
        self.map_ctx.min_meters_per_pixel = new_mpp

        # Spinbox values: positive = away from origin in the labelled direction,
        # negative = the edge has crossed the origin to the opposite side.
        west = -self.spin_bound_west.value()
        east = self.spin_bound_east.value()
        carla_min_y = -self.spin_bound_north.value()
        carla_max_y = self.spin_bound_south.value()
        self.map_ctx.world_bounds = [west, east, carla_min_y, carla_max_y]
        new_offset = [west, carla_min_y]
        self.map_ctx.world_offset = new_offset

        south = carla_min_y
        north = carla_max_y

        # Recompute pixel dimensions and scene rect
        w_m = east - west
        h_m = north - south
        if w_m > 0 and h_m > 0:
            self.map_ctx.width_in_pixels = int(math.ceil(w_m / new_mpp))
            self.map_ctx.height_in_pixels = int(math.ceil(h_m / new_mpp))

            # Always update grid offset so it stays anchored to the world during N/W drags
            if self.grid_item:
                self.grid_item.world_offset = self.map_ctx.world_offset
                self.grid_item.update()

            # During drag, we DEFER scene rect updates and other heavy property changes
            if self._extent_drag_edge is None:
                new_rect = QRectF(
                    0, 0, self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels
                )
                self._sync_scene_rect()
                if self.grid_item:
                    self.grid_item.mpp = new_mpp
                    self.grid_item.rect = new_rect
                    # world_offset updated above

        # ── Compensate viewport scroll when the offset shifts (N/W edge drags)
        # so the visible content stays anchored in the same place on screen.
        dx_m = old_offset[0] - new_offset[0]
        dy_m = old_offset[1] - new_offset[1]
        if abs(dx_m) > 1e-9 or abs(dy_m) > 1e-9:
            scale = self.view.transform().m11() or 1.0
            dx_scene = dx_m / new_mpp
            dy_scene = dy_m / new_mpp
            hbar = self.view.horizontalScrollBar()
            vbar = self.view.verticalScrollBar()
            hbar.setValue(hbar.value() + int(round(dx_scene * scale)))
            vbar.setValue(vbar.value() + int(round(dy_scene * scale)))

        # Redraw the world extent bounding box
        self._draw_world_extent_rect()

        # During drag, DEFER drawing other bounds rects.
        if self._extent_drag_edge is None:
            # Redraw the XODR bounding box if an XODR file is loaded (its pixel
            # position depends on the world offset / mpp which may have changed)
            if self.xodr_path:
                self._draw_xodr_bounds_rect()
            self._draw_osm_bounds_rect()

            # Redraw the CARLA BEV bounding box (its scene position also depends
            # on world_bounds / mpp which just changed).
            if self._carla_bev_server_meta is not None:
                self._carla_bev_draw_bounds_rect(self._carla_bev_server_meta)

        # ── Incrementally resize tile layers ──────────────────────────────
        # Shift existing pixel content to its correct new position and fetch
        # only the newly exposed tiles (existing tiles stay, no full reload).
        #
        # During an active edge drag we skip the heavy tile-buffer resize and
        # overlay re-render so that the bounding-box edges update smoothly.
        # The heavy work is flushed once when the drag ends.
        if self._extent_drag_edge is not None:
            # Capture the "old" state only on the first deferred call so the
            # flush at drag-end can diff against the true pre-drag dimensions.
            if not self._extent_drag_needs_tile_flush:
                self._extent_flush_old_offset = old_offset
                self._extent_flush_old_w = old_w
                self._extent_flush_old_h = old_h
            self._extent_drag_needs_tile_flush = True

            # Temporarily shift scene items so they appear to stay anchored to
            # their world coordinates while the scene origin (0,0) moves
            # (which happens during North/West edge drags).
            dx_s = (self._extent_flush_old_offset[0] - new_offset[0]) / new_mpp
            dy_s = (self._extent_flush_old_offset[1] - new_offset[1]) / new_mpp

            # ESRI item uses update_imagery_alignment logic + drag shift
            if self.esri_item:
                ex = self.spin_esri_x.value() / new_mpp
                ey = self.spin_esri_y.value() / new_mpp
                self.esri_item.setPos(ex + dx_s, ey + dy_s)

            # Other items shift relative to their base (0,0)
            if self.carla_bev_item:
                self.carla_bev_item.setPos(dx_s, dy_s)
            if self.opendrive_item:
                self.opendrive_item.setPos(dx_s, dy_s)
            if self._xodr_vector_group:
                self._xodr_vector_group.setPos(dx_s, dy_s)
            if self._osm_vector_group:
                self._osm_vector_group.setPos(dx_s, dy_s)
            if self._carla_bev_bounds_rect_item:
                self._carla_bev_bounds_rect_item.setPos(dx_s, dy_s)
            if self._xodr_bounds_rect_item:
                self._xodr_bounds_rect_item.setPos(dx_s, dy_s)
            if self._osm_bounds_rect_item:
                self._osm_bounds_rect_item.setPos(dx_s, dy_s)
            return

        self._flush_tile_resize(old_offset, old_w, old_h)

    # -- deferred heavy work split out so it can be called at drag-end ----

    _extent_drag_needs_tile_flush: bool = False
    _extent_flush_old_offset: list | None = None
    _extent_flush_old_w: int = 0
    _extent_flush_old_h: int = 0

    def _flush_tile_resize(self, old_offset, old_w, old_h):
        """Run the tile-buffer resize + overlay refresh for both ESRI and
        CARLA BEV layers.  Split out of ``_on_world_extent_changed`` so it
        can be called once at drag-end instead of on every pixel of movement."""
        new_mpp = self.map_ctx.mpp if self.map_ctx else 1.0

        if self.check_esri.isChecked() and self._esri_pix_data is not None:
            new_buf, dx, dy = self._resize_tile_buffer(
                self._esri_pix_data,
                self._esri_pix_lock,
                self._esri_fetch_lock,
                '_esri_fetched_tiles',
                '_esri_fetching_tiles',
                '_esri_epoch',
                self._esri_tile_pixel_region,
                old_offset,
                old_w,
                old_h,
                self.esri_item,
                self._esri_fetch_visible,
            )
            with self._esri_pix_lock:
                self._esri_pix_data = new_buf
            with self._esri_fetch_lock:
                self._esri_fetched_tiles = self._invalidate_edge_tiles(
                    self._esri_fetched_tiles,
                    self._esri_tile_pixel_region,
                    old_w,
                    old_h,
                    self._esri_current_zoom,
                    dx,
                    dy,
                )
            with self._esri_pix_lock:
                snap = Image.fromarray(self._esri_pix_data.copy())
            self.esri_item.setPixmap(self.pil_to_qpixmap(snap))
            self.update_imagery_alignment()
            self._esri_fetch_visible()

        if self.check_carla_bev.isChecked() and self._carla_bev_pix_data is not None:
            new_buf, dx, dy = self._resize_tile_buffer(
                self._carla_bev_pix_data,
                self._carla_bev_pix_lock,
                self._carla_bev_fetch_lock,
                '_carla_bev_fetched_tiles',
                '_carla_bev_fetching_tiles',
                '_carla_bev_epoch',
                self._esri_tile_pixel_region,
                old_offset,
                old_w,
                old_h,
                self.carla_bev_item,
                self._carla_bev_fetch_visible,
            )
            with self._carla_bev_pix_lock:
                self._carla_bev_pix_data = new_buf
            with self._carla_bev_fetch_lock:
                self._carla_bev_fetched_tiles = self._invalidate_edge_tiles(
                    self._carla_bev_fetched_tiles,
                    self._esri_tile_pixel_region,
                    old_w,
                    old_h,
                    self._carla_bev_current_zoom,
                    dx,
                    dy,
                )
            with self._carla_bev_pix_lock:
                snap = Image.fromarray(self._carla_bev_pix_data.copy())
            self.carla_bev_item.setPixmap(self.pil_to_qpixmap(snap))
            self.carla_bev_item.setPos(0, 0)
            self._carla_bev_fetch_visible()
        elif self.carla_bev_item:
            self.carla_bev_item.setPos(0, 0)

        # Vector/raster overlays re-render locally (no network fetch)
        if self.check_opendrive.isChecked():
            self.refresh_opendrive()
        else:
            if self.opendrive_item:
                self.opendrive_item.setPos(0, 0)
            if self._xodr_vector_group:
                self._xodr_vector_group.setPos(0, 0)

        if self.check_osm.isChecked() and self._osm_vector_group is not None:
            self.refresh_osm()
        elif self._osm_vector_group:
            self._osm_vector_group.setPos(0, 0)

    def _finish_extent_drag(self):
        """Flush deferred tile/overlay work after an edge drag ends."""
        if not self._extent_drag_needs_tile_flush or not self.map_ctx:
            self._extent_drag_needs_tile_flush = False
            return
        self._extent_drag_needs_tile_flush = False
        # Use current map_ctx state vs. the pre-drag snapshot captured at drag
        # start.  Since we deferred ALL tile work, the "old" state is whatever
        # was current before the very first deferred call, i.e. the state
        # captured by the first _on_world_extent_changed that entered the
        # early-return path.  We stored that in the instance attributes.
        old_offset = self._extent_flush_old_offset
        old_w = self._extent_flush_old_w
        old_h = self._extent_flush_old_h
        if old_offset is None:
            return

        # --- Call all the deferred updates now that the drag has finished ---
        # 1. Update scene rect and grid geometry
        new_mpp = self.map_ctx.mpp
        new_rect = QRectF(0, 0, self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels)
        self._sync_scene_rect()
        if self.grid_item:
            self.grid_item.mpp = new_mpp
            self.grid_item.rect = new_rect
            self.grid_item.world_offset = self.map_ctx.world_offset
            self.grid_item.update()

        # 2. Redraw auxiliary bounding boxes
        if self.xodr_path:
            self._draw_xodr_bounds_rect()
        self._draw_osm_bounds_rect()
        if self._carla_bev_server_meta is not None:
            self._carla_bev_draw_bounds_rect(self._carla_bev_server_meta)

        self._flush_tile_resize(old_offset, old_w, old_h)
        self._extent_flush_old_offset = None

    def _update_world_extent_spinboxes(self):
        """Sync spinboxes from map_ctx (e.g. after XODR load)."""
        if not self.map_ctx:
            return
        block = [
            self.spin_origin_lat,
            self.spin_origin_lon,
            self.spin_bound_north,
            self.spin_bound_south,
            self.spin_bound_east,
            self.spin_bound_west,
        ]
        for w in block:
            w.blockSignals(True)
        self.spin_origin_lat.setValue(self.map_ctx.earth_ref_lat)
        self.spin_origin_lon.setValue(self.map_ctx.earth_ref_lon)
        wb = self.map_ctx.world_bounds
        if wb and len(wb) == 4:
            self.spin_bound_west.setValue(-wb[0])
            self.spin_bound_east.setValue(wb[1])
            # CARLA Y is negated: wb[2]=min_y → geographic north,
            # wb[3]=max_y → geographic south.  Show both as positive.
            self.spin_bound_north.setValue(-wb[2])
            self.spin_bound_south.setValue(wb[3])
        for w in block:
            w.blockSignals(False)

    def _on_layer_checkbox_changed(self, _state):
        if self._suppress_async_layer_pipeline:
            return
        self.update_visibility(defer_fetch=True)
        self._schedule_layer_pipeline('layer-toggle', False)

    def _capture_layer_runtime_context(self) -> dict:
        """Capture UI state needed by worker threads without touching Qt in workers."""
        scene_rect = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        mouse_view_pos = self.view.viewport().mapFromGlobal(QCursor.pos())
        mouse_scene_pos = self.view.mapToScene(mouse_view_pos)
        self._last_mouse_scene_pos = mouse_scene_pos
        with self._esri_pix_lock:
            esri_ready = self._esri_pix_data is not None
        with self._carla_bev_pix_lock:
            carla_bev_ready = self._carla_bev_pix_data is not None
        return {
            'check_esri': self.check_esri.isChecked(),
            'check_carla_bev': self.check_carla_bev.isChecked(),
            'check_opendrive': self.check_opendrive.isChecked(),
            'scene_rect': (
                float(scene_rect.left()),
                float(scene_rect.top()),
                float(scene_rect.right()),
                float(scene_rect.bottom()),
            ),
            'mouse_scene': (float(mouse_scene_pos.x()), float(mouse_scene_pos.y())),
            'esri_item_pos': (float(self.esri_item.x()), float(self.esri_item.y())),
            'carla_bev_item_pos': (
                float(self.carla_bev_item.x()),
                float(self.carla_bev_item.y()),
            ),
            'map_width': int(self.map_ctx.width_in_pixels) if self.map_ctx else 0,
            'map_height': int(self.map_ctx.height_in_pixels) if self.map_ctx else 0,
            'map_mpp': float(self.map_ctx.mpp) if self.map_ctx else 1.0,
            'world_offset': tuple(self.map_ctx.world_offset) if self.map_ctx else (0.0, 0.0),
            'geo': self._esri_geo_params()
            if self.map_ctx
            else (
                float(METERS_PER_DEGREE_LAT),
                DEFAULT_REF_LAT,
                DEFAULT_REF_LON,
                0.0,
                0.0,
            ),
            'esri_zoom': int(self._esri_current_zoom),
            'carla_bev_zoom': int(self._carla_bev_current_zoom),
            'esri_ready': bool(esri_ready),
            'carla_bev_ready': bool(carla_bev_ready),
            'epoch_esri': int(self._esri_epoch),
            'epoch_carla_bev': int(self._carla_bev_epoch),
            'ts': time.time(),
        }

    @staticmethod
    def _visible_tiles_from_context(snapshot: dict, *, zoom_key: str, item_pos_key: str) -> set:
        """Compute visible tile set using only snapshot data (worker-thread safe)."""
        W = int(snapshot.get('map_width', 0))
        H = int(snapshot.get('map_height', 0))
        if W <= 0 or H <= 0:
            return set()

        left, top, right, bottom = snapshot['scene_rect']
        item_x, item_y = snapshot[item_pos_key]
        img_x0 = max(0, int(left - item_x))
        img_y0 = max(0, int(top - item_y))
        img_x1 = min(W - 1, int(right - item_x))
        img_y1 = min(H - 1, int(bottom - item_y))
        if img_x0 >= img_x1 or img_y0 >= img_y1:
            return set()

        m_per_deg, ref_lat, ref_lon, off_x0, off_y0 = snapshot['geo']
        world_off_x, world_off_y = snapshot['world_offset']
        mpp = snapshot['map_mpp']
        zoom = int(snapshot[zoom_key])
        cos_ref = math.cos(math.radians(ref_lat))
        n = 2.0**zoom

        def pix_to_tile(px, py):
            wx = world_off_x + px * mpp
            wy = world_off_y + py * mpp
            lon = (wx - off_x0) / (m_per_deg * cos_ref) + ref_lon
            lat = ref_lat - (wy - off_y0) / m_per_deg
            tx = int(math.floor((lon + 180) / 360 * n))
            ty = int(math.floor((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n))
            return tx, ty

        tl = pix_to_tile(img_x0, img_y0)
        br = pix_to_tile(img_x1, img_y1)
        tr = pix_to_tile(img_x1, img_y0)
        bl = pix_to_tile(img_x0, img_y1)
        tx_min = min(tl[0], br[0], tr[0], bl[0])
        tx_max = max(tl[0], br[0], tr[0], bl[0])
        ty_min = min(tl[1], br[1], tr[1], bl[1])
        ty_max = max(tl[1], br[1], tr[1], bl[1])
        return {(tx, ty) for tx in range(tx_min, tx_max + 1) for ty in range(ty_min, ty_max + 1)}

    def _schedule_layer_pipeline(self, trigger: str, grid_state_changed: bool) -> None:
        snapshot = self._capture_layer_runtime_context()
        with self._layer_pipeline_lock:
            self._layer_pipeline_seq += 1
            seq = self._layer_pipeline_seq

        def run_pipeline(request_seq: int, request_snapshot: dict) -> None:
            actions = {
                'trigger': trigger,
                'grid_state_changed': bool(grid_state_changed),
                'snapshot': request_snapshot,
                'esri_visible_tiles': [],
                'carla_bev_visible_tiles': [],
            }
            if request_snapshot.get('check_esri') and request_snapshot.get('esri_ready'):
                actions['esri_visible_tiles'] = list(
                    self._visible_tiles_from_context(
                        request_snapshot,
                        zoom_key='esri_zoom',
                        item_pos_key='esri_item_pos',
                    )
                )
            if request_snapshot.get('check_carla_bev') and request_snapshot.get('carla_bev_ready'):
                actions['carla_bev_visible_tiles'] = list(
                    self._visible_tiles_from_context(
                        request_snapshot,
                        zoom_key='carla_bev_zoom',
                        item_pos_key='carla_bev_item_pos',
                    )
                )
            self.layer_pipeline_ready.emit(actions, request_seq)

        threading.Thread(target=run_pipeline, args=(seq, snapshot), daemon=True).start()

    def _on_layer_pipeline_ready(self, actions: dict, seq: int) -> None:
        with self._layer_pipeline_lock:
            if seq != self._layer_pipeline_seq:
                return

        self._suppress_async_layer_pipeline = True
        try:
            self.update_visibility(
                grid_state_changed=actions.get('grid_state_changed', False),
                defer_fetch=True,
            )
        finally:
            self._suppress_async_layer_pipeline = False

        if (
            self.check_opendrive.isChecked()
            and self.opendrive_item.pixmap().isNull()
            and not self._xodr_is_vector
        ):
            self.refresh_opendrive()

        if self.check_osm.isChecked() and self._osm_vector_group is None and not self._osm_loading:
            self.refresh_osm()

        if self.check_esri.isChecked():
            if self._esri_pix_data is None:
                self.refresh_esri()
            else:
                self._esri_fetch_visible(set(actions.get('esri_visible_tiles', [])))

        if self.check_carla_bev.isChecked():
            if self._carla_bev_pix_data is None or self.carla_bev_item.pixmap().isNull():
                self.refresh_carla_bev()
            else:
                self._carla_bev_fetch_visible(set(actions.get('carla_bev_visible_tiles', [])))

        mouse_xy = actions.get('snapshot', {}).get('mouse_scene')
        if mouse_xy is not None:
            self._last_mouse_scene_pos = QPointF(mouse_xy[0], mouse_xy[1])
            if self._xodr_is_vector and self.check_opendrive.isChecked():
                pass  # hover removed

    def _on_opendrive_opacity_changed(self, value: float):
        self._apply_opendrive_layer_style(opacity=value)
        # Only persist the alpha when the spinbox is in interactive mode (i.e. not
        # locked at 1.0 for the solo-OpenDRIVE case).
        if not self._alpha_only_mode:
            self._alpha_saved = value
        self.view.viewport().update()
        if self.opendrive_item and not self.opendrive_loading:
            if not self.check_opendrive.isChecked():
                self.lbl_opendrive_status.setText('')
            elif value <= 0.0:
                self.lbl_opendrive_status.setText('Loaded (Hidden)')
            else:
                self.lbl_opendrive_status.setText('Loaded')

    def _apply_opendrive_layer_style(
        self, opacity: float | None = None, visible: bool | None = None
    ):
        op = self.spin_opendrive_alpha.value() if opacity is None else float(opacity)
        vis = self.check_opendrive.isChecked() if visible is None else bool(visible)
        render_visible = vis and op > 0.0

        if self.opendrive_item:
            self.opendrive_item.setVisible(render_visible and not self._xodr_is_vector)
            self.opendrive_item.setOpacity(op)

        if self._xodr_vector_group is not None:
            # Set visibility on the group AND on every child item.  Relying solely
            # on group-level setVisible() can silently fail in PyQt5/early PyQt6 — the group
            # node is marked invisible but child QGraphicsPathItems may still be
            # rendered.  Explicit per-item propagation guarantees correct behaviour
            # for both the checkbox-hide and opacity-zero cases.
            self._xodr_vector_group.setVisible(render_visible)
            self._xodr_vector_group.setOpacity(op)
            for _item in self._xodr_item_meta:
                _item.setVisible(render_visible)

        # Signal visibility
        show_objects = getattr(self, 'check_opendrive_objects', None)
        show_objects_checked = show_objects.isChecked() if show_objects is not None else True
        for sig_item in getattr(self, '_xodr_vector_signal_items', []):
            sig_item.setVisible(render_visible and show_objects_checked)
            sig_item.setOpacity(op)

        if not render_visible:
            pass  # xodr hover removed

    def update_visibility(self, grid_state_changed=False, defer_fetch=False):
        # While an edge drag is active, we skip all visibility updates and
        # tile fetches to keep the UI responsive.  A full refresh is
        # triggered via _finish_extent_drag once the drag ends.
        if self._extent_drag_edge is not None:
            return

        if self.esri_item:
            self.esri_item.setVisible(self.check_esri.isChecked())
            if self.check_esri.isChecked() and self._esri_pix_data is None:
                if not defer_fetch:
                    self.refresh_esri()
            elif self.check_esri.isChecked():
                # Layer is visible and already initialised: fetch any newly visible tiles
                if not defer_fetch:
                    self._esri_fetch_visible()
            elif not self.check_esri.isChecked():
                if self._esri_loading:
                    self.stop_esri_refresh()
                else:
                    self.lbl_esri_status.setText('')

        if self.carla_bev_item:
            carla_bev_checked = self.check_carla_bev.isChecked()
            self.carla_bev_item.setVisible(carla_bev_checked)
            if self._carla_bev_bounds_rect_item is not None:
                self._carla_bev_bounds_rect_item.setVisible(carla_bev_checked)
            if carla_bev_checked and self.carla_bev_item.pixmap().isNull():
                if not defer_fetch:
                    self.refresh_carla_bev()
            elif carla_bev_checked and not self._carla_bev_loading:
                self._carla_bev_update_status_label()
            elif not carla_bev_checked:
                if self._carla_bev_loading:
                    self.stop_carla_bev_refresh()
                else:
                    self.lbl_carla_bev_status.setText('')

        # -- XODR bounds rect follows OpenDRIVE layer visibility --
        if self._xodr_bounds_rect_item is not None:
            self._xodr_bounds_rect_item.setVisible(self.check_opendrive.isChecked())

        # -- OSM bounds rect follows OSM layer visible state --
        if self._osm_bounds_rect_item is not None:
            self._osm_bounds_rect_item.setVisible(
                self.check_osm.isChecked() and self.spin_osm_alpha.value() > 0.0
            )

        # -- World Extent rect is always visible when the scene is populated --
        for _it in self._world_extent_edge_items.values():
            if _it is not None:
                _it.setVisible(True)

        # Opacity is interactive only when OpenDRIVE is checked AND at least one base
        # layer (ESRI or carla_bev/CARLA) is also active.  When OpenDRIVE is the sole
        # visible layer, the spinbox is locked at 1.0 so the road network is always
        # fully visible; the previous user-chosen alpha is saved and restored when a
        # base layer returns.
        opendrive_on = self.check_opendrive.isChecked()
        has_base_layer = self.check_esri.isChecked() or self.check_carla_bev.isChecked()
        was_only_mode = self._alpha_only_mode

        if opendrive_on and not has_base_layer and not was_only_mode:
            # Transitioning into solo-OpenDRIVE mode: save alpha, force spinbox to 1.0.
            self._alpha_saved = self.spin_opendrive_alpha.value()
            self._alpha_only_mode = True
            self.spin_opendrive_alpha.blockSignals(True)
            self.spin_opendrive_alpha.setValue(1.0)
            self.spin_opendrive_alpha.blockSignals(False)
        elif (not opendrive_on or has_base_layer) and was_only_mode:
            # Leaving solo-OpenDRIVE mode: restore saved alpha.
            self._alpha_only_mode = False
            self.spin_opendrive_alpha.blockSignals(True)
            self.spin_opendrive_alpha.setValue(self._alpha_saved)
            self.spin_opendrive_alpha.blockSignals(False)

        self.spin_opendrive_alpha.setEnabled(opendrive_on and has_base_layer)

        if self.opendrive_item:
            opendrive_checked = self.check_opendrive.isChecked()
            self._apply_opendrive_layer_style()
            needs_refresh = (
                opendrive_checked
                and self.opendrive_item.pixmap().isNull()
                and not self._xodr_is_vector
            )
            if needs_refresh:
                if not defer_fetch:
                    self.refresh_opendrive()
            elif opendrive_checked and not self.opendrive_loading:
                if self.spin_opendrive_alpha.value() <= 0.0:
                    self.lbl_opendrive_status.setText('Loaded (Hidden)')
                else:
                    self.lbl_opendrive_status.setText('Loaded')
            elif not opendrive_checked:
                self.lbl_opendrive_status.setText('')

        # Grid Logic — grid is always enabled; just track the user's preference.
        if grid_state_changed:
            self._grid_saved_state = self.check_grid.isChecked()

        # Constraints
        has_xodr = self.xodr_path is not None
        if hasattr(self, 'btn_world_update_xodr'):
            self.btn_world_update_xodr.setVisible(has_xodr)
        if hasattr(self, 'btn_fit_xodr'):
            self.btn_fit_xodr.setVisible(has_xodr)
            self.btn_fit_xodr.setEnabled(has_xodr)
        if not has_xodr:
            if self.check_opendrive.isChecked():
                self.check_opendrive.setChecked(False)
            self.check_opendrive.setEnabled(False)
        else:
            self.check_opendrive.setEnabled(True)
        if hasattr(self, 'check_opendrive_objects'):
            self.check_opendrive_objects.setEnabled(has_xodr and self.check_opendrive.isChecked())

        # OSM layer constraints and visibility
        has_osm = self.osm_path is not None
        self.grp_osm_opts.setVisible(has_osm)

        if hasattr(self, 'btn_fit_carla'):
            self.btn_fit_carla.setEnabled(self.check_carla_bev.isChecked())
        self._update_xodr_export_btn()
        if hasattr(self, '_update_file_menu_actions_visibility'):
            self._update_file_menu_actions_visibility(has_xodr=has_xodr, has_osm=has_osm)
        if not has_osm and self.check_osm.isChecked():
            self.check_osm.setChecked(False)
        self.check_osm.setEnabled(has_osm)
        if hasattr(self, 'check_osm_objects'):
            self.check_osm_objects.setEnabled(has_osm and self.check_osm.isChecked())
        if self._osm_vector_group is not None:
            self._apply_osm_layer_style()
            if not self.check_osm.isChecked():
                self.lbl_osm_status.setText('')
            elif self.spin_osm_alpha.value() <= 0.0:
                self.lbl_osm_status.setText('Loaded (Hidden)')
            else:
                self.lbl_osm_status.setText('Loaded')
        elif self.check_osm.isChecked() and not self._osm_loading:
            if not defer_fetch:
                self.refresh_osm()
        elif not self.check_osm.isChecked():
            self.lbl_osm_status.setText('')

        if self.grid_item:
            self.grid_item.setVisible(self.check_grid.isChecked())
            self.grid_item.update()
            # Force full view update to ensure repaint
            self.view.viewport().update()

    def update_imagery_alignment(self):
        if self.esri_item:
            px = self.spin_esri_x.value() / self.map_ctx.mpp
            py = self.spin_esri_y.value() / self.map_ctx.mpp
            self.esri_item.setPos(px, py)

    def _on_esri_offset_changed(self, _value: float) -> None:
        self.update_imagery_alignment()
        if getattr(self, '_suppress_async_layer_pipeline', False):
            return
        if getattr(self, '_restoring_project_payload', False):
            return

    def on_esri_offset_reset(self):
        self.spin_esri_x.setValue(0.0)
        self.spin_esri_y.setValue(0.0)
        # update_imagery_alignment is called automatically via valueChanged,
        # but call explicitly in case both were already 0 (no signal emitted).
        self.update_imagery_alignment()

    def _on_extent_select_toggle(self, checked: bool) -> None:
        """Enable/disable drag-to-select-world-extent mode."""
        if checked:
            if (
                getattr(self, 'btn_osm_select_segments', None)
                and self.btn_osm_select_segments.isChecked()
            ):
                self.btn_osm_select_segments.setChecked(False)
            # Disable ESRI edit-mode dragging while selecting world extent.
            if getattr(self, 'btn_esri_edit_mode', None) and self.btn_esri_edit_mode.isChecked():
                self.btn_esri_edit_mode.setChecked(False)

            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.view.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self.view.installEventFilter(self)  # For Esc key
        else:
            self._extent_select_start = None
            if self._extent_select_rect_item:
                self.scene.removeItem(self._extent_select_rect_item)
                self._extent_select_rect_item = None
            self.view.removeEventFilter(self)
            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.view.viewport().unsetCursor()

    def _on_osm_rect_select_toggle(self, checked: bool) -> None:
        """Enable/disable drag-to-select OSM segment mode."""
        if checked:
            if getattr(self, 'btn_select_extent', None) and self.btn_select_extent.isChecked():
                self.btn_select_extent.setChecked(False)
            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.view.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self.view.installEventFilter(self)
            self._osm_rect_select_start = None
            if self._osm_rect_select_rect_item is not None:
                self.scene.removeItem(self._osm_rect_select_rect_item)
                self._osm_rect_select_rect_item = None
            self._show_project_status('Draw a rectangle to select OSM segments')
        else:
            self._osm_rect_select_start = None
            if self._osm_rect_select_rect_item is not None:
                self.scene.removeItem(self._osm_rect_select_rect_item)
                self._osm_rect_select_rect_item = None
            self.view.removeEventFilter(self)
            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.view.viewport().unsetCursor()

    def _update_extent_highlight(self, edge: 'str | None', is_drag: bool = False) -> None:
        """Highlight *edge* ('N'/'S'/'E'/'W') with hover or drag styling.

        Restores the previously highlighted edge to its normal pen first.
        Passing ``None`` just clears any existing highlight.
        """
        # Restore previous edge to normal
        if self._extent_hover_edge and self._extent_hover_edge != edge:
            old = self._world_extent_edge_items.get(self._extent_hover_edge)
            if old is not None:
                pen = QPen(WORLD_EXTENT_RECT_COLOR)
                pen.setWidth(WORLD_EXTENT_RECT_PEN_WIDTH)
                pen.setCosmetic(True)
                old.setPen(pen)
        self._extent_hover_edge = edge
        if edge is None:
            return
        item = self._world_extent_edge_items.get(edge)
        if item is None:
            return
        color = WORLD_EXTENT_EDGE_DRAG_COLOR if is_drag else WORLD_EXTENT_EDGE_HOVER_COLOR
        pen = QPen(color)
        pen.setWidth(WORLD_EXTENT_EDGE_HOVER_WIDTH)
        pen.setCosmetic(True)
        item.setPen(pen)

    def _clear_extent_hover(self) -> None:
        """Restore the highlighted edge (if any) to its normal appearance."""
        self._update_extent_highlight(None)

    def _extent_edge_at(self, scene_pos: QPointF) -> str | None:
        """Return 'N', 'S', 'E', or 'W' if *scene_pos* is within hit tolerance of
        the corresponding world-extent bounding-box edge, else ``None``.

        Tolerance is expressed in viewport pixels so it stays constant regardless
        of zoom level.
        """
        if not any(self._world_extent_edge_items.values()) or not self.map_ctx:
            return None
        w_px = self.map_ctx.width_in_pixels
        h_px = self.map_ctx.height_in_pixels
        if w_px <= 0 or h_px <= 0:
            return None
        scale = self.view.transform().m11() or 1.0
        tol = EXTENT_EDGE_HIT_PX / scale  # tolerance in scene units
        x, y = scene_pos.x(), scene_pos.y()
        # Reject if completely outside the padded bounding box
        if not (-tol <= x <= w_px + tol and -tol <= y <= h_px + tol):
            return None
        on_north = abs(y) < tol
        on_south = abs(y - h_px) < tol
        on_west = abs(x) < tol
        on_east = abs(x - w_px) < tol
        # Prefer N/S over E/W at corners
        if on_north:
            return 'N'
        if on_south:
            return 'S'
        if on_west:
            return 'W'
        if on_east:
            return 'E'
        return None

    def _on_extent_edge_drag(self, vp_pos) -> None:
        """Update the spinbox for the active world-extent edge as the mouse moves.

        Uses viewport (widget) coordinates so the drag delta is stable even when
        the scene coordinate system shifts (N/W edge drags change the world
        offset, which would cause feedback drift with scene coordinates).

        The spinbox value at drag-start is the reference point — this avoids
        cumulative drift when the canvas resizes during the drag.
        """
        edge = self._extent_drag_edge
        start = self._extent_drag_start_vp
        if not edge or start is None or not self.map_ctx:
            return
        m_per_px = self._extent_drag_meters_per_vp_px
        # Minimum gap (in metres) between opposite edges so they never cross.
        _MIN_GAP = 1.0
        if edge == 'N':
            # North edge is at scene Y = 0.  Dragging upward (Δy < 0) extends north.
            delta = vp_pos.y() - start.y()
            new_val = self._extent_drag_start_spinval - delta * m_per_px
            # N must not cross S: north_world + south_world > gap  →
            # spin_north + spin_south > gap  (both are signed, positive = away from origin)
            max_north = -(self.spin_bound_south.value() - _MIN_GAP)  # allow negative
            # Actually: world width = east - west = spin_east - (-spin_west) for E/W;
            # for N/S: h = south - (-north) = south + north > gap
            # → north > gap - south  → north > -(south - gap)
            # No lower bound — the line can cross the axis freely.
            min_north = -(self.spin_bound_south.value() - _MIN_GAP)
            new_val = max(min_north, new_val)
            self.spin_bound_north.setValue(new_val)
        elif edge == 'S':
            # South edge is at scene Y = h_px.  Dragging downward (Δy > 0) extends south.
            delta = vp_pos.y() - start.y()
            new_val = self._extent_drag_start_spinval + delta * m_per_px
            # S must not cross N: south + north > gap → south > gap - north
            min_south = -(self.spin_bound_north.value() - _MIN_GAP)
            new_val = max(min_south, new_val)
            self.spin_bound_south.setValue(new_val)
        elif edge == 'E':
            # East edge is at scene X = w_px.  Dragging right (Δx > 0) extends east.
            delta = vp_pos.x() - start.x()
            new_val = self._extent_drag_start_spinval + delta * m_per_px
            # E must not cross W: east + west > gap → east > gap - west
            min_east = -(self.spin_bound_west.value() - _MIN_GAP)
            new_val = max(min_east, new_val)
            self.spin_bound_east.setValue(new_val)
        elif edge == 'W':
            # West edge is at scene X = 0.  Dragging left (Δx < 0) extends west.
            delta = vp_pos.x() - start.x()
            new_val = self._extent_drag_start_spinval - delta * m_per_px
            # W must not cross E: west + east > gap → west > gap - east
            min_west = -(self.spin_bound_east.value() - _MIN_GAP)
            new_val = max(min_west, new_val)
            self.spin_bound_west.setValue(new_val)

    def eventFilter(self, obj, event):
        t = event.type()

        # ── Hover-blink for relation IDs in read-only mode ─────────
        if obj in self._osm_relation_hover_map:
            sid = self._osm_relation_hover_map.get(obj)
            if t == QEvent.Type.Enter and sid:
                self._start_osm_blink_by_way_id(sid)
                return False
            if t == QEvent.Type.Leave:
                self._stop_osm_blink()
                return False

        # ── World Extent Selection (High Priority) ────────────────
        if getattr(self, 'btn_select_extent', None) and self.btn_select_extent.isChecked():
            if obj is self.view.viewport():
                if (
                    t == QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.LeftButton
                ):
                    self._extent_select_start = self.view.mapToScene(event.pos())
                    # Create visual rect
                    if not self._extent_select_rect_item:
                        self._extent_select_rect_item = QGraphicsRectItem()
                        pen = QPen(EXTENT_SELECTION_PEN_COLOR)
                        pen.setStyle(Qt.PenStyle.DashLine)
                        pen.setWidth(2)
                        self._extent_select_rect_item.setPen(pen)
                        self._extent_select_rect_item.setBrush(QBrush(EXTENT_SELECTION_FILL_COLOR))
                        self._extent_select_rect_item.setZValue(1000)
                        self.scene.addItem(self._extent_select_rect_item)
                    return True

                elif t == QEvent.Type.MouseMove and self._extent_select_start is not None:
                    curr = self.view.mapToScene(event.pos())
                    rect = QRectF(self._extent_select_start, curr).normalized()
                    if self._extent_select_rect_item:
                        self._extent_select_rect_item.setRect(rect)
                    return True

                elif (
                    t == QEvent.Type.MouseButtonRelease
                    and event.button() == Qt.MouseButton.LeftButton
                ):
                    if self._extent_select_start is not None and self._extent_select_rect_item:
                        rect = self._extent_select_rect_item.rect()
                        # Only apply if rect has some size
                        if rect.width() > 1 and rect.height() > 1:
                            if not self.map_ctx:
                                return True

                            mpp = self.map_ctx.mpp
                            world_off = self.map_ctx.world_offset

                            min_scene_x = rect.left()
                            max_scene_x = rect.right()
                            min_scene_y = rect.top()
                            max_scene_y = rect.bottom()

                            min_world_x = world_off[0] + min_scene_x * mpp
                            max_world_x = world_off[0] + max_scene_x * mpp
                            min_world_y = world_off[1] + min_scene_y * mpp
                            max_world_y = world_off[1] + max_scene_y * mpp

                            # 1. Update spinboxes (triggers logic but we want ordered execution)
                            # We can set values directly. _on_world_extent_changed will be called for each.
                            # But _on_world_extent_changed handles the resize and refresh.
                            # To do "move green box first then refresh tiles", we rely on the
                            # existing optimization in _on_world_extent_changed: it ALREADY shifts
                            # tiles and only fetches new ones if MPP is unchanged.
                            # And it draws the green box immediately.

                            # Just block signals to do batch update?
                            # No, we want the logic to run. But running it 4 times is wasteful.
                            # We should block signals, update values, then call _on_world_extent_changed ONCE.

                            self.spin_bound_west.blockSignals(True)
                            self.spin_bound_east.blockSignals(True)
                            self.spin_bound_north.blockSignals(True)
                            self.spin_bound_south.blockSignals(True)

                            self.spin_bound_west.setValue(-min_world_x)
                            self.spin_bound_east.setValue(max_world_x)
                            self.spin_bound_north.setValue(-min_world_y)
                            self.spin_bound_south.setValue(max_world_y)

                            self.spin_bound_west.blockSignals(False)
                            self.spin_bound_east.blockSignals(False)
                            self.spin_bound_north.blockSignals(False)
                            self.spin_bound_south.blockSignals(False)

                            # Trigger single update
                            self._on_world_extent_changed()

                        # Reset
                        self._extent_select_start = None
                        self.scene.removeItem(self._extent_select_rect_item)
                        self._extent_select_rect_item = None
                        self.btn_select_extent.setChecked(False)
                    return True
            elif (
                obj is self.view and t == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape
            ):
                self.btn_select_extent.setChecked(False)
                return True

        # ── OSM rectangle selection mode ────────────────────────────
        if (
            getattr(self, 'btn_osm_select_segments', None)
            and self.btn_osm_select_segments.isChecked()
        ):
            if obj is self.view.viewport():
                if (
                    t == QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.LeftButton
                    and self._osm_edit_enabled()
                ):
                    self._osm_rect_select_start = self.view.mapToScene(event.pos())
                    if self._osm_rect_select_rect_item is None:
                        self._osm_rect_select_rect_item = QGraphicsRectItem()
                        pen = QPen(EXTENT_SELECTION_PEN_COLOR)
                        pen.setStyle(Qt.PenStyle.DashLine)
                        pen.setWidth(2)
                        self._osm_rect_select_rect_item.setPen(pen)
                        self._osm_rect_select_rect_item.setBrush(
                            QBrush(EXTENT_SELECTION_FILL_COLOR)
                        )
                        self._osm_rect_select_rect_item.setZValue(1000)
                        self.scene.addItem(self._osm_rect_select_rect_item)
                    return True
                elif (
                    t == QEvent.Type.MouseMove
                    and self._osm_rect_select_start is not None
                    and self._osm_edit_enabled()
                ):
                    curr = self.view.mapToScene(event.pos())
                    rect = QRectF(self._osm_rect_select_start, curr).normalized()
                    if self._osm_rect_select_rect_item is not None:
                        self._osm_rect_select_rect_item.setRect(rect)
                    return True
                elif (
                    t == QEvent.Type.MouseButtonRelease
                    and event.button() == Qt.MouseButton.LeftButton
                    and self._osm_edit_enabled()
                ):
                    if (
                        self._osm_rect_select_start is not None
                        and self._osm_rect_select_rect_item is not None
                    ):
                        rect = self._osm_rect_select_rect_item.rect()
                        append = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                        subtract = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                        count = self._select_osm_segments_by_rect(
                            rect, append=append and not subtract, subtract=subtract
                        )
                        self._show_project_status(f'Selected {count} OSM segment(s)')
                    self._osm_rect_select_start = None
                    if self._osm_rect_select_rect_item is not None:
                        self.scene.removeItem(self._osm_rect_select_rect_item)
                        self._osm_rect_select_rect_item = None
                    return True
            elif (
                obj is self.view and t == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape
            ):
                self._select_osm_item(None)
                self._show_project_status('OSM selection cleared')
                return True

        # ── Segment-properties resize handle ──────────────────────
        if obj is self._osm_props_resize_handle:
            if t == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._osm_props_dragging = True
                self._osm_props_drag_start_y = int(event.globalPosition().y())
                self._osm_props_drag_start_h = self._osm_props_scroll.height()
                return True
            elif t == QEvent.Type.MouseMove and self._osm_props_dragging:
                dy = int(event.globalPosition().y()) - self._osm_props_drag_start_y
                new_h = max(
                    self._osm_props_min_h,
                    min(self._osm_props_max_h, self._osm_props_drag_start_h + dy),
                )
                self._set_osm_props_height(new_h)
                return True
            elif (
                t == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton
            ):
                self._osm_props_dragging = False
                return True

        # ── Node-properties resize handle ─────────────────────────
        if obj is getattr(self, '_osm_node_props_resize_handle', None):
            if t == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._osm_node_props_dragging = True
                self._osm_node_props_drag_start_y = int(event.globalPosition().y())
                self._osm_node_props_drag_start_h = self._osm_node_props_scroll.height()
                return True
            elif t == QEvent.Type.MouseMove and self._osm_node_props_dragging:
                dy = int(event.globalPosition().y()) - self._osm_node_props_drag_start_y
                new_h = max(
                    self._osm_props_min_h,
                    min(self._osm_props_max_h, self._osm_node_props_drag_start_h + dy),
                )
                self._set_osm_node_props_height(new_h)
                return True
            elif (
                t == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton
            ):
                self._osm_node_props_dragging = False
                return True

        # ── OSM right-click: stitch / split / add / delete node ───
        if (
            obj is self.view.viewport()
            and t == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.RightButton
            and self._osm_edit_enabled()
        ):
            scene_pos = self.view.mapToScene(event.pos())
            mods = event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.ShiftModifier
                | Qt.KeyboardModifier.AltModifier
            )
            stitch_mod = self._modifier_text_to_flags(
                self._mouse_bindings.get('stitch_way', MOUSE_BINDING_DEFAULTS['stitch_way'])
            )
            split_mod = self._modifier_text_to_flags(
                self._mouse_bindings.get('split_way', MOUSE_BINDING_DEFAULTS['split_way'])
            )
            delete_mod = self._modifier_text_to_flags(
                self._mouse_bindings.get('delete_node', MOUSE_BINDING_DEFAULTS['delete_node'])
            )
            add_mod = self._modifier_text_to_flags(
                self._mouse_bindings.get('add_node', MOUSE_BINDING_DEFAULTS['add_node'])
            )

            if mods == stitch_mod:
                if self._on_osm_stitch_way(scene_pos):
                    return True
            elif mods == split_mod:
                if self._on_osm_split_way(scene_pos):
                    return True
            elif mods == delete_mod:
                if self._on_osm_delete_node(scene_pos):
                    return True
            elif mods == add_mod:
                # Linux desktops often reserve Alt+mouse for window actions.
                # To keep stitching usable, also attempt stitch when right-clicking
                # a selected-way node dot with no modifier.
                if self._osm_dot_at(scene_pos) is not None and self._on_osm_stitch_way(scene_pos):
                    return True
                if self._on_osm_add_node(scene_pos):
                    return True

        # ── OSM node-dot dragging (highest priority) ─────────────────
        if obj is self.view and t == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape and self._osm_relation_pick_mode is not None:
                self._osm_relation_pick_mode = None
                self._show_project_status('Pick mode cancelled')
                return True
            if self._osm_edit_enabled() and (
                event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                key = event.key()
                if key in (
                    Qt.Key.Key_Left,
                    Qt.Key.Key_Right,
                    Qt.Key.Key_Up,
                    Qt.Key.Key_Down,
                ):
                    base_step_m = float(getattr(self, '_esri_nudge_step', 0.1))
                    shift_step_m = float(getattr(self, '_esri_shift_nudge_step', 1.0))
                    step_m = (
                        shift_step_m
                        if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                        else base_step_m
                    )
                    mpp = max(float(self.map_ctx.mpp if self.map_ctx else 1.0), 1e-9)
                    step_scene = float(step_m / mpp)
                    dx = 0.0
                    dy = 0.0
                    if key == Qt.Key.Key_Left:
                        dx = -step_scene
                    elif key == Qt.Key.Key_Right:
                        dx = step_scene
                    elif key == Qt.Key.Key_Up:
                        dy = -step_scene
                    elif key == Qt.Key.Key_Down:
                        dy = step_scene
                    if self._on_osm_way_nudge(dx, dy):
                        self._show_project_status(f'Roundabout moved ({step_m:.2f} m)')
                        return True

        if obj is self.view.viewport():
            if (
                t == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
                and self._osm_edit_enabled()
                and self._osm_relation_pick_mode is not None
            ):
                scene_pos = self.view.mapToScene(event.pos())
                target = self._osm_way_item_at(scene_pos, exclude_item=self._osm_selected_item)
                if target is None:
                    self._show_project_status('Pick mode: click a valid target segment')
                    self._osm_suppress_next_click_select = True
                    self._osm_click_press_pos = None
                    return True
                target_meta = self._osm_item_meta.get(target)
                selected = self._osm_selected_item
                if target_meta is None or selected is None:
                    self._osm_suppress_next_click_select = True
                    self._osm_click_press_pos = None
                    return True
                target_id = str(target_meta[5])
                relation = self._osm_relation_pick_mode
                self._osm_relation_pick_mode = None
                draft = list(self._osm_relation_draft.get(relation) or [])
                if target_id not in [str(v) for v in draft]:
                    draft.append(target_id)
                    self._osm_relation_draft[relation] = draft
                    self._osm_show_props(selected)
                    self._show_project_status(f'Added {relation} relation draft: {target_id}')
                else:
                    self._show_project_status(f'{relation.capitalize()} relation already present')
                self._osm_suppress_next_click_select = True
                self._osm_click_press_pos = None
                return True
            if (
                t == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
                and self._osm_edit_enabled()
            ):
                self._osm_click_press_pos = event.pos()
                scene_pos = self.view.mapToScene(event.pos())
                ctrl_pressed = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                if self._on_osm_node_press(scene_pos, ctrl_pressed=ctrl_pressed):
                    self._osm_suppress_next_click_select = True
                    self._osm_click_press_pos = None  # consumed by dot drag
                    return True
                if self._on_osm_way_press(scene_pos, ctrl_pressed=ctrl_pressed):
                    self._osm_click_press_pos = None  # consumed by roundabout drag
                    return True
            elif (
                t == QEvent.Type.MouseMove
                and self._osm_edit_enabled()
                and (self._osm_dragging_dot is not None or self._osm_dragging_way_item is not None)
            ):
                scene_pos = self.view.mapToScene(event.pos())
                if self._osm_dragging_dot is not None:
                    self._on_osm_node_move(scene_pos)
                elif self._osm_dragging_way_item is not None:
                    self._on_osm_way_move(scene_pos)
                return True
            elif (
                t == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton
                and self._osm_edit_enabled()
            ):
                if self._osm_dragging_dot is not None:
                    scene_pos = self.view.mapToScene(event.pos())
                    self._on_osm_node_release(scene_pos)
                    return True
                if self._osm_dragging_way_item is not None:
                    scene_pos = self.view.mapToScene(event.pos())
                    self._on_osm_way_release(scene_pos)
                    return True
        # ── World-extent edge hover + drag ───────────────────────────
        # Active when the world-extent edges are present, ESRI-drag mode is off,
        # and no OSM dot is being dragged.
        # NOTE: QGraphicsView.DragMode.ScrollHandDrag manages the viewport cursor itself
        # (open-hand), overriding any viewport().setCursor() call.  We work
        # around this by temporarily switching to NoDrag when the pointer is
        # near an edge, so our resize cursor wins.
        _extent_active = (
            obj is self.view.viewport()
            and any(self._world_extent_edge_items.values())
            and getattr(self, 'btn_world_edit_mode', None)
            and self.btn_world_edit_mode.isChecked()
            and not (
                getattr(self, 'btn_esri_edit_mode', None) and self.btn_esri_edit_mode.isChecked()
            )
            and self._osm_dragging_dot is None
            and self._osm_dragging_way_item is None
        )
        if _extent_active:
            if t == QEvent.Type.MouseMove:
                scene_pos = self.view.mapToScene(event.pos())
                if self._extent_drag_edge:
                    # Mid-drag: update the bound spinbox and consume the event so
                    # no panning happens.  Use viewport coords (stable across
                    # scene-offset changes triggered by N/W edge resizes).
                    self._on_extent_edge_drag(event.pos())
                    return True
                else:
                    # Hovering: detect edge, update highlight and cursor.
                    edge = self._extent_edge_at(scene_pos)
                    self._update_extent_highlight(edge)
                    vp = self.view.viewport()
                    if edge in ('N', 'S'):
                        # Disable ScrollHandDrag so it can't reset the cursor, and
                        # consume the event so Qt's scene-hover dispatch (which would
                        # let the giant GridItem reset the cursor) never runs.
                        if self.view.dragMode() != QGraphicsView.DragMode.NoDrag:
                            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
                        vp.setCursor(Qt.CursorShape.SizeVerCursor)
                        # Clear any stale OSM hover before we consume the event.
                        self._clear_osm_hover()
                        return True
                    elif edge in ('E', 'W'):
                        if self.view.dragMode() != QGraphicsView.DragMode.NoDrag:
                            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
                        vp.setCursor(Qt.CursorShape.SizeHorCursor)
                        self._clear_osm_hover()
                        return True
                    else:
                        # Not near any edge — restore base NoDrag mode and let the
                        # normal hover-highlight block below handle XODR/OSM hover.
                        if self.view.dragMode() != QGraphicsView.DragMode.NoDrag:
                            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
                        vp.unsetCursor()
            elif t == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                scene_pos = self.view.mapToScene(event.pos())
                edge = self._extent_edge_at(scene_pos)
                if edge:
                    self._extent_drag_edge = edge
                    self._extent_drag_start_vp = event.pos()  # viewport coords
                    scale = self.view.transform().m11() or 1.0
                    mpp = self.map_ctx.mpp or 1.0
                    self._extent_drag_meters_per_vp_px = mpp / scale
                    if edge == 'N':
                        self._extent_drag_start_spinval = self.spin_bound_north.value()
                    elif edge == 'S':
                        self._extent_drag_start_spinval = self.spin_bound_south.value()
                    elif edge == 'E':
                        self._extent_drag_start_spinval = self.spin_bound_east.value()
                    else:  # 'W'
                        self._extent_drag_start_spinval = self.spin_bound_west.value()
                    # Switch edge to drag (amber) color.
                    self._update_extent_highlight(edge, is_drag=True)
                    # NoDrag is already set from the hover step; keep it so the
                    # view doesn't pan while we stretch the world extent.
                    return True
            elif (
                t == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton
            ):
                if self._extent_drag_edge:
                    self._extent_drag_edge = None
                    self._extent_drag_start_vp = None
                    # Flush deferred tile/overlay work now that the drag ended.
                    self._finish_extent_drag()
                    # Determine whether the pointer is still near an edge.
                    scene_pos = self.view.mapToScene(event.pos())
                    edge = self._extent_edge_at(scene_pos)
                    self._update_extent_highlight(edge)
                    if not edge:
                        # Leaving edge territory — restore base NoDrag mode.
                        self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
                        self.view.viewport().unsetCursor()
                    return True
            elif t == QEvent.Type.Leave:
                # Pointer left the viewport — cancel drag, clear highlights,
                # and restore base NoDrag mode.
                if self._extent_drag_edge:
                    self._extent_drag_edge = None
                    self._extent_drag_start_vp = None
                    self._finish_extent_drag()
                self._clear_extent_hover()
                if self.view.dragMode() != QGraphicsView.DragMode.NoDrag:
                    self.view.setDragMode(QGraphicsView.DragMode.NoDrag)

        # ── lane hover highlighting ──────────────────────────────────
        if obj is self.view.viewport() and t == QEvent.Type.MouseMove:
            scene_pos = self.view.mapToScene(event.pos())
            self._last_mouse_scene_pos = scene_pos
            self._update_osm_hover(scene_pos)
        elif obj is self.view.viewport() and t == QEvent.Type.Leave:
            self._clear_osm_hover()
        elif (
            obj is self.view.viewport()
            and t == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            if not (
                getattr(self, 'btn_esri_edit_mode', None) and self.btn_esri_edit_mode.isChecked()
            ):
                if self._osm_suppress_next_click_select:
                    self._osm_suppress_next_click_select = False
                    self._osm_click_press_pos = None
                    return True
                # Only treat as click if mouse didn't move far (not a viewport pan)
                press = self._osm_click_press_pos
                is_click = True
                if press is not None:
                    delta = event.pos() - press
                    if (delta.x() ** 2 + delta.y() ** 2) > CLICK_DRAG_THRESHOLD_SQ:
                        is_click = False
                self._osm_click_press_pos = None
                if is_click:
                    scene_pos = self.view.mapToScene(event.pos())
                    self._on_osm_click(scene_pos)

        if getattr(self, 'btn_esri_edit_mode', None) and self.btn_esri_edit_mode.isChecked():
            # ── mouse drag on the viewport ────────────────────────────────
            if obj is self.view.viewport():
                if (
                    t == QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.LeftButton
                ):
                    self._esri_drag_last = self.view.mapToScene(event.pos())
                    return True
                elif t == QEvent.Type.MouseMove and (event.buttons() & Qt.MouseButton.LeftButton):
                    if self._esri_drag_last is not None:
                        curr = self.view.mapToScene(event.pos())
                        dx = curr.x() - self._esri_drag_last.x()
                        dy = curr.y() - self._esri_drag_last.y()
                        mpp = self.map_ctx.mpp if self.map_ctx else 1.0
                        self.spin_esri_x.setValue(self.spin_esri_x.value() + dx * mpp)
                        self.spin_esri_y.setValue(self.spin_esri_y.value() + dy * mpp)
                        self._esri_drag_last = curr
                    return True
                elif (
                    t == QEvent.Type.MouseButtonRelease
                    and event.button() == Qt.MouseButton.LeftButton
                ):
                    self._esri_drag_last = None
                    return True
            # ── arrow keys on the view ────────────────────────────────────
            if obj is self.view and t == QEvent.Type.KeyPress:
                key = event.key()
                base_step = (
                    float(self.spin_esri_nudge_step.value())
                    if hasattr(self, 'spin_esri_nudge_step')
                    else float(self.spin_esri_x.singleStep())
                )
                shift_step = (
                    float(self.spin_esri_shift_nudge_step.value())
                    if hasattr(self, 'spin_esri_shift_nudge_step')
                    else 1.0
                )
                step = (
                    shift_step
                    if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                    else base_step
                )
                if key == Qt.Key.Key_Left:
                    self.spin_esri_x.setValue(self.spin_esri_x.value() - step)
                    return True
                elif key == Qt.Key.Key_Right:
                    self.spin_esri_x.setValue(self.spin_esri_x.value() + step)
                    return True
                elif key == Qt.Key.Key_Up:
                    self.spin_esri_y.setValue(self.spin_esri_y.value() - step)
                    return True
                elif key == Qt.Key.Key_Down:
                    self.spin_esri_y.setValue(self.spin_esri_y.value() + step)
                    return True
                elif key == Qt.Key.Key_Escape:
                    self.btn_esri_edit_mode.setChecked(False)
                    return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_world_edit_mode_button()
        self._position_osm_edit_mode_button()
        self._position_osm_props_edit_mode_button()
        self._position_osm_node_props_edit_mode_button()
        self._position_esri_edit_mode_button()
        self._position_carla_edit_mode_button()

    def update_grid_style(self):
        if self.grid_item:
            self.grid_item.line_thickness = self.spin_thickness.value()
            self.grid_item.label_size = self.spin_font.value()
            self.grid_item.label_sig_digits = self.spin_grid_sigdigits.value()
            self.grid_item.update()

    def pick_grid_color(self):
        if self.grid_item:
            color = QColorDialog.getColor(self.grid_item.grid_color, self, 'Select Grid Color')
            if color.isValid():
                self.grid_item.grid_color = color
                self.grid_item.update()

    def pick_viewport_bg_color(self):
        color = QColorDialog.getColor(
            self.view.backgroundBrush().color(), self, 'Select Background Color'
        )
        if color.isValid():
            self.view.setBackgroundBrush(color)

    def zoom_in(self):
        if self.view.transform().m11() < MAX_ZOOM_SCALE:
            self.view.scale(ZOOM_IN_FACTOR, ZOOM_IN_FACTOR)
            if self.grid_item:
                self.grid_item.update()
            self._update_zoom_spinbox()

    def zoom_out(self):
        current = self.view.transform().m11()
        if current > self._fit_scale:
            factor = max(self._fit_scale / current, 0.8)
            self.view.scale(factor, factor)
            if self.grid_item:
                self.grid_item.update()
            self._update_zoom_spinbox()

    def _apply_load_view(self):
        """Set the initial viewport position after the window is shown.

        - If a project is being restored, use its saved zoom and center.
        - Otherwise, if an XODR is loaded  → fit_to_window().
        - Otherwise                       → fit just the world-extent canvas.
        """
        # 1. Absolute world-coordinate restoration (legacy)
        if self._pending_project_view_scale is not None:
            view_scale = float(self._pending_project_view_scale)
            self._pending_project_view_scale = None
            world_center = self._pending_project_world_center
            self._pending_project_world_center = None
            self._suppress_auto_fit = True

            def _restore_absolute_view(_scale=view_scale, _world=world_center):
                self.view.resetTransform()
                self.view.scale(_scale, _scale)
                if _world is not None and self.map_ctx:
                    sx = (float(_world[0]) - self.map_ctx.world_offset[0]) / self.map_ctx.mpp
                    sy = (float(_world[1]) - self.map_ctx.world_offset[1]) / self.map_ctx.mpp
                    self.view.centerOn(QPointF(sx, sy))
                if self.grid_item:
                    self.grid_item.update()
                self._update_zoom_spinbox(_scale)

            QTimer.singleShot(ZOOM_RESTORE_DELAY_MS, _restore_absolute_view)
            return

        # 2. Scene-coordinate based restoration (modern)
        if self._pending_project_zoom_pct is not None:
            zp = self._pending_project_zoom_pct
            c = self._pending_project_viewport_center
            self._pending_project_zoom_pct = None
            self._pending_project_viewport_center = None
            self._suppress_auto_fit = True

            self.spin_zoom.setValue(zp)
            if c is not None:
                # Use a short delay for centering to let the view re-layout after zoom.
                QTimer.singleShot(
                    CENTER_ON_DELAY_MS,
                    lambda: self.view.centerOn(QPointF(c[0], c[1])),
                )
            return

        if self._suppress_auto_fit:
            # We've already restored a project viewport or manually loaded a map;
            # do not overwrite it with an automatic fit.
            return

        if bool(self.xodr_path):
            self.fit_to_window()
        else:
            # No XODR: fit to the world-extent canvas only.
            # This centres the view on world (0, 0) which is the mid-point of the
            # default (symmetric) world bounds regardless of CARLA server state.
            if self.map_ctx:
                w_px = self.map_ctx.width_in_pixels
                h_px = self.map_ctx.height_in_pixels
                if w_px > 0 and h_px > 0:
                    m = FIT_MARGIN_FACTOR
                    fit = QRectF(-w_px * m, -h_px * m, w_px * (1 + 2 * m), h_px * (1 + 2 * m))
                    self.view.fitInView(fit, Qt.AspectRatioMode.KeepAspectRatio)
                    self._fit_scale = self.view.transform().m11()
                    self.view._min_scale = DEFAULT_MIN_SCALE
                    self.spin_zoom.setMinimum(1)
            self._update_zoom_spinbox()

    def _fit_view_to_rect(self, rect: QRectF | None) -> bool:
        if rect is None or rect.isNull() or rect.width() <= 0 or rect.height() <= 0:
            return False
        margin_x = rect.width() * FIT_MARGIN_FACTOR
        margin_y = rect.height() * FIT_MARGIN_FACTOR
        fit_rect = QRectF(rect)
        fit_rect.adjust(-margin_x, -margin_y, margin_x, margin_y)
        self.view.fitInView(fit_rect, Qt.AspectRatioMode.KeepAspectRatio)
        self._fit_scale = self.view.transform().m11()
        self.view._min_scale = DEFAULT_MIN_SCALE
        self.spin_zoom.setMinimum(1)
        if self.grid_item:
            self.grid_item.update()
        self._update_zoom_spinbox()
        return True

    def fit_view_to_xodr(self):
        rect = None
        if self._xodr_bounds_rect_item is not None:
            rect = self._xodr_bounds_rect_item.rect()
        if rect is None and self.xodr_path and os.path.isfile(self.xodr_path) and self.map_ctx:
            bounds = MapContext.parse_xodr_bounds(self.xodr_path)
            if bounds and len(bounds) == 4:
                m_wb = self.map_ctx.world_bounds
                mpp = self.map_ctx.mpp
                rect = QRectF(
                    (bounds[0] - m_wb[0]) / mpp,
                    (bounds[2] - m_wb[2]) / mpp,
                    (bounds[1] - bounds[0]) / mpp,
                    (bounds[3] - bounds[2]) / mpp,
                )
        if not self._fit_view_to_rect(rect):
            self._show_project_status('Fit to OpenDRIVE unavailable')

    def fit_view_to_osm(self):
        rect = None
        if self._osm_bounds_rect_item is not None:
            rect = self._osm_bounds_rect_item.rect()
        elif self._osm_vector_group is not None:
            local_rect = self._osm_vector_group.childrenBoundingRect()
            if not local_rect.isNull() and local_rect.width() > 0 and local_rect.height() > 0:
                rect = self._osm_vector_group.mapRectToScene(local_rect)
        if not self._fit_view_to_rect(rect):
            self._show_project_status('Fit to OSM unavailable')

    def fit_view_to_carla(self):
        rect = None
        if self._carla_bev_bounds_rect_item is not None:
            rect = self._carla_bev_bounds_rect_item.rect()
        if not self._fit_view_to_rect(rect):
            self._show_project_status('Fit to CARLA unavailable')

    def _update_zoom_spinbox(self, scale=None):
        """Sync spin_zoom to the current view scale without triggering a loop.
        Zoom % is relative to fit-to-window: fit = 100%, 2× fit = 200%, etc.
        """
        if scale is None:
            scale = self.view.transform().m11()
        if self._fit_scale > 0:
            pct = max(1, round(scale / self._fit_scale * 100))
        else:
            pct = 100
        self.spin_zoom.blockSignals(True)
        self.spin_zoom.setValue(pct)
        self.spin_zoom.blockSignals(False)

    def _on_zoom_spinbox_changed(self, value):
        """Apply the spinbox zoom value to the view (relative to fit scale)."""
        if self._fit_scale <= 0:
            return
        self.view.resetTransform()
        self.view.scale(self._fit_scale * value / 100.0, self._fit_scale * value / 100.0)
        if self.grid_item:
            self.grid_item.update()

    def _combined_bounds_rect(self):
        """Return the union of the world-extent rect and all visible overlay
        bounding boxes (XODR, CARLA BEV).  Used by fit_to_window and for
        expanding the scene rect so scroll/drag works beyond the base extent."""
        if self.map_ctx:
            rect = QRectF(0, 0, self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels)
        else:
            rect = self.scene.sceneRect()
        if self._xodr_bounds_rect_item is not None and self._xodr_bounds_rect_item.isVisible():
            rect = rect.united(self._xodr_bounds_rect_item.rect())
        if (
            self._carla_bev_bounds_rect_item is not None
            and self._carla_bev_bounds_rect_item.isVisible()
        ):
            rect = rect.united(self._carla_bev_bounds_rect_item.rect())
        return rect

    _syncing_scene_rect = False  # re-entrancy guard

    # Scene rect matches the GridItem.boundingRect() so drag is limited to
    # exactly the same area where the grid is drawn.
    _GRID_SCENE_RECT = QRectF(-1e7, -1e7, 2e7, 2e7)

    def _sync_scene_rect(self):
        """Set the scene rect to the grid's absolute extent.

        This is the same rect returned by GridItem.boundingRect(), so the
        user can drag / scroll to exactly where the grid stops and no further."""
        if self._syncing_scene_rect:
            return
        self._syncing_scene_rect = True
        try:
            self.scene.setSceneRect(self._GRID_SCENE_RECT)
        finally:
            self._syncing_scene_rect = False
