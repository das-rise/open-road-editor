"""Application-wide constants for OpenRoadEditor."""

from PyQt6.QtGui import QColor

TILE_SIZE = 256

# Colour map used by the vector OpenDRIVE renderer (QPainterPath mode).
# Values are (R, G, B, A); they mirror the colours used in opendrive_renderer.cpp.
_XODR_LANE_COLORS: dict = {
    'driving': (46, 52, 54, 220),
    'sidewalk': (200, 200, 200, 220),
    'median': (160, 180, 160, 220),
    'shoulder': (170, 170, 170, 220),
    'parking': (180, 180, 200, 220),
    'border': (200, 100, 100, 220),
    'restricted': (200, 100, 100, 180),
    'green': (100, 200, 100, 220),
    'none': (128, 128, 128, 50),
}
_XODR_LANE_COLOR_DEFAULT = (128, 128, 128, 100)

# Highway-type styles for the OSM vector overlay.
# Each value is ((R, G, B, A), cosmetic_line_width_px).
_OSM_HIGHWAY_STYLES: dict = {
    'motorway': ((230, 30, 30, 230), 7),
    'motorway_link': ((230, 30, 30, 200), 4),
    'trunk': ((230, 80, 30, 220), 6),
    'trunk_link': ((230, 80, 30, 180), 4),
    'primary': ((230, 140, 30, 220), 5),
    'primary_link': ((230, 140, 30, 180), 4),
    'secondary': ((210, 210, 30, 210), 4),
    'secondary_link': ((210, 210, 30, 160), 3),
    'tertiary': ((210, 210, 210, 200), 4),
    'tertiary_link': ((210, 210, 210, 160), 3),
    'residential': ((255, 0, 0, 190), 3),
    'living_street': ((255, 0, 0, 160), 3),
    'service': ((255, 0, 0, 140), 2),
    'unclassified': ((200, 200, 200, 160), 3),
    'cycleway': ((30, 160, 255, 190), 3),
    'footway': ((255, 140, 60, 160), 2),
    'path': ((255, 140, 60, 140), 2),
    'track': ((140, 200, 80, 150), 2),
    'steps': ((255, 140, 60, 130), 2),
    'pedestrian': ((210, 210, 210, 160), 3),
}
_OSM_HIGHWAY_DEFAULT = ((170, 170, 170, 120), 2)

OSM_SEGMENT_PROP_MAX_LINES = 4
OSM_STITCH_MAX_DIST_PX = 40.0
OSM_DIRECTION_ARROW_LENGTH_PX = 16.0
OSM_DIRECTION_ARROW_WIDTH_PX = 10.0
OSM_DIRECTION_BIDIR_OFFSET_PX = 6.0
OSM_ALWAYS_NODE_RADIUS_PX = 2.4
OSM_LINE_THICKNESS = 1.0

EARTH_CIRCUMFERENCE = 40075000
MAX_GRID_LABEL_DIGITS = 4
DEFAULT_GRID_LABEL_DIGITS = 2
ESRI_TILE_MAX_ZOOM = 22
PROJECT_FILE_VERSION = 1
TOAST_NOTIFICATION_DURATION_MS = 2000

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
OOR_TILE_BG_COLOR = (255, 255, 255, 220)
OOR_TILE_HATCH_COLOR = (200, 200, 200, 200)
TILE_ERROR_TEXT_COLOR = (200, 0, 0, 255)
TILE_PLACEHOLDER_BG_COLOR = (140, 140, 140, 220)
DEFAULT_GRID_COLOR = QColor(0, 0, 0, 255)
VIEWPORT_BACKGROUND_COLOR = QColor(154, 153, 150)
GRID_AXIS_COLOR = QColor(220, 30, 30, 220)
GRID_CROSS_COLOR = QColor(220, 30, 30, 255)
XODR_HOVER_COLOR = QColor(255, 200, 0, 230)
OSM_NODE_DOT_OUTLINE_COLOR = QColor(255, 255, 255, 220)
OSM_NODE_DOT_FILL_COLOR = QColor(0, 180, 255, 200)
OSM_ALWAYS_NODE_OUTLINE_COLOR = QColor(20, 20, 20, 210)
OSM_ALWAYS_NODE_FILL_COLOR = QColor(255, 255, 255, 220)
OSM_HOVER_COLOR = QColor(255, 220, 0, 255)
OSM_SELECTION_COLOR = QColor(0, 200, 255, 255)
OSM_DIRECTION_ARROW_COLOR = QColor(30, 30, 30, 220)
OSM_SELECTED_ARROW_FILL_COLOR = QColor(255, 255, 0, 240)
OSM_SELECTED_ARROW_OUTLINE_COLOR = QColor(0, 0, 0, 220)
CARLA_BOUNDS_RECT_COLOR = QColor(255, 140, 0)
XODR_BOUNDS_RECT_COLOR = QColor(0, 120, 255)
OSM_BOUNDS_RECT_COLOR = QColor(220, 70, 70)
WORLD_EXTENT_RECT_COLOR = QColor(0, 200, 80)
WORLD_EXTENT_EDGE_HOVER_COLOR = QColor(120, 255, 160)  # brighter green on hover
WORLD_EXTENT_EDGE_DRAG_COLOR = QColor(255, 220, 60)  # amber while actively dragging
WORLD_EXTENT_EDGE_HOVER_WIDTH = 4  # cosmetic viewport-px pen width for hover / drag
EXTENT_SELECTION_FILL_COLOR = QColor(144, 238, 144, 50)  # LightGreen with alpha
EXTENT_SELECTION_PEN_COLOR = QColor(34, 139, 34)  # ForestGreen

