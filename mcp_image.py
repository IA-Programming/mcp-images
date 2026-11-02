#!/usr/bin/env python3

import asyncio
import functools
import logging
import os
import re
import sys
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from PIL import Image as PILImage
from PIL import UnidentifiedImageError

# Claude downscales anything with a longer edge above 1568px, so larger images only cost bytes.
MAX_DIMENSION = int(os.environ.get("MCP_IMAGE_MAX_DIMENSION", 1568))
# Claude Desktop reportedly rejects tool results over ~1 MB and base64 adds a third, so all images
# in one call share a 700 KB raw budget.
MAX_TOTAL_BYTES = int(os.environ.get("MCP_IMAGE_MAX_TOTAL_BYTES", 700_000))
MAX_SOURCES = 20
MAX_CONCURRENCY = 4
MAX_SOURCE_BYTES = 50 * 1024 * 1024
# Bounds decoder memory; checked after JPEG draft scaling, so large JPEG photos still work.
MAX_DECODE_PIXELS = 40_000_000
MIN_DIMENSION = 200
FETCH_TIMEOUT = 30.0
JPEG_QUALITIES = (85, 75, 65, 50, 35)
PASSTHROUGH_FORMATS = {"JPEG", "PNG", "GIF", "WEBP"}
LOSSLESS_FORMATS = {"PNG", "GIF", "BMP", "TIFF", "ICO", "PPM", "TGA"}
ORIENTATION_TAG = 0x0112
EXIF_TRANSPOSE = {
    2: PILImage.Transpose.FLIP_LEFT_RIGHT,
    3: PILImage.Transpose.ROTATE_180,
    4: PILImage.Transpose.FLIP_TOP_BOTTOM,
    5: PILImage.Transpose.TRANSPOSE,
    6: PILImage.Transpose.ROTATE_270,
    7: PILImage.Transpose.TRANSVERSE,
    8: PILImage.Transpose.ROTATE_90,
}
# Replaced by the MAX_DECODE_PIXELS check, which runs after JPEG draft scaling.
PILImage.MAX_IMAGE_PIXELS = None
USER_AGENT = f"mcp-image/0.2 (https://github.com/IA-Programming/mcp-images) python-httpx/{httpx.__version__}"
SVG_TARGET_DIMENSION = 1024
# Servers often label SVG with generic types, so only these are sniffed for an <svg> root.
GENERIC_CONTENT_TYPES = {"", "application/octet-stream", "binary/octet-stream", "text/plain", "text/xml", "application/xml"}
SVG_PROLOG = re.compile(rb"\s*(?:<\?.*?\?>|<!--.*?-->|<!DOCTYPE[^>\[]*(?:\[.*?\])?\s*>)", re.DOTALL | re.IGNORECASE)
SVG_ROOT = re.compile(rb"\s*<(?:[\w.-]+:)?svg[\s/>]", re.IGNORECASE)
WINDOWS_GTK3_DIR = r"C:\Program Files\GTK3-Runtime Win64\bin"


def setup_logging() -> logging.Logger:
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logger = logging.getLogger("image-mcp")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(os.environ.get("MCP_IMAGE_LOG_LEVEL", "INFO").upper())
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Clients often launch servers from an unwritable cwd, so never log relative to it.
    log_dir = Path(os.environ.get("MCP_IMAGE_LOG_DIR") or Path(__file__).resolve().parent / "data")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / f"{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
    except OSError as e:
        logger.warning("File logging disabled: %s", e)
    else:
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    return logger


logger = setup_logging()
mcp = MCPServer("image-service")


class ImageSourceError(Exception):
    """A source could not be turned into an image; the message is shown to the model."""


def is_url(source: str) -> bool:
    parsed = urlparse(source)
    return bool(parsed.scheme and parsed.netloc)


def resolve_local_path(source: str) -> Path:
    if source.startswith("file://"):
        return Path(url2pathname(urlparse(source).path))
    return Path(source).expanduser()


def read_local(source: str) -> bytes:
    path = resolve_local_path(source)
    if not path.is_file():
        raise ImageSourceError(f"file not found: {path}")
    size = path.stat().st_size
    if size > MAX_SOURCE_BYTES:
        raise ImageSourceError(f"file is {size} bytes, larger than the {MAX_SOURCE_BYTES} byte limit")
    return path.read_bytes()


async def read_url(url: str, client: httpx.AsyncClient) -> tuple[bytes, str]:
    if urlparse(url).scheme not in ("http", "https"):
        raise ImageSourceError("only http(s) URLs and local file paths are supported")
    async with asyncio.timeout(FETCH_TIMEOUT), client.stream("GET", url) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        chunks, total = [], 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise ImageSourceError(f"download exceeds the {MAX_SOURCE_BYTES} byte limit")
            chunks.append(chunk)
    data = b"".join(chunks)
    logger.debug("%s: HTTP %d, Content-Type %r, %d bytes", url, response.status_code, content_type, len(data))
    return data, content_type


