"""Assembled OpenDriveViewer application window."""

import math
import threading

from PyQt6.QtCore import (
    pyqtSignal,
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSettings,
    Qt,
    QTimer,
)
from PyQt6.QtGui import (
    QAction,
    QColor,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGraphicsItemGroup,
    QGraphicsOpacityEffect,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from open_road_editor.constants import *  # noqa: F401,F403
from open_road_editor.viewer._layers import _LayersMixin
from open_road_editor.viewer._osm import _OsmMixin
from open_road_editor.viewer._project import _ProjectMixin
from open_road_editor.viewer._shortcuts import _ShortcutsMixin
from open_road_editor.viewer._tiles import _TilesMixin
from open_road_editor.viewer._xodr import _XodrMixin
from open_road_editor.widgets import GridItem, ZoomableGraphicsView


class OpenDriveViewer(
    _ShortcutsMixin,
    _ProjectMixin,
    _XodrMixin,
    _OsmMixin,
    _LayersMixin,
    _TilesMixin,
    QMainWindow,
):
    esri_refreshed = pyqtSignal(object, int, int, int)
    carla_bev_refreshed = pyqtSignal(object, int, int, int)
    carla_bev_meta_ready = pyqtSignal(object, int)  # (meta_dict_or_None, epoch)
    opendrive_refreshed = pyqtSignal(object, int, int)
    #: Emitted from the background thread with a list of LanePolygon objects
    xodr_polygons_ready = pyqtSignal(object)
    #: Emitted from background thread with parsed OSM way geometries
    osm_ways_ready = pyqtSignal(object)
    #: Emitted by background layer pipeline planner with UI-safe actions
    layer_pipeline_ready = pyqtSignal(object, int)
    #: Emitted from background thread when netconvert auto-converts OSM→XODR after a drag
    xodr_auto_converted = pyqtSignal(str)
    #: Emitted from background thread with a list of signal dictionaries
    xodr_signals_ready = pyqtSignal(object)

    def __init__(
        self,
        carla_bev_img,
        opendrive_img,
        esri_img,
        town_name,
        max_grid_lines=10,
        map_ctx=None,
        node=None,
        show_carla_bev=None,
        show_esri=None,
        show_opendrive=None,
        show_grid=None,
        xodr_path=None,
        server_ip=None,
        server_port=None,
    ):
        super().__init__()
        self.node = node
        self.server_ip = (
            getattr(node, 'tcp_server_ip', server_ip or DEFAULT_SERVER_HOST)
            if node
            else (server_ip or DEFAULT_SERVER_HOST)
        )
        self.server_port = (
            getattr(node, 'tcp_server_port', server_port or DEFAULT_SERVER_PORT)
            if node
            else (server_port or DEFAULT_SERVER_PORT)
        )
        # Remember CLI-supplied values so new_project() won't wipe them.
        self._cli_server_ip = server_ip
        self._cli_server_port = server_port
        self.town_name = town_name
        self.xodr_path = xodr_path
        self.settings = QSettings(QSETTINGS_ORG, QSETTINGS_APP)
        self.project_file_path: str | None = None
        self._preferred_project_save_dir: str | None = None
        self._refresh_window_title()
        self._pending_project_zoom_pct: int | None = None
        self._pending_project_viewport_center: tuple | None = None  # (scene_x, scene_y)
        self._pending_project_view_scale: float | None = None
        self._pending_project_world_center: tuple | None = None  # (world_x, world_y)
        self._xodr_content: str | None = None
        self._osm_content: str | None = None
        self._osm2xodr_settings = self._normalize_osm2xodr_settings(
            self.settings.value('osm2xodr_settings')
        )
        self._restoring_project_payload = False
        self._suppress_next_xodr_title_update = False
        self._suppress_auto_fit = False

        self.esri_refreshed.connect(self.on_esri_refreshed)
        self.carla_bev_refreshed.connect(self.on_carla_bev_refreshed)
        self.carla_bev_meta_ready.connect(self._on_carla_bev_meta_ready)
        self.opendrive_refreshed.connect(self.on_opendrive_refreshed)
        self.xodr_polygons_ready.connect(self._on_xodr_polygons_ready)
        self.osm_ways_ready.connect(self._on_osm_ways_ready)
        self.layer_pipeline_ready.connect(self._on_layer_pipeline_ready)
        self.xodr_auto_converted.connect(self._on_xodr_auto_converted)
        self.xodr_signals_ready.connect(self._on_xodr_signals_ready)

        self.spinner_angle = 0
        self.carla_bev_spinner_angle = 0
        self.spinner_timer = QTimer()
        self.spinner_timer.timeout.connect(self.update_refresh_spinner)
        self.opendrive_loading = False
        self._carla_bev_loading = False
        self._carla_bev_pct = 0
        self._carla_bev_epoch = 0
        self._carla_bev_loaded_zoom = None
        self._carla_bev_loading_zoom = None
        self._esri_loading = False
        self._esri_pct = 0
        self._esri_epoch = 0
        self._esri_loaded_zoom = None
        self._esri_loading_zoom = None
        # ── Per-tile ESRI viewport-aware state ───────────────────────────
        self._esri_pix_data = None  # H×W×4 RGBA canvas for current zoom
        self._esri_pix_lock = threading.Lock()
        self._esri_fetch_lock = threading.Lock()
        self._esri_fetched_tiles = set()  # (tx,ty) already painted at current zoom
        self._esri_fetching_tiles = set()  # (tx,ty) currently in-flight
        self._esri_current_zoom = DEFAULT_TILE_ZOOM
        self._esri_vis_done = 0
        self._esri_vis_total = 0
        self._esri_tile_sema = threading.Semaphore(
            ESRI_MAX_CONCURRENT_TILES
        )  # max concurrent tile downloads
        self._esri_view_change_timer = QTimer()
        self._esri_view_change_timer.setSingleShot(True)
        self._esri_view_change_timer.timeout.connect(self._on_esri_view_changed)
        self._esri_repaint_timer = QTimer()
        self._esri_repaint_timer.setSingleShot(True)
        self._esri_repaint_timer.timeout.connect(self._esri_do_repaint)
        # ── Per-tile Carla_Bev (CARLA) viewport-aware state ─────────────────
        self._carla_bev_pix_data = None  # H×W×4 RGBA canvas for current zoom
        self._carla_bev_pix_lock = threading.Lock()
        self._carla_bev_fetch_lock = threading.Lock()
        self._carla_bev_fetched_tiles = set()  # (tx,ty) already painted at current zoom
        self._carla_bev_fetching_tiles = set()  # (tx,ty) currently in-flight
        self._carla_bev_placeholder_tiles = (
            set()
        )  # (tx,ty) painted as "Not loaded" (offline cache miss)
        self._carla_bev_current_zoom = DEFAULT_TILE_ZOOM
        self._carla_bev_vis_done = 0  # tiles genuinely loaded (cache or server)
        self._carla_bev_vis_processed = 0  # tiles handled (done + placeholders)
        self._carla_bev_vis_total = 0
        self._carla_bev_tile_sema = threading.Semaphore(
            CARLA_MAX_CONCURRENT_TILES
        )  # CARLA can be slow
        self._carla_bev_view_change_timer = QTimer()
        self._carla_bev_view_change_timer.setSingleShot(True)
        self._carla_bev_view_change_timer.timeout.connect(self._on_carla_bev_view_changed)
        self._carla_bev_repaint_timer = QTimer()
        self._carla_bev_repaint_timer.setSingleShot(True)
        self._carla_bev_repaint_timer.timeout.connect(self._carla_bev_do_repaint)
        # Server metadata (fetched once per refresh)
        self._carla_bev_server_online: bool | None = None  # None=unknown, True/False after probe
        self._carla_bev_server_meta: dict | None = None  # last successful /metadata response
        self._carla_bev_server_bounds: tuple | None = None  # (tx_min,tx_max,ty_min,ty_max) at zoom
        self._carla_bev_bounds_rect_item: QGraphicsRectItem | None = (
            None  # server world-bounds overlay
        )
        self._xodr_bounds_rect_item: QGraphicsRectItem | None = None  # XODR extent overlay
        self._osm_bounds_rect_item: QGraphicsRectItem | None = None  # OSM extent overlay
        # Four individual line items — one per edge — so each can be highlighted.
        self._world_extent_edge_items: dict = {
            'N': None,
            'S': None,
            'E': None,
            'W': None,
        }
        self._extent_hover_edge: str | None = None  # which edge is currently highlighted
        self._fit_scale = DEFAULT_MIN_SCALE  # set after first fit_to_window; used as zoom floor
        # Vector OpenDRIVE overlay (QGraphicsItemGroup of QGraphicsPathItems)
        self._xodr_vector_group: QGraphicsItemGroup | None = None
        self._xodr_is_vector: bool = False  # True when vector group is active
        # Hover highlighting for vector lanes — keyed by lane connectivity
        self._xodr_item_meta: dict = {}  # item → (lane_key, pred_key, succ_key, base_brush, topo_key)
        self._xodr_lane_key_to_item: dict = {}  # lane_key → item
        self._xodr_topology_key_to_items: dict = {}  # topo_key → [items]
        # Reverse dicts to handle one-directional XODR links:
        #   _xodr_pred_back[key] → items whose pred_key == key  (= successors of key)
        #   _xodr_succ_back[key] → items whose succ_key == key  (= predecessors of key)
        self._xodr_pred_back: dict = {}
        self._xodr_succ_back: dict = {}
        self._xodr_lane_points_scene: dict = {}  # lane_key -> [(sx, sy), ...]
        self._xodr_vector_signal_items: list = []  # list of QGraphicsItemGroup for signals

        # OSM vector overlay state
        self.osm_path: str | None = None
        self._osm_vector_group: QGraphicsItemGroup | None = None
        self._osm_loading: bool = False
        # OSM hover / selection highlighting
        # item → (highway, tags, base_pen, scene_coords, latlon_coords, way_id, node_refs)
        self._osm_item_meta: dict = {}
        self._osm_way_connectivity: dict = {}  # way_id -> (preceding_ids, succeeding_ids)
        self._osm_hover_item = None
        self._osm_selected_item = None
        self._osm_sign_item_positions: dict = {}  # sign item → (px, py)
        self._osm_selected_sign_item = None  # currently selected sign node item
        self._osm_selected_sign_node_id: str | None = None
        # OSM editing state
        self._osm_node_dots: list = []  # QGraphicsEllipseItem markers on selected segment
        self._osm_dot_to_index: dict = {}  # dot_item → node index
        self._osm_selected_dot = None  # currently selected node dot on selected segment
        self._osm_selected_node_index: int | None = None  # selected node index on segment
        self._osm_selected_arrows: list = []  # direction arrows for selected segment
        self._osm_dragging_dot = None  # dot currently being dragged
        self._osm_dragging_way_item = None  # selected roundabout way currently being dragged
        self._osm_way_drag_last_scene = None  # QPointF of last drag sample
        self._osm_way_drag_had_motion: bool = False
        self._osm_drag_start_scene = None  # (sx, sy) scene pos when drag began
        self._osm_drag_start_latlon = None  # (lat, lon) when drag began
        self._osm_click_press_pos = None  # viewport mouse pos at press for click-vs-drag
        self._osm_edits: dict = {}  # way_id → {'tags': {...}, 'node_coords': [(lat,lon),…]}
        self._osm_node_tag_edits: dict = {}  # node_id → {'tag': 'value', ...}
        self._osm_created_ways: dict = {}  # new_way_id → {'tags': {...}, 'node_coords': [...]}
        self._osm_deleted_way_ids: set = set()  # existing way IDs removed by merge
        self._osm_deleted_node_ids: set = set()  # standalone node IDs explicitly deleted
        self._osm_dirty: bool = False  # True when OSM edits changed since last successful save
        self._osm_relation_edit_mode: dict = {'preceding': False, 'succeeding': False}
        self._osm_relation_draft: dict = {'preceding': None, 'succeeding': None}
        self._osm_tags_edit_mode: bool = False
        self._osm_node_tags_edit_mode: bool = False
        self._osm_relation_hover_map: dict = {}  # widget -> way_id
        self._osm_relation_pick_mode: str | None = None  # relation currently awaiting map pick
        self._osm_suppress_next_click_select: bool = (
            False  # skip one release-click after relation pick
        )
        self._osm_rect_select_start = None
        self._osm_rect_select_rect_item: QGraphicsRectItem | None = None
        self._osm_multi_selected_items: set = set()
        self._osm_original_tree = None  # parsed ET tree (read-only reference)
        self._osm_undo_stack: list = []  # list of move-action dicts
        self._osm_redo_stack: list = []  # list of move-action dicts
        self._mark_osm_dirty_after_load: bool = False
        self._osm_next_node_id: int = 1  # generated IDs for new nodes
        self._osm_next_way_id: int = 1  # generated IDs for new ways
        self._esri_nudge_step: float = float(self.settings.value('esri_nudge_step', 0.1))
        self._esri_shift_nudge_step: float = float(
            self.settings.value('esri_shift_nudge_step', 1.0)
        )

        # Async layer-toggle pipeline state
        self._layer_pipeline_seq = 0
        self._layer_pipeline_lock = threading.Lock()
        self._suppress_async_layer_pipeline = False
        self._last_mouse_scene_pos = QPointF(0.0, 0.0)
        self._osm_blink_item = None
        self._osm_blink_on = False
        self._osm_blink_timer = QTimer(self)
        self._osm_blink_timer.setInterval(180)
        self._osm_blink_timer.timeout.connect(self._on_osm_blink_tick)

        self.max_grid_lines = max_grid_lines
        self.map_ctx = map_ctx

        # ── Central widget: splitter (map | right_pane) ─────────────────
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        main_layout = QHBoxLayout(self.central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(self.splitter)

        # ── Map view ──────────────────────────────────────────────────────
        self.scene = QGraphicsScene()
        self.view = ZoomableGraphicsView(self.scene)
        _bg_hex = self.settings.value('viewport_bg_color', DEFAULT_VIEWPORT_BG_COLOR_HEX)
        self.view.setBackgroundBrush(QColor(_bg_hex))
        self.splitter.addWidget(self.view)

        # ── Right pane: toggle strip + sidebar ────────────────────────────
        self._right_pane = QWidget()
        self._right_pane.setMinimumWidth(TOGGLE_BTN_WIDTH)
        _right_layout = QHBoxLayout(self._right_pane)
        _right_layout.setContentsMargins(0, 0, 0, 0)
        _right_layout.setSpacing(0)
        self.splitter.addWidget(self._right_pane)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)

        # ── Sidebar collapse/expand toggle button ────────────────────────
        self._sidebar_visible = True
        self.toggle_btn = QPushButton('▶')
        self.toggle_btn.setFixedWidth(TOGGLE_BTN_WIDTH)
        self.toggle_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.toggle_btn.setToolTip('Collapse / expand panel')
        self.toggle_btn.clicked.connect(self.toggle_sidebar)
        _right_layout.addWidget(self.toggle_btn)

        # ── Sidebar ───────────────────────────────────────────────────────
        self.sidebar = QFrame()
        self.sidebar.setMinimumWidth(SIDEBAR_MIN_WIDTH)
        self.sidebar.setFrameShape(QFrame.Shape.NoFrame)
        _right_layout.addWidget(self.sidebar)
        sidebar_outer = QVBoxLayout(self.sidebar)
        sidebar_outer.setContentsMargins(0, 0, 0, 0)
        sidebar_outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_outer.addWidget(scroll)

        self.status_bar = QFrame()
        self.status_bar.setFrameShape(QFrame.Shape.StyledPanel)
        self.status_bar.setObjectName('rightPaneStatusBar')
        status_layout = QHBoxLayout(self.status_bar)
        status_layout.setContentsMargins(*STATUS_BAR_MARGINS)
        self.lbl_project_status = QLabel('')
        self.lbl_project_status.setWordWrap(True)
        self.lbl_project_status.setVisible(False)
        status_layout.addWidget(self.lbl_project_status)
        sidebar_outer.addWidget(self.status_bar)

        self._project_status_effect = QGraphicsOpacityEffect(self.lbl_project_status)
        self.lbl_project_status.setGraphicsEffect(self._project_status_effect)
        self._project_status_effect.setOpacity(0.0)
        self._project_status_hide_timer = QTimer(self)
        self._project_status_hide_timer.setSingleShot(True)
        self._project_status_hide_timer.timeout.connect(self._start_project_status_fade)
        self._project_status_fade_anim = QPropertyAnimation(
            self._project_status_effect, b'opacity', self
        )
        self._project_status_fade_anim.setDuration(STATUS_FADE_DURATION_MS)
        self._project_status_fade_anim.setStartValue(1.0)
        self._project_status_fade_anim.setEndValue(0.0)
        self._project_status_fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._project_status_fade_anim.finished.connect(self._on_project_status_fade_finished)

        content_widget = QWidget()
        scroll.setWidget(content_widget)
        panel = QVBoxLayout(content_widget)
        panel.setContentsMargins(*PANEL_MARGINS)
        panel.setSpacing(10)

        def make_group(title):
            return QGroupBox(title)

        def make_sep():
            s = QFrame()
            s.setFrameShape(QFrame.Shape.HLine)
            s.setFrameShadow(QFrame.Shadow.Sunken)
            return s

        # ── OpenDRIVE File (hidden – use File > Import) ────────────────
        self._xodr_file_container = QWidget()
        _xodr_container_layout = QVBoxLayout(self._xodr_file_container)
        _xodr_container_layout.setContentsMargins(0, 0, 0, 0)
        _xodr_container_layout.setSpacing(0)
        hdr_xodr = QLabel('OpenDRIVE File')
        _xodr_container_layout.addWidget(hdr_xodr)

        xodr_row = QHBoxLayout()
        xodr_row.setSpacing(4)
        self.edit_xodr = QLineEdit()
        self.edit_xodr.setPlaceholderText('Path to .xodr file…')
        self.edit_xodr.setText(self.xodr_path if self.xodr_path else '')
        self.edit_xodr.textChanged.connect(self.on_xodr_path_changed)
        xodr_row.addWidget(self.edit_xodr)

        self.btn_browse_xodr = QPushButton()
        self.btn_browse_xodr.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        )
        self.btn_browse_xodr.setFixedWidth(BROWSE_BTN_WIDTH)
        self.btn_browse_xodr.setToolTip('Browse for .xodr file')
        self.btn_browse_xodr.clicked.connect(self.browse_xodr)
        xodr_row.addWidget(self.btn_browse_xodr)
        _xodr_container_layout.addLayout(xodr_row)
        self._xodr_file_container.setVisible(False)
        panel.addWidget(self._xodr_file_container)

        # ── OSM File (hidden – use File > Import) ─────────────────────
        self._osm_file_container = QWidget()
        _osm_container_layout = QVBoxLayout(self._osm_file_container)
        _osm_container_layout.setContentsMargins(0, 0, 0, 0)
        _osm_container_layout.setSpacing(0)
        hdr_osm = QLabel('OSM File')
        _osm_container_layout.addWidget(hdr_osm)

        osm_row = QHBoxLayout()
        osm_row.setSpacing(4)
        self.edit_osm = QLineEdit()
        self.edit_osm.setPlaceholderText('Path to .osm file…')
        self.edit_osm.textChanged.connect(self.on_osm_path_changed)
        osm_row.addWidget(self.edit_osm)

        self.btn_browse_osm = QPushButton()
        self.btn_browse_osm.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        )
        self.btn_browse_osm.setFixedWidth(BROWSE_BTN_WIDTH)
        self.btn_browse_osm.setToolTip('Browse for .osm file')
        self.btn_browse_osm.clicked.connect(self.browse_osm)
        osm_row.addWidget(self.btn_browse_osm)
        _osm_container_layout.addLayout(osm_row)
        self._osm_file_container.setVisible(False)
        panel.addWidget(self._osm_file_container)

        # ── World Info ────────────────────────────────────────────────────
        self.grp_world_info = make_group('World')
        world_info_layout = QVBoxLayout(self.grp_world_info)
        world_info_layout.setContentsMargins(10, 8, 10, 10)
        world_info_layout.setSpacing(4)
        self.btn_world_edit_mode = QPushButton(self.grp_world_info)
        self._style_osm_lock_button(
            self.btn_world_edit_mode,
            self.settings.value('world_edit_mode', False, type=bool),
            'World edit mode disabled',
        )
        self.btn_world_edit_mode.toggled.connect(self._on_world_edit_mode_toggled)
        self._position_world_edit_mode_button()

        # ── Origin Group ──────────────────────────────────────────────────
        grp_origin = make_group('Origin')
        origin_layout = QVBoxLayout(grp_origin)
        origin_layout.setContentsMargins(6, 6, 6, 6)
        origin_layout.setSpacing(2)
        world_info_layout.addWidget(grp_origin)

        # Origin row: Lat / Lon
        origin_row = QHBoxLayout()
        origin_row.setSpacing(4)
        origin_row.addWidget(QLabel('Lat:'))
        self.spin_origin_lat = QDoubleSpinBox()
        self.spin_origin_lat.setDecimals(6)
        self.spin_origin_lat.setRange(-90.0, 90.0)
        self.spin_origin_lat.setSingleStep(0.001)
        self.spin_origin_lat.setValue(DEFAULT_ORIGIN_LAT)
        self.spin_origin_lat.setFixedWidth(ORIGIN_SPINBOX_WIDTH)
        origin_row.addWidget(self.spin_origin_lat)
        origin_row.addWidget(QLabel('Lon:'))
        self.spin_origin_lon = QDoubleSpinBox()
        self.spin_origin_lon.setDecimals(6)
        self.spin_origin_lon.setRange(-180.0, 180.0)
        self.spin_origin_lon.setSingleStep(0.001)
        self.spin_origin_lon.setValue(DEFAULT_ORIGIN_LON)
        self.spin_origin_lon.setFixedWidth(ORIGIN_SPINBOX_WIDTH)
        origin_row.addWidget(self.spin_origin_lon)
        origin_row.addStretch()
        origin_layout.addLayout(origin_row)

        # ── Bounds Group ──────────────────────────────────────────────────
        grp_bounds = make_group('Bounds')
        bounds_layout = QVBoxLayout(grp_bounds)
        bounds_layout.setContentsMargins(6, 6, 6, 6)
        bounds_layout.setSpacing(2)
        world_info_layout.addWidget(grp_bounds)

        # Bounds row: North / South / East / West  (metres, origin at centre)
        bounds_row = QHBoxLayout()
        bounds_row.setSpacing(4)
        self.btn_select_extent = QPushButton('\u25a3')
        self.btn_select_extent.setCheckable(True)
        self.btn_select_extent.setFixedWidth(ESRI_DRAG_BTN_WIDTH)
        self.btn_select_extent.setToolTip('Select world extent region on map')
        self.btn_select_extent.toggled.connect(self._on_extent_select_toggle)
        bounds_row.addWidget(self.btn_select_extent)
        bounds_row.addWidget(QLabel('N:'))
        self.spin_bound_north = QDoubleSpinBox()
        self.spin_bound_north.setDecimals(2)
        self.spin_bound_north.setRange(*BOUND_SPINBOX_RANGE)
        self.spin_bound_north.setSingleStep(10.0)
        self.spin_bound_north.setValue(DEFAULT_BOUND_EXTENT)
        self.spin_bound_north.setFixedWidth(BOUND_SPINBOX_WIDTH)
        bounds_row.addWidget(self.spin_bound_north)
        bounds_row.addWidget(QLabel('S:'))
        self.spin_bound_south = QDoubleSpinBox()
        self.spin_bound_south.setDecimals(2)
        self.spin_bound_south.setRange(*BOUND_SPINBOX_RANGE)
        self.spin_bound_south.setSingleStep(10.0)
        self.spin_bound_south.setValue(DEFAULT_BOUND_EXTENT)
        self.spin_bound_south.setFixedWidth(BOUND_SPINBOX_WIDTH)
        bounds_row.addWidget(self.spin_bound_south)
        bounds_row.addWidget(QLabel('E:'))
        self.spin_bound_east = QDoubleSpinBox()
        self.spin_bound_east.setDecimals(2)
        self.spin_bound_east.setRange(*BOUND_SPINBOX_RANGE)
        self.spin_bound_east.setSingleStep(10.0)
        self.spin_bound_east.setValue(DEFAULT_BOUND_EXTENT)
        self.spin_bound_east.setFixedWidth(BOUND_SPINBOX_WIDTH)
        bounds_row.addWidget(self.spin_bound_east)
        bounds_row.addWidget(QLabel('W:'))
        self.spin_bound_west = QDoubleSpinBox()
        self.spin_bound_west.setDecimals(2)
        self.spin_bound_west.setRange(*BOUND_SPINBOX_RANGE)
        self.spin_bound_west.setSingleStep(10.0)
        self.spin_bound_west.setValue(DEFAULT_BOUND_EXTENT)
        self.spin_bound_west.setFixedWidth(BOUND_SPINBOX_WIDTH)
        bounds_row.addWidget(self.spin_bound_west)
        bounds_row.addStretch()
        bounds_layout.addLayout(bounds_row)

        update_bounds_row = QHBoxLayout()
        update_bounds_row.setSpacing(6)
        update_bounds_row.addWidget(QLabel('Update with'))

        self.btn_world_update_xodr = QPushButton('OpenDRIVE')
        self.btn_world_update_xodr.setToolTip(
            'Update world origin and bounds to match the loaded OpenDRIVE file'
        )
        self.btn_world_update_xodr.setStyleSheet(
            'QPushButton { border: 2px solid rgb(0, 120, 255); border-radius: 3px; padding: 2px 6px; }'
            ' QPushButton:hover { background-color: rgba(0, 120, 255, 40); }'
        )
        self.btn_world_update_xodr.clicked.connect(self.fit_world_extent_to_xodr)
        update_bounds_row.addWidget(self.btn_world_update_xodr)
        self.btn_world_update_carla = QPushButton('CARLA')
        self.btn_world_update_carla.setToolTip(
            'Update world origin and bounds to match the CARLA server'
        )
        self.btn_world_update_carla.setStyleSheet(
            'QPushButton { border: 2px solid rgb(255, 140, 0); border-radius: 3px; padding: 2px 6px; }'
            ' QPushButton:hover { background-color: rgba(255, 140, 0, 40); }'
        )
        self.btn_world_update_carla.clicked.connect(self.fit_world_extent_to_carla)
        update_bounds_row.addWidget(self.btn_world_update_carla)
        update_bounds_row.addStretch()
        bounds_layout.addLayout(update_bounds_row)

        # Connect signals to push values into map_ctx
        self.spin_origin_lat.valueChanged.connect(self._on_world_extent_changed)
        self.spin_origin_lon.valueChanged.connect(self._on_world_extent_changed)
        self.spin_bound_north.valueChanged.connect(self._on_world_extent_changed)
        self.spin_bound_south.valueChanged.connect(self._on_world_extent_changed)
        self.spin_bound_east.valueChanged.connect(self._on_world_extent_changed)
        self.spin_bound_west.valueChanged.connect(self._on_world_extent_changed)
        self._on_world_edit_mode_toggled(self.btn_world_edit_mode.isChecked())

        panel.addWidget(self.grp_world_info)

        # ── Layers ────────────────────────────────────────────────────────
        grp_layers = make_group('Layers')
        self._layers_layout = QVBoxLayout(grp_layers)
        self._layers_layout.setContentsMargins(10, 8, 10, 10)
        self._layers_layout.setSpacing(6)

        # ── OpenDRIVE layer row (wrapped in a hideable container) ─────
        self._opendrive_layer_widget = QWidget()
        _xodr_row = QHBoxLayout(self._opendrive_layer_widget)
        _xodr_row.setContentsMargins(0, 0, 0, 0)
        self.check_opendrive = QCheckBox('OpenDRIVE')
        if show_opendrive is not None:
            self.check_opendrive.setChecked(show_opendrive)
        else:
            self.check_opendrive.setChecked(
                self.settings.value('show_opendrive', False, type=bool)
            )
        self.check_opendrive.stateChanged.connect(self._on_layer_checkbox_changed)
        _xodr_row.addWidget(self.check_opendrive)
        self.check_opendrive_objects = QCheckBox('Objects')
        self.check_opendrive_objects.setChecked(
            self.settings.value('show_opendrive_objects', True, type=bool)
        )
        self.check_opendrive_objects.setEnabled(self.check_opendrive.isChecked())
        self.check_opendrive_objects.stateChanged.connect(self._on_layer_checkbox_changed)
        _xodr_row.addWidget(self.check_opendrive_objects)
        _xodr_row.addStretch()
        self.lbl_opendrive_status = QLabel('')
        self.lbl_opendrive_status.setFixedWidth(LAYER_STATUS_LABEL_WIDTH)
        self.lbl_opendrive_status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        _xodr_row.addWidget(self.lbl_opendrive_status)
        self._opendrive_layer_widget.setVisible(False)
        self._layers_layout.addWidget(self._opendrive_layer_widget)

        # ── OSM layer row (wrapped in a hideable container) ───────────
        self._osm_layer_widget = QWidget()
        _osm_row = QHBoxLayout(self._osm_layer_widget)
        _osm_row.setContentsMargins(0, 0, 0, 0)
        self.check_osm = QCheckBox('OSM')
        self.check_osm.setChecked(self.settings.value('show_osm', False, type=bool))
        self.check_osm.stateChanged.connect(self._on_layer_checkbox_changed)
        _osm_row.addWidget(self.check_osm)
        self.check_osm_objects = QCheckBox('Objects')
        self.check_osm_objects.setChecked(self.settings.value('show_osm_objects', True, type=bool))
        self.check_osm_objects.setEnabled(self.check_osm.isChecked())
        self.check_osm_objects.stateChanged.connect(self._on_layer_checkbox_changed)
        _osm_row.addWidget(self.check_osm_objects)
        _osm_row.addStretch()
        self.lbl_osm_status = QLabel('')
        self.lbl_osm_status.setFixedWidth(LAYER_STATUS_LABEL_WIDTH)
        self.lbl_osm_status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        _osm_row.addWidget(self.lbl_osm_status)
        self._osm_layer_widget.setVisible(False)
        self._layers_layout.addWidget(self._osm_layer_widget)

        self.check_esri = QCheckBox('ESRI World Imagery')
        if show_esri is not None:
            self.check_esri.setChecked(show_esri)
        else:
            self.check_esri.setChecked(self.settings.value('show_esri', False, type=bool))
        self.check_esri.stateChanged.connect(self._on_layer_checkbox_changed)
        _esri_row = QHBoxLayout()
        _esri_row.addWidget(self.check_esri)
        _esri_row.addStretch()
        self.lbl_esri_status = QLabel('')
        self.lbl_esri_status.setFixedWidth(LAYER_STATUS_LABEL_WIDTH)
        self.lbl_esri_status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        _esri_row.addWidget(self.lbl_esri_status)
        self._layers_layout.addLayout(_esri_row)

        self.check_carla_bev = QCheckBox('CARLA Tile Server')
        if show_carla_bev is not None:
            self.check_carla_bev.setChecked(show_carla_bev)
        else:
            self.check_carla_bev.setChecked(
                self.settings.value('show_carla_bev', False, type=bool)
            )
        self.check_carla_bev.stateChanged.connect(self._on_layer_checkbox_changed)
        _carla_bev_row = QHBoxLayout()
        _carla_bev_row.addWidget(self.check_carla_bev)
        _carla_bev_row.addStretch()
        self.lbl_carla_bev_status = QLabel('')
        self.lbl_carla_bev_status.setFixedWidth(CARLA_STATUS_LABEL_WIDTH)
        self.lbl_carla_bev_status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        _carla_bev_row.addWidget(self.lbl_carla_bev_status)
        self._layers_layout.addLayout(_carla_bev_row)

        # ── Tile Zoom row (always active, shared by ESRI + carla_bev) ────────
        _tz_row = QHBoxLayout()
        _tz_row.setSpacing(6)
        _tz_row.addWidget(QLabel('Tile Zoom:'))
        self.spin_tile_zoom = QSpinBox()
        _max_tz = min(
            ESRI_TILE_MAX_ZOOM,
            self.map_ctx.tile_max_zoom_level if self.map_ctx else ESRI_TILE_MAX_ZOOM,
        )
        self.spin_tile_zoom.setRange(MIN_TILE_ZOOM, _max_tz)
        self.spin_tile_zoom.setFixedWidth(TILE_ZOOM_SPINBOX_WIDTH)
        self.spin_tile_zoom.setValue(int(self.settings.value('tile_zoom', _max_tz)))
        self.spin_esri_zoom = self.spin_tile_zoom
        self.spin_carla_bev_zoom = self.spin_tile_zoom
        _tz_row.addWidget(self.spin_tile_zoom)
        self.btn_tile_zoom_refresh = QPushButton('Refresh')
        self.btn_tile_zoom_refresh.setToolTip(
            'Re-fetch ESRI and carla_bev tiles at the current tile zoom level'
        )
        self.btn_tile_zoom_refresh.clicked.connect(self.on_tile_zoom_refresh)
        _tz_row.addWidget(self.btn_tile_zoom_refresh)
        _tz_row.addStretch()
        self.lbl_tile_mpp = QLabel()
        self.lbl_tile_mpp.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        def _update_mpp_label(zoom: int) -> None:
            ref_lat = self.map_ctx.earth_ref_lat if self.map_ctx else DEFAULT_REF_LAT
            mpp = (EARTH_CIRCUMFERENCE * math.cos(math.radians(ref_lat))) / ((2**zoom) * TILE_SIZE)
            self.lbl_tile_mpp.setText(f'meters/pixel: {mpp:.4f}')

        self.spin_tile_zoom.valueChanged.connect(_update_mpp_label)
        _update_mpp_label(self.spin_tile_zoom.value())
        _tz_row.addWidget(self.lbl_tile_mpp)
        self._layers_layout.addLayout(_tz_row)

        panel.addWidget(grp_layers)

        # ── OpenDRIVE Options ─────────────────────────────────────────────
        self.grp_xodr_opts = make_group('OpenDRIVE Options')
        xodr_opts_layout = QVBoxLayout(self.grp_xodr_opts)
        xodr_opts_layout.setContentsMargins(10, 8, 10, 10)
        xodr_opts_layout.setSpacing(6)

        alpha_row = QHBoxLayout()
        alpha_row.addWidget(QLabel('Opacity:'))
        self.spin_opendrive_alpha = QDoubleSpinBox()
        self.spin_opendrive_alpha.setRange(0.0, 1.0)
        self.spin_opendrive_alpha.setSingleStep(0.05)
        self.spin_opendrive_alpha.setValue(
            float(self.settings.value('opendrive_alpha', DEFAULT_OPENDRIVE_ALPHA))
        )
        self.spin_opendrive_alpha.valueChanged.connect(self._on_opendrive_opacity_changed)
        # Saved alpha restored when a second layer is turned on; False = multi-layer mode
        self._alpha_saved = float(self.settings.value('opendrive_alpha', DEFAULT_OPENDRIVE_ALPHA))
        self._alpha_only_mode = False  # resolved correctly on first update_visibility()
        alpha_row.addWidget(self.spin_opendrive_alpha)
        alpha_row.addStretch()
        xodr_opts_layout.addLayout(alpha_row)

        # ── OSM Options ───────────────────────────────────────────────────
        self.grp_osm_opts = make_group('OSM Options')
        osm_opts_layout = QVBoxLayout(self.grp_osm_opts)
        osm_opts_layout.setContentsMargins(10, 8, 10, 10)
        osm_opts_layout.setSpacing(6)

        self.btn_osm_edit_mode = QPushButton(self.grp_osm_opts)
        self.btn_osm_edit_mode.setCheckable(True)
        self.btn_osm_edit_mode.setChecked(self.settings.value('osm_edit_mode', False, type=bool))
        self.btn_osm_edit_mode.setFixedSize(24, 20)
        self.btn_osm_edit_mode.setStyleSheet(
            'QPushButton { padding: 0; border: 1px solid palette(mid); border-radius: 4px; background: palette(button); }'
            'QPushButton:checked { background: palette(base); }'
        )
        self.btn_osm_edit_mode.toggled.connect(self._on_osm_edit_mode_toggled)
        self._on_osm_edit_mode_toggled(self.btn_osm_edit_mode.isChecked())
        self._position_osm_edit_mode_button()

        osm_alpha_row = QHBoxLayout()
        self.btn_osm_select_segments = QPushButton('\u25a3')
        self.btn_osm_select_segments.setCheckable(True)
        self.btn_osm_select_segments.setFixedWidth(ESRI_DRAG_BTN_WIDTH)
        self.btn_osm_select_segments.setToolTip('Select OSM segments touched by a rectangle')
        self.btn_osm_select_segments.toggled.connect(self._on_osm_rect_select_toggle)
        osm_alpha_row.addWidget(self.btn_osm_select_segments)
        osm_alpha_row.addWidget(QLabel('Opacity:'))
        self.spin_osm_alpha = QDoubleSpinBox()
        self.spin_osm_alpha.setRange(0.0, 1.0)
        self.spin_osm_alpha.setSingleStep(0.05)
        self.spin_osm_alpha.setValue(float(self.settings.value('osm_alpha', 0.6)))
        self.spin_osm_alpha.valueChanged.connect(self._on_osm_opacity_changed)
        osm_alpha_row.addWidget(self.spin_osm_alpha)
        self.btn_osm2xodr_settings = QPushButton('osm2xodr config')
        self.btn_osm2xodr_settings.setToolTip(
            'Edit netconvert settings used to convert imported OSM to OpenDRIVE'
        )
        self.btn_osm2xodr_settings.clicked.connect(self._open_osm2xodr_settings_dialog)
        osm_alpha_row.addWidget(self.btn_osm2xodr_settings)
        self.btn_reverse_osm_sign = QPushButton('Reverse Sign')
        self.btn_reverse_osm_sign.setToolTip(
            'Add a 180 degree heading offset to the selected OSM sign node and regenerate OpenDRIVE'
        )
        self.btn_reverse_osm_sign.clicked.connect(self._reverse_selected_osm_sign)
        self.btn_reverse_osm_sign.setVisible(False)
        osm_alpha_row.addWidget(self.btn_reverse_osm_sign)
        osm_alpha_row.addStretch()
        osm_opts_layout.addLayout(osm_alpha_row)

        # Dynamic OSM segment properties (shown on hover / selection — editable)
        self._osm_props_group = QGroupBox('Segment Properties')
        self._osm_props_group.setVisible(False)
        self.btn_osm_props_edit_mode = QPushButton(self._osm_props_group)
        self.btn_osm_props_edit_mode.setCheckable(True)
        self.btn_osm_props_edit_mode.setChecked(False)
        self.btn_osm_props_edit_mode.setFixedSize(24, 20)
        self.btn_osm_props_edit_mode.toggled.connect(self._on_osm_props_edit_mode_toggled)
        self._on_osm_props_edit_mode_toggled(False)
        self._position_osm_props_edit_mode_button()
        _props_group_layout = QVBoxLayout(self._osm_props_group)
        _props_group_layout.setContentsMargins(4, 4, 4, 4)
        _props_group_layout.setSpacing(2)
        # Scrollable tag editor container (height is user-adjustable)
        self._osm_tag_editor_widget = QWidget()
        self._osm_tag_rows: list = []
        self._osm_props_scroll = QScrollArea()
        self._osm_props_scroll.setWidgetResizable(True)
        self._osm_props_scroll.setWidget(self._osm_tag_editor_widget)
        _line_height = QLabel().fontMetrics().lineSpacing()
        _default_h = _line_height * OSM_SEGMENT_PROP_MAX_LINES + 8
        self._osm_props_default_height = _default_h
        self._osm_props_min_h = _line_height * 2
        self._osm_props_max_h = _line_height * 20
        _initial_h = int(self.settings.value('osm_props_height', self._osm_props_max_h))
        self._set_osm_props_height(_initial_h)
        self._osm_props_scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Resize handle: a thin draggable bar below the scroll area
        self._osm_props_resize_handle = QWidget()
        self._osm_props_resize_handle.setFixedHeight(RESIZE_HANDLE_HEIGHT)
        self._osm_props_resize_handle.setCursor(Qt.CursorShape.SizeVerCursor)
        self._osm_props_resize_handle.setStyleSheet(
            'background: qlineargradient(y1:0,y2:1,stop:0 transparent,stop:0.3 #999,stop:0.5 #666,stop:0.7 #999,stop:1 transparent);'
        )
        self._osm_props_resize_handle.installEventFilter(self)
        self._osm_props_dragging = False
        self._osm_props_drag_start_y = 0
        self._osm_props_drag_start_h = 0
        _props_group_layout.addWidget(self._osm_props_scroll)
        _props_group_layout.addWidget(self._osm_props_resize_handle)
        osm_opts_layout.addWidget(self._osm_props_group)

        self._osm_node_props_group = QGroupBox('Node Properties')
        self._osm_node_props_group.setVisible(False)
        self.btn_osm_node_props_edit_mode = QPushButton(self._osm_node_props_group)
        self.btn_osm_node_props_edit_mode.setCheckable(True)
        self.btn_osm_node_props_edit_mode.setChecked(False)
        self.btn_osm_node_props_edit_mode.setFixedSize(24, 20)
        self.btn_osm_node_props_edit_mode.toggled.connect(
            self._on_osm_node_props_edit_mode_toggled
        )
        self._on_osm_node_props_edit_mode_toggled(False)
        self._position_osm_node_props_edit_mode_button()
        _node_props_layout = QVBoxLayout(self._osm_node_props_group)
        _node_props_layout.setContentsMargins(4, 4, 4, 4)
        _node_props_layout.setSpacing(2)
        self._osm_node_tag_editor_widget = QWidget()
        self._osm_node_tag_rows: list = []
        self._osm_node_props_scroll = QScrollArea()
        self._osm_node_props_scroll.setWidgetResizable(True)
        self._osm_node_props_scroll.setWidget(self._osm_node_tag_editor_widget)
        self._osm_node_props_scroll.setFrameShape(QFrame.Shape.NoFrame)
        _node_initial_h = int(self.settings.value('osm_node_props_height', self._osm_props_max_h))
        self._set_osm_node_props_height(_node_initial_h)
        _node_props_layout.addWidget(self._osm_node_props_scroll)
        self._osm_node_props_resize_handle = QWidget()
        self._osm_node_props_resize_handle.setFixedHeight(RESIZE_HANDLE_HEIGHT)
        self._osm_node_props_resize_handle.setCursor(Qt.CursorShape.SizeVerCursor)
        self._osm_node_props_resize_handle.setStyleSheet(
            'background: qlineargradient(y1:0,y2:1,stop:0 transparent,stop:0.3 #999,stop:0.5 #666,stop:0.7 #999,stop:1 transparent);'
        )
        self._osm_node_props_resize_handle.installEventFilter(self)
        self._osm_node_props_dragging = False
        self._osm_node_props_drag_start_y = 0
        self._osm_node_props_drag_start_h = 0
        _node_props_layout.addWidget(self._osm_node_props_resize_handle)
        self._osm_node_sign_actions = QWidget()
        _node_sign_actions_layout = QHBoxLayout(self._osm_node_sign_actions)
        _node_sign_actions_layout.setContentsMargins(0, 0, 0, 0)
        _node_sign_actions_layout.setSpacing(4)
        self.btn_osm_node_add_sign = QPushButton('Add Sign Info')
        self.btn_osm_node_add_sign.setToolTip(
            'Add a generic traffic_sign tag to the selected node'
        )
        self.btn_osm_node_add_sign.clicked.connect(self._add_sign_info_to_selected_osm_node)
        _node_sign_actions_layout.addWidget(self.btn_osm_node_add_sign)
        self.btn_osm_node_remove_sign = QPushButton('Remove Sign Info')
        self.btn_osm_node_remove_sign.setToolTip('Remove sign-related tags from the selected node')
        self.btn_osm_node_remove_sign.clicked.connect(
            self._remove_sign_info_from_selected_osm_node
        )
        _node_sign_actions_layout.addWidget(self.btn_osm_node_remove_sign)
        _node_sign_actions_layout.addStretch()
        _node_props_layout.addWidget(self._osm_node_sign_actions)
        osm_opts_layout.addWidget(self._osm_node_props_group)

        panel.addWidget(self.grp_osm_opts)
        panel.addWidget(self.grp_xodr_opts)

        # ── ESRI World Imagery Options ────────────────────────────────
        self.grp_esri = make_group('ESRI World Imagery Options')
        esri_layout = QVBoxLayout(self.grp_esri)
        esri_layout.setContentsMargins(10, 8, 10, 10)
        esri_layout.setSpacing(6)
        self.btn_esri_edit_mode = QPushButton(self.grp_esri)
        self._style_osm_lock_button(
            self.btn_esri_edit_mode,
            self.settings.value('esri_edit_mode', False, type=bool),
            'ESRI read-only mode',
        )
        self.btn_esri_edit_mode.toggled.connect(self._on_esri_edit_mode_toggled)
        self._position_esri_edit_mode_button()

        off_row = QHBoxLayout()
        off_row.setSpacing(4)
        self._esri_drag_last = None

        # World-extent edge drag state
        self._extent_drag_edge: str | None = None  # 'N','S','E','W' while dragging
        self._extent_drag_start_vp: QPoint | None = None  # viewport pos at drag start
        self._extent_drag_start_spinval: float = 0.0  # spinbox value at drag start
        self._extent_drag_meters_per_vp_px: float = 1.0  # conversion factor at drag start
        # Extent selection state
        self._extent_select_start = None
        self._extent_select_rect_item = None
        off_row.addWidget(QLabel('X-offset'))
        self.spin_esri_x = QDoubleSpinBox()
        self.spin_esri_x.setRange(*ESRI_OFFSET_RANGE)
        self.spin_esri_x.setSingleStep(0.1)
        self.spin_esri_x.setSuffix(' m')
        self.spin_esri_x.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.spin_esri_x.setValue(float(self.settings.value('esri_offset_x', 0.0)))
        self.spin_esri_x.valueChanged.connect(self._on_esri_offset_changed)
        off_row.addWidget(self.spin_esri_x)
        off_row.addWidget(QLabel('Y-offset'))
        self.spin_esri_y = QDoubleSpinBox()
        self.spin_esri_y.setRange(*ESRI_OFFSET_RANGE)
        self.spin_esri_y.setSingleStep(0.1)
        self.spin_esri_y.setSuffix(' m')
        self.spin_esri_y.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.spin_esri_y.setValue(float(self.settings.value('esri_offset_y', 0.0)))
        self.spin_esri_y.valueChanged.connect(self._on_esri_offset_changed)
        off_row.addWidget(self.spin_esri_y)
        self.btn_esri_offset_reset = QPushButton('Reset')
        self.btn_esri_offset_reset.setToolTip('Reset X and Y offsets to zero')
        self.btn_esri_offset_reset.clicked.connect(self.on_esri_offset_reset)
        off_row.addWidget(self.btn_esri_offset_reset)
        esri_layout.addLayout(off_row)

        panel.addWidget(self.grp_esri)

        # ── CARLA Tile Server Options ─────────────────────────────────────
        self.grp_carla_bev = make_group('CARLA Tile Server Options')
        carla_bev_layout = QVBoxLayout(self.grp_carla_bev)
        carla_bev_layout.setContentsMargins(10, 8, 10, 10)
        carla_bev_layout.setSpacing(6)
        self.btn_carla_edit_mode = QPushButton(self.grp_carla_bev)
        self._style_osm_lock_button(
            self.btn_carla_edit_mode,
            self.settings.value('carla_edit_mode', False, type=bool),
            'CARLA read-only mode',
        )
        self.btn_carla_edit_mode.toggled.connect(self._on_carla_edit_mode_toggled)
        self._position_carla_edit_mode_button()

        host_port_row = QHBoxLayout()
        host_port_row.setSpacing(6)
        host_port_row.addWidget(QLabel('Host:'))
        self.edit_server_ip = QLineEdit()
        self.edit_server_ip.setPlaceholderText(DEFAULT_SERVER_HOST)
        self.edit_server_ip.setText(
            server_ip
            if server_ip is not None
            else self.settings.value('server_ip', DEFAULT_SERVER_HOST)
        )
        self.edit_server_ip.textChanged.connect(
            lambda t: setattr(self, 'server_ip', t.strip() or DEFAULT_SERVER_HOST)
        )
        host_port_row.addWidget(self.edit_server_ip)
        host_port_row.addWidget(QLabel('Port:'))
        self.edit_server_port = QLineEdit()
        self.edit_server_port.setPlaceholderText(str(DEFAULT_SERVER_PORT))
        self.edit_server_port.setFixedWidth(PORT_FIELD_WIDTH)
        self.edit_server_port.setText(
            str(server_port)
            if server_port is not None
            else str(self.settings.value('server_port', str(DEFAULT_SERVER_PORT)))
        )

        def _on_port_changed(t):
            try:
                self.server_port = int(t.strip())
            except ValueError:
                pass

        self.edit_server_port.textChanged.connect(_on_port_changed)
        host_port_row.addWidget(self.edit_server_port)

        carla_bev_layout.addLayout(host_port_row)

        panel.addWidget(self.grp_carla_bev)
        self._on_esri_edit_mode_toggled(self.btn_esri_edit_mode.isChecked())
        self._on_carla_edit_mode_toggled(self.btn_carla_edit_mode.isChecked())

        # ── General Settings ────────────────────────────────────────
        grp_general = make_group('General Options')
        general_layout = QVBoxLayout(grp_general)
        general_layout.setContentsMargins(10, 8, 10, 10)
        general_layout.setSpacing(6)

        zoom_row = QHBoxLayout()
        zoom_row.setSpacing(6)
        zoom_row.addWidget(QLabel('Display Zoom:'))
        self.spin_zoom = QSpinBox()
        self.spin_zoom.setRange(*DISPLAY_ZOOM_RANGE)
        self.spin_zoom.setSuffix(' %')
        self.spin_zoom.setFixedWidth(ZOOM_SPINBOX_WIDTH)
        self.spin_zoom.setValue(100)
        self.spin_zoom.setToolTip(
            'Current display zoom level. Reflects and controls mouse scroll zoom in real time.'
        )
        self.spin_zoom.valueChanged.connect(self._on_zoom_spinbox_changed)
        zoom_row.addWidget(self.spin_zoom)
        zoom_row.addStretch()
        general_layout.addLayout(zoom_row)

        fit_row = QHBoxLayout()
        fit_row.setSpacing(6)
        fit_row.addWidget(QLabel('Fit to'))

        self.btn_fit_xodr = QPushButton('OpenDRIVE')
        self.btn_fit_xodr.setToolTip('Fit view to the loaded OpenDRIVE bounds')
        self.btn_fit_xodr.setStyleSheet(
            'QPushButton { border: 2px solid rgb(0, 120, 255); border-radius: 3px; padding: 2px 6px; }'
            ' QPushButton:hover { background-color: rgba(0, 120, 255, 40); }'
        )
        self.btn_fit_xodr.clicked.connect(self.fit_view_to_xodr)
        fit_row.addWidget(self.btn_fit_xodr)
        self.btn_fit_carla = QPushButton('CARLA')
        self.btn_fit_carla.setToolTip('Fit view to CARLA server bounds')
        self.btn_fit_carla.setStyleSheet(
            'QPushButton { border: 2px solid rgb(255, 140, 0); border-radius: 3px; padding: 2px 6px; }'
            ' QPushButton:hover { background-color: rgba(255, 140, 0, 40); }'
        )
        self.btn_fit_carla.clicked.connect(self.fit_view_to_carla)
        fit_row.addWidget(self.btn_fit_carla)
        fit_row.addStretch()
        general_layout.addLayout(fit_row)

        # ── Grid Options (single row) ─────────────────────────────────────
        self._grid_saved_state = True
        initial_layers = (
            int(self.check_carla_bev.isChecked())
            + int(self.check_opendrive.isChecked())
            + int(self.check_esri.isChecked())
            + int(self.check_osm.isChecked())
        )
        grid_row = QHBoxLayout()
        grid_row.setSpacing(6)
        self.check_grid = QCheckBox('Grid')
        if show_grid is not None:
            self._grid_saved_state = bool(show_grid)
            self.check_grid.setChecked(show_grid)
        else:
            # Default: grid is always checked on startup.
            self._grid_saved_state = True
            self.check_grid.setChecked(True)
        self.check_grid.setEnabled(True)  # always enabled; no layer dependency
        self.check_grid.stateChanged.connect(
            lambda: self.update_visibility(grid_state_changed=True)
        )
        grid_row.addWidget(self.check_grid)
        grid_row.addWidget(QLabel('Thickness:'))
        self.spin_thickness = QSpinBox()
        self.spin_thickness.setRange(*GRID_THICKNESS_RANGE)
        self.spin_thickness.setFixedWidth(GRID_OPTION_SPINBOX_WIDTH)
        self.spin_thickness.setValue(int(self.settings.value('grid_thickness', 2)))
        self.spin_thickness.valueChanged.connect(self.update_grid_style)
        grid_row.addWidget(self.spin_thickness)
        grid_row.addWidget(QLabel('Font:'))
        self.spin_font = QSpinBox()
        self.spin_font.setRange(*GRID_FONT_SIZE_RANGE)
        self.spin_font.setFixedWidth(GRID_OPTION_SPINBOX_WIDTH)
        self.spin_font.setValue(int(self.settings.value('grid_font_size', 12)))
        self.spin_font.valueChanged.connect(self.update_grid_style)
        grid_row.addWidget(self.spin_font)
        grid_row.addWidget(QLabel('Digits:'))
        self.spin_grid_sigdigits = QSpinBox()
        self.spin_grid_sigdigits.setRange(1, MAX_GRID_LABEL_DIGITS)
        self.spin_grid_sigdigits.setFixedWidth(GRID_OPTION_SPINBOX_WIDTH)
        self.spin_grid_sigdigits.setValue(
            int(self.settings.value('grid_sig_digits', MAX_GRID_LABEL_DIGITS))
        )
        self.spin_grid_sigdigits.valueChanged.connect(self.update_grid_style)
        grid_row.addWidget(self.spin_grid_sigdigits)
        self.btn_color = QPushButton('Color…')
        self.btn_color.setToolTip('Grid Color')
        self.btn_color.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn_color.clicked.connect(self.pick_grid_color)
        grid_row.addWidget(self.btn_color)
        general_layout.addLayout(grid_row)

        appearance_row = QHBoxLayout()
        appearance_row.setSpacing(6)
        appearance_row.addWidget(QLabel('Background:'))
        self.btn_bg_color = QPushButton('Color\u2026')
        self.btn_bg_color.setToolTip('Viewport background color')
        self.btn_bg_color.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn_bg_color.clicked.connect(self.pick_viewport_bg_color)
        appearance_row.addWidget(self.btn_bg_color)
        appearance_row.addStretch()
        general_layout.addLayout(appearance_row)
        panel.addWidget(grp_general)

        # ── Layer-settings enable/disable wiring ──────────────────────────
        self.check_opendrive.toggled.connect(self.grp_xodr_opts.setEnabled)
        self.check_opendrive.toggled.connect(self._update_world_bounds_action_buttons)
        self.grp_xodr_opts.setEnabled(self.check_opendrive.isChecked())

        self.check_osm.toggled.connect(self.grp_osm_opts.setEnabled)
        self.check_osm.toggled.connect(self._update_world_bounds_action_buttons)
        self.grp_osm_opts.setEnabled(self.check_osm.isChecked())

        self.grp_carla_bev.setEnabled(True)
        self.check_carla_bev.toggled.connect(self._update_world_bounds_action_buttons)

        self.check_esri.toggled.connect(self.grp_esri.setEnabled)
        self.grp_esri.setEnabled(self.check_esri.isChecked())

        self.spin_tile_zoom.setEnabled(True)
        self.btn_tile_zoom_refresh.setEnabled(True)

        def _sync_grid_controls(checked):
            self.spin_thickness.setEnabled(checked)
            self.spin_font.setEnabled(checked)
            self.spin_grid_sigdigits.setEnabled(checked)
            self.btn_color.setEnabled(checked)

        self.check_grid.toggled.connect(_sync_grid_controls)
        _sync_grid_controls(self.check_grid.isChecked())

        panel.addStretch()

        # ── Scene items ───────────────────────────────────────────────────
        width = self.map_ctx.width_in_pixels if self.map_ctx else DEFAULT_CANVAS_SIZE_PX
        height = self.map_ctx.height_in_pixels if self.map_ctx else DEFAULT_CANVAS_SIZE_PX

        self.esri_item = QGraphicsPixmapItem(self.pil_to_qpixmap(esri_img))
        self.scene.addItem(self.esri_item)
        self.esri_item.setZValue(Z_ESRI_LAYER)

        self.carla_bev_item = QGraphicsPixmapItem(self.pil_to_qpixmap(carla_bev_img))
        self.scene.addItem(self.carla_bev_item)
        self.carla_bev_item.setZValue(Z_CARLA_BEV_LAYER)

        self.opendrive_item = QGraphicsPixmapItem(self.pil_to_qpixmap(opendrive_img))
        self.scene.addItem(self.opendrive_item)
        self.opendrive_item.setZValue(Z_OPENDRIVE_LAYER)

        self.grid_item = GridItem(
            self.map_ctx.mpp,
            self.map_ctx.world_offset,
            QRectF(0, 0, width, height),
            self.max_grid_lines,
        )
        color_hex = self.settings.value('grid_color', DEFAULT_GRID_COLOR_HEX)
        self.grid_item.grid_color = QColor(color_hex)
        self.scene.addItem(self.grid_item)

        # Set sceneRect to the grid's full extent so drag reaches the grid boundary.
        self._sync_scene_rect()

        # Flag: fit to window after initial opendrive load
        self._fit_after_opendrive_load = xodr_path is not None
        self.view.zoom_changed_cb = self._update_zoom_spinbox

        def _on_viewport_changed():
            self._esri_view_change_timer.start(VIEW_CHANGE_DEBOUNCE_MS)
            self._carla_bev_view_change_timer.start(VIEW_CHANGE_DEBOUNCE_MS)

        self.view.viewport_changed_cb = _on_viewport_changed

        self.showMaximized()
        if width > 0:
            QTimer.singleShot(INITIAL_LOAD_DELAY_MS, self._apply_load_view)

        self._setup_file_menu()
        self._setup_help_menu()
        self._setup_keyboard_shortcuts()

        # Restore splitter sizes (map vs right pane).
        # After restoring, immediately re-fit the view: the splitter resize changes
        # the viewport dimensions, invalidating any fitInView computed before it.
        saved_map = int(self.settings.value('splitter_map_w', -1))
        saved_right = int(self.settings.value('splitter_right_w', DEFAULT_SIDEBAR_WIDTH))
        if saved_map > 0:

            def _restore_splitter_saved():
                self.splitter.setSizes([saved_map, saved_right])
                # Re-fit after Qt has processed the resize event.
                QTimer.singleShot(0, self._apply_load_view)

            QTimer.singleShot(SPLITTER_RESTORE_DELAY_MS, _restore_splitter_saved)
        else:
            # Default: sidebar ~DEFAULT_SIDEBAR_WIDTH px (SIDEBAR_DEFAULT_SAVED_WIDTH sidebar + TOGGLE_BTN_WIDTH toggle)
            def _restore_splitter_default():
                self.splitter.setSizes(
                    [
                        max(100, self.splitter.width() - DEFAULT_SIDEBAR_WIDTH),
                        DEFAULT_SIDEBAR_WIDTH,
                    ]
                )
                # Re-fit after Qt has processed the resize event.
                QTimer.singleShot(0, self._apply_load_view)

            QTimer.singleShot(SPLITTER_RESTORE_DELAY_MS, _restore_splitter_default)

        # Permanent event filters for hover detection, drag handling, and key routing.
        self.view.installEventFilter(self)
        self.view.viewport().installEventFilter(self)

        self.update_imagery_alignment()

        # If launched with a --xodr path, show the OpenDRIVE layer row
        if self.xodr_path:
            self._arrange_import_layers(show_xodr=True, show_osm=False, reset_objects=True)

        self.update_visibility()

    def _setup_file_menu(self):
        file_menu = self.menuBar().addMenu('File')

        self.action_new = QAction('New Project', self)
        self.action_new.triggered.connect(self.new_project)
        file_menu.addAction(self.action_new)

        self.action_open = QAction('Open Project…', self)
        self.action_open.triggered.connect(self.open_project)
        file_menu.addAction(self.action_open)

        self.action_save = QAction('Save Project', self)
        self.action_save.triggered.connect(self.save_project)
        file_menu.addAction(self.action_save)

        self.action_save_as = QAction('Save Project As…', self)
        self.action_save_as.triggered.connect(self.save_project_as)
        file_menu.addAction(self.action_save_as)

        file_menu.addSeparator()

        self.action_import_osm = QAction('Import OSM…', self)
        self.action_import_osm.triggered.connect(lambda: self._import_file('osm'))
        file_menu.addAction(self.action_import_osm)

        file_menu.addSeparator()

        self.action_export_osm = QAction('Export OSM…', self)
        self.action_export_osm.triggered.connect(self._export_osm_only)
        file_menu.addAction(self.action_export_osm)

        self.action_export_opendrive = QAction('Export OpenDRIVE…', self)
        self.action_export_opendrive.triggered.connect(self._export_xodr_file)
        file_menu.addAction(self.action_export_opendrive)

    def _setup_help_menu(self):
        help_menu = self.menuBar().addMenu('Help')
        self.action_keyboard_shortcuts = QAction('Keyboard Shortcuts…', self)
        self.action_keyboard_shortcuts.triggered.connect(self._open_keyboard_shortcuts_dialog)
        help_menu.addAction(self.action_keyboard_shortcuts)
