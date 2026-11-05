"""Render an SVG read from stdin to PNG on stdout.

mcp_image runs this in a separate process so a slow SVG can be stopped: cairosvg's cost grows with
pattern tiles and masks, which no size check bounds.

Usage: python mcp_image_svg.py MAX_DIMENSION TARGET_DIMENSION MAX_PIXELS CPU_SECONDS [DPI] [CROP_BOX_JSON] [EMOJI_FONT_PATH]
Exits with status CAIRO_MISSING when cairosvg or the Cairo library is unavailable; on any other
failure it prints the reason to stderr and exits with status 1.
"""

import base64
import json
import re
import sys
import warnings
from io import BytesIO
from pathlib import Path

from PIL import Image

CAIRO_MISSING = 3
EMBEDDED_FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "BMP")
# Masks, patterns and nested viewports can allocate far beyond the output size; normal SVGs need
# well under 300 MB.
MEMORY_LIMIT = 1_500_000_000
MAX_FONT_BYTES = 30_000_000
EMOJI_PATTERN = re.compile(r"[\U0001F300-\U0001FAFF]")
SYMBOL_PATTERN = re.compile(r"[\u0391-\u03C9\u2206\u220F\u2211]")


class RenderError(Exception):
    """A reason the SVG cannot be rendered, reported as-is."""


def _normalize_crop_box(crop_box: list[int] | None, image_width: int, image_height: int) -> tuple[int, int, int, int] | None:
    """Validate a crop box in [x, y, width, height] form and clamp it to the rendered image bounds."""
    if crop_box is None:
        return None
    if len(crop_box) != 4:
        raise RenderError(f"crop_box must contain four integers [x, y, width, height], got {crop_box}")
    try:
        x, y, width, height = (int(value) for value in crop_box)
    except (TypeError, ValueError) as error:
        raise RenderError(f"crop_box must contain four integers, got {crop_box}") from error
    if width <= 0 or height <= 0:
        raise RenderError("crop_box width and height must be positive")
    if x >= image_width or y >= image_height:
        raise RenderError(f"crop_box origin ({x}, {y}) is outside the {image_width}x{image_height} SVG rendering")
    x, y = max(0, x), max(0, y)
    return x, y, min(width, image_width - x), min(height, image_height - y)


def _format_number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _replace_root_attribute(tag: str, name: str, value: str) -> str:
    pattern = rf'(\s{name}\s*=\s*)(["\'])[^"\']*\2'
    if re.search(pattern, tag):
        return re.sub(pattern, lambda match: match.group(1) + match.group(2) + value + match.group(2), tag, count=1)
    return tag[:-1] + f' {name}="{value}">'


def _rewrite_svg_crop(data: bytes, crop_box: tuple[int, int, int, int], full_width: int, full_height: int) -> bytes | None:
    """Rewrite the root viewBox and dimensions so only the requested vector region is rendered."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    root_match = re.search(r"<(?:[\w.-]+:)?svg\b[^>]*>", text)
    if not root_match:
        return None
    root_tag = root_match.group(0)
    viewbox_match = re.search(
        r"""viewBox\s*=\s*["']\s*([-\d.eE+]+)[\s,]+([-\d.eE+]+)[\s,]+([-\d.eE+]+)[\s,]+([-\d.eE+]+)""",
        root_tag,
        re.IGNORECASE,
    )
    if not viewbox_match:
        return None
    try:
        view_x, view_y, view_width, view_height = (float(value) for value in viewbox_match.groups())
    except ValueError:
        return None
    if view_width <= 0 or view_height <= 0:
        return None
    x, y, width, height = crop_box
    scale_x, scale_y = view_width / full_width, view_height / full_height
    new_viewbox = " ".join(
        _format_number(value)
        for value in (view_x + x * scale_x, view_y + y * scale_y, width * scale_x, height * scale_y)
    )
    new_tag = _replace_root_attribute(root_tag, "viewBox", new_viewbox)
    new_tag = _replace_root_attribute(new_tag, "width", str(width))
    new_tag = _replace_root_attribute(new_tag, "height", str(height))
    return (text[:root_match.start()] + new_tag + text[root_match.end():]).encode("utf-8")