def is_svg(data: bytes, content_type: str) -> bool:
    """True when the server says SVG, or when a generically typed body has an <svg> root element."""
    if content_type in ("image/svg+xml", "image/svg"):
        return True
    if content_type not in GENERIC_CONTENT_TYPES:
        return False
    head, pos = data[:65536].removeprefix(b"\xef\xbb\xbf"), 0
    while match := SVG_PROLOG.match(head, pos):
        pos = match.end()
    return SVG_ROOT.match(head, pos) is not None


@functools.cache
def add_windows_cairo_dirs() -> None:
    """Let cairocffi find a Cairo DLL outside PATH, such as the GTK3 runtime's."""
    configured = os.environ.get("CAIRO_DLL_DIRS")
    for directory in configured.split(os.pathsep) if configured else [WINDOWS_GTK3_DIR]:
        if directory and os.path.isdir(directory):
            os.add_dll_directory(directory)
            os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")


def render_svg(data: bytes, source: str) -> bytes:
    """Rasterize an SVG to PNG with its longer edge near SVG_TARGET_DIMENSION."""
    if sys.platform == "win32":
        add_windows_cairo_dirs()
    try:
        import cairosvg
        from cairosvg.parser import Tree
        from cairosvg.surface import SVGSurface
    except (ImportError, OSError) as e:
        raise ImageSourceError(
            "SVG support is not installed: install the `mcp-image[svg]` extra and the Cairo library"
        ) from e
    try:
        # Measuring on a vector surface reads the natural size without allocating pixels.
        # unsafe=False (the default) also stops the SVG from pulling in local files or URLs.
        surface = SVGSurface(Tree(bytestring=data), None, 96)
        surface.finish()
        longest = max(surface.width, surface.height)
        if longest <= 0:
            raise ImageSourceError("SVG has no width or height")
        scale = min(max(longest, SVG_TARGET_DIMENSION), MAX_DIMENSION) / longest
        png = cairosvg.svg2png(bytestring=data, scale=scale)
    except ImageSourceError:
        raise
    except Exception as e:
        raise ImageSourceError(f"could not render SVG ({type(e).__name__}: {e})") from e
    logger.debug("%s: rendered SVG at scale %.4g (%d bytes)", source, scale, len(png))
    return png


def has_alpha(img: PILImage.Image) -> bool:
    return img.mode in ("RGBA", "RGBa", "LA", "La", "PA") or (img.mode == "P" and "transparency" in img.info)


def decode(img: PILImage.Image) -> None:
    try:
        img.load()
    except (OSError, SyntaxError, ValueError) as e:
        raise ImageSourceError(f"image data is truncated or corrupt ({e})") from None


def to_display_mode(img: PILImage.Image) -> PILImage.Image:
    """Convert to L, RGB or RGBA; Pillow resizes palette and 1-bit images with nearest neighbour."""
    if img.mode in ("L", "RGB", "RGBA"):
        return img
    if img.mode == "1":
        return img.convert("L")
    if img.mode.startswith("I") or img.mode == "F":
        # 16/32-bit and float pixels clip to white in an 8-bit conversion, so stretch their range first.
        img = img.convert("F")
        lo, hi = img.getextrema()
        scale = 255 / (hi - lo) if hi > lo else 1
        return img.point(lambda v: v * scale - lo * scale).convert("L")
    return img.convert("RGBA" if has_alpha(img) else "RGB")


def encode(img: PILImage.Image, fmt: str, quality: int | None = None) -> bytes:
    buf = BytesIO()
    if fmt == "JPEG":
        img.save(buf, "JPEG", quality=quality, optimize=True)
    else:
        img.save(buf, fmt)
    return buf.getvalue()


