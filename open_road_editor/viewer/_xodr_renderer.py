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
        this_file.parent.parent / "external" / "ORBIT",
        # Backward-compatible fallback for previous location.
        this_file.parent / "external" / "ORBIT",
    ]
    for candidate in candidates:
        if (candidate / "orbit" / "import" / "opendrive_parser.py").is_file():
            return candidate
    raise ImportError(
        "Could not locate ORBIT sources. Ensure open_road_editor/external/ORBIT is available."
    )


_ORBIT_ROOT = _find_orbit_root()
_PARSER_PATH = _ORBIT_ROOT / "orbit" / "import" / "opendrive_parser.py"
_PARSER_SPEC = importlib.util.spec_from_file_location(
    "ore_xodr_parser",
    str(_PARSER_PATH),
)
if _PARSER_SPEC is None or _PARSER_SPEC.loader is None:
    raise ImportError(f"Failed to load ORBIT parser from {_PARSER_PATH}")
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
    # Number of points belonging to the outer edge (first slice of ``points``).
    # The remaining points are the inner edge in reverse order.
    outer_point_count: int = 0


@dataclass
class ReconstructedLane:
    road_id: str = ""
    lanesection_s0: float = 0.0
    lane_id: int = -1
    lane_type: str = "driving"
    outer_edge: List[Tuple[float, float]] = None
    inner_edge: List[Tuple[float, float]] = None

    def __post_init__(self) -> None:
        if self.outer_edge is None:
            self.outer_edge = []
        if self.inner_edge is None:
            self.inner_edge = []


# Lane types that make up the drivable road surface (used for marking union).
_ROAD_SURFACE_TYPES: frozenset = frozenset(
    {"driving", "border", "shoulder", "parking", "restricted"}
)
_ROAD_MARK_WIDTH_M = "0.13"
_DEFAULT_BIDIRECTIONAL_CENTERLINE_STYLE = "WhiteBroken"
_BIDIRECTIONAL_REFERENCE_MATCH_TOLERANCE_M = 2.0
_BIDIRECTIONAL_BROKEN_LINE_LENGTH_M = 3.0
_BIDIRECTIONAL_BROKEN_LINE_GAP_M = 6.0

_LANE_TYPE_COLORS = {
    "sidewalk": (200, 200, 200, 255),
    "median": (160, 180, 160, 255),
    "shoulder": (170, 170, 170, 255),
    "parking": (180, 180, 200, 255),
    "border": (200, 100, 100, 255),
    "restricted": (200, 100, 100, 255),
    "green": (100, 200, 100, 255),
    "none": (128, 128, 128, 50),
}


def _angle_diff(a: float, b: float) -> float:
    d = (b - a + math.pi) % (2.0 * math.pi) - math.pi
    return d


def _edge_lengths(points: Sequence[Tuple[float, float]]) -> List[float]:
    out = [0.0]
    total = 0.0
    for i in range(len(points) - 1):
        total += math.hypot(
            points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1]
        )
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


def _ensure_single_road_mark(
    lane_el: ET.Element,
    mark_type: str,
    color: str = "standard",
    width: str = _ROAD_MARK_WIDTH_M,
) -> None:
    road_marks = lane_el.findall("roadMark")
    if not road_marks:
        road_marks = [ET.SubElement(lane_el, "roadMark")]

    first = road_marks[0]
    first.set("sOffset", "0")
    first.set("type", str(mark_type or "none"))
    first.set("weight", "standard")
    first.set("color", str(color or "standard"))
    first.set("width", str(width or _ROAD_MARK_WIDTH_M))

    for extra in road_marks[1:]:
        lane_el.remove(extra)


def _format_road_mark_s_offset(s_offset: float) -> str:
    text = f"{max(0.0, float(s_offset)):.6f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _set_road_mark_records(
    lane_el: ET.Element,
    records: Sequence[Tuple[float, str, str]],
    width: str = _ROAD_MARK_WIDTH_M,
) -> None:
    for road_mark in list(lane_el.findall("roadMark")):
        lane_el.remove(road_mark)

    clean_records: List[Tuple[float, str, str]] = []
    for s_offset, mark_type, color in records:
        clean_records.append(
            (
                max(0.0, float(s_offset)),
                str(mark_type or "none"),
                str(color or "standard"),
            )
        )
    if not clean_records:
        clean_records = [(0.0, "none", "standard")]
    clean_records.sort(key=lambda record: record[0])
    if clean_records[0][0] > 1e-6:
        clean_records.insert(0, (0.0, "none", "standard"))
    else:
        first = clean_records[0]
        clean_records[0] = (0.0, first[1], first[2])

    merged_records: List[Tuple[float, str, str]] = []
    for s_offset, mark_type, color in clean_records:
        if merged_records and mark_type == merged_records[-1][1] and color == merged_records[-1][2]:
            continue
        merged_records.append((s_offset, mark_type, color))

    for s_offset, mark_type, color in merged_records:
        road_mark = ET.SubElement(lane_el, "roadMark")
        road_mark.set("sOffset", _format_road_mark_s_offset(s_offset))
        road_mark.set("type", mark_type)
        road_mark.set("weight", "standard")
        road_mark.set("color", color)
        road_mark.set("width", str(width or _ROAD_MARK_WIDTH_M))


