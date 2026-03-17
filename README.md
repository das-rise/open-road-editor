# OpenRoadEditor

An OSM and OpenDRIVE road-network editor built on top of [ORBIT](https://github.com/RI-SE/ORBIT/blob/18285361336dd23a81487a4adee388d506d16b78/README.md) and PyQt6.

## Features

- **OpenDRIVE visualisation** — load and display `.xodr` files with full lane polygon rendering using the built-in vector renderer backed by ORBIT.
- **Live XODR refresh** — edits to the OSM layer automatically trigger a re-conversion via `netconvert` and update the OpenDRIVE overlay in real time.
- **OSM overlay** — fetch, display, and edit OpenStreetMap way/node data; stitch or split roads; export changes to a local OSM file.
- **OSM → OpenDRIVE conversion** — drive `netconvert` (SUMO) via [osm-to-xodr](https://github.com/das-rise/osm-to-xodr/blob/349a31f479653056c5301d66a7c94c4a9b9e50d7/README.md) from within the UI to convert an OSM file to `.xodr`.
- **CARLA OSM tile server** — connect to a running [WayWiser CARLA](https://github.com/das-rise/WayWiseR/tree/humble/waywiser_carla) OSM tile server to stream map tiles into the editor.
- **Imagery layers** — ESRI tile imagery and CARLA bird's-eye-view (BEV) tile server, with independent zoom/opacity controls.
- **Project persistence** — save and reload the full editor state (layers, edits, view position) as a `.ore` project file.
- **Georeferencing** — reads PROJ `+tmerc` strings from the XODR `<geoReference>` block to correctly align map tiles with the road network.

## Requirements

- Python >= 3.12
- PyQt6 >= 6.6
- SUMO >= 1.26.0. See [installation instructions](https://sumo.dlr.de/docs/Installing/index.html).
- See [pyproject.toml](pyproject.toml) for the full dependency list.

## Installation

### 1. Clone the repository

```bash
git clone --recurse-submodules https://github.com/das-rise/open-road-editor.git
cd open-road-editor
```

### 2A. Install with uv (recommended)

```bash
# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create the virtual environment and install dependencies
uv sync
source .venv/bin/activate
```

### 2B. Install with pip

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

## Usage

Use `open-road-editor` or `ore` to open the editor.

```bash
# Open the editor (no file)
open-road-editor

# Open an OSM file directly
open-road-editor --osm path/to/map.osm

# Open a saved project
open-road-editor --project path/to/session.ore

# Connect to a running CARLA OSM tile server
open-road-editor --host 192.168.1.10 --port 8080

# Override reference coordinates
open-road-editor --lat 57.474 --lon 11.984
```

Full argument reference:

| Flag                        | Description                          |
| --------------------------- | ------------------------------------ |
| `--osm PATH`                | OSM file to load on startup          |
| `--project PATH`            | `.ore` project file to restore       |
| `--host HOST`               | Tile / CARLA server hostname or IP   |
| `--port PORT`               | Server port (default 8080)           |
| `--lat FLOAT`               | Reference latitude (WGS-84)          |
| `--lon FLOAT`               | Reference longitude (WGS-84)         |
| `--tile_max_zoom_level INT` | Maximum tile zoom level (default 18) |
| `--bounds X0 X1 Y0 Y1`      | World-space bounds override (metres) |
| `--width INT`               | Canvas width in pixels               |
| `--height INT`              | Canvas height in pixels              |

## License

GPL-3.0 — see [LICENSE](open_road_editor/external/ORBIT/LICENSE) for details.