def prepare_image(data: bytes, source: str, budget: int, content_type: str = "") -> Image:
    """Return the image as-is when a client can use it directly, otherwise re-encode it within budget bytes."""
    try:
        img = PILImage.open(BytesIO(data))
    except UnidentifiedImageError:
        detail = f" (Content-Type: {content_type})" if content_type else ""
        raise ImageSourceError(f"not a recognized image{detail}") from None

    with img:
        fmt, (width, height) = img.format, img.size
        logger.debug("%s: %s %dx%d mode=%s, %d bytes", source, fmt, width, height, img.mode, len(data))

        img.draft("RGB", (MAX_DIMENSION, MAX_DIMENSION))
        if img.width * img.height > MAX_DECODE_PIXELS:
            raise ImageSourceError(
                f"image is {width}x{height}; at most {MAX_DECODE_PIXELS / 1_000_000:g} megapixels can be decoded"
            )
        decode(img)
        orientation = img.getexif().get(ORIENTATION_TAG, 1)

        if fmt in PASSTHROUGH_FORMATS and len(data) <= budget and max(width, height) <= MAX_DIMENSION and orientation == 1:
            logger.info("%s: passed through as %s %dx%d (%d bytes)", source, fmt, width, height, len(data))
            return Image(data=data, format=fmt.lower())

        img = to_display_mode(img)
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), PILImage.Resampling.LANCZOS)
        if orientation in EXIF_TRANSPOSE:
            img = img.transpose(EXIF_TRANSPOSE[orientation])

        if img.mode == "RGBA" or fmt in LOSSLESS_FORMATS:
            png = encode(img, "PNG")
            if len(png) <= budget:
                logger.info("%s: %s %dx%d -> PNG %dx%d (%d bytes)", source, fmt, width, height, *img.size, len(png))
                return Image(data=png, format="png")

        if img.mode == "RGBA":
            background = PILImage.new("RGB", img.size, "white")
            background.paste(img, mask=img.getchannel("A"))
            img = background

        while True:
            for quality in JPEG_QUALITIES:
                jpeg = encode(img, "JPEG", quality)
                if len(jpeg) <= budget:
                    logger.info(
                        "%s: %s %dx%d -> JPEG %dx%d q=%d (%d bytes)",
                        source, fmt, width, height, *img.size, quality, len(jpeg),
                    )
                    return Image(data=jpeg, format="jpeg")
            new_size = (int(img.width * 0.75), int(img.height * 0.75))
            if min(new_size) < MIN_DIMENSION:
                raise ImageSourceError(f"could not compress below {budget} bytes without dropping under {MIN_DIMENSION}px")
            logger.debug("%s: still %d bytes at q=%d, scaling to %dx%d", source, len(jpeg), quality, *new_size)
            img = img.resize(new_size, PILImage.Resampling.LANCZOS)


async def process_source(source: str, client: httpx.AsyncClient, budget: int, limit: asyncio.Semaphore) -> Image:
    async with limit:
        if is_url(source):
            data, content_type = await read_url(source, client)
        else:
            data, content_type = await asyncio.to_thread(read_local, source), ""
        if is_svg(data, content_type):
            data = await asyncio.to_thread(render_svg, data, source)
        return await asyncio.to_thread(prepare_image, data, source, budget, content_type)


def describe_error(source: str, error: BaseException) -> str:
    if isinstance(error, ImageSourceError):
        reason = str(error)
    elif isinstance(error, httpx.HTTPStatusError):
        reason = f"HTTP {error.response.status_code} {error.response.reason_phrase}"
    elif isinstance(error, httpx.HTTPError):
        reason = f"request failed ({type(error).__name__}: {error})"
    elif isinstance(error, TimeoutError):
        reason = f"download took longer than {FETCH_TIMEOUT:g}s"
    else:
        logger.error("%s: unexpected error", source, exc_info=error)
        return f"{type(error).__name__}: {error}"
    logger.warning("%s: %s", source, reason)
    return reason


@mcp.tool(
    title="Fetch images",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    structured_output=False,
)
async def fetch_images(image_sources: list[str]) -> list[Image | str]:
    """
    Fetch images from URLs or local file paths and return them so you can see them.

    Each source can be:
    1. An http:// or https:// URL to a web-hosted image
    2. A local file path (e.g. "C:/images/photo1.jpg", "~/Pictures/a.png") or file:// URI

    Common raster formats are supported, plus SVG when the optional `svg` extra is installed.

    For a single image, provide a one-element list; at most 20 sources per call, processed in parallel.
    Images are downscaled and recompressed when needed. All images in one call share a size budget,
    so request fewer images per call when fine detail matters.
    Returns one entry per source, in input order: the image, or a text message explaining why that
    source failed.
    """
    if not image_sources:
        raise ToolError("image_sources is empty; provide at least one URL or file path")
    if len(image_sources) > MAX_SOURCES:
        raise ToolError(f"got {len(image_sources)} sources; at most {MAX_SOURCES} per call, so split them across calls")

    start = time.perf_counter()
    budget = MAX_TOTAL_BYTES // len(image_sources)
    limit = asyncio.Semaphore(MAX_CONCURRENCY)
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(follow_redirects=True, timeout=FETCH_TIMEOUT, headers=headers) as client:
        results = await asyncio.gather(
            *(process_source(source, client, budget, limit) for source in image_sources), return_exceptions=True
        )

    output: list[Image | str] = []
    for source, result in zip(image_sources, results):
        if isinstance(result, Image):
            output.append(result)
        else:
            output.append(f"Error processing {source}: {describe_error(source, result)}")

    ok = sum(isinstance(r, Image) for r in output)
    logger.info("Processed %d sources in %.2fs: %d ok, %d failed", len(output), time.perf_counter() - start, ok, len(output) - ok)
    return output


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
