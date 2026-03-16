"""Tile rendering helpers."""
from open_road_editor.constants import (
    OOR_TILE_BG_COLOR,
    OOR_TILE_HATCH_COLOR,
    TILE_ERROR_TEXT_COLOR,
    OOR_TILE_HATCH_SPACING_PX,
    MIN_FONT_SIZE,
)


def _oor_tile_rgba(font_size: int = 12) -> "np.ndarray":
    """Return a cached 256×256×4 RGBA numpy array used as an 'out of range' tile.

    Results are cached per font_size so the label matches the grid font setting.
    """
    if not hasattr(_oor_tile_rgba, "_cache"):
        _oor_tile_rgba._cache = {}
    if font_size not in _oor_tile_rgba._cache:
        import numpy as _np
        from PIL import Image as _Image
        from PIL import ImageDraw as _IDraw
        from PIL import ImageFont as _IFont

        _sz = 256
        img = _Image.new("RGBA", (_sz, _sz), OOR_TILE_BG_COLOR)
        draw = _IDraw.Draw(img)
        # hatching — diagonal crosses every OOR_TILE_HATCH_SPACING_PX px
        for offset in range(-_sz, _sz * 2, OOR_TILE_HATCH_SPACING_PX):
            draw.line(
                [(offset, 0), (offset + _sz, _sz)], fill=OOR_TILE_HATCH_COLOR, width=1
            )
            draw.line(
                [(offset + _sz, 0), (offset, _sz)], fill=OOR_TILE_HATCH_COLOR, width=1
            )
        # centred label
        try:
            font = _IFont.load_default(size=max(MIN_FONT_SIZE, font_size))
        except TypeError:  # Pillow < 10
            font = _IFont.load_default()
        text = "Out of range"
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except AttributeError:  # Pillow < 9
            tw, th = draw.textsize(text, font=font)
        draw.text(
            ((_sz - tw) // 2, (_sz - th) // 2),
            text,
            fill=TILE_ERROR_TEXT_COLOR,
            font=font,
        )
        _oor_tile_rgba._cache[font_size] = _np.array(img, dtype=_np.uint8)
    return _oor_tile_rgba._cache[font_size]