def _normalize_centerline_style(style: str | None) -> str:
    text = str(style or _DEFAULT_BIDIRECTIONAL_CENTERLINE_STYLE).strip()
    compact = text.replace("_", "").replace("-", "").replace(" ", "").lower()
    if compact in ("none", "off", "false", "0", "disabled"):
        return "None"
    if compact in ("yellowsolid", "solidyellow", "yellow", "solid"):
        return "YellowSolid"
    return "WhiteBroken"


def _centerline_mark_for_style(style: str | None) -> Tuple[str, str] | None:
    normalized = _normalize_centerline_style(style)
    if normalized == "None":
        return None
    if normalized == "YellowSolid":
        return ("solid", "yellow")
    return ("broken", "standard")


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
    geo_reference: str = "",
    header_name: str = "OpenRoadEditor Baked Export",
    header_version: str = "1.00",
    header_date: str = "",
) -> str:
    root = ET.Element("OpenDRIVE")
    header = ET.SubElement(root, "header")
    header.set("revMajor", "1")
    header.set("revMinor", "4")
    header.set("name", str(header_name or "OpenRoadEditor Baked Export"))
    header.set("version", str(header_version or "1.00"))
    header.set("date", str(header_date or ""))
    if geo_reference:
        geo = ET.SubElement(header, "geoReference")
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

        road = ET.SubElement(root, "road")
        road.set("name", f"{lane.road_id}/{lane.lanesection_s0:.6f}/{lane.lane_id}")
        road.set("length", f"{length:.6f}")
        road.set("id", str(road_id_counter))
        road.set("junction", "-1")
        ET.SubElement(road, "link")

        plan_view = ET.SubElement(road, "planView")
        accum_s = 0.0
        for i in range(len(ref_points) - 1):
            x0, y0 = ref_points[i]
            x1, y1 = ref_points[i + 1]
            dx = x1 - x0
            dy = y1 - y0
            seg_len = math.hypot(dx, dy)
            if seg_len <= 1e-9:
                continue
            geom = ET.SubElement(plan_view, "geometry")
            geom.set("s", f"{accum_s:.6f}")
            geom.set("x", f"{x0:.6f}")
            geom.set("y", f"{y0:.6f}")
            geom.set("hdg", f"{math.atan2(dy, dx):.12f}")
            geom.set("length", f"{seg_len:.6f}")
            ET.SubElement(geom, "line")
            accum_s += seg_len

        lanes = ET.SubElement(road, "lanes")
        lane_offset = ET.SubElement(lanes, "laneOffset")
        lane_offset.set("s", "0")
        lane_offset.set("a", "0")
        lane_offset.set("b", "0")
        lane_offset.set("c", "0")
        lane_offset.set("d", "0")

        section = ET.SubElement(lanes, "laneSection")
        section.set("s", "0")

        center = ET.SubElement(section, "center")
        center_lane = ET.SubElement(center, "lane")
        center_lane.set("id", "0")
        center_lane.set("type", "none")
        center_lane.set("level", "true")
        ET.SubElement(center_lane, "link")

        right = ET.SubElement(section, "right")
        right_lane = ET.SubElement(right, "lane")
        right_lane.set("id", "-1")
        right_lane.set("type", str(lane.lane_type or "driving"))
        right_lane.set("level", "true")
        ET.SubElement(right_lane, "link")

        for i in range(len(widths) - 1):
            ds = s_vals[i + 1] - s_vals[i]
            width = ET.SubElement(right_lane, "width")
            width.set("sOffset", f"{s_vals[i]:.6f}")
            width.set("a", f"{widths[i]:.6f}")
            width.set(
                "b", f"{((widths[i + 1] - widths[i]) / ds) if ds > 1e-9 else 0.0:.12f}"
            )
            width.set("c", "0")
            width.set("d", "0")
        if len(widths) == 1:
            width = ET.SubElement(right_lane, "width")
            width.set("sOffset", "0")
            width.set("a", f"{widths[0]:.6f}")
            width.set("b", "0")
            width.set("c", "0")
            width.set("d", "0")

        rm = ET.SubElement(right_lane, "roadMark")
        rm.set("sOffset", "0")
        rm.set("type", "solid")
        rm.set("weight", "standard")
        rm.set("color", "standard")
        rm.set("width", "0.13")

        bounds_x.extend([x for x, _ in outer] + [x for x, _ in inner])
        bounds_y.extend([y for _, y in outer] + [y for _, y in inner])
        road_id_counter += 1

    if bounds_x and bounds_y:
        header.set("west", f"{min(bounds_x):.6f}")
        header.set("east", f"{max(bounds_x):.6f}")
        header.set("south", f"{min(bounds_y):.6f}")
        header.set("north", f"{max(bounds_y):.6f}")

    return ET.tostring(root, encoding="unicode")


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
                samples = self._sample_road_centerline(road, 0.5)
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

        image = Image.new("RGBA", (int(width_px), int(height_px)), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image, "RGBA")
        default_lane_color = (int(r), int(g), int(b), 255)

        polygons = self.get_lane_polygons(2.0)
        for poly in polygons:
            lane_color = _LANE_TYPE_COLORS.get(poly.lane_type, default_lane_color)
            pixel_points = [
                ((x - min_x) / mpp, (y - min_y) / mpp) for x, y in poly.points
            ]
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
                s1 = (
                    float(sections[idx + 1].s)
                    if idx + 1 < len(sections)
                    else float(road.length)
                )
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
                        side="left",
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
                        side="right",
                    )
                )
        return out

    def get_signals(self) -> List[Dict]:
        """Get all signals with their world positions."""
        if self._data is None:
            return []

        out = []
        for road in self._data.roads:
            for signal in road.signals:
                pose = self._pose_on_road(road.id, signal.s, signal.t)
                if pose is None:
                    continue
                xw, yw = pose

                # Calculate heading at signal position for orientation
                hdg = 0.0
                state = self._road_state_at_s(road.id, signal.s)
                if state:
                    hdg = state[3]

                out.append(
                    {
                        "id": str(signal.id),
                        "type": str(signal.type),
                        "name": str(getattr(signal, "name", "") or ""),
                        "x": xw,
                        "y": yw,
                        "hdg": hdg,
                        "h_offset": float(getattr(signal, "h_offset", 0.0) or 0.0),
                        "orientation": str(getattr(signal, "orientation", "none")),
                        "country": str(getattr(signal, "country", "") or ""),
                        "value": str(getattr(signal, "value", "") or ""),
                    }
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

                if side == "left":
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
            predecessor_key = self._connected_lane_key(
                road, section_index, lane, predecessor=True
            )
            successor_key = self._connected_lane_key(
                road, section_index, lane, predecessor=False
            )

            out.append(
                LanePolygon(
                    road_id=road.id,
                    lanesection_s0=section_s0,
                    lane_id=lane.id,
                    lane_type=str(lane.type or "driving"),
                    lane_key=lane_key,
                    predecessor_key=predecessor_key,
                    successor_key=successor_key,
                    points=outer_points + list(reversed(inner_points)),
                    outer_point_count=len(outer_points),
                )
            )
        return out

    def _build_section_lane_edge_samples(
        self,
        road: ODRRoad,
        section_s0: float,
        section_s1: float,
        section_samples: Sequence[Tuple[float, float, float, float]],
        lanes: Sequence[ODRLane],
        side: str,
    ) -> Dict[int, List[Tuple[float, Tuple[float, float], Tuple[float, float]]]]:
        """Return lane edge samples keyed by lane id.

        Each sample is ``(sOffset, outer_point, inner_point)`` in the same
        display/world coordinate frame used by lane polygons.
        """
        out: Dict[int, List[Tuple[float, Tuple[float, float], Tuple[float, float]]]] = {}
        for lane_idx, lane in enumerate(lanes):
            samples: List[Tuple[float, Tuple[float, float], Tuple[float, float]]] = []
            inner_lanes = lanes[:lane_idx]

            for s, x, y, hdg in section_samples:
                ds = max(0.0, min(s - section_s0, section_s1 - section_s0))
                lane_offset = self._lane_offset_at_s(road, s)
                inner_width_sum = sum(
                    self._lane_width_at_s(inner_lane, ds) for inner_lane in inner_lanes
                )
                lane_width = self._lane_width_at_s(lane, ds)

                if side == "left":
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
                samples.append((ds, (ox, -oy), (ix, -iy)))

            if len(samples) >= 2:
                out[int(lane.id)] = samples
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
            link_lane_id = (
                lane.link.predecessor_id if predecessor else lane.link.successor_id
            )
        if link_lane_id is None:
            return ""

        sections = sorted(road.lane_sections, key=lambda sec: sec.s)
        if predecessor:
            if section_index > 0:
                return self._lane_key(
                    road.id, sections[section_index - 1].s, int(link_lane_id)
                )
            if (
                road.predecessor_type == "road"
                and road.predecessor_id in self._roads_by_id
            ):
                pred_road = self._roads_by_id[road.predecessor_id]
                pred_sections = sorted(pred_road.lane_sections, key=lambda sec: sec.s)
                if not pred_sections:
                    return ""
                use_first = (
                    str(road.predecessor_contact or "").strip().lower() == "start"
                )
                target_s0 = pred_sections[0].s if use_first else pred_sections[-1].s
                return self._lane_key(pred_road.id, target_s0, int(link_lane_id))
            return ""

        if section_index + 1 < len(sections):
            return self._lane_key(
                road.id, sections[section_index + 1].s, int(link_lane_id)
            )
        if road.successor_type == "road" and road.successor_id in self._roads_by_id:
            succ_road = self._roads_by_id[road.successor_id]
            succ_sections = sorted(succ_road.lane_sections, key=lambda sec: sec.s)
            if not succ_sections:
                return ""
            use_last = str(road.successor_contact or "").strip().lower() == "end"
            target_s0 = succ_sections[-1].s if use_last else succ_sections[0].s
            return self._lane_key(succ_road.id, target_s0, int(link_lane_id))
        return ""

    @staticmethod
    def _lane_key(road_id: str, section_s0: float, lane_id: int) -> str:
        return f"{road_id}/{float(section_s0):.6f}/{int(lane_id)}"

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

    def _pose_on_road(
        self, road_id: str, s: float, t: float
    ) -> Tuple[float, float] | None:
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
        if end_sample is not None and (
            not out or abs(end_sample[0] - out[-1][0]) > 1e-9
        ):
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
            curvature = float(geom.params.get("curvature", 0.0))
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
            curv_start = float(geom.params.get("curvStart", 0.0))
            curv_end = float(geom.params.get("curvEnd", 0.0))
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
            a = float(geom.params.get("a", 0.0))
            b = float(geom.params.get("b", 0.0))
            c = float(geom.params.get("c", 0.0))
            d = float(geom.params.get("d", 0.0))
            v = s
            u = a + b * v + c * v * v + d * v * v * v
            du = b + 2.0 * c * v + 3.0 * d * v * v
            x = x0 + math.cos(hdg0) * v - math.sin(hdg0) * u
            y = y0 + math.sin(hdg0) * v + math.cos(hdg0) * u
            hdg = hdg0 + math.atan2(du, 1.0)
            return (x, y, hdg)

        if geom.geometry_type == GeometryType.PARAM_POLY3:
            p_range = str(geom.params.get("pRange", "arcLength")).strip().lower()
            p = s / max(float(geom.length), 1e-9) if p_range == "normalized" else s
            a_u = float(geom.params.get("aU", 0.0))
            b_u = float(geom.params.get("bU", 0.0))
            c_u = float(geom.params.get("cU", 0.0))
            d_u = float(geom.params.get("dU", 0.0))
            a_v = float(geom.params.get("aV", 0.0))
            b_v = float(geom.params.get("bV", 0.0))
            c_v = float(geom.params.get("cV", 0.0))
            d_v = float(geom.params.get("dV", 0.0))
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

    # ------------------------------------------------------------------
    # Exterior road-marking computation
    # ------------------------------------------------------------------

    def get_road_marking_polylines(
        self,
        s_step: float = 1.0,
        raster_mpp: float = 0.2,
    ) -> List[List[Tuple[float, float]]]:
        """Return the exterior boundary of the road surface as polylines.

        Uses a raster union of all driving-type lane polygons to find the
        outer contour, avoiding markings on internal road patches (e.g.
        inside roundabout junction areas).

        Returns a list of closed polylines in world coordinates (x, y).
        """
        import cv2
        from scipy.ndimage import uniform_filter1d

        polygons = self.get_lane_polygons(max(0.25, float(s_step)))
        if not polygons:
            return []

        road_polys = [p for p in polygons if p.lane_type in _ROAD_SURFACE_TYPES]
        if not road_polys:
            return []

        all_x = [x for p in road_polys for x, y in p.points]
        all_y = [y for p in road_polys for x, y in p.points]
        if not all_x:
            return []

        scale = max(0.1, float(raster_mpp))
        margin = max(2.0, scale * 8)
        min_x = min(all_x) - margin
        min_y = min(all_y) - margin
        max_x = max(all_x) + margin
        max_y = max(all_y) + margin

        w = int((max_x - min_x) / scale) + 2
        h = int((max_y - min_y) / scale) + 2
        # Cap to a reasonable raster size to avoid excessive memory use.
        if w > 10000 or h > 10000:
            scale = max(
                (max_x - min_x) / 9998.0,
                (max_y - min_y) / 9998.0,
            )
            w = int((max_x - min_x) / scale) + 2
            h = int((max_y - min_y) / scale) + 2
        if w <= 1 or h <= 1:
            return []

        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)
        for poly in road_polys:
            px_pts = [
                (int((x - min_x) / scale), int((y - min_y) / scale))
                for x, y in poly.points
            ]
            if len(px_pts) >= 3:
                draw.polygon(px_pts, fill=255)

        mask_arr = np.array(mask, dtype=np.uint8)

        # RETR_CCOMP: returns both the outer road boundary (level-0 external
        # contours) AND the inner hole boundaries (level-1 holes, e.g. the
        # inner edge of a roundabout ring or median island).  Internal patch
        # seams are absorbed into the union fill and never appear as contours.
        # CHAIN_APPROX_NONE: keep every contour pixel so smoothing has full data.
        contours, _ = cv2.findContours(
            mask_arr, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE
        )

        # Smoothing window: ~1.5 m in pixels, minimum 5 pixels.
        smooth_px = max(5, int(1.5 / scale) | 1)  # keep odd for symmetry

        # Approximation epsilon: ~0.3 m in pixels (reduces point density post-smooth).
        approx_eps = max(1.0, 0.3 / scale)

        result: List[List[Tuple[float, float]]] = []
        for cnt in contours:
            if len(cnt) < 5:
                continue
            pts_px = cnt[:, 0, :].astype(float)  # shape (N, 2)

            # Circular moving-average smoothing on x and y independently.
            # mode='wrap' treats the contour as a closed loop so the seam
            # between the first and last point is handled correctly.
            pts_px[:, 0] = uniform_filter1d(pts_px[:, 0], size=smooth_px, mode="wrap")
            pts_px[:, 1] = uniform_filter1d(pts_px[:, 1], size=smooth_px, mode="wrap")

            # Reduce point density with Douglas-Peucker approximation.
            pts_int = pts_px.astype(np.int32).reshape(-1, 1, 2)
            approx = cv2.approxPolyDP(pts_int, epsilon=approx_eps, closed=True)
            if len(approx) < 3:
                continue

            pts = [
                (
                    float(p[0][0]) * scale + min_x,
                    float(p[0][1]) * scale + min_y,
                )
                for p in approx
            ]
            pts.append(pts[0])  # close the loop
            result.append(pts)
        return result

    @staticmethod
    def _is_single_surface_lane_road(road: ODRRoad) -> bool:
        for section in getattr(road, "lane_sections", []) or []:
            surface_lane_count = 0
            for lane in list(getattr(section, "left_lanes", []) or []) + list(
                getattr(section, "right_lanes", []) or []
            ):
                try:
                    lane_id = int(getattr(lane, "id", 0))
                except (TypeError, ValueError):
                    lane_id = 0
                if (
                    lane_id != 0
                    and str(getattr(lane, "type", "") or "") in _ROAD_SURFACE_TYPES
                ):
                    surface_lane_count += 1
            if surface_lane_count != 1:
                return False
        return True

    def _road_reference_point(
        self,
        road: ODRRoad,
        s: float,
    ) -> Tuple[float, float] | None:
        length = float(getattr(road, "length", 0.0) or 0.0)
        if length <= 1e-6:
            return None
        state = self._road_state_at_s(
            road.id,
            max(1e-6, min(float(s), length - 1e-6)),
        )
        if state is None:
            return None
        _, x, y, _ = state
        return (float(x), -float(y))

    def _is_opposite_undivided_road_pair(
        self,
        road: ODRRoad,
        candidate: ODRRoad,
    ) -> bool:
        if (
            road.id == candidate.id
            or str(getattr(road, "junction_id", "-1")) != "-1"
            or str(getattr(candidate, "junction_id", "-1")) != "-1"
            or not self._is_single_surface_lane_road(road)
            or not self._is_single_surface_lane_road(candidate)
        ):
            return False

        length = float(getattr(road, "length", 0.0) or 0.0)
        candidate_length = float(getattr(candidate, "length", 0.0) or 0.0)
        if length <= 1e-6 or candidate_length <= 1e-6:
            return False

        length_tolerance = max(2.0, max(length, candidate_length) * 0.03)
        if abs(length - candidate_length) > length_tolerance:
            return False

        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            road_s = min(length - 1e-6, max(1e-6, length * fraction))
            candidate_s = min(
                candidate_length - 1e-6,
                max(1e-6, candidate_length * (1.0 - fraction)),
            )
            road_point = self._road_reference_point(road, road_s)
            candidate_point = self._road_reference_point(candidate, candidate_s)
            if road_point is None or candidate_point is None:
                return False
            if (
                math.hypot(
                    road_point[0] - candidate_point[0],
                    road_point[1] - candidate_point[1],
                )
                > _BIDIRECTIONAL_REFERENCE_MATCH_TOLERANCE_M
            ):
                return False
        return True

    def _bidirectional_center_mark_road_ids(self) -> set[str]:
        # Match CARLA's topology redraw: ordinary undivided two-way roads are
        # represented as two opposite one-lane roads sharing the same reference
        # line.  Mark one representative so the centerline is not duplicated.
        bidirectional_center_mark_roads: set[str] = set()
        if self._data is None:
            return bidirectional_center_mark_roads

        roads = list(getattr(self._data, "roads", []) or [])
        handled_roads: set[str] = set()
        for road in roads:
            if road.id in handled_roads:
                continue
            if (
                str(getattr(road, "junction_id", "-1")) != "-1"
                or not self._is_single_surface_lane_road(road)
            ):
                continue

            opposite_road = None
            for candidate in roads:
                if candidate.id in handled_roads:
                    continue
                if self._is_opposite_undivided_road_pair(road, candidate):
                    opposite_road = candidate
                    break
            if opposite_road is None:
                continue

            bidirectional_center_mark_roads.add(road.id)
            handled_roads.add(road.id)
            handled_roads.add(opposite_road.id)
        return bidirectional_center_mark_roads

    @staticmethod
    def _roundabout_ring_road_ids_from_root(root: ET.Element) -> set[str]:
        groups: dict[str, list[str]] = {}
        for road_el in root.findall("road"):
            if road_el.get("junction", "-1") != "-1":
                continue
            pred_el = road_el.find("./link/predecessor")
            succ_el = road_el.find("./link/successor")
            if (
                pred_el is None
                or succ_el is None
                or pred_el.get("elementType") != "junction"
                or succ_el.get("elementType") != "junction"
            ):
                continue

            sumo_id = ""
            for user_data in road_el.findall("userData"):
                if user_data.get("code") == "sumoId":
                    sumo_id = str(user_data.get("value", "") or "")
                    break
            if "#" not in sumo_id:
                continue
            base_id = sumo_id.split("#", 1)[0]
            if base_id:
                groups.setdefault(base_id, []).append(str(road_el.get("id", "")))

        ring_roads: set[str] = set()
        for road_ids in groups.values():
            if len(road_ids) >= 4:
                ring_roads.update(road_id for road_id in road_ids if road_id)
        return ring_roads

    @staticmethod
    def _roundabout_connector_road_ids_from_root(
        root: ET.Element,
        roundabout_ring_road_ids: set[str],
    ) -> Tuple[set[str], set[str]]:
        ring_ids = {str(road_id) for road_id in roundabout_ring_road_ids if road_id}
        connector_roads: set[str] = set()
        circulator_connector_roads: set[str] = set()
        if not ring_ids:
            return connector_roads, circulator_connector_roads

        for road_el in root.findall("road"):
            road_id = str(road_el.get("id", "") or "")
            if not road_id or road_el.get("junction", "-1") == "-1":
                continue

            connected_ring_ids: set[str] = set()
            for link_tag in ("predecessor", "successor"):
                link_el = road_el.find(f"./link/{link_tag}")
                if link_el is None or link_el.get("elementType") != "road":
                    continue
                link_road_id = str(link_el.get("elementId", "") or "")
                if link_road_id in ring_ids:
                    connected_ring_ids.add(link_road_id)

            if connected_ring_ids:
                connector_roads.add(road_id)
            if len(connected_ring_ids) >= 2:
                circulator_connector_roads.add(road_id)

        return connector_roads, circulator_connector_roads

    def _road_reference_polyline(
        self,
        road: ODRRoad,
        s_start: float,
        s_end: float,
        step_m: float,
    ) -> List[Tuple[float, float]]:
        length = float(getattr(road, "length", 0.0) or 0.0)
        if length <= 1e-6:
            return []

        clamped_start = min(length - 1e-6, max(1e-6, float(s_start)))
        clamped_end = min(length - 1e-6, max(1e-6, float(s_end)))
        if clamped_end - clamped_start <= 1e-6:
            return []

        step = max(0.25, float(step_m))
        targets: List[float] = []
        s = clamped_start
        while s < clamped_end:
            targets.append(s)
            s += step
        if not targets or abs(targets[-1] - clamped_end) > 1e-9:
            targets.append(clamped_end)

        points: List[Tuple[float, float]] = []
        for target_s in targets:
            state = self._road_state_at_s(road.id, target_s)
            if state is None:
                continue
            _, x, y, _ = state
            point = (float(x), -float(y))
            if not points or math.hypot(
                point[0] - points[-1][0], point[1] - points[-1][1]
            ) > 1e-6:
                points.append(point)
        return points

    def get_bidirectional_center_marking_polylines(
        self,
        centerline_style: str | None = _DEFAULT_BIDIRECTIONAL_CENTERLINE_STYLE,
        s_step: float = 1.0,
    ) -> List[Dict[str, object]]:
        """Return ORE/CARLA-style centerline markings for undivided roads.

        Each returned record contains ``points`` plus a simple ``type`` and
        ``color``.  Broken markings are returned as individual dash polylines so
        the viewer uses the same 3 m dash / 6 m gap pattern as CARLA's topology
        lane-marking redraw.
        """
        mark = _centerline_mark_for_style(centerline_style)
        if self._data is None or mark is None:
            return []

        mark_type, mark_color = mark
        display_color = "yellow" if mark_color == "yellow" else "white"
        road_ids = self._bidirectional_center_mark_road_ids()
        out: List[Dict[str, object]] = []

        for road_id in sorted(road_ids):
            road = self._roads_by_id.get(road_id)
            if road is None or str(getattr(road, "junction_id", "-1")) != "-1":
                continue

            road_length = float(getattr(road, "length", 0.0) or 0.0)
            if road_length <= 1e-6:
                continue

            if mark_type == "broken":
                dash_start = 1e-6
                while dash_start < road_length - 1e-6:
                    dash_end = min(
                        dash_start + _BIDIRECTIONAL_BROKEN_LINE_LENGTH_M,
                        road_length - 1e-6,
                    )
                    points = self._road_reference_polyline(
                        road, dash_start, dash_end, max(0.25, float(s_step))
                    )
                    if len(points) >= 2:
                        out.append(
                            {
                                "points": points,
                                "type": mark_type,
                                "color": display_color,
                            }
                        )
                    dash_start += (
                        _BIDIRECTIONAL_BROKEN_LINE_LENGTH_M
                        + _BIDIRECTIONAL_BROKEN_LINE_GAP_M
                    )
            else:
                points = self._road_reference_polyline(
                    road, 1e-6, road_length - 1e-6, max(0.25, float(s_step))
                )
                if len(points) >= 2:
                    out.append(
                        {
                            "points": points,
                            "type": mark_type,
                            "color": display_color,
                        }
                    )
        return out

    def rewrite_exterior_road_marks(
        self,
        xodr_path: str,
        s_step: float = 1.0,
        tolerance_m: float = 1.5,
        centerline_style: str | None = _DEFAULT_BIDIRECTIONAL_CENTERLINE_STYLE,
    ) -> bool:
        """Rewrite roadMark elements so the XODR carries ORE-style markings.

        Algorithm:
        1. Rasterise all road-surface lane polygons to build a binary mask.
        2. Find the exterior contour and inner holes of the union.
        3. Build a tolerance-band contour mask by drawing the contour with a
           thickness equal to ``tolerance_m`` converted to pixels.
        4. For every lane polygon, sample the outer and inner edge points and
           check whether they fall inside the tolerance band.
        5. Non-center lanes whose outer edge is on the exterior get
           ``type="solid"``.  Center lanes get ``type="solid"`` when the road
           reference line is an exterior/hole boundary, or the selected
           bidirectional style for one representative of each opposite-
           direction undivided road pair.

        Returns True on success, False on failure.
        """
        import cv2

        if self._data is None:
            return False

        # ---- Step 1: raster mask ----------------------------------------
        polygons = self.get_lane_polygons(max(0.25, float(s_step)))
        road_polys = [p for p in polygons if p.lane_type in _ROAD_SURFACE_TYPES]
        if not road_polys:
            return False

        all_x = [x for p in road_polys for x, y in p.points]
        all_y = [y for p in road_polys for x, y in p.points]

        raster_mpp = 0.4
        margin = max(2.0, raster_mpp * 4)
        min_x = min(all_x) - margin
        min_y = min(all_y) - margin
        max_x = max(all_x) + margin
        max_y = max(all_y) + margin

        scale = raster_mpp
        w = int((max_x - min_x) / scale) + 2
        h = int((max_y - min_y) / scale) + 2
        if w > 8000 or h > 8000:
            scale = max((max_x - min_x) / 7998.0, (max_y - min_y) / 7998.0)
            w = int((max_x - min_x) / scale) + 2
            h = int((max_y - min_y) / scale) + 2
        if w <= 1 or h <= 1:
            return False

        mask = Image.new("L", (w, h), 0)
        draw_img = ImageDraw.Draw(mask)
        for poly in road_polys:
            px_pts = [
                (int((x - min_x) / scale), int((y - min_y) / scale))
                for x, y in poly.points
            ]
            if len(px_pts) >= 3:
                draw_img.polygon(px_pts, fill=255)

        mask_arr = np.array(mask, dtype=np.uint8)

        # ---- Step 2 & 3: road boundary tolerance band (outer + inner edges) ----
        # RETR_CCOMP captures both the outer road boundary and inner hole edges
        # (e.g. the inner edge of a roundabout ring), matching what is drawn in
        # the viewer so the xodr marks align with the displayed lines.
        contours, _ = cv2.findContours(
            mask_arr, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_TC89_L1
        )
        thickness_px = max(1, int(tolerance_m / scale) * 2 + 1)
        contour_band = np.zeros_like(mask_arr)
        cv2.drawContours(contour_band, contours, -1, 255, thickness=thickness_px)

        try:
            tree = ET.parse(xodr_path)
            root = tree.getroot()
        except ET.ParseError:
            return False
        roundabout_ring_road_ids = self._roundabout_ring_road_ids_from_root(root)
        (
            roundabout_connector_road_ids,
            roundabout_circulator_connector_road_ids,
        ) = self._roundabout_connector_road_ids_from_root(root, roundabout_ring_road_ids)

        # ---- Step 4: segment each lane's outer and inner edges ----------
        # Build maps:
        #   lane_key -> roadMark records for the lane's outer edge.
        #   lane_key(0) -> roadMark records for the road reference/inner edge.
        # Mark records are segmented so roundabout entries/exits cut out the
        # ring's outer line when that edge is no longer part of the exterior
        # road-surface contour.
        lane_mark_records: dict[str, List[Tuple[float, str, str]]] = {}
        center_mark_records: dict[str, List[Tuple[float, str, str]]] = {}

        def point_in_road_mask(point: Tuple[float, float]) -> bool:
            x, y = point
            px = int((x - min_x) / scale)
            py = int((y - min_y) / scale)
            return 0 <= px < w and 0 <= py < h and mask_arr[py, px] > 0

        def point_touches_contour_band(point: Tuple[float, float]) -> bool:
            x, y = point
            px = int((x - min_x) / scale)
            py = int((y - min_y) / scale)
            return 0 <= px < w and 0 <= py < h and contour_band[py, px] > 0

        def point_is_surface_boundary(
            boundary: Tuple[float, float],
            opposite: Tuple[float, float],
        ) -> bool:
            vx = float(opposite[0]) - float(boundary[0])
            vy = float(opposite[1]) - float(boundary[1])
            length = math.hypot(vx, vy)
            if length <= 1e-6:
                return point_touches_contour_band(boundary)

            nx = vx / length
            ny = vy / length
            probe = min(max(0.35, scale * 1.5), max(0.15, length * 0.45))
            lane_side = (boundary[0] + nx * probe, boundary[1] + ny * probe)
            other_side = (boundary[0] - nx * probe, boundary[1] - ny * probe)

            lane_side_is_road = point_in_road_mask(lane_side) or point_in_road_mask(boundary)
            other_side_is_road = point_in_road_mask(other_side)
            return lane_side_is_road and not other_side_is_road

        def smooth_flags(flags: Sequence[bool]) -> List[bool]:
            if len(flags) < 5:
                return list(flags)
            out_flags: List[bool] = []
            for idx in range(len(flags)):
                window = flags[max(0, idx - 2) : min(len(flags), idx + 3)]
                out_flags.append(sum(1 for flag in window if flag) >= (len(window) + 1) // 2)
            return out_flags

        def edge_boundary_ratio(
            edge_samples: Sequence[
                Tuple[float, Tuple[float, float], Tuple[float, float]]
            ],
        ) -> float:
            if not edge_samples:
                return 0.0
            return sum(
                1
                for _, boundary, opposite in edge_samples
                if point_is_surface_boundary(boundary, opposite)
            ) / float(
                len(edge_samples)
            )

        def records_for_edge(
            edge_samples: Sequence[
                Tuple[float, Tuple[float, float], Tuple[float, float]]
            ],
        ) -> List[Tuple[float, str, str]]:
            if not edge_samples:
                return [(0.0, "none", "standard")]
            flags = smooth_flags(
                [
                    point_is_surface_boundary(boundary, opposite)
                    for _, boundary, opposite in edge_samples
                ]
            )
            if not flags:
                return [(0.0, "none", "standard")]

            current = flags[0]
            records: List[Tuple[float, str, str]] = [
                (0.0, "solid" if current else "none", "standard")
            ]
            for idx in range(1, len(flags)):
                if flags[idx] == current:
                    continue
                transition_s = 0.5 * (
                    float(edge_samples[idx - 1][0]) + float(edge_samples[idx][0])
                )
                if transition_s - records[-1][0] > 0.25:
                    records.append(
                        (transition_s, "solid" if flags[idx] else "none", "standard")
                    )
                current = flags[idx]
            return records

        for road in self._data.roads:
            sections = sorted(road.lane_sections, key=lambda sec: sec.s)
            for idx, section in enumerate(sections):
                s0 = float(section.s)
                s1 = float(sections[idx + 1].s) if idx + 1 < len(sections) else float(road.length)
                section_samples = self._section_centerline_samples(road.id, s0, s1)
                if len(section_samples) < 2:
                    continue

                side_edges: Dict[int, List[Tuple[float, Tuple[float, float], Tuple[float, float]]]] = {}
                left_lanes = sorted(section.left_lanes, key=lambda lane: lane.id)
                right_lanes = sorted(section.right_lanes, key=lambda lane: abs(lane.id))
                side_edges.update(
                    self._build_section_lane_edge_samples(
                        road, s0, s1, section_samples, left_lanes, "left"
                    )
                )
                side_edges.update(
                    self._build_section_lane_edge_samples(
                        road, s0, s1, section_samples, right_lanes, "right"
                    )
                )

                center_key = self._lane_key(road.id, s0, 0)
                best_center_ratio = -1.0
                best_center_records: List[Tuple[float, str, str]] = [
                    (0.0, "none", "standard")
                ]

                for lane_id, samples in side_edges.items():
                    lane = next(
                        (
                            candidate
                            for candidate in list(left_lanes) + list(right_lanes)
                            if int(candidate.id) == int(lane_id)
                        ),
                        None,
                    )
                    lane_key = self._lane_key(road.id, s0, int(lane_id))
                    if lane is None or str(lane.type or "") not in _ROAD_SURFACE_TYPES:
                        lane_mark_records[lane_key] = [(0.0, "none", "standard")]
                        continue

                    outer_samples = [
                        (s_offset, outer, inner) for s_offset, outer, inner in samples
                    ]
                    inner_samples = [
                        (s_offset, inner, outer) for s_offset, outer, inner in samples
                    ]
                    lane_mark_records[lane_key] = records_for_edge(outer_samples)

                    center_ratio = edge_boundary_ratio(inner_samples)
                    if center_ratio > best_center_ratio:
                        best_center_ratio = center_ratio
                        best_center_records = records_for_edge(inner_samples)

                center_mark_records[center_key] = best_center_records

        def points_touch_contour_band(points: Sequence[Tuple[float, float]]) -> bool:
            if not points:
                return False
            step = max(1, len(points) // 16)
            sampled_points = list(points[::step])
            if not sampled_points:
                return False
            touch_count = 0
            for x, y in sampled_points:
                px = int((x - min_x) / scale)
                py = int((y - min_y) / scale)
                if 0 <= px < w and 0 <= py < h and contour_band[py, px] > 0:
                    touch_count += 1
            if len(sampled_points) <= 3:
                return touch_count >= max(1, len(sampled_points) - 1)
            return (touch_count / float(len(sampled_points))) >= 0.35

        for poly in polygons:
            if poly.lane_type not in _ROAD_SURFACE_TYPES:
                continue
            n = len(poly.points)
            nc = poly.outer_point_count if poly.outer_point_count > 0 else n // 2
            inner_pts = list(reversed(poly.points[nc:]))
            if points_touch_contour_band(inner_pts):
                center_key = self._lane_key(poly.road_id, poly.lanesection_s0, 0)
                if center_mark_records.get(center_key) == [(0.0, "none", "standard")]:
                    center_mark_records[center_key] = [(0.0, "solid", "standard")]

        bidirectional_mark = _centerline_mark_for_style(centerline_style)
        bidirectional_center_mark_roads = (
            self._bidirectional_center_mark_road_ids()
            if bidirectional_mark is not None
            else set()
        )

        for road_el in root.findall("road"):
            road_id = road_el.get("id", "")
            try:
                road_length = float(road_el.get("length", "0"))
            except ValueError:
                road_length = 0.0
            section_els = road_el.findall("./lanes/laneSection")

            for section_index, section_el in enumerate(section_els):
                try:
                    s0 = float(section_el.get("s", "0"))
                except ValueError:
                    s0 = 0.0
                s0_str = f"{s0:.6f}"
                try:
                    s1 = (
                        float(section_els[section_index + 1].get("s", "0"))
                        if section_index + 1 < len(section_els)
                        else road_length
                    )
                except ValueError:
                    s1 = road_length

                for side_tag in ("left", "right", "center"):
                    side_el = section_el.find(side_tag)
                    if side_el is None:
                        continue
                    for lane_el in side_el.findall("lane"):
                        try:
                            lane_id = int(lane_el.get("id", "0"))
                        except ValueError:
                            lane_id = 0

                        if lane_id == 0:
                            if (
                                road_id in roundabout_ring_road_ids
                                or road_id in roundabout_circulator_connector_road_ids
                            ):
                                _ensure_single_road_mark(
                                    lane_el,
                                    "solid",
                                    "standard",
                                )
                            elif road_id in roundabout_connector_road_ids:
                                _ensure_single_road_mark(
                                    lane_el,
                                    "none",
                                    "standard",
                                )
                            elif road_id in bidirectional_center_mark_roads:
                                mark_type, mark_color = bidirectional_mark or (
                                    "none",
                                    "standard",
                                )
                                _ensure_single_road_mark(
                                    lane_el,
                                    mark_type,
                                    mark_color,
                                )
                            else:
                                center_key = self._lane_key(road_id, s0, 0)
                                _set_road_mark_records(
                                    lane_el,
                                    center_mark_records.get(
                                        center_key,
                                        [(0.0, "none", "standard")],
                                    ),
                                )
                            continue

                        lane_key = f"{road_id}/{s0_str}/{lane_id}"
                        records = lane_mark_records.get(
                            lane_key,
                            [(0.0, "none", "standard")],
                        )
                        _set_road_mark_records(
                            lane_el,
                            records,
                        )

        try:
            tree.write(xodr_path, encoding="UTF-8", xml_declaration=True)
        except Exception:
            return False

        return True