# ---------------------------------------------------------------------------
# Z-value layer ordering
# ---------------------------------------------------------------------------
Z_ESRI_LAYER = -1
Z_CARLA_BEV_LAYER = 0
Z_OPENDRIVE_LAYER = 1
Z_OSM_LAYER = 2.0
Z_OSM_BOUNDS_RECT = 3.5
Z_XODR_BOUNDS_RECT = 4
Z_CARLA_BOUNDS_RECT = 5
Z_WORLD_EXTENT_RECT = 6
Z_GRID = 100
Z_OSM_NODE_DOTS = 200
Z_OSM_SELECTED_ARROWS = 201

XODR_SEAM_PEN_WIDTH_PX = 2.4
# Width of the exterior road-boundary marking lines drawn in the vector viewer.
XODR_MARKING_LINE_WIDTH_PX = 2.0

# ---------------------------------------------------------------------------
# Timing / timers (milliseconds)
# ---------------------------------------------------------------------------
SPINNER_TIMER_INTERVAL_MS = 100
TILE_REPAINT_THROTTLE_MS = 80
VIEW_CHANGE_DEBOUNCE_MS = 150
STATUS_FADE_DURATION_MS = 1200
INITIAL_LOAD_DELAY_MS = 100
SPLITTER_RESTORE_DELAY_MS = 150
FIT_AFTER_LOAD_DELAY_MS = 150
CENTER_ON_DELAY_MS = 30
ZOOM_RESTORE_DELAY_MS = 260
SIGINT_POLL_INTERVAL_MS = 200

# ---------------------------------------------------------------------------
# Network / timeouts
# ---------------------------------------------------------------------------
HTTP_USER_AGENT = 'ORE/1.0'
TILE_FETCH_TIMEOUT_S = 10
ESRI_TILE_FETCH_TIMEOUT_S = 15
METADATA_PROBE_TIMEOUT_S = 5
DEFAULT_SERVER_HOST = 'localhost'
DEFAULT_SERVER_PORT = 8080

# ---------------------------------------------------------------------------
# Zoom / scaling
# ---------------------------------------------------------------------------
ZOOM_IN_FACTOR = 1.25
MAX_ZOOM_SCALE = 50.0
DEFAULT_MIN_SCALE = 0.01
DEFAULT_TILE_ZOOM = 18
FIT_MARGIN_FACTOR = 0.02  # 2% margin around the combined bounds on fit-to-window

# ---------------------------------------------------------------------------
# Default coordinates / geo
# ---------------------------------------------------------------------------
DEFAULT_ORIGIN_LAT = 57.474079
DEFAULT_ORIGIN_LON = 11.984080
DEFAULT_REF_LAT = 57.474079
DEFAULT_REF_LON = 11.984080
CLI_DEFAULT_LAT = 57.474079
CLI_DEFAULT_LON = 11.984080
METERS_PER_DEGREE_LAT = 111320
DEFAULT_BOUND_EXTENT = 500.0

