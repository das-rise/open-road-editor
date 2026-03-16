#!/usr/bin/env python3

"""Compatibility renderer module backed by ORBIT instead of libOpenDRIVE."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
import importlib.util
import math
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw


def _find_orbit_root() -> Path:
    this_file = Path(__file__).resolve()
    candidates = [
        # Current module location: open_road_editor/viewer/
        this_file.parent.parent / 'external' / 'ORBIT',
        # Backward-compatible fallback for previous location.
        this_file.parent / 'external' / 'ORBIT',
    ]
    for candidate in candidates:
        if (candidate / 'orbit' / 'import' / 'opendrive_parser.py').is_file():
            return candidate
    raise ImportError(
        'Could not locate ORBIT sources. Ensure open_road_editor/external/ORBIT is available.'
    )


_ORBIT_ROOT = _find_orbit_root()
_PARSER_PATH = _ORBIT_ROOT / 'orbit' / 'import' / 'opendrive_parser.py'
_PARSER_SPEC = importlib.util.spec_from_file_location(
    'ore_xodr_parser',
    str(_PARSER_PATH),
)
if _PARSER_SPEC is None or _PARSER_SPEC.loader is None:
    raise ImportError(f'Failed to load ORBIT parser from {_PARSER_PATH}')
_odr_parser_mod = importlib.util.module_from_spec(_PARSER_SPEC)
_PARSER_SPEC.loader.exec_module(_odr_parser_mod)
GeometryType = _odr_parser_mod.GeometryType
ODRLane = _odr_parser_mod.ODRLane
ODRRoad = _odr_parser_mod.ODRRoad
OpenDriveData = _odr_parser_mod.OpenDriveData
OpenDriveParser = _odr_parser_mod.OpenDriveParser


@dataclass
class LanePolygon:
    road_id: str
    lanesection_s0: float
    lane_id: int
    lane_type: str
    lane_key: str
    predecessor_key: str
    successor_key: str
    points: List[Tuple[float, float]]


@dataclass
class ReconstructedLane:
    road_id: str = ''
    lanesection_s0: float = 0.0
    lane_id: int = -1
    lane_type: str = 'driving'
    outer_edge: List[Tuple[float, float]] = None
    inner_edge: List[Tuple[float, float]] = None

    def __post_init__(self) -> None:
        if self.outer_edge is None:
            self.outer_edge = []
        if self.inner_edge is None:
            self.inner_edge = []


_LANE_TYPE_COLORS = {
    'sidewalk': (200, 200, 200, 255),
    'median': (160, 180, 160, 255),
    'shoulder': (170, 170, 170, 255),
    'parking': (180, 180, 200, 255),
    'border': (200, 100, 100, 255),
    'restricted': (200, 100, 100, 255),
    'green': (100, 200, 100, 255),
    'none': (128, 128, 128, 50),
}


def _angle_diff(a: float, b: float) -> float:
    d = (b - a + math.pi) % (2.0 * math.pi) - math.pi
    return d


def _edge_lengths(points: Sequence[Tuple[float, float]]) -> List[float]:
    out = [0.0]
    total = 0.0
    for i in range(len(points) - 1):
        total += math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
        out.append(total)
    return out


def _interpolate(
    p0: Tuple[float, float], p1: Tuple[float, float], t: float
) -> Tuple[float, float]:
    tt = max(0.0, min(1.0, float(t)))
    return (
        float(p0[0]) + (float(p1[0]) - float(p0[0])) * tt,
        float(p0[1]) + (float(p1[1]) - float(p0[1])) * tt,
    )


def _resample(
    points: Sequence[Tuple[float, float]], target_s: Sequence[float]
) -> List[Tuple[float, float]]:
    if not points:
        return []
    if len(points) == 1:
        return [tuple(points[0]) for _ in target_s]
    src_s = _edge_lengths(points)
    out: List[Tuple[float, float]] = []
    seg = 0
    for s in target_s:
        while seg + 1 < len(src_s) and src_s[seg + 1] < s:
            seg += 1
        if seg + 1 >= len(src_s):
            out.append(tuple(points[-1]))
            continue
        s0 = src_s[seg]
        s1 = src_s[seg + 1]
        if s1 - s0 <= 1e-9:
            out.append(tuple(points[seg]))
            continue
        out.append(_interpolate(points[seg], points[seg + 1], (s - s0) / (s1 - s0)))
    return out


def _left_right_edges(
    outer: Sequence[Tuple[float, float]],
    inner: Sequence[Tuple[float, float]],
) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    left = list(outer)
    right = list(inner)
    count = min(len(left), len(right))
    if count < 2:
        return left, right

    tangents: List[Tuple[float, float]] = []
    for i in range(count - 1):
        mx0 = 0.5 * (left[i][0] + right[i][0])
        my0 = 0.5 * (left[i][1] + right[i][1])
        mx1 = 0.5 * (left[i + 1][0] + right[i + 1][0])
        my1 = 0.5 * (left[i + 1][1] + right[i + 1][1])
        tangents.append((mx1 - mx0, my1 - my0))

    cross_sum = 0.0
    used = 0
    for i in range(min(len(tangents), count)):
        tx, ty = tangents[min(i, len(tangents) - 1)]
        vx = left[i][0] - right[i][0]
        vy = left[i][1] - right[i][1]
        if abs(tx) <= 1e-9 and abs(ty) <= 1e-9:
            continue
        cross_sum += tx * vy - ty * vx
        used += 1

    # Positive means left->right orientation already.
    if used <= 0 or cross_sum >= 0.0:
        return left, right
    return right, left


def reconstruct_xodr_from_lanes(
    reconstructed_lanes: Sequence[ReconstructedLane],
    geo_reference: str = '',
    header_name: str = 'OpenRoadEditor Baked Export',
    header_version: str = '1.00',
    header_date: str = '',
) -> str:
    root = ET.Element('OpenDRIVE')
    header = ET.SubElement(root, 'header')
    header.set('revMajor', '1')
    header.set('revMinor', '4')
    header.set('name', str(header_name or 'OpenRoadEditor Baked Export'))
    header.set('version', str(header_version or '1.00'))
    header.set('date', str(header_date or ''))
    if geo_reference:
        geo = ET.SubElement(header, 'geoReference')
        geo.text = str(geo_reference)

    bounds_x: List[float] = []
    bounds_y: List[float] = []

    road_id_counter = 1
    for lane in reconstructed_lanes:
        outer = [(float(x), float(y)) for x, y in (lane.outer_edge or [])]
        inner = [(float(x), float(y)) for x, y in (lane.inner_edge or [])]
        if len(outer) < 2 or len(inner) < 2:
            continue

        left_edge, right_edge = _left_right_edges(outer, inner)
        ref_points = list(left_edge)
        s_vals = _edge_lengths(ref_points)
        length = float(s_vals[-1]) if s_vals else 0.0
        if length <= 1e-9:
            continue
        right_samples = _resample(right_edge, s_vals)
        widths = [
            math.hypot(
                ref_points[i][0] - right_samples[i][0],
                ref_points[i][1] - right_samples[i][1],
            )
            for i in range(len(ref_points))
        ]

        road = ET.SubElement(root, 'road')
        road.set('name', f'{lane.road_id}/{lane.lanesection_s0:.6f}/{lane.lane_id}')
        road.set('length', f'{length:.6f}')
        road.set('id', str(road_id_counter))
        road.set('junction', '-1')
        ET.SubElement(road, 'link')

        plan_view = ET.SubElement(road, 'planView')
        accum_s = 0.0
        for i in range(len(ref_points) - 1):
            x0, y0 = ref_points[i]
            x1, y1 = ref_points[i + 1]
            dx = x1 - x0
            dy = y1 - y0
            seg_len = math.hypot(dx, dy)
            if seg_len <= 1e-9:
                continue
            geom = ET.SubElement(plan_view, 'geometry')
            geom.set('s', f'{accum_s:.6f}')
            geom.set('x', f'{x0:.6f}')
            geom.set('y', f'{y0:.6f}')
            geom.set('hdg', f'{math.atan2(dy, dx):.12f}')
            geom.set('length', f'{seg_len:.6f}')
            ET.SubElement(geom, 'line')
            accum_s += seg_len

        lanes = ET.SubElement(road, 'lanes')
        lane_offset = ET.SubElement(lanes, 'laneOffset')
        lane_offset.set('s', '0')
        lane_offset.set('a', '0')
        lane_offset.set('b', '0')
        lane_offset.set('c', '0')
        lane_offset.set('d', '0')

        section = ET.SubElement(lanes, 'laneSection')
        section.set('s', '0')

        center = ET.SubElement(section, 'center')
        center_lane = ET.SubElement(center, 'lane')
        center_lane.set('id', '0')
        center_lane.set('type', 'none')
        center_lane.set('level', 'true')
        ET.SubElement(center_lane, 'link')

        right = ET.SubElement(section, 'right')
        right_lane = ET.SubElement(right, 'lane')
        right_lane.set('id', '-1')
        right_lane.set('type', str(lane.lane_type or 'driving'))
        right_lane.set('level', 'true')
        ET.SubElement(right_lane, 'link')

        for i in range(len(widths) - 1):
            ds = s_vals[i + 1] - s_vals[i]
            width = ET.SubElement(right_lane, 'width')
            width.set('sOffset', f'{s_vals[i]:.6f}')
            width.set('a', f'{widths[i]:.6f}')
            width.set('b', f'{((widths[i + 1] - widths[i]) / ds) if ds > 1e-9 else 0.0:.12f}')
            width.set('c', '0')
            width.set('d', '0')
        if len(widths) == 1:
            width = ET.SubElement(right_lane, 'width')
            width.set('sOffset', '0')
            width.set('a', f'{widths[0]:.6f}')
            width.set('b', '0')
            width.set('c', '0')
            width.set('d', '0')

        rm = ET.SubElement(right_lane, 'roadMark')
        rm.set('sOffset', '0')
        rm.set('type', 'solid')
        rm.set('weight', 'standard')
        rm.set('color', 'standard')
        rm.set('width', '0.13')

        bounds_x.extend([x for x, _ in outer] + [x for x, _ in inner])
        bounds_y.extend([y for _, y in outer] + [y for _, y in inner])
        road_id_counter += 1

    if bounds_x and bounds_y:
        header.set('west', f'{min(bounds_x):.6f}')
        header.set('east', f'{max(bounds_x):.6f}')
        header.set('south', f'{min(bounds_y):.6f}')
        header.set('north', f'{max(bounds_y):.6f}')

    return ET.tostring(root, encoding='unicode')


class OpenDriveRenderer:
    def __init__(self) -> None:
        self._data: OpenDriveData | None = None
        self._roads_by_id: Dict[str, ODRRoad] = {}
        self._road_samples: Dict[str, List[Tuple[float, float, float, float]]] = {}
        self._road_sample_s: Dict[str, List[float]] = {}

    def load_map(self, xodr_path: str) -> bool:
        try:
            parser = OpenDriveParser()
            self._data = parser.parse_file(xodr_path)
            self._roads_by_id = {road.id: road for road in self._data.roads}
            self._road_samples.clear()
            self._road_sample_s.clear()
            for road in self._data.roads:
                samples = self._sample_road_centerline(road, 1.0)
                if not samples:
                    continue
                self._road_samples[road.id] = samples
                self._road_sample_s[road.id] = [s for s, _, _, _ in samples]
            return True
        except Exception:
            self._data = None
            self._roads_by_id = {}
            self._road_samples = {}
            self._road_sample_s = {}
            return False

    def render(
        self,
        min_x: float,
        min_y: float,
        width_px: int,
        height_px: int,
        mpp: float,
        r: int = 0,
        g: int = 0,
        b: int = 0,
        draw_signals: bool = True,
        draw_objects: bool = True,
    ) -> np.ndarray:
        if self._data is None:
            return np.zeros((0, 0, 4), dtype=np.uint8)

        image = Image.new('RGBA', (int(width_px), int(height_px)), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image, 'RGBA')
        default_lane_color = (int(r), int(g), int(b), 255)

        polygons = self.get_lane_polygons(2.0)
        for poly in polygons:
            lane_color = _LANE_TYPE_COLORS.get(poly.lane_type, default_lane_color)
            pixel_points = [((x - min_x) / mpp, (y - min_y) / mpp) for x, y in poly.points]
            if len(pixel_points) >= 3:
                draw.polygon(pixel_points, fill=lane_color)

        if draw_signals:
            for road in self._data.roads:
                for signal in road.signals:
                    pose = self._pose_on_road(road.id, signal.s, signal.t)
                    if pose is None:
                        continue
                    xw, yw = pose
                    px = (xw - min_x) / mpp
                    py = (yw - min_y) / mpp
                    rad = max(1, int(1.0 / max(mpp, 1e-9)))
                    draw.ellipse(
                        (px - rad, py - rad, px + rad, py + rad),
                        fill=(255, 200, 10, 255),
                    )

        if draw_objects:
            for road in self._data.roads:
                for obj in road.objects:
                    pose = self._pose_on_road(road.id, obj.s, obj.t)
                    if pose is None:
                        continue
                    xw, yw = pose
                    px = (xw - min_x) / mpp
                    py = (yw - min_y) / mpp
                    rad = max(1, int(0.5 / max(mpp, 1e-9)))
                    draw.ellipse(
                        (px - rad, py - rad, px + rad, py + rad),
                        fill=(255, 255, 255, 220),
                    )

        return np.array(image, dtype=np.uint8)

    def get_lane_polygons(self, s_step: float = 2.0) -> List[LanePolygon]:
        if self._data is None:
            return []

        out: List[LanePolygon] = []
        for road in self._data.roads:
            road_samples = self._sample_road_centerline(road, max(float(s_step), 0.25))
            if len(road_samples) < 2:
                continue

            sections = sorted(road.lane_sections, key=lambda sec: sec.s)
            for idx, section in enumerate(sections):
                s0 = float(section.s)
                s1 = float(sections[idx + 1].s) if idx + 1 < len(sections) else float(road.length)
                section_samples = self._section_centerline_samples(road.id, s0, s1)
                if len(section_samples) < 2:
                    continue

                left_lanes = sorted(section.left_lanes, key=lambda lane: lane.id)
                right_lanes = sorted(section.right_lanes, key=lambda lane: abs(lane.id))

                out.extend(
                    self._build_section_lane_polygons(
                        road=road,
                        section_index=idx,
                        section_s0=s0,
                        section_s1=s1,
                        section_samples=section_samples,
                        lanes=left_lanes,
                        side='left',
                    )
                )
                out.extend(
                    self._build_section_lane_polygons(
                        road=road,
                        section_index=idx,
                        section_s0=s0,
                        section_s1=s1,
                        section_samples=section_samples,
                        lanes=right_lanes,
                        side='right',
                    )
                )
        return out

    def _build_section_lane_polygons(
        self,
        road: ODRRoad,
        section_index: int,
        section_s0: float,
        section_s1: float,
        section_samples: Sequence[Tuple[float, float, float, float]],
        lanes: Sequence[ODRLane],
        side: str,
    ) -> List[LanePolygon]:
        if not lanes:
            return []

        out: List[LanePolygon] = []
        for lane_idx, lane in enumerate(lanes):
            outer_points: List[Tuple[float, float]] = []
            inner_points: List[Tuple[float, float]] = []
            inner_lanes = lanes[:lane_idx]

            for s, x, y, hdg in section_samples:
                ds = max(0.0, min(s - section_s0, section_s1 - section_s0))
                lane_offset = self._lane_offset_at_s(road, s)
                inner_width_sum = sum(
                    self._lane_width_at_s(inner_lane, ds) for inner_lane in inner_lanes
                )
                lane_width = self._lane_width_at_s(lane, ds)

                if side == 'left':
                    inner_off = lane_offset + inner_width_sum
                    outer_off = inner_off + lane_width
                else:
                    inner_off = lane_offset - inner_width_sum
                    outer_off = inner_off - lane_width

                px, py = -math.sin(hdg), math.cos(hdg)
                ix = x + px * inner_off
                iy = y + py * inner_off
                ox = x + px * outer_off
                oy = y + py * outer_off
                inner_points.append((ix, -iy))
                outer_points.append((ox, -oy))

            if len(outer_points) < 2 or len(inner_points) < 2:
                continue

            lane_key = self._lane_key(road.id, section_s0, lane.id)
            predecessor_key = self._connected_lane_key(road, section_index, lane, predecessor=True)
            successor_key = self._connected_lane_key(road, section_index, lane, predecessor=False)

            out.append(
                LanePolygon(
                    road_id=road.id,
                    lanesection_s0=section_s0,
                    lane_id=lane.id,
                    lane_type=str(lane.type or 'driving'),
                    lane_key=lane_key,
                    predecessor_key=predecessor_key,
                    successor_key=successor_key,
                    points=outer_points + list(reversed(inner_points)),
                )
            )
        return out

    def _connected_lane_key(
        self,
        road: ODRRoad,
        section_index: int,
        lane: ODRLane,
        predecessor: bool,
    ) -> str:
        link_lane_id = None
        if lane.link is not None:
            link_lane_id = lane.link.predecessor_id if predecessor else lane.link.successor_id
        if link_lane_id is None:
            return ''

        sections = sorted(road.lane_sections, key=lambda sec: sec.s)
        if predecessor:
            if section_index > 0:
                return self._lane_key(road.id, sections[section_index - 1].s, int(link_lane_id))
            if road.predecessor_type == 'road' and road.predecessor_id in self._roads_by_id:
                pred_road = self._roads_by_id[road.predecessor_id]
                pred_sections = sorted(pred_road.lane_sections, key=lambda sec: sec.s)
                if not pred_sections:
                    return ''
                use_first = str(road.predecessor_contact or '').strip().lower() == 'start'
                target_s0 = pred_sections[0].s if use_first else pred_sections[-1].s
                return self._lane_key(pred_road.id, target_s0, int(link_lane_id))
            return ''

        if section_index + 1 < len(sections):
            return self._lane_key(road.id, sections[section_index + 1].s, int(link_lane_id))
        if road.successor_type == 'road' and road.successor_id in self._roads_by_id:
            succ_road = self._roads_by_id[road.successor_id]
            succ_sections = sorted(succ_road.lane_sections, key=lambda sec: sec.s)
            if not succ_sections:
                return ''
            use_last = str(road.successor_contact or '').strip().lower() == 'end'
            target_s0 = succ_sections[-1].s if use_last else succ_sections[0].s
            return self._lane_key(succ_road.id, target_s0, int(link_lane_id))
        return ''

    @staticmethod
    def _lane_key(road_id: str, section_s0: float, lane_id: int) -> str:
        return f'{road_id}/{float(section_s0):.6f}/{int(lane_id)}'

    @staticmethod
    def _lane_offset_at_s(road: ODRRoad, s: float) -> float:
        if road.lane_offset is None or not road.lane_offset.offsets:
            return 0.0
        coeff = road.lane_offset.offsets[0]
        for cand in road.lane_offset.offsets:
            if s >= cand[0]:
                coeff = cand
            else:
                break
        s0, a, b, c, d = coeff
        ds = max(0.0, float(s) - float(s0))
        return a + b * ds + c * ds * ds + d * ds * ds * ds

    @staticmethod
    def _lane_width_at_s(lane: ODRLane, ds: float) -> float:
        if not lane.widths:
            return 0.0 if lane.id == 0 else 3.5
        width_rec = lane.widths[0]
        for cand in lane.widths:
            if ds >= cand.s_offset:
                width_rec = cand
            else:
                break
        local_ds = max(0.0, float(ds) - float(width_rec.s_offset))
        return max(0.0, float(width_rec.get_width_at(local_ds)))

    def _pose_on_road(self, road_id: str, s: float, t: float) -> Tuple[float, float] | None:
        samples = self._road_samples.get(road_id)
        s_values = self._road_sample_s.get(road_id)
        if not samples or not s_values:
            return None

        target_s = float(s)
        idx = bisect.bisect_left(s_values, target_s)
        if idx <= 0:
            _, x, y, hdg = samples[0]
        elif idx >= len(samples):
            _, x, y, hdg = samples[-1]
        else:
            s0, x0, y0, h0 = samples[idx - 1]
            s1, x1, y1, h1 = samples[idx]
            if abs(s1 - s0) < 1e-9:
                ratio = 0.0
            else:
                ratio = (target_s - s0) / (s1 - s0)
            x = x0 + (x1 - x0) * ratio
            y = y0 + (y1 - y0) * ratio
            hdg = h0 + _angle_diff(h0, h1) * ratio

        px, py = -math.sin(hdg), math.cos(hdg)
        wx = x + px * float(t)
        wy = y + py * float(t)
        return (wx, -wy)

    def _road_state_at_s(
        self, road_id: str, target_s: float
    ) -> Tuple[float, float, float, float] | None:
        samples = self._road_samples.get(road_id)
        s_values = self._road_sample_s.get(road_id)
        if not samples or not s_values:
            return None

        s_target = float(target_s)
        idx = bisect.bisect_left(s_values, s_target)
        if idx <= 0:
            _, x, y, hdg = samples[0]
            return (s_target, x, y, hdg)
        if idx >= len(samples):
            _, x, y, hdg = samples[-1]
            return (s_target, x, y, hdg)

        s0, x0, y0, h0 = samples[idx - 1]
        s1, x1, y1, h1 = samples[idx]
        if abs(s1 - s0) < 1e-9:
            ratio = 0.0
        else:
            ratio = (s_target - s0) / (s1 - s0)
        x = x0 + (x1 - x0) * ratio
        y = y0 + (y1 - y0) * ratio
        hdg = h0 + _angle_diff(h0, h1) * ratio
        return (s_target, x, y, hdg)

    def _section_centerline_samples(
        self, road_id: str, s0: float, s1: float
    ) -> List[Tuple[float, float, float, float]]:
        road_samples = self._road_samples.get(road_id) or []
        interior = [
            sample
            for sample in road_samples
            if (sample[0] > float(s0) + 1e-6 and sample[0] < float(s1) - 1e-6)
        ]
        out: List[Tuple[float, float, float, float]] = []
        start_sample = self._road_state_at_s(road_id, s0)
        end_sample = self._road_state_at_s(road_id, s1)
        if start_sample is not None:
            out.append(start_sample)
        out.extend(interior)
        if end_sample is not None and (not out or abs(end_sample[0] - out[-1][0]) > 1e-9):
            out.append(end_sample)
        return out

    def _sample_road_centerline(
        self, road: ODRRoad, step_m: float
    ) -> List[Tuple[float, float, float, float]]:
        samples: List[Tuple[float, float, float, float]] = []
        geometry = sorted(road.geometry, key=lambda geom: geom.s)
        step = max(0.1, float(step_m))

        for geom in geometry:
            if geom.length <= 1e-9:
                continue
            n_steps = max(2, int(math.ceil(geom.length / step)) + 1)
            for i in range(n_steps):
                local_s = min(geom.length, i * geom.length / (n_steps - 1))
                x, y, hdg = self._sample_geometry_at(geom, local_s)
                s = float(geom.s) + float(local_s)
                if s > road.length + 1e-6:
                    continue
                if samples and abs(s - samples[-1][0]) < 1e-9:
                    continue
                samples.append((s, x, y, hdg))
        return samples

    def _sample_geometry_at(self, geom, local_s: float) -> Tuple[float, float, float]:
        x0 = float(geom.x)
        y0 = float(geom.y)
        hdg0 = float(geom.hdg)
        s = float(local_s)

        if geom.geometry_type == GeometryType.LINE:
            x = x0 + s * math.cos(hdg0)
            y = y0 + s * math.sin(hdg0)
            return (x, y, hdg0)

        if geom.geometry_type == GeometryType.ARC:
            curvature = float(geom.params.get('curvature', 0.0))
            if abs(curvature) < 1e-9:
                x = x0 + s * math.cos(hdg0)
                y = y0 + s * math.sin(hdg0)
                return (x, y, hdg0)
            radius = 1.0 / curvature
            theta = s * curvature
            dx_local = radius * math.sin(theta)
            dy_local = radius * (1.0 - math.cos(theta))
            x = x0 + math.cos(hdg0) * dx_local - math.sin(hdg0) * dy_local
            y = y0 + math.sin(hdg0) * dx_local + math.cos(hdg0) * dy_local
            return (x, y, hdg0 + theta)

        if geom.geometry_type == GeometryType.SPIRAL:
            curv_start = float(geom.params.get('curvStart', 0.0))
            curv_end = float(geom.params.get('curvEnd', 0.0))
            length = max(float(geom.length), 1e-9)
            curv_rate = (curv_end - curv_start) / length
            n = max(10, int(s / 0.2) + 1)
            ds = s / n if n > 0 else 0.0
            lx = 0.0
            ly = 0.0
            heading = 0.0
            for _ in range(n):
                lx += math.cos(heading) * ds
                ly += math.sin(heading) * ds
                heading += (curv_start + curv_rate * (_ * ds)) * ds
            x = x0 + math.cos(hdg0) * lx - math.sin(hdg0) * ly
            y = y0 + math.sin(hdg0) * lx + math.cos(hdg0) * ly
            hdg = hdg0 + curv_start * s + 0.5 * curv_rate * s * s
            return (x, y, hdg)

        if geom.geometry_type == GeometryType.POLY3:
            a = float(geom.params.get('a', 0.0))
            b = float(geom.params.get('b', 0.0))
            c = float(geom.params.get('c', 0.0))
            d = float(geom.params.get('d', 0.0))
            v = s
            u = a + b * v + c * v * v + d * v * v * v
            du = b + 2.0 * c * v + 3.0 * d * v * v
            x = x0 + math.cos(hdg0) * v - math.sin(hdg0) * u
            y = y0 + math.sin(hdg0) * v + math.cos(hdg0) * u
            hdg = hdg0 + math.atan2(du, 1.0)
            return (x, y, hdg)

        if geom.geometry_type == GeometryType.PARAM_POLY3:
            p_range = str(geom.params.get('pRange', 'arcLength')).strip().lower()
            p = s / max(float(geom.length), 1e-9) if p_range == 'normalized' else s
            a_u = float(geom.params.get('aU', 0.0))
            b_u = float(geom.params.get('bU', 0.0))
            c_u = float(geom.params.get('cU', 0.0))
            d_u = float(geom.params.get('dU', 0.0))
            a_v = float(geom.params.get('aV', 0.0))
            b_v = float(geom.params.get('bV', 0.0))
            c_v = float(geom.params.get('cV', 0.0))
            d_v = float(geom.params.get('dV', 0.0))
            u = a_u + b_u * p + c_u * p * p + d_u * p * p * p
            v = a_v + b_v * p + c_v * p * p + d_v * p * p * p
            du = b_u + 2.0 * c_u * p + 3.0 * d_u * p * p
            dv = b_v + 2.0 * c_v * p + 3.0 * d_v * p * p
            x = x0 + math.cos(hdg0) * u - math.sin(hdg0) * v
            y = y0 + math.sin(hdg0) * u + math.cos(hdg0) * v
            hdg = hdg0 + math.atan2(dv, du)
            return (x, y, hdg)

        x = x0 + s * math.cos(hdg0)
        y = y0 + s * math.sin(hdg0)
        return (x, y, hdg0)
