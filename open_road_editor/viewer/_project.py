"""Project, settings and XODR-state persistence mixin."""

import copy
import json
import math
import os

from PyQt6.QtCore import (
    QPointF,
    QRectF,
    QTimer,
)
from PyQt6.QtGui import (
    QColor,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QFileDialog,
    QMessageBox,
    QStyle,
)

from open_road_editor.constants import *  # noqa: F401,F403


class _ProjectMixin:
    """Mixin — see viewer/main.py for the assembled class."""

    def _window_title_name(self) -> str:
        project_path = str(getattr(self, 'project_file_path', '') or '').strip()
        if project_path:
            return os.path.splitext(os.path.basename(project_path))[0]
        town_name = str(getattr(self, 'town_name', '') or '').strip()
        if town_name and town_name != 'Untitled':
            return town_name
        return ''

    def _refresh_window_title(self) -> None:
        title_name = self._window_title_name()
        self.setWindowTitle(f'OpenRoadEditor - {title_name}' if title_name else 'OpenRoadEditor')

    def _collect_persistent_settings(self) -> dict:
        state = {
            'show_esri': self.check_esri.isChecked(),
            'show_carla_bev': self.check_carla_bev.isChecked(),
            'show_opendrive': self.check_opendrive.isChecked(),
            'show_opendrive_objects': self.check_opendrive_objects.isChecked()
            if hasattr(self, 'check_opendrive_objects')
            else True,
            'world_edit_mode': self.btn_world_edit_mode.isChecked()
            if hasattr(self, 'btn_world_edit_mode')
            else False,
            'esri_edit_mode': self.btn_esri_edit_mode.isChecked()
            if hasattr(self, 'btn_esri_edit_mode')
            else False,
            'carla_edit_mode': self.btn_carla_edit_mode.isChecked()
            if hasattr(self, 'btn_carla_edit_mode')
            else False,
            'server_ip': self.edit_server_ip.text().strip() or DEFAULT_SERVER_HOST,
            'server_port': self.edit_server_port.text().strip() or str(DEFAULT_SERVER_PORT),
            'opendrive_alpha': self.spin_opendrive_alpha.value(),
            'zoom_pct': self.spin_zoom.value(),
            'show_grid': self.check_grid.isChecked(),
            'tile_zoom': self.spin_tile_zoom.value(),
            'esri_offset_x': self.spin_esri_x.value(),
            'esri_offset_y': self.spin_esri_y.value(),
            'esri_nudge_step': self.spin_esri_nudge_step.value()
            if hasattr(self, 'spin_esri_nudge_step')
            else 0.1,
            'esri_shift_nudge_step': self.spin_esri_shift_nudge_step.value()
            if hasattr(self, 'spin_esri_shift_nudge_step')
            else 1.0,
            'grid_thickness': self.spin_thickness.value(),
            'grid_font_size': self.spin_font.value(),
            'grid_sig_digits': self.spin_grid_sigdigits.value(),
            'show_osm': self.check_osm.isChecked(),
            'show_osm_objects': self.check_osm_objects.isChecked()
            if hasattr(self, 'check_osm_objects')
            else True,
            'osm_edit_mode': self.btn_osm_edit_mode.isChecked()
            if hasattr(self, 'btn_osm_edit_mode')
            else False,
            'osm_alpha': self.spin_osm_alpha.value(),
            'osm2xodr_settings': copy.deepcopy(self._osm2xodr_settings),
            'osm_props_height': self._osm_props_scroll.height(),
            'osm_node_props_height': self._osm_node_props_scroll.height(),
            'osm_props_key_col_width': int(
                getattr(self, '_osm_tag_key_col_width', TAG_KEY_FIELD_WIDTH)
            ),
            'grid_color': self.grid_item.grid_color.name()
            if self.grid_item
            else DEFAULT_GRID_COLOR_HEX,
            'viewport_bg_color': self.view.backgroundBrush().color().name(),
            'origin_lat': self.spin_origin_lat.value(),
            'origin_lon': self.spin_origin_lon.value(),
            'bound_north': self.spin_bound_north.value(),
            'bound_south': self.spin_bound_south.value(),
            'bound_east': self.spin_bound_east.value(),
            'bound_west': self.spin_bound_west.value(),
        }
        sizes = self.splitter.sizes()
        if len(sizes) == 2:
            state['splitter_map_w'] = sizes[0]
            state['splitter_right_w'] = sizes[1]
        return state

    def _apply_persistent_settings(self, state: dict):
        if not isinstance(state, dict):
            return

        self._suppress_async_layer_pipeline = True
        try:
            if 'show_esri' in state:
                self.check_esri.setChecked(bool(state.get('show_esri')))
            if 'show_carla_bev' in state:
                self.check_carla_bev.setChecked(bool(state.get('show_carla_bev')))
            if 'show_opendrive' in state:
                self.check_opendrive.setChecked(bool(state.get('show_opendrive')))
            if 'show_opendrive_objects' in state and hasattr(self, 'check_opendrive_objects'):
                self.check_opendrive_objects.setChecked(bool(state.get('show_opendrive_objects')))
            if 'world_edit_mode' in state and hasattr(self, 'btn_world_edit_mode'):
                self.btn_world_edit_mode.setChecked(bool(state.get('world_edit_mode')))
            if 'esri_edit_mode' in state and hasattr(self, 'btn_esri_edit_mode'):
                self.btn_esri_edit_mode.setChecked(bool(state.get('esri_edit_mode')))
            if 'carla_edit_mode' in state and hasattr(self, 'btn_carla_edit_mode'):
                self.btn_carla_edit_mode.setChecked(bool(state.get('carla_edit_mode')))
            if 'show_osm' in state:
                self.check_osm.setChecked(bool(state.get('show_osm')))
            if 'show_osm_objects' in state and hasattr(self, 'check_osm_objects'):
                self.check_osm_objects.setChecked(bool(state.get('show_osm_objects')))
            if 'osm_edit_mode' in state and hasattr(self, 'btn_osm_edit_mode'):
                self.btn_osm_edit_mode.setChecked(bool(state.get('osm_edit_mode')))
            if 'osm_alpha' in state:
                self.spin_osm_alpha.setValue(float(state.get('osm_alpha', 0.6)))
            if 'osm2xodr_settings' in state:
                self._osm2xodr_settings = self._normalize_osm2xodr_settings(
                    state.get('osm2xodr_settings')
                )
            if 'osm_props_height' in state:
                _h = int(state.get('osm_props_height', self._osm_props_default_height))
                self._set_osm_props_height(_h)
            if 'osm_node_props_height' in state:
                _node_h = int(state.get('osm_node_props_height', self._osm_props_default_height))
                self._set_osm_node_props_height(_node_h)
            if 'osm_props_key_col_width' in state:
                self._osm_tag_key_col_width = max(60, int(state.get('osm_props_key_col_width')))

            cli_host = str(getattr(self, '_cli_server_ip', '') or '').strip()
            cli_port = getattr(self, '_cli_server_port', None)
            if cli_host:
                self.edit_server_ip.setText(cli_host)
            elif 'server_ip' in state:
                self.edit_server_ip.setText(str(state.get('server_ip') or DEFAULT_SERVER_HOST))
            if cli_port is not None:
                self.edit_server_port.setText(str(cli_port))
            elif 'server_port' in state:
                self.edit_server_port.setText(
                    str(state.get('server_port') or str(DEFAULT_SERVER_PORT))
                )

            if 'origin_lat' in state:
                self.spin_origin_lat.setValue(float(state['origin_lat']))
            if 'origin_lon' in state:
                self.spin_origin_lon.setValue(float(state['origin_lon']))
            if 'bound_north' in state:
                self.spin_bound_north.setValue(float(state['bound_north']))
            if 'bound_south' in state:
                self.spin_bound_south.setValue(float(state['bound_south']))
            if 'bound_east' in state:
                self.spin_bound_east.setValue(float(state['bound_east']))
            if 'bound_west' in state:
                self.spin_bound_west.setValue(float(state['bound_west']))

            if 'opendrive_alpha' in state:
                alpha = float(state.get('opendrive_alpha', DEFAULT_OPENDRIVE_ALPHA))
                self._alpha_saved = alpha
                self.spin_opendrive_alpha.setValue(alpha)

            if 'tile_zoom' in state:
                self.spin_tile_zoom.setValue(
                    int(state.get('tile_zoom', self.spin_tile_zoom.value()))
                )
            if 'esri_offset_x' in state:
                self.spin_esri_x.setValue(float(state.get('esri_offset_x', 0.0)))
            if 'esri_offset_y' in state:
                self.spin_esri_y.setValue(float(state.get('esri_offset_y', 0.0)))
            if 'esri_nudge_step' in state and hasattr(self, 'spin_esri_nudge_step'):
                self.spin_esri_nudge_step.setValue(float(state.get('esri_nudge_step', 0.1)))
            if 'esri_shift_nudge_step' in state and hasattr(self, 'spin_esri_shift_nudge_step'):
                self.spin_esri_shift_nudge_step.setValue(
                    float(state.get('esri_shift_nudge_step', 1.0))
                )

            if 'show_grid' in state:
                self._grid_saved_state = bool(state.get('show_grid'))
                self.check_grid.setChecked(self._grid_saved_state)
            if 'grid_thickness' in state:
                self.spin_thickness.setValue(
                    int(state.get('grid_thickness', self.spin_thickness.value()))
                )
            if 'grid_font_size' in state:
                self.spin_font.setValue(int(state.get('grid_font_size', self.spin_font.value())))
            if 'grid_sig_digits' in state:
                self.spin_grid_sigdigits.setValue(
                    int(state.get('grid_sig_digits', self.spin_grid_sigdigits.value()))
                )
            if 'grid_color' in state and self.grid_item:
                self.grid_item.grid_color = QColor(str(state.get('grid_color')))
            if 'viewport_bg_color' in state:
                self.view.setBackgroundBrush(QColor(str(state.get('viewport_bg_color'))))

            saved_map = state.get('splitter_map_w')
            saved_right = state.get('splitter_right_w')
            if saved_map is not None and saved_right is not None:
                QTimer.singleShot(
                    50,
                    lambda: self.splitter.setSizes([int(saved_map), int(saved_right)]),
                )
        finally:
            self._suppress_async_layer_pipeline = False

        self.update_grid_style()
        self.update_imagery_alignment()
        self.update_visibility(defer_fetch=True)

    def _collect_project_payload(self, storage_mode: str | None = None) -> dict:
        persistent = self._collect_persistent_settings()
        carla_meta = {
            'server_meta': self._carla_bev_server_meta
            if isinstance(self._carla_bev_server_meta, dict)
            else None,
            'server_bounds': list(self._carla_bev_server_bounds)
            if isinstance(self._carla_bev_server_bounds, tuple)
            else None,
        }
        # Capture the centre of the current viewport in scene coordinates so it
        # can be restored when the project is re-opened.
        vp_center = self.view.mapToScene(self.view.viewport().rect().center())

        # Flush pending panel edits so project save captures latest OSM state.
        self._flush_pending_osm_panel_edits()

        # Use content loaded in memory
        composed_osm = self._compose_current_osm_content()
        osm_content = composed_osm if composed_osm is not None else self._osm_content
        if composed_osm is not None:
            self._osm_content = composed_osm
        # If OSM exists, do not persist XODR content in project file.
        xodr_content = None if osm_content else self._xodr_content

        return {
            'format': 'ore',
            'version': PROJECT_FILE_VERSION,
            'project': {
                'town_name': self.town_name,
                'xodr_content': xodr_content,
                'osm_content': osm_content,
                'segment_info_height': int(self._osm_props_scroll.height()),
                'node_info_height': int(self._osm_node_props_scroll.height()),
                'segment_info_key_col_width': int(
                    getattr(self, '_osm_tag_key_col_width', TAG_KEY_FIELD_WIDTH)
                ),
                'display_zoom_pct': int(self.spin_zoom.value()),
                'viewport_center_x': float(vp_center.x()),
                'viewport_center_y': float(vp_center.y()),
                'settings': persistent,
                'carla_metadata': carla_meta,
            },
        }

    def _apply_project_payload(
        self, payload: dict, project_path: str, storage_mode: str | None = None
    ):
        project = payload.get('project', payload)
        settings_state = project.get('settings', {})
        project_town_name = str(project.get('town_name') or '').strip()
        self._apply_persistent_settings(settings_state)

        # Handle embedded content
        osm_content = project.get('osm_content')
        osm_path = str(project.get('osm_path') or '')
        xodr_path = ''

        if osm_content:
            try:
                import tempfile

                fd, tmp_path = tempfile.mkstemp(suffix='.osm', prefix='embedded_osm_')
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(osm_content)
                osm_path = tmp_path
            except Exception as e:
                print(f'Failed to restore embedded OSM: {e}')
        else:
            # Backward compatibility for old projects that only embed XODR.
            xodr_content = project.get('xodr_content')
            xodr_path = str(project.get('xodr_path') or '')
            if xodr_content:
                try:
                    import tempfile

                    fd, tmp_path = tempfile.mkstemp(suffix='.xodr', prefix='embedded_xodr_')
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        f.write(xodr_content)
                    xodr_path = tmp_path
                except Exception as e:
                    print(f'Failed to restore embedded XODR: {e}')

        self._restoring_project_payload = True
        try:
            self.edit_xodr.setText('')
            self.edit_osm.setText(osm_path)

            # For OSM-backed projects, regenerate temporary XODR on load
            # (do not rely on persisted XODR content/path).
            if osm_path and os.path.isfile(osm_path):
                generated_xodr = self._convert_osm_to_xodr(osm_path)
                if generated_xodr:
                    xodr_path = generated_xodr
                    self._suppress_next_xodr_title_update = True
                    self.edit_xodr.setText(generated_xodr)
            elif xodr_path and os.path.isfile(xodr_path):
                self._suppress_next_xodr_title_update = True
                self.edit_xodr.setText(xodr_path)

            # Restore all saved visibility settings AFTER path-change triggers are done.
            # This ensures path-change handlers don't override the user's saved visibility.
            self._suppress_async_layer_pipeline = True
            try:
                if 'show_opendrive' in settings_state:
                    self.check_opendrive.setChecked(bool(settings_state.get('show_opendrive')))
                if 'show_opendrive_objects' in settings_state and hasattr(
                    self, 'check_opendrive_objects'
                ):
                    self.check_opendrive_objects.setChecked(
                        bool(settings_state.get('show_opendrive_objects'))
                    )
                if 'show_osm' in settings_state:
                    self.check_osm.setChecked(bool(settings_state.get('show_osm')))
                if 'show_osm_objects' in settings_state and hasattr(self, 'check_osm_objects'):
                    self.check_osm_objects.setChecked(bool(settings_state.get('show_osm_objects')))
            finally:
                self._suppress_async_layer_pipeline = False

            if project_town_name:
                self.town_name = project_town_name

            project_seg_h = project.get('segment_info_height')
            if project_seg_h is not None:
                self._set_osm_props_height(int(project_seg_h))
            project_node_h = project.get('node_info_height')
            if project_node_h is not None:
                self._set_osm_node_props_height(int(project_node_h))
            project_key_w = project.get('segment_info_key_col_width')
            if project_key_w is not None:
                self._osm_tag_key_col_width = max(60, int(project_key_w))

            zoom_pct = int(project.get('display_zoom_pct', settings_state.get('zoom_pct', 100)))
            self._pending_project_zoom_pct = max(100, zoom_pct)
            cx = project.get('viewport_center_x')
            cy = project.get('viewport_center_y')
            self._pending_project_viewport_center = (
                (float(cx), float(cy)) if cx is not None and cy is not None else None
            )

            carla_meta = project.get('carla_metadata', {})
            server_meta = carla_meta.get('server_meta') if isinstance(carla_meta, dict) else None
            if isinstance(server_meta, dict):
                self._carla_bev_server_meta = server_meta
                bounds = carla_meta.get('server_bounds')
                if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
                    self._carla_bev_server_bounds = tuple(int(v) for v in bounds)
                else:
                    self._carla_bev_server_bounds = self._carla_bev_compute_server_tile_bounds(
                        server_meta, self.spin_tile_zoom.value()
                    )
                self._carla_bev_draw_bounds_rect(server_meta)
                if self._carla_bev_bounds_rect_item is not None:
                    self._carla_bev_bounds_rect_item.setVisible(self.check_carla_bev.isChecked())
            else:
                self._carla_bev_server_meta = None
                self._carla_bev_server_bounds = None
                if self._carla_bev_bounds_rect_item is not None:
                    self.scene.removeItem(self._carla_bev_bounds_rect_item)
                    self._carla_bev_bounds_rect_item = None

            self.project_file_path = project_path
            self._preferred_project_save_dir = os.path.dirname(project_path) or None
            self._refresh_window_title()
            self._reset_osm_dirty()

            # Show the right layer rows based on which paths the project contains
            has_xodr = bool(xodr_path)
            has_osm = bool(osm_path)
            self._arrange_import_layers(
                show_xodr=has_xodr,
                show_osm=has_osm,
                osm_first=has_osm,
            )

            self.update_visibility()
            # Restore saved viewport (zoom/center) immediately.
            self._apply_load_view()
        finally:
            self._restoring_project_payload = False

    def new_project(self):
        has_unsaved_changes = bool(self._osm_dirty)
        if has_unsaved_changes:
            message = (
                'You have unsaved OSM changes. Do you want to save them before '
                'creating a new project?'
            )
            reply = QMessageBox.question(
                self,
                'Unsaved Changes',
                message,
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Save:
                if not self.save_project():
                    self._show_project_status('Create new project cancelled')
                    return
            elif reply == QMessageBox.StandardButton.Cancel:
                self._show_project_status('Create new project cancelled')
                return

        # Suppress async layer pipeline while we reset everything so
        # individual checkbox/spinbox changes don't trigger cascading fetches.
        self._suppress_async_layer_pipeline = True
        try:
            # ── Project identity ──────────────────────────────────────────
            self.project_file_path = None
            self._preferred_project_save_dir = None
            self._pending_project_zoom_pct = None
            self._pending_project_viewport_center = None
            self._pending_project_view_scale = None
            self._pending_project_world_center = None
            self.town_name = 'Untitled'
            self._refresh_window_title()

            # ── Stop any in-flight tile fetches ───────────────────────────
            if self._esri_loading or self._esri_pix_data is not None:
                self.stop_esri_refresh()
            if self._carla_bev_loading or self._carla_bev_pix_data is not None:
                self.stop_carla_bev_refresh()

            # ── Clear OpenDRIVE overlay (vector + raster) ─────────────────
            self._clear_xodr_vector_items()
            if self.opendrive_item:
                self.opendrive_item.setPixmap(QPixmap())
            self.opendrive_loading = False
            self.lbl_opendrive_status.setText('')
            self.btn_browse_xodr.setIcon(
                self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
            )

            # ── Clear OSM overlay ─────────────────────────────────────────
            self._clear_osm_items()
            self._osm_loading = False
            self.lbl_osm_status.setText('')
            self._reset_osm_dirty()

            # ── File paths ────────────────────────────────────────────────
            self.edit_xodr.setText('')
            self.xodr_path = None
            self.edit_osm.setText('')
            self.osm_path = None

            # ── Layer checkboxes ──────────────────────────────────────────
            self.check_opendrive.setChecked(False)
            self.check_osm.setChecked(False)
            self.check_esri.setChecked(False)
            self.check_carla_bev.setChecked(False)

            # ── CARLA server metadata ─────────────────────────────────────
            self._carla_bev_server_meta = None
            self._carla_bev_server_bounds = None
            if self._carla_bev_bounds_rect_item is not None:
                self.scene.removeItem(self._carla_bev_bounds_rect_item)
                self._carla_bev_bounds_rect_item = None
            if self._xodr_bounds_rect_item is not None:
                self.scene.removeItem(self._xodr_bounds_rect_item)
                self._xodr_bounds_rect_item = None
            for _e, _it in list(self._world_extent_edge_items.items()):
                if _it is not None:
                    self.scene.removeItem(_it)
                    self._world_extent_edge_items[_e] = None
            self._extent_hover_edge = None

            # ── World Extent ──────────────────────────────────────────────
            self.spin_origin_lat.setValue(DEFAULT_ORIGIN_LAT)
            self.spin_origin_lon.setValue(DEFAULT_ORIGIN_LON)
            if hasattr(self, 'btn_world_edit_mode'):
                self.btn_world_edit_mode.setChecked(False)
            if hasattr(self, 'btn_esri_edit_mode'):
                self.btn_esri_edit_mode.setChecked(False)
            if hasattr(self, 'btn_carla_edit_mode'):
                self.btn_carla_edit_mode.setChecked(False)
            # CARLA Y is negated: spinbox shows user-friendly values
            # (north positive, south negative); negate for defaults.
            self.spin_bound_north.setValue(DEFAULT_BOUND_EXTENT)
            self.spin_bound_south.setValue(DEFAULT_BOUND_EXTENT)
            self.spin_bound_east.setValue(DEFAULT_BOUND_EXTENT)
            self.spin_bound_west.setValue(DEFAULT_BOUND_EXTENT)

            # ── Recompute map_ctx dimensions & scene rect ─────────────────
            if self.map_ctx:
                new_mpp = (EARTH_CIRCUMFERENCE * math.cos(math.radians(DEFAULT_ORIGIN_LAT))) / (
                    (2**self.map_ctx.tile_max_zoom_level) * TILE_SIZE
                )
                self.map_ctx.mpp = new_mpp
                self.map_ctx.min_meters_per_pixel = new_mpp
                self.map_ctx.earth_ref_lat = DEFAULT_ORIGIN_LAT
                self.map_ctx.earth_ref_lon = DEFAULT_ORIGIN_LON
                self.map_ctx.proj_false_easting = 0.0
                self.map_ctx.proj_false_northing = 0.0
                self.map_ctx.proj_scale_factor = 1.0
                default_bounds = [
                    -DEFAULT_BOUND_EXTENT,
                    DEFAULT_BOUND_EXTENT,
                    -DEFAULT_BOUND_EXTENT,
                    DEFAULT_BOUND_EXTENT,
                ]
                self.map_ctx.world_bounds = default_bounds
                self.map_ctx.world_offset = [default_bounds[0], default_bounds[2]]
                w = default_bounds[1] - default_bounds[0]
                h = default_bounds[3] - default_bounds[2]
                self.map_ctx.width_in_pixels = int(math.ceil(w / new_mpp))
                self.map_ctx.height_in_pixels = int(math.ceil(h / new_mpp))
                new_rect = QRectF(
                    0, 0, self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels
                )
                self.scene.setSceneRect(new_rect)
                self._sync_scene_rect()
                if self.grid_item:
                    self.grid_item.mpp = new_mpp
                    self.grid_item.rect = new_rect
                    self.grid_item.world_offset = self.map_ctx.world_offset

            # ── Display zoom ─────────────────────────────────────────────
            # Block signals so setValue(100) doesn't fire _on_zoom_spinbox_changed
            # with the stale _fit_scale (DEFAULT_MIN_SCALE = 0.01) before
            # fit_to_window has computed the real fit scale.  fit_to_window
            # (scheduled below) calls _update_zoom_spinbox to set the correct value.
            self.spin_zoom.blockSignals(True)
            self.spin_zoom.setValue(100)
            self.spin_zoom.blockSignals(False)

            # ── OpenDRIVE options ─────────────────────────────────────────
            self._alpha_saved = DEFAULT_OPENDRIVE_ALPHA
            self._alpha_only_mode = False
            self.spin_opendrive_alpha.setValue(DEFAULT_OPENDRIVE_ALPHA)

            # ── OSM options ───────────────────────────────────────────────
            self.spin_osm_alpha.setValue(0.6)
            if hasattr(self, 'btn_osm_edit_mode'):
                self.btn_osm_edit_mode.setChecked(False)
            self._osm2xodr_settings = copy.deepcopy(DEFAULT_OSM2XODR_SETTINGS)
            self._set_osm_props_height(self._osm_props_max_h)
            self._set_osm_node_props_height(self._osm_props_max_h)

            # ── ESRI options ──────────────────────────────────────────────
            self.spin_tile_zoom.setValue(DEFAULT_TILE_ZOOM)
            self.spin_esri_x.setValue(0.0)
            self.spin_esri_y.setValue(0.0)

            # ── Server host / port ────────────────────────────────────────
            # Preserve CLI-supplied values; only fall back to defaults when
            # no --host / --port was given.
            reset_ip = self._cli_server_ip or DEFAULT_SERVER_HOST
            reset_port = self._cli_server_port or DEFAULT_SERVER_PORT
            self.server_ip = reset_ip
            self.server_port = reset_port
            self.edit_server_ip.setText(reset_ip)
            self.edit_server_port.setText(str(reset_port))

            # ── Grid options ──────────────────────────────────────────────
            self._grid_saved_state = True
            self.check_grid.blockSignals(True)
            self.check_grid.setChecked(True)
            self.check_grid.blockSignals(False)
            self.spin_thickness.setValue(2)
            self.spin_font.setValue(12)
            self.spin_grid_sigdigits.setValue(MAX_GRID_LABEL_DIGITS)
            if self.grid_item:
                self.grid_item.grid_color = QColor(DEFAULT_GRID_COLOR_HEX)
            self.view.setBackgroundBrush(QColor(DEFAULT_VIEWPORT_BG_COLOR_HEX))

            # ── Stop spinner if running ───────────────────────────────────
            self.spinner_timer.stop()

            # ── Hide import-layer rows ────────────────────────────────────
            self._arrange_import_layers(show_xodr=False, show_osm=False)
        finally:
            self._suppress_async_layer_pipeline = False

        self.update_grid_style()
        self.update_imagery_alignment()
        self.update_visibility()
        # Create the interactive world-extent edge items so the bounding box
        # is visible and draggable even before any XODR file is loaded.
        self._draw_world_extent_rect()
        QTimer.singleShot(INITIAL_LOAD_DELAY_MS, self.fit_to_window)
        self._show_project_status('Created a new project')

    def open_project(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            'Open Project File',
            self.project_file_path or '',
            'OpenRoadEditor Project (*.ore);;JSON Files (*.json);;All Files (*)',
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not file_path:
            return
        self.load_project_file(file_path, show_status=True)

    def load_project_file(self, file_path: str, show_status: bool = True) -> bool:
        ext = os.path.splitext(file_path)[1].lower()
        try:
            with open(file_path, 'r', encoding='utf-8') as handle:
                payload = json.load(handle)
            self._apply_project_payload(payload, file_path, storage_mode='ore')
            if show_status:
                self._show_project_status(f'Opened project: {os.path.basename(file_path)}')
            return True
        except Exception as error:
            QMessageBox.warning(
                self, 'Open Project Failed', f'Could not open project file:\n{error}'
            )
            if show_status:
                self._show_project_status('Failed to open project')
            return False

    def _project_storage_mode(self) -> str:
        return 'ore'

    def _default_project_save_path(self) -> str:
        current = self.project_file_path or ''
        if current:
            return current
        preferred_dir = str(self._preferred_project_save_dir or '').strip()
        if preferred_dir and os.path.isdir(preferred_dir):
            base_name = self.town_name.strip() if isinstance(self.town_name, str) else ''
            if not base_name:
                base_name = 'project'
            return os.path.join(preferred_dir, f'{base_name}.ore')
        base = ''
        if self.osm_path:
            base = os.path.splitext(self.osm_path)[0]
        elif self.town_name:
            base = self.town_name
        return f'{base}.ore' if base else '.ore'

    def _get_save_project_file_path(self) -> str | None:
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            'Save Project File',
            self._default_project_save_path(),
            'OpenRoadEditor Project (*.ore);;JSON Files (*.json);;All Files (*)',
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not file_path:
            return None
        if not os.path.splitext(file_path)[1]:
            file_path += '.ore'
        return file_path

    def save_project(self) -> bool:
        file_path = self.project_file_path
        if not file_path:
            file_path = self._get_save_project_file_path()
            if not file_path:
                return False
        return self._write_project_file(file_path)

    def save_project_as(self) -> bool:
        file_path = self._get_save_project_file_path()
        if not file_path:
            return False
        return self._write_project_file(file_path)

    def _write_project_file(self, file_path: str) -> bool:
        try:
            payload = self._collect_project_payload(storage_mode='ore')
            with open(file_path, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, indent=2)
                handle.write('\n')
            self.project_file_path = file_path
            self._preferred_project_save_dir = os.path.dirname(file_path) or None
            self._refresh_window_title()
            self._reset_osm_dirty()
            self._show_project_status(f'Saved project: {os.path.basename(file_path)}')
            return True
        except Exception as error:
            QMessageBox.warning(
                self, 'Save Project Failed', f'Could not save project file:\n{error}'
            )
            self._show_project_status('Failed to save project')
            return False

    def _current_xodr_content(self) -> str | None:
        xodr_content = self._xodr_content
        if not xodr_content and self.xodr_path and os.path.isfile(self.xodr_path):
            try:
                with open(self.xodr_path, 'r', encoding='utf-8') as handle:
                    xodr_content = handle.read()
            except Exception:
                return None
        return xodr_content
