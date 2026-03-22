"""Tile-fetching mixin (ESRI imagery + CARLA BEV)."""

import concurrent.futures
import io
import math
import os
import threading
import time
from urllib import request as urllib_request

import numpy as np
from PIL import Image, ImageDraw
from PyQt6.QtCore import (
    QRectF,
    Qt,
)
from PyQt6.QtGui import (
    QBrush,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QGraphicsLineItem,
    QGraphicsRectItem,
    QMessageBox,
)

from open_road_editor.constants import *  # noqa: F401,F403
from open_road_editor.utils.map_context import MapContext
from open_road_editor.utils.tiles import _oor_tile_rgba


class _TilesMixin:
    """Mixin — see viewer/main.py for the assembled class."""

    def fit_to_window(self):
        # Expand sceneRect so fitInView and drag/scroll can reach all bounds.
        self._sync_scene_rect()
        fit_rect = self._combined_bounds_rect()
        # Add a small margin (FIT_MARGIN_FACTOR) so the bounds don't touch the edges.
        margin_x = fit_rect.width() * FIT_MARGIN_FACTOR
        margin_y = fit_rect.height() * FIT_MARGIN_FACTOR
        fit_rect.adjust(-margin_x, -margin_y, margin_x, margin_y)
        self.view.fitInView(fit_rect, Qt.AspectRatioMode.KeepAspectRatio)
        self._fit_scale = self.view.transform().m11()
        # Allow zooming out further than fit-to-window so the grid
        # remains visible beyond the layer bounds.
        self.view._min_scale = DEFAULT_MIN_SCALE
        self.spin_zoom.setMinimum(1)
        if self.grid_item:
            self.grid_item.update()
        self._update_zoom_spinbox()

    def fetch_tiles_generic(
        self,
        layer,
        zoom,
        base_url_template,
        on_progress,
        on_complete,
        cancelled_flag_name,
        tile_dir=None,
        retry_until_complete=False,
    ):
        tl_lon, tl_lat = self.map_ctx.carla_to_earth_transform(
            self.map_ctx.world_bounds[0], self.map_ctx.world_bounds[2]
        )
        br_lon, br_lat = self.map_ctx.carla_to_earth_transform(
            self.map_ctx.world_bounds[1], self.map_ctx.world_bounds[3]
        )
        min_lat, max_lat = min(tl_lat, br_lat), max(tl_lat, br_lat)
        min_lon, max_lon = min(tl_lon, br_lon), max(tl_lon, br_lon)

        def lat2tiley(lat, z):
            return (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * (2.0**z)

        def lon2tilex(lon, z):
            return (lon + 180.0) / 360.0 * (2.0**z)

        x_min, x_max = (
            int(math.floor(lon2tilex(min_lon, zoom))),
            int(math.floor(lon2tilex(max_lon, zoom))),
        )
        y_min, y_max = (
            int(math.floor(lat2tiley(max_lat, zoom))),
            int(math.floor(lat2tiley(min_lat, zoom))),
        )
        total_tiles = (x_max - x_min + 1) * (y_max - y_min + 1)

        # Pre-compute coordinate lookup once — maps every output pixel → (tile_x, tile_y, off_x, off_y)
        H, W = self.map_ctx.height_in_pixels, self.map_ctx.width_in_pixels
        y_i, x_i = np.arange(H), np.arange(W)
        xx, yy = np.meshgrid(x_i, y_i)
        world_y = self.map_ctx.world_offset[1] + yy * self.map_ctx.mpp
        world_x = self.map_ctx.world_offset[0] + xx * self.map_ctx.mpp
        m_per_deg = self.map_ctx.meters_per_degree_lat
        ref_lat, ref_lon = self.map_ctx.earth_ref_lat, self.map_ctx.earth_ref_lon
        off_x0 = self.map_ctx.carla_world_origin_offset.x
        off_y0 = self.map_ctx.carla_world_origin_offset.y
        lon_arr = ((world_x - off_x0) / (m_per_deg * math.cos(math.radians(ref_lat)))) + ref_lon
        lat_arr = ref_lat - ((world_y - off_y0) / m_per_deg)
        tx_f = (lon_arr + 180.0) / 360.0 * (2.0**zoom)
        ty_f = (1.0 - np.arcsinh(np.tan(np.radians(lat_arr))) / np.pi) / 2.0 * (2.0**zoom)
        tile_x_map = np.floor(tx_f).astype(np.int32)
        tile_y_map = np.floor(ty_f).astype(np.int32)
        ox_map = np.clip(((tx_f - tile_x_map) * TILE_SIZE).astype(np.int32), 0, TILE_SIZE - 1)
        oy_map = np.clip(((ty_f - tile_y_map) * TILE_SIZE).astype(np.int32), 0, TILE_SIZE - 1)

        # Shared pixel buffer — start by filling out-of-range pixels with the OOR tile
        pix_data = np.zeros((H, W, 4), dtype=np.uint8)
        oor_mask = (
            (tile_x_map < x_min)
            | (tile_x_map > x_max)
            | (tile_y_map < y_min)
            | (tile_y_map > y_max)
        )
        if np.any(oor_mask):
            oor_arr = _oor_tile_rgba(self.spin_font.value())
            pix_data[oor_mask] = oor_arr[oy_map[oor_mask], ox_map[oor_mask]]
        pix_lock = threading.Lock()

        def apply_tile(pos, img_arr):
            """Paint one tile into pix_data in-place."""
            mask = (tile_x_map == pos[0]) & (tile_y_map == pos[1])
            if np.any(mask):
                with pix_lock:
                    pix_data[mask] = img_arr[oy_map[mask], ox_map[mask]]

        def fetch_single_tile(tx, ty):
            if getattr(self.map_ctx, cancelled_flag_name, False):
                return (tx, ty), None
            if tile_dir:
                cache_file = os.path.join(tile_dir, str(zoom), str(tx), f'{ty}.png')
                if os.path.exists(cache_file):
                    try:
                        return (tx, ty), Image.open(cache_file).convert('RGBA')
                    except:
                        pass
            url = base_url_template.format(zoom=zoom, x=tx, y=ty)
            try:
                req = urllib_request.Request(url, headers={'User-Agent': HTTP_USER_AGENT})
                with urllib_request.urlopen(req, timeout=TILE_FETCH_TIMEOUT_S) as response:
                    img = Image.open(io.BytesIO(response.read())).convert('RGBA')
                    if retry_until_complete and np.sum(np.array(img)[:, :, 3]) == 0:
                        return (tx, ty), None
                    if tile_dir:
                        d = os.path.join(tile_dir, str(zoom), str(tx))
                        os.makedirs(d, exist_ok=True)
                        img.save(os.path.join(d, f'{ty}.png'))
                    return (tx, ty), img
            except:
                return (tx, ty), None

        count, pending_tiles = (
            0,
            [(tx, ty) for tx in range(x_min, x_max + 1) for ty in range(y_min, y_max + 1)],
        )
        while pending_tiles and not getattr(self.map_ctx, cancelled_flag_name, False):
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=GENERIC_TILE_POOL_WORKERS
            ) as executor:
                futures = {
                    executor.submit(fetch_single_tile, tx, ty): (tx, ty)
                    for (tx, ty) in pending_tiles
                }
                new_pending = []
                for f in concurrent.futures.as_completed(futures):
                    if getattr(self.map_ctx, cancelled_flag_name, False):
                        break
                    pos, img = f.result()
                    if img:
                        apply_tile(pos, np.array(img))
                        count += 1
                        # Emit a snapshot after each tile for progressive display
                        with pix_lock:
                            snapshot = Image.fromarray(pix_data.copy())
                        on_progress(snapshot, count, total_tiles)
                    else:
                        new_pending.append(futures[f])
                pending_tiles = new_pending
                if not retry_until_complete:
                    break
                if pending_tiles:
                    time.sleep(TILE_RETRY_SLEEP_S)
        on_complete()

    # ── ESRI viewport-aware tile helpers ─────────────────────────────────

    def _esri_geo_params(self):
        """Return commonly-used geo transform params as a tuple."""
        m = self.map_ctx
        return (
            m.meters_per_degree_lat,
            m.earth_ref_lat,
            m.earth_ref_lon,
            m.carla_world_origin_offset.x,
            m.carla_world_origin_offset.y,
        )

    def _esri_visible_tiles(self):
        """Return set of (tx, ty) tile indices in the current viewport.
        Pure geo-math only — no numpy, no cached coord maps."""
        zoom = self._esri_current_zoom
        if not self.map_ctx or not self.esri_item or self._esri_pix_data is None:
            return set()
        W, H = self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels
        scene_rect = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        item_pos = self.esri_item.pos()
        img_x0 = max(0, int(scene_rect.left() - item_pos.x()))
        img_y0 = max(0, int(scene_rect.top() - item_pos.y()))
        img_x1 = min(W - 1, int(scene_rect.right() - item_pos.x()))
        img_y1 = min(H - 1, int(scene_rect.bottom() - item_pos.y()))
        if img_x0 >= img_x1 or img_y0 >= img_y1:
            return set()
        m_per_deg, ref_lat, ref_lon, off_x0, off_y0 = self._esri_geo_params()
        cos_ref = math.cos(math.radians(ref_lat))
        n = 2.0**zoom

        def pix_to_tile(px, py):
            wx = self.map_ctx.world_offset[0] + px * self.map_ctx.mpp
            wy = self.map_ctx.world_offset[1] + py * self.map_ctx.mpp
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

    def _esri_tile_center_pixel(self, tx, ty, zoom):
        """Return (px, py) image-pixel coordinate of tile (tx, ty) centre. O(1)."""
        m_per_deg, ref_lat, ref_lon, off_x0, off_y0 = self._esri_geo_params()
        n = 2.0**zoom
        lon = (tx + 0.5) / n * 360 - 180
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 0.5) / n))))
        cx = (lon - ref_lon) * m_per_deg * math.cos(math.radians(ref_lat)) + off_x0
        cy = (ref_lat - lat) * m_per_deg + off_y0
        return (cx - self.map_ctx.world_offset[0]) / self.map_ctx.mpp, (
            cy - self.map_ctx.world_offset[1]
        ) / self.map_ctx.mpp

    def _esri_tile_pixel_region(self, tx, ty, zoom):
        """Compute the pixel mask for one tile in the output image.
        Returns (y0, x0, mask, tile_oy, tile_ox) over a sub-rect, or None.
        Memory cost ∝ tile footprint, NOT H×W."""
        W, H = self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels
        m_per_deg, ref_lat, ref_lon, off_x0, off_y0 = self._esri_geo_params()
        cos_ref = math.cos(math.radians(ref_lat))
        n = 2.0**zoom

        # Tile lat/lon bounds
        lon_min = tx / n * 360 - 180
        lon_max = (tx + 1) / n * 360 - 180
        lat_max = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
        lat_min = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))

        def ll_to_pix(lat, lon):
            cx = (lon - ref_lon) * m_per_deg * cos_ref + off_x0
            cy = (ref_lat - lat) * m_per_deg + off_y0
            return (cx - self.map_ctx.world_offset[0]) / self.map_ctx.mpp, (
                cy - self.map_ctx.world_offset[1]
            ) / self.map_ctx.mpp

        corners = [
            ll_to_pix(lat_min, lon_min),
            ll_to_pix(lat_min, lon_max),
            ll_to_pix(lat_max, lon_min),
            ll_to_pix(lat_max, lon_max),
        ]
        x0 = max(0, int(math.floor(min(c[0] for c in corners))) - 2)
        x1 = min(W - 1, int(math.ceil(max(c[0] for c in corners))) + 2)
        y0 = max(0, int(math.floor(min(c[1] for c in corners))) - 2)
        y1 = min(H - 1, int(math.ceil(max(c[1] for c in corners))) + 2)
        if x0 >= x1 or y0 >= y1:
            return None

        # Precise mask over the sub-region only
        xx, yy = np.meshgrid(np.arange(x0, x1 + 1), np.arange(y0, y1 + 1))
        world_x = self.map_ctx.world_offset[0] + xx * self.map_ctx.mpp
        world_y = self.map_ctx.world_offset[1] + yy * self.map_ctx.mpp
        lon_arr = ((world_x - off_x0) / (m_per_deg * cos_ref)) + ref_lon
        lat_arr = ref_lat - ((world_y - off_y0) / m_per_deg)
        tx_f = (lon_arr + 180.0) / 360.0 * n
        ty_f = (1.0 - np.arcsinh(np.tan(np.radians(lat_arr))) / np.pi) / 2.0 * n
        mask = (np.floor(tx_f).astype(np.int32) == tx) & (np.floor(ty_f).astype(np.int32) == ty)
        if not np.any(mask):
            return None
        ox = np.clip(((tx_f - tx) * TILE_SIZE).astype(np.int32), 0, TILE_SIZE - 1)
        oy = np.clip(((ty_f - ty) * TILE_SIZE).astype(np.int32), 0, TILE_SIZE - 1)
        return y0, x0, mask, oy, ox

    @staticmethod
    def _paint_tile_region(canvas: np.ndarray, region, tile_arr: np.ndarray) -> bool:
        """Safely paint a tile array into a canvas for a precomputed region.

        Returns True when pixels were painted, False when region/canvas dimensions
        no longer match (e.g., stale async region after map resize).
        """
        if canvas is None or region is None:
            return False
        y0, x0, mask, t_oy, t_ox = region
        if not hasattr(mask, 'shape') or len(mask.shape) != 2:
            return False
        height, width = mask.shape
        if height <= 0 or width <= 0:
            return False
        if y0 < 0 or x0 < 0 or y0 + height > canvas.shape[0] or x0 + width > canvas.shape[1]:
            return False
        sub = canvas[y0 : y0 + height, x0 : x0 + width]
        if sub.shape[0] != height or sub.shape[1] != width:
            return False
        sub[mask] = tile_arr[t_oy[mask], t_ox[mask]]
        return True

    def _esri_paint_tile_placeholder(self, tx, ty, zoom):
        """Paint a grey 'Not loaded' tile into pix_data at that tile's image region."""
        region = self._esri_tile_pixel_region(tx, ty, zoom)
        if region is None:
            return
        y0, x0, mask, t_oy, t_ox = region
        label_size = self.spin_font.value() if hasattr(self, 'spin_font') else 12
        tile_img = Image.new('RGBA', (TILE_SIZE, TILE_SIZE), TILE_PLACEHOLDER_BG_COLOR)
        draw = ImageDraw.Draw(tile_img)
        try:
            from PIL import ImageFont as _IFont

            font = _IFont.load_default(size=max(MIN_FONT_SIZE, label_size))
        except Exception:
            font = None
        text = 'Not loaded'
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(
            ((TILE_SIZE - tw) // 2, (TILE_SIZE - th) // 2),
            text,
            fill=TILE_ERROR_TEXT_COLOR,
            font=font,
        )
        tile_arr = np.array(tile_img)
        with self._esri_pix_lock:
            if self._esri_pix_data is not None:
                self._paint_tile_region(self._esri_pix_data, region, tile_arr)

    def _esri_update_status_label(self):
        """Refresh the ESRI status label from current visible-tile counts."""
        z = self._esri_current_zoom
        if self._esri_vis_total == 0:
            self.lbl_esri_status.setText(f'TZL-{z} Loading 0%')
            return
        if self._esri_vis_done >= self._esri_vis_total:
            self.lbl_esri_status.setText(f'TZL-{z} Loaded')
            if not self._carla_bev_loading and not self.opendrive_loading:
                self.spinner_timer.stop()
        else:
            pct = int(self._esri_vis_done / self._esri_vis_total * 100)
            self.lbl_esri_status.setText(f'TZL-{z} Loading {pct}%')
            self.spinner_timer.start(SPINNER_TIMER_INTERVAL_MS)

    def _esri_fetch_visible(self, visible_override=None):
        """Launch fetches for visible tiles not yet fetched/in-flight."""
        if not self.check_esri.isChecked() or self._esri_pix_data is None:
            return
        epoch = self._esri_epoch
        visible = visible_override if visible_override is not None else self._esri_visible_tiles()
        with self._esri_fetch_lock:
            to_fetch = visible - self._esri_fetched_tiles - self._esri_fetching_tiles
            self._esri_fetching_tiles.update(to_fetch)
            vis_done = len(visible & self._esri_fetched_tiles)
        self._esri_vis_total = len(visible)
        self._esri_vis_done = vis_done
        self._esri_update_status_label()
        if to_fetch:
            self.spinner_timer.start(SPINNER_TIMER_INTERVAL_MS)
            # Paint placeholder for each new tile before fetching
            for tx, ty in to_fetch:
                self._esri_paint_tile_placeholder(tx, ty, self._esri_current_zoom)
            # Show updated placeholder canvas
            with self._esri_pix_lock:
                snap = (
                    Image.fromarray(self._esri_pix_data.copy())
                    if self._esri_pix_data is not None
                    else None
                )
            if snap:
                self.esri_item.setPixmap(self.pil_to_qpixmap(snap))
                self.update_imagery_alignment()
            for tx, ty in to_fetch:
                threading.Thread(
                    target=self._esri_fetch_one_tile,
                    args=(tx, ty, self._esri_current_zoom, epoch),
                    daemon=True,
                ).start()

    def _on_esri_view_changed(self):
        """Debounced handler: fetch any newly visible tiles after scroll/zoom/pan."""
        # Do not fetch tiles while dragging a world-extent edge
        if self._extent_drag_edge is not None:
            return
        if self.check_esri.isChecked() and self._esri_pix_data is not None:
            self._esri_fetch_visible()

    def _esri_fetch_one_tile(self, tx, ty, zoom, epoch):
        """Background: download one ESRI tile, paint into shared buffer, emit signal."""
        with self._esri_tile_sema:
            if epoch != self._esri_epoch or getattr(self.map_ctx, 'esri_fetch_cancelled', False):
                with self._esri_fetch_lock:
                    self._esri_fetching_tiles.discard((tx, ty))
                return
            tile_dir = os.path.join('tiles', 'esri')
            img = None
            cache_file = os.path.join(tile_dir, str(zoom), str(tx), f'{ty}.png')
            if os.path.exists(cache_file):
                try:
                    img = Image.open(cache_file).convert('RGBA')
                except Exception:
                    pass
            if img is None and not getattr(self.map_ctx, 'esri_fetch_cancelled', False):
                url = (
                    'https://clarity.maptiles.arcgis.com/arcgis/rest/services/'
                    f'World_Imagery/MapServer/tile/{zoom}/{ty}/{tx}'
                )
                try:
                    req = urllib_request.Request(url, headers={'User-Agent': HTTP_USER_AGENT})
                    with urllib_request.urlopen(
                        req, timeout=ESRI_TILE_FETCH_TIMEOUT_S
                    ) as response:
                        img = Image.open(io.BytesIO(response.read())).convert('RGBA')
                    d = os.path.join(tile_dir, str(zoom), str(tx))
                    os.makedirs(d, exist_ok=True)
                    img.save(os.path.join(d, f'{ty}.png'))
                except Exception:
                    pass
            if epoch != self._esri_epoch:
                with self._esri_fetch_lock:
                    self._esri_fetching_tiles.discard((tx, ty))
                return
            with self._esri_fetch_lock:
                self._esri_fetched_tiles.add((tx, ty))
                self._esri_fetching_tiles.discard((tx, ty))
            if img is not None:
                # Compute per-tile pixel region (small array, NOT H×W)
                region = self._esri_tile_pixel_region(tx, ty, zoom)
                if region is not None:
                    img_arr = np.array(img)
                    with self._esri_pix_lock:
                        if self._esri_pix_data is not None:
                            self._paint_tile_region(self._esri_pix_data, region, img_arr)
            # Signal main thread — no snapshot; repaint timer batches the update
            self.esri_refreshed.emit(None, 0, 0, epoch)

    # ── Carla_Bev (CARLA) viewport-aware tile helpers ────────────────────────

    def _carla_bev_visible_tiles(self):
        """Return set of (tx, ty) tile indices in the current viewport for the carla_bev layer."""
        zoom = self._carla_bev_current_zoom
        if not self.map_ctx or not self.carla_bev_item or self._carla_bev_pix_data is None:
            return set()
        W, H = self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels
        scene_rect = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        item_pos = self.carla_bev_item.pos()
        img_x0 = max(0, int(scene_rect.left() - item_pos.x()))
        img_y0 = max(0, int(scene_rect.top() - item_pos.y()))
        img_x1 = min(W - 1, int(scene_rect.right() - item_pos.x()))
        img_y1 = min(H - 1, int(scene_rect.bottom() - item_pos.y()))
        if img_x0 >= img_x1 or img_y0 >= img_y1:
            return set()
        m_per_deg, ref_lat, ref_lon, off_x0, off_y0 = self._esri_geo_params()
        cos_ref = math.cos(math.radians(ref_lat))
        n = 2.0**zoom

        def pix_to_tile(px, py):
            wx = self.map_ctx.world_offset[0] + px * self.map_ctx.mpp
            wy = self.map_ctx.world_offset[1] + py * self.map_ctx.mpp
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

    def _carla_bev_paint_tile_placeholder(self, tx, ty, zoom):
        """Paint a grey 'Not loaded' tile into carla_bev pix_data."""
        region = self._esri_tile_pixel_region(tx, ty, zoom)
        if region is None:
            return
        y0, x0, mask, t_oy, t_ox = region
        label_size = self.spin_font.value() if hasattr(self, 'spin_font') else 12
        tile_img = Image.new('RGBA', (TILE_SIZE, TILE_SIZE), TILE_PLACEHOLDER_BG_COLOR)
        draw = ImageDraw.Draw(tile_img)
        try:
            from PIL import ImageFont as _IFont

            font = _IFont.load_default(size=max(MIN_FONT_SIZE, label_size))
        except Exception:
            font = None
        text = 'Not loaded'
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(
            ((TILE_SIZE - tw) // 2, (TILE_SIZE - th) // 2),
            text,
            fill=TILE_ERROR_TEXT_COLOR,
            font=font,
        )
        tile_arr = np.array(tile_img)
        with self._carla_bev_pix_lock:
            if self._carla_bev_pix_data is not None:
                self._paint_tile_region(self._carla_bev_pix_data, region, tile_arr)

    def _carla_bev_update_status_label(self):
        """Refresh the carla_bev status label from current visible-tile counts."""
        z = self._carla_bev_current_zoom
        offline_suffix = ' (offline)' if self._carla_bev_server_online is False else ''
        if self._carla_bev_vis_total == 0:
            self.lbl_carla_bev_status.setText(f'TZL-{z} Loading 0%{offline_suffix}')
            return
        pct = int(self._carla_bev_vis_done / self._carla_bev_vis_total * 100)
        if self._carla_bev_vis_processed >= self._carla_bev_vis_total:
            if self._carla_bev_server_online is False:
                self.lbl_carla_bev_status.setText(f'TZL-{z} {pct}% Loaded (offline)')
            else:
                self.lbl_carla_bev_status.setText(f'TZL-{z} Loaded')
            self._carla_bev_loading = False
            if not self._esri_loading and not self.opendrive_loading:
                self.spinner_timer.stop()
        else:
            if self._carla_bev_server_online is False:
                self.lbl_carla_bev_status.setText(f'TZL-{z} {pct}%{offline_suffix}')
            else:
                self.lbl_carla_bev_status.setText(f'TZL-{z} {pct}% Loaded')
            self.spinner_timer.start(SPINNER_TIMER_INTERVAL_MS)

    def _carla_bev_do_repaint(self):
        """Throttled pixmap update for carla_bev layer — runs on main thread."""
        with self._carla_bev_pix_lock:
            snapshot = (
                Image.fromarray(self._carla_bev_pix_data.copy())
                if self._carla_bev_pix_data is not None
                else None
            )
        if snapshot and self.carla_bev_item:
            self.carla_bev_item.setPixmap(self.pil_to_qpixmap(snapshot))
            self.update_imagery_alignment()

    def _carla_bev_fetch_visible(self, visible_override=None):
        """Launch fetches for visible carla_bev tiles not yet fetched/in-flight.

        Tiles outside the server's world bounds are painted with the OOR tile
        immediately and counted as done. Tiles inside bounds are fetched normally;
        if the server is offline only the cache is consulted.
        """
        if not self.check_carla_bev.isChecked() or self._carla_bev_pix_data is None:
            return
        # If server probe is still in flight, wait for _on_carla_bev_meta_ready to call us.
        if self._carla_bev_server_online is None:
            return
        epoch = self._carla_bev_epoch
        zoom = self._carla_bev_current_zoom
        bounds = self._carla_bev_server_bounds  # (tx_min,tx_max,ty_min,ty_max) or None
        visible = (
            visible_override if visible_override is not None else self._carla_bev_visible_tiles()
        )

        # Partition into in-bounds vs out-of-server-bounds
        if bounds is not None:
            tx_min, tx_max, ty_min, ty_max = bounds
            in_bounds = {
                (tx, ty)
                for (tx, ty) in visible
                if tx_min <= tx <= tx_max and ty_min <= ty <= ty_max
            }
            out_of_bounds = visible - in_bounds
        else:
            in_bounds = visible
            out_of_bounds = set()

        # Paint OOR tiles immediately and count them as done
        if out_of_bounds:
            oor_arr = _oor_tile_rgba(self.spin_font.value())
            for tx, ty in out_of_bounds:
                region = self._esri_tile_pixel_region(tx, ty, zoom)
                if region is not None:
                    with self._carla_bev_pix_lock:
                        if self._carla_bev_pix_data is not None:
                            self._paint_tile_region(self._carla_bev_pix_data, region, oor_arr)
            with self._carla_bev_fetch_lock:
                self._carla_bev_fetched_tiles.update(out_of_bounds)

        with self._carla_bev_fetch_lock:
            to_fetch = in_bounds - self._carla_bev_fetched_tiles - self._carla_bev_fetching_tiles
            self._carla_bev_fetching_tiles.update(to_fetch)
            vis_done = len(
                (visible & self._carla_bev_fetched_tiles) - self._carla_bev_placeholder_tiles
            )
            vis_processed = len(visible & self._carla_bev_fetched_tiles)
        self._carla_bev_vis_total = len(visible)
        self._carla_bev_vis_done = vis_done
        self._carla_bev_vis_processed = vis_processed
        self._carla_bev_update_status_label()
        if to_fetch:
            if not self._carla_bev_server_online:
                # Offline: try cache for each tile; paint "Not loaded" only on cache miss
                tile_dir = os.path.join('tiles', f'carla_{self.town_name}')
                real_loaded = set()
                for tx, ty in to_fetch:
                    loaded = False
                    cache_file = os.path.join(tile_dir, str(zoom), str(tx), f'{ty}.png')
                    if os.path.exists(cache_file):
                        try:
                            img = Image.open(cache_file).convert('RGBA')
                            if np.sum(np.array(img)[:, :, 3]) > 0:
                                region = self._esri_tile_pixel_region(tx, ty, zoom)
                                if region is not None:
                                    img_arr = np.array(img)
                                    with self._carla_bev_pix_lock:
                                        if self._carla_bev_pix_data is not None:
                                            self._paint_tile_region(
                                                self._carla_bev_pix_data,
                                                region,
                                                img_arr,
                                            )
                                real_loaded.add((tx, ty))
                                loaded = True
                                # print(f'[CARLA] tile z={zoom} ({tx},{ty}): loaded from cache')
                        except Exception:
                            pass
                    if not loaded:
                        self._carla_bev_paint_tile_placeholder(tx, ty, zoom)
                with self._carla_bev_fetch_lock:
                    self._carla_bev_fetching_tiles -= to_fetch
                    self._carla_bev_fetched_tiles.update(to_fetch)
                    self._carla_bev_placeholder_tiles.update(to_fetch - real_loaded)
                    self._carla_bev_vis_done = len(
                        (visible & self._carla_bev_fetched_tiles)
                        - self._carla_bev_placeholder_tiles
                    )
                    self._carla_bev_vis_processed = len(visible & self._carla_bev_fetched_tiles)
                self._carla_bev_vis_total = len(visible)
                self._carla_bev_update_status_label()
                self._carla_bev_loading = False
                if not self._esri_loading and not self.opendrive_loading:
                    self.spinner_timer.stop()
                if not self._carla_bev_repaint_timer.isActive():
                    self._carla_bev_repaint_timer.start(TILE_REPAINT_THROTTLE_MS)
            else:
                print(
                    f'[CARLA] queuing {len(to_fetch)} tile(s) '
                    f'({len(out_of_bounds)} OOR, {vis_done}/{len(visible)} already done)'
                )
                self.spinner_timer.start(SPINNER_TIMER_INTERVAL_MS)
                for tx, ty in to_fetch:
                    self._carla_bev_paint_tile_placeholder(tx, ty, zoom)
                with self._carla_bev_pix_lock:
                    snap = (
                        Image.fromarray(self._carla_bev_pix_data.copy())
                        if self._carla_bev_pix_data is not None
                        else None
                    )
                if snap:
                    self.carla_bev_item.setPixmap(self.pil_to_qpixmap(snap))
                    self.update_imagery_alignment()
                for tx, ty in to_fetch:
                    threading.Thread(
                        target=self._carla_bev_fetch_one_tile,
                        args=(tx, ty, zoom, epoch),
                        daemon=True,
                    ).start()
        elif out_of_bounds:
            # All visible tiles were OOR — repaint immediately
            if not self._carla_bev_repaint_timer.isActive():
                self._carla_bev_repaint_timer.start(TILE_REPAINT_THROTTLE_MS)

    def _on_carla_bev_view_changed(self):
        """Debounced handler: fetch any newly visible carla_bev tiles after scroll/zoom/pan."""
        # Do not fetch tiles while dragging a world-extent edge
        if self._extent_drag_edge is not None:
            return
        if self.check_carla_bev.isChecked() and self._carla_bev_pix_data is not None:
            self._carla_bev_fetch_visible()

    # ------------------------------------------------------------------
    # Server metadata probe helpers
    # ------------------------------------------------------------------

    def _carla_bev_compute_server_tile_bounds(self, meta, zoom):
        """Convert /metadata world_bounds to tile indices at the given zoom level.
        Uses the identical geo-math as pix_to_tile/_esri_visible_tiles for consistency.
        """
        wb = meta.get('world_bounds', None)  # [min_x, max_x, min_y, max_y] in Carla metres
        if wb is None or not self.map_ctx:
            return None
        m_per_deg, ref_lat, ref_lon, off_x0, off_y0 = self._esri_geo_params()
        cos_ref = math.cos(math.radians(ref_lat))
        n = 2.0**zoom

        def carla_xy_to_tile(cx, cy):
            lon = (cx - off_x0) / (m_per_deg * cos_ref) + ref_lon
            lat = ref_lat - (cy - off_y0) / m_per_deg
            tx = int(math.floor((lon + 180.0) / 360.0 * n))
            ty = int(
                math.floor((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
            )
            return tx, ty

        min_x, max_x, min_y, max_y = wb[0], wb[1], wb[2], wb[3]
        corners = [
            carla_xy_to_tile(min_x, min_y),
            carla_xy_to_tile(min_x, max_y),
            carla_xy_to_tile(max_x, min_y),
            carla_xy_to_tile(max_x, max_y),
        ]
        tx_min = min(c[0] for c in corners)
        tx_max = max(c[0] for c in corners)
        ty_min = min(c[1] for c in corners)
        ty_max = max(c[1] for c in corners)
        return (tx_min, tx_max, ty_min, ty_max)

    def _carla_bev_fetch_server_meta(self, epoch):
        """Background thread: GET /metadata and emit carla_bev_meta_ready signal."""
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
            with urllib_request.urlopen(req, timeout=METADATA_PROBE_TIMEOUT_S) as resp:
                import json as _json

                meta = _json.loads(resp.read().decode())
            print(
                f'[CARLA] Server online at {ip}:{port} — world_bounds={meta.get("world_bounds")}'
            )
            self.carla_bev_meta_ready.emit(meta, epoch)
        except Exception:
            print(f'[CARLA] Server offline ({ip}:{port})')
            self.carla_bev_meta_ready.emit(None, epoch)

    def _on_carla_bev_meta_ready(self, meta, epoch):
        """Main-thread slot: handle /metadata probe result and start tile fetching."""
        if epoch != self._carla_bev_epoch:
            return  # stale probe

        if meta is not None:
            was_offline = self._carla_bev_server_online is False
            self._carla_bev_server_online = True
            if was_offline:
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
                print(f'[CARLA] Server is back online at {ip}:{port}!')
            self._carla_bev_server_meta = meta
            meta_town = str(meta.get('town') or '').strip()
            if meta_town and meta_town != self.town_name:
                self.town_name = meta_town
                self._refresh_window_title()
            print(f'[CARLA] Town: {meta_town or "(unknown)"}')
            print(f'[CARLA] Tile cache dir: {os.path.join("tiles", f"carla_{self.town_name}")}')
            zoom = self.spin_tile_zoom.value()
            bounds = self._carla_bev_compute_server_tile_bounds(meta, zoom)
            self._carla_bev_server_bounds = bounds
            if bounds:
                print(f'[CARLA] Server tile bounds at zoom={zoom}: {bounds}')

            # Draw a bounding-box rectangle on the scene for the server world bounds
            self._carla_bev_draw_bounds_rect(meta)
        else:
            self._carla_bev_server_online = False
            if isinstance(self._carla_bev_server_meta, dict):
                self._carla_bev_server_bounds = self._carla_bev_compute_server_tile_bounds(
                    self._carla_bev_server_meta, self.spin_tile_zoom.value()
                )
                self._carla_bev_draw_bounds_rect(self._carla_bev_server_meta)
            self._carla_bev_update_status_label()

        self._carla_bev_fetch_visible()

    def _carla_bev_fetch_one_tile(self, tx, ty, zoom, epoch):
        """Background: download one CARLA carla_bev tile with retry, paint into shared buffer."""
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
        url = f'http://{ip}:{port}/carla_bev/{zoom}/{tx}/{ty}.png'
        tile_dir = os.path.join('tiles', f'carla_{self.town_name}')
        tile_tag = f'z={zoom} ({tx},{ty})'
        server_online = self._carla_bev_server_online

        def try_fetch():
            """Single attempt: check cache first, then server (if online). Returns Image or None."""
            cache_file = os.path.join(tile_dir, str(zoom), str(tx), f'{ty}.png')
            if os.path.exists(cache_file):
                try:
                    img = Image.open(cache_file).convert('RGBA')
                    if np.sum(np.array(img)[:, :, 3]) > 0:
                        # print(f'[CARLA] tile {tile_tag}: loaded from cache')
                        return img
                except Exception:
                    pass
            if not server_online:
                return None  # cache miss and server is offline
            try:
                req = urllib_request.Request(url, headers={'User-Agent': HTTP_USER_AGENT})
                with urllib_request.urlopen(req, timeout=TILE_FETCH_TIMEOUT_S) as response:
                    img = Image.open(io.BytesIO(response.read())).convert('RGBA')
                if np.sum(np.array(img)[:, :, 3]) == 0:
                    print(
                        f'[CARLA] tile {tile_tag}: server returned empty tile (CARLA still rendering)'
                    )
                    return None  # empty tile — CARLA not done rendering yet
                d = os.path.join(tile_dir, str(zoom), str(tx))
                os.makedirs(d, exist_ok=True)
                img.save(os.path.join(d, f'{ty}.png'))
                # print(f'[CARLA] tile {tile_tag}: fetched and cached')
                return img
            except Exception as _e:
                # print(f'[CARLA] tile {tile_tag}: request error — {_e}')
                return None

        # When server is offline: single attempt (cache only); no retries
        max_attempts = 1 if not server_online else CARLA_TILE_MAX_RETRIES
        img = None
        for attempt in range(max_attempts):
            if epoch != self._carla_bev_epoch or getattr(
                self.map_ctx, 'carla_bev_fetch_cancelled', False
            ):
                # print(f'[CARLA] tile {tile_tag}: cancelled (attempt {attempt})')
                with self._carla_bev_fetch_lock:
                    self._carla_bev_fetching_tiles.discard((tx, ty))
                return
            with self._carla_bev_tile_sema:
                if epoch != self._carla_bev_epoch:
                    # print(f'[CARLA] tile {tile_tag}: epoch changed, aborting')
                    with self._carla_bev_fetch_lock:
                        self._carla_bev_fetching_tiles.discard((tx, ty))
                    return
                img = try_fetch()
            if img is not None:
                break
            if attempt < max_attempts - 1:
                # print(f'[CARLA] tile {tile_tag}: retry {attempt + 1}/{max_attempts} in 2 s')
                time.sleep(CARLA_TILE_RETRY_SLEEP_S)

        if epoch != self._carla_bev_epoch:
            # print(f'[CARLA] tile {tile_tag}: epoch changed after retries, discarding')
            with self._carla_bev_fetch_lock:
                self._carla_bev_fetching_tiles.discard((tx, ty))
            return

        cancelled: set = set()
        with self._carla_bev_fetch_lock:
            if img is not None:
                self._carla_bev_fetched_tiles.add((tx, ty))
            else:
                # Mark this tile as processed (placeholder)
                self._carla_bev_fetched_tiles.add((tx, ty))
                self._carla_bev_placeholder_tiles.add((tx, ty))
                # On first give-up: cancel all remaining queued tiles immediately
                if self._carla_bev_server_online is not False:
                    print('[CARLA] Server appears offline...')
                    self._carla_bev_server_online = False
                    self._carla_bev_epoch += 1  # invalidates all in-flight threads
                    cancelled = set(self._carla_bev_fetching_tiles)
                    self._carla_bev_fetched_tiles.update(cancelled)
                    self._carla_bev_placeholder_tiles.update(cancelled)
                    self._carla_bev_fetching_tiles.clear()
            self._carla_bev_fetching_tiles.discard((tx, ty))

        if img is None:
            self._carla_bev_paint_tile_placeholder(tx, ty, zoom)
            for ctx, cty in cancelled:
                self._carla_bev_paint_tile_placeholder(ctx, cty, zoom)

        if img is not None:
            region = self._esri_tile_pixel_region(tx, ty, zoom)
            if region is not None:
                img_arr = np.array(img)
                with self._carla_bev_pix_lock:
                    if self._carla_bev_pix_data is not None:
                        self._paint_tile_region(self._carla_bev_pix_data, region, img_arr)
                # print(f'[CARLA] tile {tile_tag}: painted into canvas')
        # Signal main thread
        self.carla_bev_refreshed.emit(None, 0, 0, epoch)

    def refresh_esri(self):
        if self.map_ctx:
            self.map_ctx.esri_fetch_cancelled = False
        self._esri_loading = True
        self._esri_pct = 0
        self._esri_epoch += 1
        zoom = self.spin_esri_zoom.value()
        self._esri_loading_zoom = zoom
        self._esri_current_zoom = zoom
        self._esri_vis_done = 0
        self._esri_vis_total = 0
        self.lbl_esri_status.setText(f'TZL-{zoom} Loading 0%')
        self.spinner_timer.start(SPINNER_TIMER_INTERVAL_MS)
        # Allocate fresh pixel buffer (grey bg) — only ~H×W×4 bytes, no coord maps
        W, H = self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels
        with self._esri_fetch_lock:
            self._esri_fetched_tiles = set()
            self._esri_fetching_tiles = set()
        with self._esri_pix_lock:
            self._esri_pix_data = np.full(
                (H, W, 4), list(TILE_PLACEHOLDER_BG_COLOR), dtype=np.uint8
            )
        # Show grey canvas immediately, then fetch visible (placeholders painted per-tile)
        with self._esri_pix_lock:
            snap = Image.fromarray(self._esri_pix_data.copy())
        self.esri_item.setPixmap(self.pil_to_qpixmap(snap))
        self.update_imagery_alignment()
        self._esri_fetch_visible()

    def refresh_carla_bev(self):
        if self.map_ctx:
            self.map_ctx.carla_bev_fetch_cancelled = False
        self._carla_bev_loading = True
        self._carla_bev_epoch += 1
        zoom = self.spin_carla_bev_zoom.value()
        self._carla_bev_loading_zoom = zoom
        self._carla_bev_current_zoom = zoom
        self._carla_bev_vis_done = 0
        self._carla_bev_vis_total = 0
        self._carla_bev_server_online = None  # unknown until metadata probe completes
        self._carla_bev_server_bounds = None

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
        print(f'[CARLA] refresh_carla_bev: zoom={zoom} server={ip}:{port}')
        self.lbl_carla_bev_status.setText('Probing server...')
        self.spinner_timer.start(SPINNER_TIMER_INTERVAL_MS)

        # Allocate fresh pixel buffer (grey bg)
        W, H = self.map_ctx.width_in_pixels, self.map_ctx.height_in_pixels
        with self._carla_bev_fetch_lock:
            self._carla_bev_fetched_tiles = set()
            self._carla_bev_fetching_tiles = set()
            self._carla_bev_placeholder_tiles = set()
        with self._carla_bev_pix_lock:
            if self._carla_bev_pix_data is None:
                self._carla_bev_pix_data = np.full(
                    (H, W, 4), list(TILE_PLACEHOLDER_BG_COLOR), dtype=np.uint8
                )
            snap = Image.fromarray(self._carla_bev_pix_data.copy())
        self.carla_bev_item.setPixmap(self.pil_to_qpixmap(snap))
        self.update_imagery_alignment()

        # Probe server metadata in background; tile fetching starts in _on_carla_bev_meta_ready
        epoch = self._carla_bev_epoch
        threading.Thread(
            target=self._carla_bev_fetch_server_meta, args=(epoch,), daemon=True
        ).start()

    def on_carla_bev_refreshed(self, image, count, total, epoch):
        if epoch != self._carla_bev_epoch:
            return  # stale signal from a cancelled/superseded load
        if count == -1:
            # Explicit stop/cancel
            print(f'[CARLA] on_carla_bev_refreshed: received stop signal (epoch={epoch})')
            self._carla_bev_loading = False
            if not self._esri_loading and not self.opendrive_loading:
                self.spinner_timer.stop()
            return
        # A tile just completed — recompute visible-tile counts
        visible = self._carla_bev_visible_tiles()
        with self._carla_bev_fetch_lock:
            vis_done = len(
                (visible & self._carla_bev_fetched_tiles) - self._carla_bev_placeholder_tiles
            )
            vis_processed = len(visible & self._carla_bev_fetched_tiles)
        self._carla_bev_vis_done = vis_done
        self._carla_bev_vis_processed = vis_processed
        self._carla_bev_vis_total = len(visible)
        self._carla_bev_update_status_label()
        # Schedule a single repaint — collapses many rapid tile signals into one update
        if not self._carla_bev_repaint_timer.isActive():
            self._carla_bev_repaint_timer.start(TILE_REPAINT_THROTTLE_MS)

    def _carla_bev_draw_bounds_rect(self, meta):
        """Add/update a scene rectangle showing the server's world bounds."""
        if not self.map_ctx:
            return
        wb = meta.get('world_bounds')  # [min_x, max_x, min_y, max_y] in Carla metres
        if not wb or len(wb) < 4:
            return
        m_wb = self.map_ctx.world_bounds
        mpp = self.map_ctx.mpp
        sx0 = (wb[0] - m_wb[0]) / mpp
        sy0 = (wb[2] - m_wb[2]) / mpp
        sx1 = (wb[1] - m_wb[0]) / mpp
        sy1 = (wb[3] - m_wb[2]) / mpp
        rect = QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0)
        # Remove stale rect from previous probe
        if self._carla_bev_bounds_rect_item is not None:
            self.scene.removeItem(self._carla_bev_bounds_rect_item)
            self._carla_bev_bounds_rect_item = None
        pen = QPen(CARLA_BOUNDS_RECT_COLOR)
        pen.setWidth(CARLA_BOUNDS_RECT_PEN_WIDTH)
        pen.setCosmetic(True)  # width fixed in screen pixels at any zoom
        item = QGraphicsRectItem(rect)
        item.setPen(pen)
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        item.setZValue(Z_CARLA_BOUNDS_RECT)  # above tile layers, below grid
        self.scene.addItem(item)
        self._carla_bev_bounds_rect_item = item
        self._sync_scene_rect()

    def _draw_xodr_bounds_rect(self):
        """Add/update a scene rectangle showing the OpenDRIVE file's world bounds."""
        # Remove old rect
        if self._xodr_bounds_rect_item is not None:
            self.scene.removeItem(self._xodr_bounds_rect_item)
            self._xodr_bounds_rect_item = None
        if not self.map_ctx or not self.xodr_path:
            return
        xodr_bounds = MapContext.parse_xodr_bounds(self.xodr_path)
        if not xodr_bounds or len(xodr_bounds) < 4:
            return
        m_wb = self.map_ctx.world_bounds
        mpp = self.map_ctx.mpp
        sx0 = (xodr_bounds[0] - m_wb[0]) / mpp
        sy0 = (xodr_bounds[2] - m_wb[2]) / mpp
        sx1 = (xodr_bounds[1] - m_wb[0]) / mpp
        sy1 = (xodr_bounds[3] - m_wb[2]) / mpp
        rect = QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0)
        pen = QPen(XODR_BOUNDS_RECT_COLOR)
        pen.setWidth(XODR_BOUNDS_RECT_PEN_WIDTH)
        pen.setCosmetic(True)
        item = QGraphicsRectItem(rect)
        item.setPen(pen)
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        item.setZValue(Z_XODR_BOUNDS_RECT)
        self.scene.addItem(item)
        self._xodr_bounds_rect_item = item
        # Match visibility to the OpenDRIVE layer checkbox
        item.setVisible(self.check_opendrive.isChecked())
        self._sync_scene_rect()

    def _draw_osm_bounds_rect(self):
        """Add/update a scene rectangle showing the loaded OSM geometry bounds."""
        if self._osm_bounds_rect_item is not None:
            self.scene.removeItem(self._osm_bounds_rect_item)
            self._osm_bounds_rect_item = None
        if not self.map_ctx or self._osm_vector_group is None:
            return

        local_rect = self._osm_vector_group.childrenBoundingRect()
        if local_rect.isNull() or local_rect.width() <= 0 or local_rect.height() <= 0:
            return

        rect = self._osm_vector_group.mapRectToScene(local_rect)
        pen = QPen(OSM_BOUNDS_RECT_COLOR)
        pen.setWidth(OSM_BOUNDS_RECT_PEN_WIDTH)
        pen.setCosmetic(True)
        item = QGraphicsRectItem(rect)
        item.setPen(pen)
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        item.setZValue(Z_OSM_BOUNDS_RECT)
        self.scene.addItem(item)
        self._osm_bounds_rect_item = item
        item.setVisible(self.check_osm.isChecked() and self.spin_osm_alpha.value() > 0.0)
        self._sync_scene_rect()

    def _draw_world_extent_rect(self):
        """Add/update four line items that form the world-extent bounding box.

        Each edge is a separate ``QGraphicsLineItem`` so it can be individually
        highlighted on hover / drag without redrawing the others.
        """
        if not self.map_ctx:
            return
        w_px = self.map_ctx.width_in_pixels
        h_px = self.map_ctx.height_in_pixels
        if w_px <= 0 or h_px <= 0:
            return

        # Edge coordinates in scene space (origin at top-left of canvas)
        edges = {
            'N': (0, 0, w_px, 0),  # top horizontal
            'S': (0, h_px, w_px, h_px),  # bottom horizontal
            'W': (0, 0, 0, h_px),  # left vertical
            'E': (w_px, 0, w_px, h_px),  # right vertical
        }

        for key, (x1, y1, x2, y2) in edges.items():
            item = self._world_extent_edge_items.get(key)
            if item is None:
                # Create new line item if missing
                pen = QPen(WORLD_EXTENT_RECT_COLOR)
                pen.setWidth(WORLD_EXTENT_RECT_PEN_WIDTH)
                pen.setCosmetic(True)
                item = QGraphicsLineItem(x1, y1, x2, y2)
                item.setPen(pen)
                item.setZValue(Z_WORLD_EXTENT_RECT)
                self.scene.addItem(item)
                self._world_extent_edge_items[key] = item
            else:
                # Update existing item geometry
                item.setLine(x1, y1, x2, y2)

        # Clear any stale hover state if dragging
        if self._extent_drag_edge:
            self._extent_hover_edge = None

    def stop_carla_bev_refresh(self):
        # print('[CARLA] stop_carla_bev_refresh: cancelling all in-flight tile threads')
        if self._carla_bev_bounds_rect_item is not None:
            self.scene.removeItem(self._carla_bev_bounds_rect_item)
            self._carla_bev_bounds_rect_item = None
        self._carla_bev_epoch += 1  # invalidate all in-flight tile threads immediately
        self._carla_bev_loading = False
        self._carla_bev_loaded_zoom = None
        self.lbl_carla_bev_status.setText('')
        self.carla_bev_item.setPixmap(QPixmap())
        if self.map_ctx:
            self.map_ctx.carla_bev_fetch_cancelled = True
        with self._carla_bev_fetch_lock:
            self._carla_bev_fetched_tiles = set()
            self._carla_bev_fetching_tiles = set()
            self._carla_bev_placeholder_tiles = set()
        self._carla_bev_vis_done = 0
        self._carla_bev_vis_processed = 0
        with self._carla_bev_pix_lock:
            self._carla_bev_pix_data = None

    def stop_esri_refresh(self):
        self._esri_epoch += 1  # invalidate all in-flight tile threads immediately
        self._esri_loading = False
        self._esri_loaded_zoom = None
        self.lbl_esri_status.setText('')
        self.esri_item.setPixmap(QPixmap())
        if self.map_ctx:
            self.map_ctx.esri_fetch_cancelled = True
        with self._esri_fetch_lock:
            self._esri_fetched_tiles = set()
            self._esri_fetching_tiles = set()
        with self._esri_pix_lock:
            self._esri_pix_data = None

    def _esri_do_repaint(self):
        """Throttled pixmap update — runs on main thread, at most once per 80 ms."""
        with self._esri_pix_lock:
            snapshot = (
                Image.fromarray(self._esri_pix_data.copy())
                if self._esri_pix_data is not None
                else None
            )
        if snapshot and self.esri_item:
            self.esri_item.setPixmap(self.pil_to_qpixmap(snapshot))
            self.update_imagery_alignment()

    def on_esri_refreshed(self, image, count, total, epoch):
        if epoch != self._esri_epoch:
            return  # stale signal from a cancelled/superseded load
        if count == -1:
            # Explicit stop/cancel
            self._esri_loading = False
            if not self._carla_bev_loading and not self.opendrive_loading:
                self.spinner_timer.stop()
            return
        # Recompute visible-tile counts on every tile arrival
        visible = self._esri_visible_tiles()
        with self._esri_fetch_lock:
            vis_done = len(visible & self._esri_fetched_tiles)
        self._esri_vis_done = vis_done
        self._esri_vis_total = len(visible)
        self._esri_update_status_label()
        # Schedule a single repaint — collapses many rapid tile signals into one update
        if not self._esri_repaint_timer.isActive():
            self._esri_repaint_timer.start(TILE_REPAINT_THROTTLE_MS)

    def on_tile_zoom_changed(self, zoom: int) -> None:
        """Re-fetch tiles at the new zoom level without touching the coordinate system."""
        if not self.map_ctx:
            return
        self._do_tile_refresh(force=False)

    def on_tile_zoom_refresh(self) -> None:
        """Button handler: refresh all layers including tiles, OSM signs and OpenDRIVE."""
        self.refresh_all_layers()

    def _retry_failed_tiles(self) -> None:
        """Re-fetch failed (placeholder) tiles without discarding successfully-loaded tiles.

        ESRI: full re-fetch (tiles are cached on disk so it is fast).
        CARLA: smart retry — only tiles that previously failed as placeholders are
               re-queued; already-painted pixels are left untouched.
        """
        # --- ESRI ---
        if self.check_esri.isChecked():
            self.refresh_esri()

        # --- CARLA ---
        if self.check_carla_bev.isChecked():
            with self._carla_bev_fetch_lock:
                placeholders = set(self._carla_bev_placeholder_tiles)
            if placeholders:
                # Smart retry: drop failed tiles from fetched set so they get re-queued
                with self._carla_bev_fetch_lock:
                    self._carla_bev_fetched_tiles -= placeholders
                    self._carla_bev_fetching_tiles -= placeholders
                    self._carla_bev_placeholder_tiles.clear()
                self._carla_bev_server_online = None  # re-probe server
                self._carla_bev_loading = True
                self._carla_bev_epoch += 1
                self.lbl_carla_bev_status.setText('Probing server...')
                self.spinner_timer.start(SPINNER_TIMER_INTERVAL_MS)
                epoch = self._carla_bev_epoch
                threading.Thread(
                    target=self._carla_bev_fetch_server_meta, args=(epoch,), daemon=True
                ).start()
            else:
                # No known failures → full refresh
                self._carla_bev_loaded_zoom = None
                self.on_carla_bev_zoom_refresh()

    def _do_tile_refresh(self, *, force: bool) -> None:
        """Invalidate and reload ESRI and carla_bev tile layers."""
        # --- ESRI ---
        if force:
            self._esri_loaded_zoom = None  # bypass the early-exit equality check
        self.on_esri_zoom_refresh()

        # --- Carla_Bev ---
        if force:
            self._carla_bev_loaded_zoom = None
        self.on_carla_bev_zoom_refresh()

    def on_esri_zoom_refresh(self):
        if (
            self.spin_esri_zoom.value() == self._esri_current_zoom
            and self._esri_pix_data is not None
        ):
            return
        # Bump epoch to invalidate all in-flight fetches for the old zoom
        self._esri_epoch += 1
        self._esri_loading = False
        self._esri_loaded_zoom = None
        self.lbl_esri_status.setText('')
        with self._esri_fetch_lock:
            self._esri_fetched_tiles = set()
            self._esri_fetching_tiles = set()
        with self._esri_pix_lock:
            self._esri_pix_data = None
        if self.esri_item:
            self.esri_item.setPixmap(QPixmap())
        if self.check_esri.isChecked():
            self.refresh_esri()

    def on_carla_bev_zoom_refresh(self):
        new_zoom = self.spin_carla_bev_zoom.value()
        if new_zoom == self._carla_bev_current_zoom and self._carla_bev_pix_data is not None:
            print(f'[CARLA] on_carla_bev_zoom_refresh: zoom unchanged ({new_zoom}), skipping')
            return
        print(
            f'[CARLA] on_carla_bev_zoom_refresh: zoom {self._carla_bev_current_zoom} → {new_zoom}'
        )
        # Bump epoch to invalidate all in-flight fetches for the old zoom
        self._carla_bev_epoch += 1
        self._carla_bev_loading = False
        self._carla_bev_loaded_zoom = None
        self.lbl_carla_bev_status.setText('')
        with self._carla_bev_fetch_lock:
            self._carla_bev_fetched_tiles = set()
            self._carla_bev_fetching_tiles = set()
            self._carla_bev_placeholder_tiles = set()
        self._carla_bev_vis_done = 0
        self._carla_bev_vis_processed = 0
        with self._carla_bev_pix_lock:
            self._carla_bev_pix_data = None
        if self.carla_bev_item:
            self.carla_bev_item.setPixmap(QPixmap())
        if self.check_carla_bev.isChecked():
            self.refresh_carla_bev()

    def update_refresh_spinner(self):
        self.spinner_angle = (self.spinner_angle + SPINNER_ANGLE_INCREMENT) % 360
        spin_char = '|/-\\|/-\\|/-\\'[self.spinner_angle // SPINNER_ANGLE_INCREMENT]
        if self._carla_bev_loading:
            self._carla_bev_update_status_label()
        if self._esri_loading:
            self._esri_update_status_label()
        if self.opendrive_loading:
            self.lbl_opendrive_status.setText('Loading...')
            pixmap = QPixmap(SPINNER_ICON_SIZE, SPINNER_ICON_SIZE)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(Qt.GlobalColor.black, 2))
            painter.drawArc(
                *SPINNER_ARC_RECT,
                self.spinner_angle * 16,
                SPINNER_ARC_SPAN_DEGREES * 16,
            )
            painter.end()
            self.btn_browse_xodr.setIcon(QIcon(pixmap))

    def closeEvent(self, event):
        has_unsaved_changes = self._osm_dirty
        if has_unsaved_changes:
            message = 'You have unsaved changes to the OSM layer. Do you want to save them to the project file before exiting?'
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
                # Save OSM edits to project file
                composed_osm = self._compose_current_osm_content()
                if composed_osm is not None:
                    self._osm_content = composed_osm
                    self._osm_edits.clear()
                    self._osm_node_tag_edits.clear()
                    self._osm_created_ways.clear()
                    self._osm_deleted_way_ids.clear()
                self.save_project()
                if self._osm_dirty:
                    event.ignore()
                    return
            elif reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return

        for k, v in self._collect_persistent_settings().items():
            self.settings.setValue(k, v)
        super().closeEvent(event)
