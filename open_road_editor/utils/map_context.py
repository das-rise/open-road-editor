"""MapContext: lightweight metadata container for tile assembly."""

import math
import xml.etree.ElementTree as ET

from open_road_editor.constants import (
    DEFAULT_CANVAS_SIZE_PX,
    DEFAULT_REF_LAT,
    DEFAULT_REF_LON,
    METERS_PER_DEGREE_LAT,
)


class MapContext:
    """Helper class to provide metadata for tile assembly without CARLA connection."""

    def __init__(self, metadata):
        # Metadata values from server
        self.world_offset = metadata.get('world_offset', [0, 0])
        self.mpp = metadata.get('mpp', 1.0)
        self.min_meters_per_pixel = self.mpp
        self.tile_max_zoom_level = metadata.get('tile_max_zoom_level', 22)
        self.earth_ref_lat = metadata.get('ref_lat', DEFAULT_REF_LAT)
        self.earth_ref_lon = metadata.get('ref_lon', DEFAULT_REF_LON)
        self.world_bounds = metadata.get('world_bounds', [0, 0, 0, 0])
        self.width_in_pixels = metadata.get('width_px', DEFAULT_CANVAS_SIZE_PX)
        self.height_in_pixels = metadata.get('height_px', DEFAULT_CANVAS_SIZE_PX)

        self.meters_per_degree_lat = METERS_PER_DEGREE_LAT
        # Transverse Mercator false easting/northing from the XODR geoReference PROJ string.
        # Default 0 matches CARLA custom maps that omit +x_0/+y_0.
        self.proj_false_easting: float = float(metadata.get('proj_false_easting', 0.0))
        self.proj_false_northing: float = float(metadata.get('proj_false_northing', 0.0))
        # Scale factor at the central meridian (+k / +k_0); default 1.0.
        self.proj_scale_factor: float = float(metadata.get('proj_scale_factor', 1.0))
        # Mock Carla location object for the origin offset as expected by the logic
        self.carla_world_origin_offset = type('Offset', (), {'x': 0.0, 'y': 0.0})()

        # Tiles/Images
        self.carla_bev_image = None
        self.opendrive_image = None
        self.esri_image = None

        # Flags for cancellation
        self.carla_bev_fetch_cancelled = False
        self.esri_fetch_cancelled = False
        self.opendrive_fetch_cancelled = False

    def carla_to_earth_transform(self, x: float, y: float):
        """Minimal implementation of coordinate transform."""
        m_per_deg_lat = self.meters_per_degree_lat
        ref_lat = self.earth_ref_lat
        ref_lon = self.earth_ref_lon
        off_x = self.carla_world_origin_offset.x
        off_y = self.carla_world_origin_offset.y

        lon = ((x - off_x) / (m_per_deg_lat * math.cos(math.radians(ref_lat)))) + ref_lon
        lat = ref_lat - ((y - off_y) / m_per_deg_lat)
        return lon, lat

    @staticmethod
    def parse_xodr_bounds(xodr_path):
        """Extract bounds from XODR, preferring header extents when available.

        Coordinate convention: OpenDRIVE Y is converted to CARLA/viewer Y via
        ``carla_y = -od_y``.
        """
        try:
            tree = ET.parse(xodr_path)
            root = tree.getroot()

            # 1) Prefer exact header bounds when present.
            header = root.find('header')
            if header is not None:
                west = header.get('west')
                east = header.get('east')
                south = header.get('south')
                north = header.get('north')
                if None not in (west, east, south, north):
                    try:
                        od_west = float(west)
                        od_east = float(east)
                        od_south = float(south)
                        od_north = float(north)
                        # OpenDRIVE -> CARLA/viewer Y flip
                        carla_min_y = -od_north
                        carla_max_y = -od_south
                        return [od_west, od_east, carla_min_y, carla_max_y]
                    except Exception:
                        pass

            # 2) Fallback: sample planView geometries along s for tighter bounds.
            min_x, max_x = float('inf'), float('-inf')
            min_y, max_y = float('inf'), float('-inf')

            found = False
            for road in root.findall('road'):
                plan_view = road.find('planView')
                if plan_view is not None:
                    for geom in plan_view.findall('geometry'):
                        try:
                            g_s = float(geom.get('s', '0') or '0')
                            g_x = float(geom.get('x', '0') or '0')
                            g_y = float(geom.get('y', '0') or '0')
                            g_hdg = float(geom.get('hdg', '0') or '0')
                            g_len = float(geom.get('length', '0') or '0')
                        except Exception:
                            continue
                        if g_len <= 1e-9:
                            continue

                        def _eval_geom_at(s_abs: float):
                            ds = max(0.0, min(float(s_abs) - g_s, g_len))

                            if geom.find('line') is not None:
                                return (
                                    g_x + ds * math.cos(g_hdg),
                                    g_y + ds * math.sin(g_hdg),
                                )

                            arc_el = geom.find('arc')
                            if arc_el is not None:
                                try:
                                    curvature = float(arc_el.get('curvature', '0') or '0')
                                except Exception:
                                    curvature = 0.0
                                if abs(curvature) <= 1e-9:
                                    return (
                                        g_x + ds * math.cos(g_hdg),
                                        g_y + ds * math.sin(g_hdg),
                                    )
                                radius = 1.0 / curvature
                                theta = ds * curvature
                                dx_local = radius * math.sin(theta)
                                dy_local = radius * (1.0 - math.cos(theta))
                                return (
                                    g_x + math.cos(g_hdg) * dx_local - math.sin(g_hdg) * dy_local,
                                    g_y + math.sin(g_hdg) * dx_local + math.cos(g_hdg) * dy_local,
                                )

                            spiral_el = geom.find('spiral')
                            if spiral_el is not None:
                                try:
                                    curv_start = float(spiral_el.get('curvStart', '0') or '0')
                                except Exception:
                                    curv_start = 0.0
                                try:
                                    curv_end = float(spiral_el.get('curvEnd', '0') or '0')
                                except Exception:
                                    curv_end = curv_start
                                curv_rate = (curv_end - curv_start) / max(g_len, 1e-9)
                                n_steps = max(10, int(ds / 0.2) + 1)
                                step = ds / float(n_steps) if n_steps > 0 else 0.0
                                lx = 0.0
                                ly = 0.0
                                heading = 0.0
                                for step_idx in range(n_steps):
                                    lx += math.cos(heading) * step
                                    ly += math.sin(heading) * step
                                    heading += (curv_start + curv_rate * (step_idx * step)) * step
                                return (
                                    g_x + math.cos(g_hdg) * lx - math.sin(g_hdg) * ly,
                                    g_y + math.sin(g_hdg) * lx + math.cos(g_hdg) * ly,
                                )

                            poly3_el = geom.find('poly3')
                            if poly3_el is not None:
                                try:
                                    a = float(poly3_el.get('a', '0') or '0')
                                    b = float(poly3_el.get('b', '0') or '0')
                                    c = float(poly3_el.get('c', '0') or '0')
                                    d = float(poly3_el.get('d', '0') or '0')
                                except Exception:
                                    a = b = c = d = 0.0
                                v = ds
                                u = a + b * v + c * v * v + d * v * v * v
                                return (
                                    g_x + math.cos(g_hdg) * v - math.sin(g_hdg) * u,
                                    g_y + math.sin(g_hdg) * v + math.cos(g_hdg) * u,
                                )

                            param_poly3_el = geom.find('paramPoly3')
                            if param_poly3_el is not None:
                                p_range = (
                                    str(param_poly3_el.get('pRange', 'arcLength') or 'arcLength')
                                    .strip()
                                    .lower()
                                )
                                p = ds / max(g_len, 1e-9) if p_range == 'normalized' else ds
                                try:
                                    a_u = float(param_poly3_el.get('aU', '0') or '0')
                                    b_u = float(param_poly3_el.get('bU', '0') or '0')
                                    c_u = float(param_poly3_el.get('cU', '0') or '0')
                                    d_u = float(param_poly3_el.get('dU', '0') or '0')
                                    a_v = float(param_poly3_el.get('aV', '0') or '0')
                                    b_v = float(param_poly3_el.get('bV', '0') or '0')
                                    c_v = float(param_poly3_el.get('cV', '0') or '0')
                                    d_v = float(param_poly3_el.get('dV', '0') or '0')
                                except Exception:
                                    a_u = b_u = c_u = d_u = 0.0
                                    a_v = b_v = c_v = d_v = 0.0
                                u = a_u + b_u * p + c_u * p * p + d_u * p * p * p
                                v = a_v + b_v * p + c_v * p * p + d_v * p * p * p
                                return (
                                    g_x + math.cos(g_hdg) * u - math.sin(g_hdg) * v,
                                    g_y + math.sin(g_hdg) * u + math.cos(g_hdg) * v,
                                )

                            return (
                                g_x + ds * math.cos(g_hdg),
                                g_y + ds * math.sin(g_hdg),
                            )

                        sample_step = max(0.5, min(5.0, g_len / 20.0))
                        s_val = g_s
                        while s_val < g_s + g_len:
                            x, y = _eval_geom_at(s_val)
                            min_x = min(min_x, x)
                            max_x = max(max_x, x)
                            carla_y = -y
                            min_y = min(min_y, carla_y)
                            max_y = max(max_y, carla_y)
                            found = True
                            s_val += sample_step

                        x_end, y_end = _eval_geom_at(g_s + g_len)
                        min_x = min(min_x, x_end)
                        max_x = max(max_x, x_end)
                        carla_y_end = -y_end
                        min_y = min(min_y, carla_y_end)
                        max_y = max(max_y, carla_y_end)
                        found = True

            if found:
                return [min_x, max_x, min_y, max_y]
        except Exception as e:
            print(f'Failed to parse XODR bounds: {e}')
        return None

    @staticmethod
    def parse_xodr_georef(xodr_path):
        """Extract Transverse Mercator parameters from the XODR <geoReference> PROJ string.
        Returns (ref_lat, ref_lon, x0, y0, k0) floats or None."""
        import re

        try:
            tree = ET.parse(xodr_path)
            root = tree.getroot()
            header = root.find('header')
            if header is None:
                return None
            geo_el = header.find('geoReference')
            if geo_el is None:
                return None
            proj_str = (geo_el.text or '').strip()
            lat_m = re.search(r'\+lat_0=([+-]?[\d.]+)', proj_str)
            lon_m = re.search(r'\+lon_0=([+-]?[\d.]+)', proj_str)
            if lat_m and lon_m:
                x0_m = re.search(r'\+x_0=([+-]?[\d.]+)', proj_str)
                y0_m = re.search(r'\+y_0=([+-]?[\d.]+)', proj_str)
                # Scale factor: PROJ accepts +k= or +k_0=
                k_m = re.search(r'\+k(?:_0)?=([+-]?[\d.]+)', proj_str)
                x0 = float(x0_m.group(1)) if x0_m else 0.0
                y0 = float(y0_m.group(1)) if y0_m else 0.0
                k0 = float(k_m.group(1)) if k_m else 1.0
                return float(lat_m.group(1)), float(lon_m.group(1)), x0, y0, k0
        except Exception as e:
            print(f'Failed to parse XODR geoReference: {e}')
        return None
