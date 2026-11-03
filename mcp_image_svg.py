"""Render an SVG read from stdin to PNG on stdout.

mcp_image runs this in a separate process so a slow SVG can be stopped: cairosvg's cost grows with
pattern tiles and masks, which no size check bounds.

Usage: python mcp_image_svg.py MAX_DIMENSION TARGET_DIMENSION MAX_PIXELS
Exits with status CAIRO_MISSING when cairosvg or the Cairo library is unavailable; on any other
failure it prints the reason to stderr and exits with status 1.
"""

import sys
import warnings
from io import BytesIO

from PIL import Image

CAIRO_MISSING = 3
EMBEDDED_FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "BMP")


class RenderError(Exception):
    """A reason the SVG cannot be rendered, reported as-is."""


def render(data: bytes, max_dimension: int, target: int, max_pixels: int) -> bytes:
    from cairosvg.parser import Tree
    from cairosvg.surface import PNGSurface, SVGSurface
    from cairosvg.url import safe_fetch

    def fetch(url: str, resource_type: str) -> bytes:
        # cairosvg's safe mode (data: URLs only), plus checks on embedded images, which cairo
        # would otherwise decode at full size, or Pillow in any format it knows.
        content = safe_fetch(url, resource_type)
        if url.startswith("data:") and resource_type.startswith("image"):
            try:
                with Image.open(BytesIO(content), formats=EMBEDDED_FORMATS):
                    pass
            except Image.DecompressionBombError:
                raise RenderError(f"an embedded image is over {max_pixels / 1_000_000:g} megapixels") from None
            except Image.UnidentifiedImageError:
                raise RenderError("embedded images must be PNG, JPEG, GIF, WebP or BMP") from None
        return content

    # Measuring on a vector surface reads the declared size without allocating pixels.
    surface = SVGSurface(Tree(bytestring=data, url_fetcher=fetch), None, 96)
    surface.finish()
    width, height = surface.width, surface.height
    if width <= 0 or height <= 0:
        raise RenderError("SVG has no width or height")
    # Masks and patterns allocate at the declared size, whatever the output scale.
    if width * height > max_pixels:
        raise RenderError(
            f"SVG declares {width:.0f}x{height:.0f}px; at most {max_pixels / 1_000_000:g} megapixels are supported"
        )
    longest = max(width, height)
    scale = min(max(longest, target), max_dimension) / longest
    return PNGSurface.convert(bytestring=data, scale=scale, url_fetcher=fetch)


def main() -> None:
    max_dimension, target, max_pixels = (int(arg) for arg in sys.argv[1:4])
    Image.MAX_IMAGE_PIXELS = max_pixels // 2
    warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)
    try:
        import cairosvg  # noqa: F401
    except (ImportError, OSError):
        sys.exit(CAIRO_MISSING)
    try:
        png = render(sys.stdin.buffer.read(), max_dimension, target, max_pixels)
    except RenderError as e:
        sys.exit(str(e))
    except Exception as e:
        sys.exit(f"{type(e).__name__}: {e}")
    sys.stdout.buffer.write(png)


if __name__ == "__main__":
    main()
