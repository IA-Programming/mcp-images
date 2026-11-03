#!/usr/bin/env python3

import asyncio
import logging
import math
import os
import re
import subprocess
import sys
import time
import warnings
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from importlib import metadata
from importlib.util import find_spec
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse, urlsplit
from urllib.request import url2pathname

import httpx
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from mcp_image_svg import CAIRO_MISSING
from PIL import Image as PILImage
from PIL import JpegImagePlugin, UnidentifiedImageError, features

# Claude downscales anything with a longer edge above 1568px, so larger images only cost bytes.
MAX_DIMENSION = int(os.environ.get("MCP_IMAGE_MAX_DIMENSION", 1568))
# Claude Desktop reportedly rejects tool results over ~1 MB and base64 adds a third, so all images
# in one call share a 700 KB raw budget.
MAX_TOTAL_BYTES = int(os.environ.get("MCP_IMAGE_MAX_TOTAL_BYTES", 700_000))
MAX_SOURCES = 20
# Decoding and SVG rendering share one pool across calls, which bounds their memory use.
MAX_CONCURRENCY = 4
PROCESSING_POOL = ThreadPoolExecutor(max_workers=MAX_CONCURRENCY, thread_name_prefix="mcp-image")
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
# Pillow refuses images over twice this (= MAX_DECODE_PIXELS) before decoding them, including ones
# it decodes while opening, such as PNGs inside ICO files. Oversized JPEGs are reopened and drafted.
PILImage.MAX_IMAGE_PIXELS = MAX_DECODE_PIXELS // 2
warnings.filterwarnings("ignore", category=PILImage.DecompressionBombWarning)
# Pillow opens dozens of formats, some through external programs (EPS runs Ghostscript), so only
# the formats photos, screenshots and icons use are allowed.
OPEN_FORMATS = ("JPEG", "PNG", "GIF", "WEBP", "BMP", "TIFF", "ICO", "PPM", "TGA") + (("AVIF",) if features.check("avif") else ())
# Servers that negotiate formats should prefer ones LLM clients accept.
ACCEPT = "image/png,image/jpeg,image/webp,image/gif,image/*;q=0.8,*/*;q=0.5"
try:
    VERSION = metadata.version("mcp-image")
except metadata.PackageNotFoundError:
    VERSION = "0+unknown"
USER_AGENT = f"mcp-image/{VERSION} (https://github.com/IA-Programming/mcp-images) python-httpx/{httpx.__version__}"
TELEGRAM_BOT_TOKEN = re.compile(r"/bot\d+:[\w-]+")
SVG_TARGET_DIMENSION = 1024
# Servers often label SVG with generic types, so only these are sniffed for an <svg> root.
GENERIC_CONTENT_TYPES = {"", "application/octet-stream", "binary/octet-stream", "text/plain", "text/xml", "application/xml"}
SVG_PROLOG = re.compile(rb"\s*(?:<\?.*?\?>|<!--.*?-->|<!DOCTYPE[^>\[]*(?:\[.*?\])?\s*>)", re.DOTALL | re.IGNORECASE)
SVG_ROOT = re.compile(rb"\s*<(?:[\w.-]+:)?svg[\s/>]", re.IGNORECASE)
SVG_RENDERER = Path(__file__).with_name("mcp_image_svg.py")
SVG_RENDER_TIMEOUT = 10.0
MAX_SVG_BYTES = 10_000_000
SVG_NOT_INSTALLED = "SVG support is not installed: install the `mcp-image[svg]` extra and the Cairo library"
NO_CONSOLE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINDOWS_GTK3_DIR = r"C:\Program Files\GTK3-Runtime Win64\bin"
if sys.platform == "win32" and not os.environ.get("CAIROCFFI_DLL_DIRECTORIES") and os.path.isdir(WINDOWS_GTK3_DIR):
    # Read by cairocffi in the SVG renderer process, which inherits this environment.
    os.environ["CAIROCFFI_DLL_DIRECTORIES"] = WINDOWS_GTK3_DIR


