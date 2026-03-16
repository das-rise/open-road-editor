"""open_road_editor.viewer package."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from open_road_editor.viewer.main import OpenDriveViewer

__all__ = ["OpenDriveViewer"]


def __getattr__(name: str):
    # Lazy import avoids circular import when utility modules import viewer submodules.
    if name == "OpenDriveViewer":
        from open_road_editor.viewer.main import OpenDriveViewer

        return OpenDriveViewer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