# ---------------------------------------------------------------------------
# OSM -> OpenDRIVE conversion defaults
# ---------------------------------------------------------------------------
OSM2XODR_SCHEMA: list = [
    (
        'lane_dimensions',
        [
            ('lane_width', 'float', '3.5'),
            ('sidewalk_width', 'float', '2.0'),
            ('bikelane_width', 'float', '1.5'),
            ('crossing_width', 'float', '4.0'),
        ],
    ),
    (
        'shape',
        [
            ('shape_match_dist', 'float', '10.0'),
        ],
    ),
    (
        'junctions',
        [
            ('junction_corner_detail', 'int', '5'),
            ('junction_scurve_stretch', 'float', '1.0'),
            ('junction_join_dist', 'float', '10.0'),
        ],
    ),
    (
        'postprocess',
        [
            ('auto_prune_split_junctions', 'bool', 'true'),
            ('prune_connection_rules', 'str', ''),
        ],
    ),
    (
        'feature_flags',
        [
            ('guess_roundabouts', 'bool', 'true'),
            ('guess_ramps', 'bool', 'true'),
            ('guess_tls_signals', 'bool', 'true'),
            ('import_sidewalks', 'bool', 'true'),
            ('import_crossings', 'bool', 'true'),
            ('import_turn_lanes', 'bool', 'true'),
            ('import_bike_access', 'bool', 'true'),
            ('import_netconvert_signs', 'bool', 'false'),
            ('remove_geometry', 'bool', 'true'),
            ('no_turnarounds', 'bool', 'true'),
        ],
    ),
    (
        'signals',
        [
            ('country', 'str', 'SE'),
        ],
    ),
    ('report', [('aggregate_warnings', 'int', '5')]),
]

DEFAULT_OSM2XODR_SETTINGS: dict = {
    section: {key: default for key, _, default in options} for section, options in OSM2XODR_SCHEMA
}

KEYBOARD_SHORTCUT_DEFAULTS: dict = {
    'file_new': 'Ctrl+N',
    'file_open': 'Ctrl+O',
    'file_save': 'Ctrl+S',
    'file_save_as': 'Ctrl+Shift+S',
    'osm_undo': 'Ctrl+Z',
    'osm_redo_primary': 'Ctrl+Shift+Z',
    'osm_redo_secondary': 'Ctrl+Y',
    'osm_delete_segment': 'Delete',
    'refresh_all_layers': 'Ctrl+R',
}

KEYBOARD_SHORTCUT_LABELS: list = [
    ('file_new', 'File: New Project'),
    ('file_open', 'File: Open Project'),
    ('file_save', 'File: Save Project'),
    ('file_save_as', 'File: Save Project As'),
    ('osm_undo', 'OSM: Undo'),
    ('osm_redo_primary', 'OSM: Redo (Primary)'),
    ('osm_redo_secondary', 'OSM: Redo (Secondary)'),
    ('osm_delete_segment', 'OSM: Delete Selected Segment'),
    ('refresh_all_layers', 'Refresh All Layers'),
]

MOUSE_BINDING_DEFAULTS: dict = {
    'stitch_way': 'Alt',
    'split_way': 'Shift',
    'delete_node': 'Ctrl',
    'add_node': 'None',
}

MOUSE_BINDING_LABELS: list = [
    ('stitch_way', 'OSM: Stitch Road (Right-click)'),
    ('split_way', 'OSM: Split Road (Right-click)'),
    ('delete_node', 'OSM: Delete Node (Right-click)'),
    ('add_node', 'OSM: Add Node (Right-click)'),
]

MOUSE_MODIFIER_OPTIONS: list = [
    'None',
    'Ctrl',
    'Shift',
    'Alt',
    'Ctrl+Shift',
    'Ctrl+Alt',
    'Shift+Alt',
    'Ctrl+Shift+Alt',
]