def _inject_svg_fonts(data: bytes, emoji_font_path: str | None) -> bytes:
    """Inject emoji and symbol fonts without permitting external SVG resources."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    if not EMOJI_PATTERN.search(text) and not SYMBOL_PATTERN.search(text):
        return data

    font_declaration = ""
    emoji_fonts = "'Segoe UI Emoji', 'Apple Color Emoji', 'Noto Color Emoji'"
    if emoji_font_path:
        font_file = Path(emoji_font_path)
        try:
            font_data = font_file.read_bytes()
        except OSError as error:
            raise RenderError(f"could not read the emoji font: {error}") from error
        if len(font_data) > MAX_FONT_BYTES:
            raise RenderError(f"emoji font exceeds the {MAX_FONT_BYTES / 1_000_000:g} MB limit")
        font_type = {".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2"}.get(font_file.suffix.lower(), "font/ttf")
        font_url = f"data:{font_type};base64,{base64.b64encode(font_data).decode('ascii')}"
        font_declaration = f"<style type=\"text/css\">@font-face{{font-family:'MCP Emoji';src:url('{font_url}');}}</style>"
        emoji_fonts = "'MCP Emoji', " + emoji_fonts

    def rewrite_text_node(match: re.Match[str]) -> str:
        attributes, content = match.group(1), match.group(2)
        has_emoji = bool(EMOJI_PATTERN.search(content))
        if not has_emoji and not SYMBOL_PATTERN.search(content):
            return match.group(0)
        preferred_fonts = emoji_fonts if has_emoji else "'Segoe UI Symbol', 'Cambria Math', 'Arial Unicode MS'"
        new_attributes = re.sub(
            r"font-family\s*=\s*([\"'])(.*?)\1",
            lambda font: f"font-family={font.group(1)}{preferred_fonts}, {font.group(2)}{font.group(1)}",
            attributes,
            count=1,
        )
        new_attributes = re.sub(
            r"font-family\s*:\s*([^;\"'<>}]+)",
            lambda font: f"font-family: {preferred_fonts}, {font.group(1)}",
            new_attributes,
            count=1,
        )
        if new_attributes == attributes:
            new_attributes = f'{attributes} font-family="{preferred_fonts}"'
        return f"<text{new_attributes}>{content}</text>"

    text = re.sub(r"<text([^>]*)>(.*?)</text>", rewrite_text_node, text, flags=re.DOTALL)
    if font_declaration:
        root_match = re.search(r"<(?:[\w.-]+:)?svg\b[^>]*>", text)
        if root_match:
            text = text[:root_match.end()] + font_declaration + text[root_match.end():]
    return text.encode("utf-8")


def _root_uses_pixel_size(data: bytes) -> bool:
    root_match = re.search(rb"<(?:[\w.-]+:)?svg\b[^>]*>", data, re.IGNORECASE)
    if not root_match:
        return False
    root_tag = root_match.group(0)
    for attribute in (b"width", b"height"):
        match = re.search(rb"\s" + attribute + rb"\s*=\s*([\"'])(.*?)\1", root_tag, re.IGNORECASE)
        if not match or not re.fullmatch(rb"\s*[-+\d.]+(?:px)?\s*", match.group(2), re.IGNORECASE):
            return False
    return True


def render(
    data: bytes,
    max_dimension: int,
    target: int,
    max_pixels: int,
    dpi: int | None = None,
    crop_box: list[int] | None = None,
    emoji_font_path: str | None = None,
) -> bytes:
    from cairosvg.parser import Tree
    from cairosvg.surface import PNGSurface, SVGSurface
    from cairosvg.url import safe_fetch

    data = _inject_svg_fonts(data, emoji_font_path)

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
    if dpi is None:
        scale = min(max(longest, target), max_dimension) / longest
    else:
        # Honor DPI for physical SVG units while preserving the pixel semantics of explicitly pixel-sized documents.
        requested_scale = 1.0 if _root_uses_pixel_size(data) else max(50, min(dpi, 1200)) / 96.0
        scale = min(requested_scale, max_dimension / longest)
    full_width, full_height = max(1, round(width * scale)), max(1, round(height * scale))
    normalized_crop = _normalize_crop_box(crop_box, full_width, full_height)
    if normalized_crop:
        rewritten_data = _rewrite_svg_crop(data, normalized_crop, full_width, full_height)
        if rewritten_data is not None:
            x, y, crop_width, crop_height = normalized_crop
            return PNGSurface.convert(
                bytestring=rewritten_data,
                output_width=crop_width,
                output_height=crop_height,
                url_fetcher=fetch,
            )

    png = PNGSurface.convert(
        bytestring=data,
        output_width=full_width,
        output_height=full_height,
        url_fetcher=fetch,
    )
    if normalized_crop:
        x, y, crop_width, crop_height = normalized_crop
        with Image.open(BytesIO(png)) as image:
            cropped = image.crop((x, y, x + crop_width, y + crop_height))
            output = BytesIO()
            cropped.save(output, "PNG")
            return output.getvalue()
    return png


def limit_resources(cpu_seconds: int) -> None:
    """Cap memory and CPU time in the kernel, so the renderer stays bounded even if the server dies.

    Only POSIX has these limits, and macOS does not enforce the memory one.
    """
    try:
        import resource
    except ImportError:
        return
    for which, soft, hard in (
        (resource.RLIMIT_AS, MEMORY_LIMIT, MEMORY_LIMIT),
        (resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 1),
    ):
        try:
            resource.setrlimit(which, (soft, hard))
        except (ValueError, OSError):
            pass


def main() -> None:
    max_dimension, target, max_pixels, cpu_seconds = (int(arg) for arg in sys.argv[1:5])
    dpi = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else None
    crop_box = json.loads(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] else None
    emoji_font_path = sys.argv[7] if len(sys.argv) > 7 and sys.argv[7] else None
    limit_resources(cpu_seconds)
    Image.MAX_IMAGE_PIXELS = max_pixels // 2
    warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)
    try:
        import cairosvg  # noqa: F401
    except (ImportError, OSError):
        sys.exit(CAIRO_MISSING)
    try:
        png = render(sys.stdin.buffer.read(), max_dimension, target, max_pixels, dpi, crop_box, emoji_font_path)
    except RenderError as e:
        sys.exit(str(e))
    except MemoryError:
        sys.exit(f"SVG needs more than {MEMORY_LIMIT / 1e9:g} GB of memory to render")
    except Exception as e:
        sys.exit(f"{type(e).__name__}: {e}")
    sys.stdout.buffer.write(png)


if __name__ == "__main__":
    main()
