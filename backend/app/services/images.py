"""Local Pillow image preparation. Functions return new, exact-size RGB images.

Transparency is composited onto the explicit background for PDF/JPEG use.
The input image is never modified. No network or image model is invoked here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image, ImageColor, ImageOps, UnidentifiedImageError

Size = tuple[int, int]
Color = str | tuple[int, int, int]
ImageSource = Image.Image | str | Path | bytes
DEFAULT_BACKGROUND = "#F3EBDD"
MAX_PIXELS = 40_000_000


class ImageInputError(ValueError):
    pass


@dataclass(frozen=True)
class ImageResult:
    image: Image.Image
    is_placeholder: bool
    warning: str | None = None


def _size(size: Size) -> Size:
    if not isinstance(size, (tuple, list)) or len(size) != 2:
        raise ValueError("size must be (width, height)")
    if any(type(value) is not int or value <= 0 for value in size):
        raise ValueError("width and height must be positive integers")
    if size[0] * size[1] > MAX_PIXELS:
        raise ValueError(f"Output must not exceed {MAX_PIXELS} pixels")
    return tuple(size)


def _color(background: Color) -> tuple[int, int, int]:
    color = ImageColor.getrgb(background) if isinstance(background, str) else background
    if not isinstance(color, tuple) or len(color) != 3 or any(
        type(value) is not int or not 0 <= value <= 255 for value in color
    ):
        raise ValueError("background must be an opaque RGB color")
    return color


def _load(source: ImageSource, background: tuple[int, int, int]) -> Image.Image:
    def normalize(image: Image.Image) -> Image.Image:
        if image.width <= 0 or image.height <= 0:
            raise ImageInputError("Source has no pixels")
        if image.width * image.height > MAX_PIXELS:
            raise ImageInputError(f"Source must not exceed {MAX_PIXELS} pixels")
        oriented = ImageOps.exif_transpose(image)
        rgba = oriented.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (*background, 255))
        return Image.alpha_composite(canvas, rgba).convert("RGB")

    if isinstance(source, Image.Image):
        return normalize(source)
    if not isinstance(source, (str, Path, bytes)):
        raise TypeError("source must be a Pillow image, local path or encoded image bytes")
    with Image.open(BytesIO(source) if isinstance(source, bytes) else source) as image:
        return normalize(image)


def placeholder_image(size: Size, background: Color = DEFAULT_BACKGROUND) -> Image.Image:
    """Solid background only, with no text or simulated destination photograph."""
    return Image.new("RGB", _size(size), _color(background))


def resize_image(
    source: ImageSource, size: Size, *, background: Color = DEFAULT_BACKGROUND,
) -> Image.Image:
    """Contain: preserve all content and aspect ratio, pad to exact (width, height)."""
    size, color = _size(size), _color(background)
    image = _load(source, color)
    # Clamp to one pixel for extreme aspect ratios and tiny output dimensions.
    scale = min(size[0] / image.width, size[1] / image.height)
    scaled = (min(size[0], max(1, round(image.width * scale))),
              min(size[1], max(1, round(image.height * scale))))
    resized = image.resize(scaled, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, color)
    canvas.paste(resized, ((size[0] - scaled[0]) // 2, (size[1] - scaled[1]) // 2))
    return canvas


def crop_image(
    source: ImageSource, size: Size, *, box: tuple[int, int, int, int] | None = None,
    centering: tuple[float, float] = (0.5, 0.5), background: Color = DEFAULT_BACKGROUND,
) -> Image.Image:
    """Cover: crop then resize without distortion; box uses EXIF-oriented pixels.

    centering=(0, 0) anchors top-left; (1, 1) anchors bottom-right.
    """
    size, color = _size(size), _color(background)
    if not isinstance(centering, (tuple, list)) or len(centering) != 2 or any(
        type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
        for value in centering
    ):
        raise ValueError("centering must contain two numbers in [0, 1]")
    image = _load(source, color)
    if box is not None:
        if not isinstance(box, (tuple, list)) or len(box) != 4 or any(type(v) is not int for v in box):
            raise ValueError("box must contain four integer pixel coordinates")
        left, top, right, bottom = box
        if not (0 <= left < right <= image.width and 0 <= top < bottom <= image.height):
            raise ValueError("box must be a non-empty region inside the oriented source")
        image = image.crop(box)
    return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=tuple(centering))


def prepare_image(
    source: ImageSource | None, size: Size, *, mode: Literal["contain", "cover", "stretch"] = "cover",
    background: Color = DEFAULT_BACKGROUND, fallback: bool = True,
) -> ImageResult:
    """Prepare an image, explicitly flagging fallback when source is missing/broken.

    Invalid dimensions, colors and mode are programming errors and always raise.
    With fallback=False, source-loading errors propagate to the caller.
    """
    size, color = _size(size), _color(background)
    if mode not in {"contain", "cover", "stretch"}:
        raise ValueError("mode must be contain, cover or stretch")
    try:
        if source is None:
            raise ImageInputError("source_missing")
        if mode == "contain":
            image = resize_image(source, size, background=color)
        elif mode == "cover":
            image = crop_image(source, size, background=color)
        else:
            image = _load(source, color).resize(size, Image.Resampling.LANCZOS)
        return ImageResult(image=image, is_placeholder=False)
    except (OSError, UnidentifiedImageError, ImageInputError, Image.DecompressionBombError):
        if not fallback:
            raise
        return ImageResult(
            image=placeholder_image(size, color), is_placeholder=True,
            warning="source_missing" if source is None else "source_unreadable_or_too_large",
        )
