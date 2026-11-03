import base64
import os
import struct
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

os.environ.setdefault("MCP_IMAGE_LOG_DIR", tempfile.mkdtemp(prefix="mcp-image-test-logs-"))

import pytest
from mcp.client import Client
from PIL import Image

import mcp_image


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_image(mode="RGB", size=(64, 48), color="red", noise=False):
    if noise:
        bands = len(Image.new(mode, (1, 1)).getbands())
        return Image.frombytes(mode, size, os.urandom(size[0] * size[1] * bands))
    return Image.new(mode, size, color)


def to_bytes(img, fmt, **params):
    buf = BytesIO()
    img.save(buf, fmt, **params)
    return buf.getvalue()


def decode(block):
    return Image.open(BytesIO(base64.b64decode(block.data)))


async def call_fetch(sources):
    async with Client(mcp_image.mcp) as client:
        return await client.call_tool("fetch_images", {"image_sources": sources})


SMALL_PNG = to_bytes(make_image(), "PNG")


def png_with_declared_size(width, height):
    """A tiny PNG whose header claims the given size, without the pixel data to back it."""
    data = bytearray(SMALL_PNG)
    struct.pack_into(">II", data, 16, width, height)
    struct.pack_into(">I", data, 29, zlib.crc32(bytes(data[12:29])))
    return bytes(data)


def ico_containing(png):
    """An ICO file with one PNG-compressed entry."""
    return struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22) + png


ICON_SVG = (
    b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="24" height="12">'
    b'<rect width="24" height="12" fill="#ff0000"/></svg>'
)


class Handler(BaseHTTPRequestHandler):
    seen_headers: list[dict] = []

    routes = {
        "/img.png": (200, "image/png", SMALL_PNG),
        "/img-charset.png": (200, "image/png; charset=binary", SMALL_PNG),
        "/octet": (200, "application/octet-stream", SMALL_PNG),
        "/no-type": (200, None, SMALL_PNG),
        "/html": (200, "text/html; charset=utf-8", b"<html><body>not an image</body></html>"),
        "/missing": (404, "text/plain", b"nope"),
        "/icon": (200, "image/svg+xml", ICON_SVG),
        "/icon-as-text": (200, "text/plain", ICON_SVG),
        "/page-with-svg": (200, "text/html", b"<!doctype html><html><body>" + ICON_SVG + b"</body></html>"),
    }

    def do_GET(self):
        Handler.seen_headers.append(dict(self.headers))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/img.png")
            self.end_headers()
            return
        if self.path == "/slow":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            try:
                for byte in SMALL_PNG[:40]:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.25)
            except OSError:
                pass
            return
        status, content_type, body = self.routes.get(self.path, (404, "text/plain", b""))
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