def setup_logging() -> logging.Logger:
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logger = logging.getLogger("image-mcp")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    level_name = os.environ.get("MCP_IMAGE_LOG_LEVEL", "INFO").upper()
    level = logging.getLevelNamesMapping().get(level_name)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level or logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    if level is None:
        logger.warning("Unknown MCP_IMAGE_LOG_LEVEL %r, using INFO", level_name)

    log_dir = os.environ.get("MCP_IMAGE_LOG_DIR")
    if log_dir:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(Path(log_dir) / f"mcp-image-{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
        except OSError as e:
            logger.warning("File logging disabled: %s", e)
        else:
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
    return logger


logger = setup_logging()
mcp = MCPServer("image-service", version=VERSION)


class ImageSourceError(Exception):
    """A source could not be turned into an image; the message is shown to the model."""


def is_url(source: str) -> bool:
    parsed = urlparse(source)
    return bool(parsed.scheme and parsed.netloc)


def redact(source: str) -> str:
    """Strip credentials, query strings and Telegram bot tokens from a URL before it is logged."""
    if not is_url(source):
        return source
    parts = urlsplit(source)
    host = parts.netloc.rpartition("@")[2]
    query = "?<redacted>" if parts.query else ""
    return f"{parts.scheme}://{host}{TELEGRAM_BOT_TOKEN.sub('/bot<redacted>', parts.path)}{query}"


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
    logger.debug("%s: HTTP %d, Content-Type %r, %d bytes", redact(url), response.status_code, content_type, len(data))
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


def gunzip(data: bytes, limit: int) -> bytes:
    decompressor = zlib.decompressobj(wbits=31)
    try:
        data = decompressor.decompress(data, limit + 1)
    except zlib.error as e:
        raise ImageSourceError(f"invalid gzip data ({e})") from None
    if len(data) > limit:
        raise ImageSourceError(f"SVG is larger than {limit / 1_000_000:g} MB once decompressed")
    return data


def render_svg(data: bytes, source: str) -> bytes:
    """Rasterize an SVG to PNG, with its longer edge near SVG_TARGET_DIMENSION, in a time-limited process."""
    if find_spec("cairosvg") is None:
        raise ImageSourceError(SVG_NOT_INSTALLED)
    if data.startswith(b"\x1f\x8b"):
        data = gunzip(data, MAX_SVG_BYTES)
        if data.startswith(b"\x1f\x8b"):
            # cairosvg would inflate a second layer without any limit.
            raise ImageSourceError("SVG is compressed more than once")
    if len(data) > MAX_SVG_BYTES:
        raise ImageSourceError(f"SVG is larger than {MAX_SVG_BYTES / 1_000_000:g} MB")
    # The CPU limit, enforced by the kernel, stops the renderer even if this server dies mid-render.
    cpu_seconds = math.ceil(SVG_RENDER_TIMEOUT) + 5
    command = [sys.executable, str(SVG_RENDERER), *map(str, (MAX_DIMENSION, SVG_TARGET_DIMENSION, MAX_DECODE_PIXELS, cpu_seconds))]
    try:
        result = subprocess.run(
            command, input=data, capture_output=True, timeout=SVG_RENDER_TIMEOUT, creationflags=NO_CONSOLE_WINDOW
        )
    except subprocess.TimeoutExpired:
        raise ImageSourceError(f"SVG took longer than {SVG_RENDER_TIMEOUT:g}s to render") from None
    if result.returncode == CAIRO_MISSING:
        raise ImageSourceError(SVG_NOT_INSTALLED)
    if result.returncode != 0:
        lines = result.stderr.decode(errors="replace").strip().splitlines()
        raise ImageSourceError(f"could not render SVG ({lines[-1] if lines else f'exit status {result.returncode}'})")
    logger.debug("%s: rendered SVG (%d bytes)", source, len(result.stdout))
    return result.stdout


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


def open_image(data: bytes, content_type: str) -> PILImage.Image:
    try:
        return PILImage.open(BytesIO(data), formats=OPEN_FORMATS)
    except UnidentifiedImageError:
        detail = f" (Content-Type: {content_type})" if content_type else ""
        raise ImageSourceError(f"not a recognized image{detail}") from None
    except PILImage.DecompressionBombError:
        if data.startswith(b"\xff\xd8\xff"):
            # Baseline JPEGs decode at a reduced scale via draft(), which prepare_image checks against
            # the limit. Progressive ones still buffer coefficients for the full image, so they can't.
            img = JpegImagePlugin.JpegImageFile(BytesIO(data))
            if not img.info.get("progressive"):
                return img
            img.close()
        raise ImageSourceError(
            f"image is too large; at most {MAX_DECODE_PIXELS / 1_000_000:g} megapixels can be decoded"
        ) from None


def prepare_image(data: bytes, source: str, budget: int, content_type: str = "") -> Image:
    """Return the image as-is when a client can use it directly, otherwise re-encode it within budget bytes."""
    with open_image(data, content_type) as img:
        fmt, (width, height) = img.format, img.size
        logger.debug("%s: %s %dx%d mode=%s, %d bytes", source, fmt, width, height, img.mode, len(data))

        reduction = MAX_DIMENSION / max(width, height)
        if reduction < 1:
            img.draft("RGB", (math.ceil(width * reduction), math.ceil(height * reduction)))
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


def looks_like_image(data: bytes, content_type: str) -> bool:
    """Cheap header check used to share the size budget only among sources that will become images."""
    if is_svg(data, content_type):
        return True
    try:
        with PILImage.open(BytesIO(data), formats=OPEN_FORMATS):
            return True
    except PILImage.DecompressionBombError:
        return data.startswith(b"\xff\xd8\xff")
    except Exception:
        return False


async def load_source(source: str, client: httpx.AsyncClient) -> tuple[bytes, str]:
    if is_url(source):
        return await read_url(source, client)
    return await asyncio.to_thread(read_local, source), ""


async def process_source(source: str, data: bytes, content_type: str, budget: int) -> Image:
    loop = asyncio.get_running_loop()
    if is_svg(data, content_type):
        data = await loop.run_in_executor(PROCESSING_POOL, render_svg, data, redact(source))
    return await loop.run_in_executor(PROCESSING_POOL, prepare_image, data, redact(source), budget, content_type)


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
    headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT}
    async with httpx.AsyncClient(follow_redirects=True, timeout=FETCH_TIMEOUT, headers=headers) as client:
        results = await asyncio.gather(*(load_source(source, client) for source in image_sources), return_exceptions=True)

    # Sources that failed to load or are not images leave their share of the size budget to the others.
    loaded = {i: item for i, item in enumerate(results) if not isinstance(item, BaseException)}
    images = await asyncio.to_thread(lambda: sum(looks_like_image(*item) for item in loaded.values()))
    budget = MAX_TOTAL_BYTES // max(1, images)
    processed = await asyncio.gather(
        *(process_source(image_sources[i], data, content_type, budget) for i, (data, content_type) in loaded.items()),
        return_exceptions=True,
    )
    for i, result in zip(loaded, processed):
        results[i] = result

    output: list[Image | str] = []
    for source, result in zip(image_sources, results):
        if isinstance(result, Image):
            output.append(result)
        else:
            output.append(f"Error processing {source}: {describe_error(redact(source), result)}")

    ok = sum(isinstance(r, Image) for r in output)
    logger.info("Processed %d sources in %.2fs: %d ok, %d failed", len(output), time.perf_counter() - start, ok, len(output) - ok)
    return output


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
