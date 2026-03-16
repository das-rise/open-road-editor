#!/usr/bin/env python3
"""OpenRoadEditor CLI entry-point. All implementation lives in sub-packages."""

import logging
import math
import os
import signal as _signal
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from open_road_editor.constants import (
    CLI_DEFAULT_LAT,
    CLI_DEFAULT_LON,
    DEFAULT_BOUND_EXTENT,
    DEFAULT_TILE_ZOOM,
    EARTH_CIRCUMFERENCE,
    ESRI_TILE_MAX_ZOOM,
    SIGINT_POLL_INTERVAL_MS,
    TILE_SIZE,
)
from open_road_editor.utils.map_context import MapContext
from open_road_editor.viewer import OpenDriveViewer


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=str, default=None)
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument(
        '--osm',
        type=str,
        default=None,
        help='Path to an OSM file (.osm/.xml) to open on startup',
    )
    parser.add_argument(
        '--project',
        type=str,
        default=None,
        help='Path to a saved OpenRoadEditor project file (.ore) to open on startup',
    )
    # Metadata overrides
    parser.add_argument('--lat', type=float, default=CLI_DEFAULT_LAT, help='Reference Latitude')
    parser.add_argument('--lon', type=float, default=CLI_DEFAULT_LON, help='Reference Longitude')
    parser.add_argument(
        '--tile_max_zoom_level',
        type=int,
        default=DEFAULT_TILE_ZOOM,
        help='Max Tile Zoom Level',
    )
    parser.add_argument(
        '--bounds',
        type=float,
        nargs=4,
        metavar=('MIN_X', 'MAX_X', 'MIN_Y', 'MAX_Y'),
        default=None,
        help='World bounds override in metres: min_x max_x min_y max_y',
    )
    parser.add_argument(
        '--width',
        type=int,
        default=None,
        help='Canvas width in pixels (overrides computed value)',
    )
    parser.add_argument(
        '--height',
        type=int,
        default=None,
        help='Canvas height in pixels (overrides computed value)',
    )
    args = parser.parse_args()
    args.tile_max_zoom_level = max(0, min(ESRI_TILE_MAX_ZOOM, args.tile_max_zoom_level))

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger('open-road-editor')

    # Calculate MPP based on lat and zoom (matching server logic)
    mpp = (EARTH_CIRCUMFERENCE * math.cos(math.radians(args.lat))) / (
        (2**args.tile_max_zoom_level) * TILE_SIZE
    )

    town_default = 'Unknown'

    default_bounds = (
        args.bounds
        if args.bounds
        else [
            -DEFAULT_BOUND_EXTENT,
            DEFAULT_BOUND_EXTENT,
            -DEFAULT_BOUND_EXTENT,
            DEFAULT_BOUND_EXTENT,
        ]
    )
    default_offset = [default_bounds[0], default_bounds[2]]
    default_w = (
        args.width if args.width else int(math.ceil((default_bounds[1] - default_bounds[0]) / mpp))
    )
    default_h = (
        args.height
        if args.height
        else int(math.ceil((default_bounds[3] - default_bounds[2]) / mpp))
    )

    metadata = {
        'town': town_default,
        'mpp': mpp,
        'world_offset': default_offset,
        'world_bounds': default_bounds,
        'width_px': default_w,
        'height_px': default_h,
        'tile_max_zoom_level': args.tile_max_zoom_level,
        'ref_lat': args.lat,
        'ref_lon': args.lon,
    }

    map_ctx = MapContext(metadata)
    app = QApplication(sys.argv)

    # Allow Ctrl+C to quit cleanly. Qt's event loop blocks Python signal delivery
    # by default, so we install a handler that calls app.quit() and use a short
    # QTimer to periodically wake the Python interpreter so the signal is checked.

    _signal.signal(_signal.SIGINT, lambda *_: app.closeAllWindows())
    _sigint_timer = QTimer()
    _sigint_timer.start(SIGINT_POLL_INTERVAL_MS)
    _sigint_timer.timeout.connect(lambda: None)  # wakes Python interpreter

    viewer = OpenDriveViewer(
        carla_bev_img=None,
        opendrive_img=None,
        esri_img=None,
        town_name=metadata.get('town', 'Unknown'),
        map_ctx=map_ctx,
        show_carla_bev=False,
        show_esri=False,
        show_opendrive=False,
        show_grid=None,
        xodr_path=None,
        server_ip=args.host,
        server_port=args.port,
    )
    viewer.show()

    def _open_osm_startup(path: str) -> None:
        viewer.edit_osm.setText(path)  # triggers on_osm_path_changed
        viewer.check_osm.setChecked(True)

    if args.project:
        if args.osm:
            logger.info('Ignoring --osm because --project was provided')
        QTimer.singleShot(0, lambda p=args.project: viewer.load_project_file(p, show_status=True))
    elif args.osm:
        if os.path.isfile(args.osm):
            QTimer.singleShot(0, lambda p=args.osm: _open_osm_startup(p))
        else:
            logger.warning('OSM file not found: %s', args.osm)
            QTimer.singleShot(0, viewer.new_project)
    else:
        # No project specified — run new_project() so the viewer starts in a
        # clean, consistent state identical to File → New Project.
        QTimer.singleShot(0, viewer.new_project)
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