# ---------------------------------------------------------------------------
# UI layout dimensions
# ---------------------------------------------------------------------------
DEFAULT_CANVAS_SIZE_PX = 1920
DEFAULT_SIDEBAR_WIDTH = 308
TOGGLE_BTN_WIDTH = 18
TOGGLE_STRIP_WIDTH = 22
SIDEBAR_MIN_WIDTH = 180
SIDEBAR_DEFAULT_SAVED_WIDTH = 290
BROWSE_BTN_WIDTH = 32
ORIGIN_SPINBOX_WIDTH = 110
BOUND_SPINBOX_WIDTH = 80
LAYER_STATUS_LABEL_WIDTH = 135
CARLA_STATUS_LABEL_WIDTH = 185
TILE_ZOOM_SPINBOX_WIDTH = 44
ESRI_DRAG_BTN_WIDTH = 28
PORT_FIELD_WIDTH = 55
ZOOM_SPINBOX_WIDTH = 72
GRID_OPTION_SPINBOX_WIDTH = 40
STATUS_BAR_MARGINS = (8, 4, 8, 4)
PANEL_MARGINS = (10, 12, 10, 12)
RESIZE_HANDLE_HEIGHT = 8
TAG_KEY_FIELD_WIDTH = 80
TAG_ROW_HEIGHT = 22

# ---------------------------------------------------------------------------
# Grid / rendering
# ---------------------------------------------------------------------------
GRID_TARGET_SPACING_PX = 400.0
GRID_MIN_SPACING_PX = 300.0
GRID_MAX_SPACING_PX = 700.0
GRID_ZOOM_SPACING_EXPONENT = 0.75
GRID_FONT_FAMILY = 'Arial'
MIN_FONT_SIZE = 8
GRID_LABEL_PAD_X_V = 0.6
GRID_LABEL_PAD_X_H = -0.7
GRID_LABEL_PAD_Y_H = 0.4
GRID_LABEL_PAD_Y_V = -0.5
GRID_AXIS_PEN_WIDTH = 2.0
GRID_CROSS_PEN_WIDTH = 2.5
GRID_CROSS_RADIUS_PX = 6.0
OOR_TILE_HATCH_SPACING_PX = 64

# ---------------------------------------------------------------------------
# Defaults / thresholds
# ---------------------------------------------------------------------------
DEFAULT_OPENDRIVE_ALPHA = 0.6
OSM_HIGHLIGHT_PEN_EXTRA_WIDTH = 3.0
OSM_NODE_DOT_OUTLINE_WIDTH = 1.5
CLICK_DRAG_THRESHOLD_SQ = 25
CARLA_BOUNDS_RECT_PEN_WIDTH = 3
XODR_BOUNDS_RECT_PEN_WIDTH = 2
OSM_BOUNDS_RECT_PEN_WIDTH = 2
WORLD_EXTENT_RECT_PEN_WIDTH = 2
EXTENT_EDGE_HIT_PX = 12  # viewport-pixel tolerance for world-extent edge hover / drag
DISPLAY_ZOOM_RANGE = (100, 50000)
MIN_TILE_ZOOM = 10
BOUND_SPINBOX_RANGE = (-99999.0, 99999.0)
ESRI_OFFSET_RANGE = (-1000, 1000)
GRID_THICKNESS_RANGE = (1, 10)
GRID_FONT_SIZE_RANGE = (6, 48)
DEFAULT_GRID_COLOR_HEX = '#000000'
DEFAULT_VIEWPORT_BG_COLOR_HEX = '#9a9996'

# ---------------------------------------------------------------------------
# Concurrency / threading
# ---------------------------------------------------------------------------
ESRI_MAX_CONCURRENT_TILES = 8
CARLA_MAX_CONCURRENT_TILES = 4
GENERIC_TILE_POOL_WORKERS = 8
CARLA_TILE_MAX_RETRIES = 3
TILE_RETRY_SLEEP_S = 0.5
CARLA_TILE_RETRY_SLEEP_S = 2.0

# ---------------------------------------------------------------------------
# Raster rendering
# ---------------------------------------------------------------------------
MAX_RENDER_DIMENSION = 16384
XODR_POLYGON_STEP_M = 1.0

# ---------------------------------------------------------------------------
# Spinner animation
# ---------------------------------------------------------------------------
SPINNER_ANGLE_INCREMENT = 30
SPINNER_ICON_SIZE = 16
SPINNER_ARC_RECT = (2, 2, 12, 12)
SPINNER_ARC_SPAN_DEGREES = 270

# ---------------------------------------------------------------------------
# QSettings
# ---------------------------------------------------------------------------
QSETTINGS_ORG = 'DAS'
QSETTINGS_APP = 'ORE'

# ---------------------------------------------------------------------------
# WGS-84 ellipsoidal Transverse Mercator (Karney / Krüger 6th-order series)
# ---------------------------------------------------------------------------
# Pre-computed constants for the WGS-84 ellipsoid.
