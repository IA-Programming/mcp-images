import asyncio
import base64
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from PIL import Image

import mcp_image
from conftest import (
    ICON_SVG,
    SMALL_PNG,
    Handler,
    call_fetch,
    decode,
    ico_containing,
    make_image,
    png_with_declared_size,
    to_bytes,
)

pytestmark = pytest.mark.anyio


def assert_within_limits(block):
    assert block.type == "image"
    assert block.mime_type in {"image/jpeg", "image/png", "image/gif", "image/webp"}
    assert len(base64.b64decode(block.data)) <= mcp_image.MAX_TOTAL_BYTES
    assert max(decode(block).size) <= mcp_image.MAX_DIMENSION


async def test_tool_is_listed_with_read_only_hint():
    async with Client(mcp_image.mcp) as client:
        (tool,) = (await client.list_tools()).tools
        version = client.server_info.version
    assert tool.name == "fetch_images"
    assert tool.annotations.read_only_hint is True
    assert version == mcp_image.VERSION != "0+unknown"


def test_urls_are_redacted_for_logs():
    url = "https://user:pw@api.telegram.org/file/bot123456:AAH-secret_x/photos/a.jpg?sig=abc#frag"
    assert mcp_image.redact(url) == "https://api.telegram.org/file/bot<redacted>/photos/a.jpg?<redacted>"
    assert mcp_image.redact("/home/me/a.png") == "/home/me/a.png"


async def test_log_file_does_not_contain_url_secrets(http_server):
    await call_fetch([http_server + "/file/bot123456:AAH-secret_x/a.jpg?token=hunter2"])
    log_text = "".join(p.read_text() for p in Path(os.environ["MCP_IMAGE_LOG_DIR"]).glob("*.log"))
    assert "/file/bot<redacted>/a.jpg?<redacted>" in log_text
    assert "AAH-secret_x" not in log_text and "hunter2" not in log_text


async def test_small_png_is_passed_through_unchanged(tmp_path):
    path = tmp_path / "small.png"
    path.write_bytes(SMALL_PNG)
    result = await call_fetch([str(path)])
    (block,) = result.content
    assert block.mime_type == "image/png"
    assert base64.b64decode(block.data) == SMALL_PNG


async def test_large_photo_is_downscaled_and_compressed(tmp_path):
    path = tmp_path / "big.jpg"
    path.write_bytes(to_bytes(make_image(size=(4000, 3000), noise=True), "JPEG", quality=95))
    assert path.stat().st_size > mcp_image.MAX_TOTAL_BYTES
    (block,) = (await call_fetch([str(path)])).content
    assert_within_limits(block)
    assert block.mime_type == "image/jpeg"
    assert decode(block).size[0] / decode(block).size[1] == pytest.approx(4 / 3, rel=0.01)


async def test_oversized_transparent_png_keeps_alpha(tmp_path):
    path = tmp_path / "logo.png"
    path.write_bytes(to_bytes(make_image("RGBA", (3000, 2000), (0, 0, 255, 0)), "PNG"))
    (block,) = (await call_fetch([str(path)])).content
    assert_within_limits(block)
    img = decode(block)
    assert block.mime_type == "image/png"
    assert img.mode == "RGBA" and img.getpixel((0, 0))[3] == 0


async def test_transparency_flattens_to_white_when_jpeg_is_needed(tmp_path):
    img = make_image("RGBA", (2000, 2000), noise=True)
    img.paste((0, 0, 0, 0), (0, 0, 400, 400))
    path = tmp_path / "noisy.png"
    path.write_bytes(to_bytes(img, "PNG"))
    (block,) = (await call_fetch([str(path)])).content
    assert_within_limits(block)
    assert block.mime_type == "image/jpeg"
    assert min(decode(block).convert("RGB").getpixel((5, 5))) > 240


@pytest.mark.parametrize("fmt, ext", [("BMP", "bmp"), ("TIFF", "tiff")])
async def test_formats_llms_cannot_read_are_converted(tmp_path, fmt, ext):
    path = tmp_path / f"image.{ext}"
    path.write_bytes(to_bytes(make_image(), fmt))
    (block,) = (await call_fetch([str(path)])).content
    assert block.mime_type == "image/png"
    assert decode(block).getpixel((0, 0)) == (255, 0, 0)


@pytest.mark.parametrize("size", [(200, 100), (2000, 1000)])
async def test_exif_orientation_is_applied(tmp_path, size):
    img = make_image(size=size)
    exif = img.getexif()
    exif[0x0112] = 6
    path = tmp_path / "rotated.jpg"
    path.write_bytes(to_bytes(img, "JPEG", exif=exif))
    (block,) = (await call_fetch([str(path)])).content
    width, height = decode(block).size
    assert height > width


async def test_extension_is_ignored_in_favor_of_content(tmp_path):
    path = tmp_path / "actually-png.jpg"
    path.write_bytes(SMALL_PNG)
    (block,) = (await call_fetch([str(path)])).content
    assert block.mime_type == "image/png"


async def test_file_uri_and_home_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "a.png").write_bytes(SMALL_PNG)
    result = await call_fetch([(tmp_path / "a.png").as_uri(), "~/a.png"])
    assert [b.type for b in result.content] == ["image", "image"]


async def test_errors_are_reported_in_input_order(tmp_path):
    good = tmp_path / "good.png"
    good.write_bytes(SMALL_PNG)
    text_file = tmp_path / "notes.txt"
    text_file.write_text("hello")
    sources = [str(good), str(tmp_path / "missing.png"), str(text_file), "ftp://example.com/a.png", str(good)]
    result = await call_fetch(sources)
    assert [b.type for b in result.content] == ["image", "text", "text", "text", "image"]
    assert "file not found" in result.content[1].text
    assert "not a recognized image" in result.content[2].text
    assert "only http(s) URLs" in result.content[3].text
    assert not result.is_error


async def test_empty_source_list_is_a_tool_error():
    result = await call_fetch([])
    assert result.is_error
    assert "empty" in result.content[0].text


async def test_reports_when_image_cannot_be_compressed(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_image, "MAX_TOTAL_BYTES", 2_000)
    path = tmp_path / "noise.jpg"
    path.write_bytes(to_bytes(make_image(size=(1200, 1200), noise=True), "JPEG"))
    (block,) = (await call_fetch([str(path)])).content
    assert block.type == "text" and "could not compress" in block.text


async def test_images_in_one_call_share_the_size_budget(tmp_path):
    path = tmp_path / "big.jpg"
    path.write_bytes(to_bytes(make_image(size=(3000, 2000), noise=True), "JPEG", quality=95))
    result = await call_fetch([str(path)] * 3)
    sizes = [len(base64.b64decode(b.data)) for b in result.content]
    assert [b.type for b in result.content] == ["image"] * 3
    assert sum(sizes) <= mcp_image.MAX_TOTAL_BYTES


async def test_failed_sources_leave_their_budget_to_the_others(tmp_path):
    path = tmp_path / "big.jpg"
    path.write_bytes(to_bytes(make_image(size=(3000, 2000), noise=True), "JPEG", quality=95))
    photo, missing = (await call_fetch([str(path), str(tmp_path / "missing.png")])).content
    assert missing.type == "text"
    assert mcp_image.MAX_TOTAL_BYTES // 2 < len(base64.b64decode(photo.data)) <= mcp_image.MAX_TOTAL_BYTES


async def test_processing_is_limited_across_parallel_calls(tmp_path, monkeypatch):
    lock, running, peak = threading.Lock(), [0], [0]
    original = mcp_image.prepare_image

    def counting_prepare_image(*args):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        try:
            return original(*args)
        finally:
            with lock:
                running[0] -= 1

    monkeypatch.setattr(mcp_image, "prepare_image", counting_prepare_image)
    (tmp_path / "a.png").write_bytes(SMALL_PNG)
    sources = [str(tmp_path / "a.png")] * 6
    await asyncio.gather(call_fetch(sources), call_fetch(sources))
    assert peak[0] == mcp_image.MAX_CONCURRENCY


async def test_too_many_sources_is_a_tool_error():
    result = await call_fetch(["a.png"] * (mcp_image.MAX_SOURCES + 1))
    assert result.is_error
    assert f"at most {mcp_image.MAX_SOURCES}" in result.content[0].text


async def test_images_too_large_to_decode_are_rejected_before_decoding(tmp_path):
    path = tmp_path / "bomb.png"
    path.write_bytes(png_with_declared_size(30_000, 30_000))
    (block,) = (await call_fetch([str(path)])).content
    assert block.type == "text" and "megapixels" in block.text


@pytest.fixture
def small_decode_limits(monkeypatch):
    monkeypatch.setattr(mcp_image, "MAX_DIMENSION", 500)
    monkeypatch.setattr(mcp_image, "MAX_DECODE_PIXELS", 1_000_000)
    monkeypatch.setattr(mcp_image.PILImage, "MAX_IMAGE_PIXELS", 500_000)


@pytest.mark.usefixtures("small_decode_limits")
@pytest.mark.parametrize("size", [(4000, 4000), (4000, 500)], ids=["square", "panorama"])
async def test_large_jpegs_are_decoded_at_reduced_scale(tmp_path, size):
    jpeg, png = tmp_path / "big.jpg", tmp_path / "big.png"
    jpeg.write_bytes(to_bytes(make_image(size=size), "JPEG"))
    png.write_bytes(to_bytes(make_image(size=(2000, 2000)), "PNG"))
    result = await call_fetch([str(jpeg), str(png)])
    assert [b.type for b in result.content] == ["image", "text"]
    assert max(decode(result.content[0]).size) <= 500
    assert "too large" in result.content[1].text


async def test_huge_images_inside_ico_files_are_rejected(tmp_path):
    path = tmp_path / "favicon.ico"
    path.write_bytes(ico_containing(png_with_declared_size(15_000, 15_000)))
    (block,) = (await call_fetch([str(path)])).content
    assert block.type == "text" and "too large" in block.text


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script as a fake Ghostscript")
async def test_eps_is_not_opened_so_ghostscript_never_runs(tmp_path, monkeypatch):
    marker = tmp_path / "gs-was-run"
    fake_gs = tmp_path / "bin" / "gs"
    fake_gs.parent.mkdir()
    fake_gs.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    fake_gs.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_gs.parent}{os.pathsep}{os.environ['PATH']}")
    path = tmp_path / "drawing.eps"
    path.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nnewpath 0 0 moveto 10 10 lineto stroke\n%%EOF\n")
    (block,) = (await call_fetch([str(path)])).content
    assert block.type == "text" and "not a recognized image" in block.text
    assert not marker.exists()


@pytest.mark.parametrize("mode", ["1", "P"])
async def test_palette_and_1bit_images_are_smoothed_when_downscaled(tmp_path, mode):
    row_bytes = 3000 // 8
    stripes = Image.frombytes("1", (3000, 3000), (b"\x00" * row_bytes + b"\xff" * row_bytes) * 1500)
    path = tmp_path / "stripes.png"
    path.write_bytes(to_bytes(stripes if mode == "1" else stripes.convert("P"), "PNG"))
    (block,) = (await call_fetch([str(path)])).content
    gray = decode(block).convert("L")
    assert len(gray.getcolors(256)) > 2
    assert 100 < gray.getpixel((gray.width // 2, gray.height // 2)) < 155


async def test_16bit_images_keep_their_tones(tmp_path):
    gradient = Image.linear_gradient("L").resize((2000, 2000)).convert("I").point(lambda v: v * 256).convert("I;16")
    path = tmp_path / "depth.png"
    path.write_bytes(to_bytes(gradient, "PNG"))
    (block,) = (await call_fetch([str(path)])).content
    gray = decode(block).convert("L")
    assert 100 < gray.getpixel((gray.width // 2, gray.height // 2)) < 155
    assert len(gray.getcolors(256)) > 200


async def test_truncated_images_are_reported(tmp_path):
    path = tmp_path / "cut.jpg"
    path.write_bytes(to_bytes(make_image(size=(300, 200), noise=True), "JPEG")[:-2000])
    (block,) = (await call_fetch([str(path)])).content
    assert block.type == "text" and "truncated or corrupt" in block.text


async def test_slow_downloads_have_an_overall_deadline(http_server, monkeypatch):
    monkeypatch.setattr(mcp_image, "FETCH_TIMEOUT", 1)
    (block,) = (await call_fetch([http_server + "/slow"])).content
    assert block.type == "text" and "took longer than 1s" in block.text


@pytest.mark.parametrize("route", ["/img.png", "/img-charset.png", "/redirect", "/octet", "/no-type"])
async def test_url_variants_return_the_image(http_server, route):
    (block,) = (await call_fetch([http_server + route])).content
    assert block.type == "image" and block.mime_type == "image/png"


async def test_url_sends_user_agent_and_accept(http_server):
    Handler.seen_headers.clear()
    await call_fetch([http_server + "/img.png"])
    assert Handler.seen_headers[-1]["User-Agent"].startswith("mcp-image/")
    assert Handler.seen_headers[-1]["Accept"].startswith("image/png,image/jpeg")


@pytest.mark.parametrize(
    "route, message", [("/html", "not a recognized image (Content-Type: text/html)"), ("/missing", "HTTP 404 Not Found")]
)
async def test_url_failures_explain_why(http_server, route, message):
    (block,) = (await call_fetch([http_server + route])).content
    assert block.type == "text" and message in block.text


async def test_stdio_server_starts_without_writing_to_cwd_even_with_a_bad_log_level(tmp_path):
    module = Path(mcp_image.__file__).resolve()
    work = tmp_path / "work"
    work.mkdir()
    params = StdioServerParameters(
        command=sys.executable, args=[str(module)], cwd=work, env={"MCP_IMAGE_LOG_DIR": str(tmp_path / "logs"), "MCP_IMAGE_LOG_LEVEL": "verbose"}
    )
    async with Client(params) as client:
        result = await client.call_tool("fetch_images", {"image_sources": [str(module)]})
    assert result.content[0].type == "text"
    assert list(work.iterdir()) == []
    assert any((tmp_path / "logs").glob("*.log"))


@pytest.fixture
def svg_support():
    try:
        import cairosvg  # noqa: F401
    except (ImportError, OSError) as e:
        if os.environ.get("REQUIRE_SVG"):
            pytest.fail(f"REQUIRE_SVG is set but SVG support is unavailable: {e}")
        pytest.skip(f"SVG support unavailable: {e}")


@pytest.mark.usefixtures("svg_support")
async def test_small_svg_is_rendered_large_enough_to_read(tmp_path):
    path = tmp_path / "icon.svg"
    path.write_bytes(ICON_SVG)
    (block,) = (await call_fetch([str(path)])).content
    img = decode(block)
    assert block.mime_type == "image/png"
    assert img.size == (mcp_image.SVG_TARGET_DIMENSION, mcp_image.SVG_TARGET_DIMENSION // 2)
    assert img.convert("RGB").getpixel((10, 10)) == (255, 0, 0)


@pytest.mark.usefixtures("svg_support")
async def test_svg_from_url(http_server):
    (block,) = (await call_fetch([http_server + "/icon"])).content
    assert block.type == "image" and block.mime_type == "image/png"


def svg_document(width, height, body='<rect width="100%" height="100%" fill="blue"/>', defs=""):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width}" height="{height}"><defs>{defs}</defs>{body}</svg>'
    ).encode()


def embedded_image(data, mime="image/png"):
    return f'<image xlink:href="data:{mime};base64,{base64.b64encode(data).decode()}" width="10" height="10"/>'


@pytest.mark.usefixtures("svg_support")
async def test_large_svg_is_scaled_down(tmp_path):
    path = tmp_path / "poster.svg"
    path.write_bytes(svg_document(6000, 4000))
    (block,) = (await call_fetch([str(path)])).content
    assert_within_limits(block)
    assert decode(block).size == (mcp_image.MAX_DIMENSION, round(mcp_image.MAX_DIMENSION * 4000 / 6000))


@pytest.mark.usefixtures("svg_support")
@pytest.mark.parametrize("size", [(20_000, 10_000), (200_000, 200_000)])
async def test_svgs_declaring_more_than_the_pixel_limit_are_rejected(tmp_path, size):
    path = tmp_path / "poster.svg"
    path.write_bytes(svg_document(*size, defs='<mask id="m"><rect width="100%" height="100%" fill="white"/></mask>'))
    start = time.perf_counter()
    (block,) = (await call_fetch([str(path)])).content
    assert time.perf_counter() - start < 5
    assert block.type == "text" and f"SVG declares {size[0]}x{size[1]}px" in block.text


@pytest.mark.usefixtures("svg_support")
async def test_slow_svgs_are_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_image, "SVG_RENDER_TIMEOUT", 1)
    tiles = '<pattern id="p" width="4" height="4" patternUnits="userSpaceOnUse"><rect width="2" height="2"/></pattern>'
    path = tmp_path / "tiles.svg"
    path.write_bytes(svg_document(6300, 6300, body='<rect width="100%" height="100%" fill="url(#p)"/>', defs=tiles))
    (block,) = (await call_fetch([str(path)])).content
    assert block.type == "text" and "took longer than 1s to render" in block.text


@pytest.mark.usefixtures("svg_support")
@pytest.mark.parametrize(
    "image, message",
    [
        (embedded_image(png_with_declared_size(15_000, 15_000)), "an embedded image is over 40 megapixels"),
        (embedded_image(ICON_SVG, "image/svg+xml"), "embedded images must be PNG, JPEG, GIF, WebP or BMP"),
        (embedded_image(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n%%EOF\n", "application/postscript"),
         "embedded images must be PNG, JPEG, GIF, WebP or BMP"),
    ],
    ids=["huge-png", "nested-svg", "eps"],
)
async def test_embedded_images_are_checked(tmp_path, image, message):
    path = tmp_path / "embeds.svg"
    path.write_bytes(svg_document(100, 100, body=image))
    (block,) = (await call_fetch([str(path)])).content
    assert block.type == "text" and message in block.text


@pytest.mark.usefixtures("svg_support")
async def test_gzipped_svg_bodies_are_decompressed_within_a_limit(http_server):
    result = await call_fetch([http_server + "/icon.svgz", http_server + "/bomb.svgz"])
    assert result.content[0].type == "image"
    assert "larger than 10 MB once decompressed" in result.content[1].text


@pytest.mark.usefixtures("svg_support")
@pytest.mark.parametrize("attrs", ['width="400" height="4"', 'width="1" height="1" viewBox="0 0 100 100"'])
async def test_svgs_with_tiny_declared_sizes_render(tmp_path, attrs):
    path = tmp_path / "tiny.svg"
    path.write_bytes(f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}><rect width="100%" height="100%" fill="red"/></svg>'.encode())
    (block,) = (await call_fetch([str(path)])).content
    assert block.type == "image"
    assert max(decode(block).size) == mcp_image.SVG_TARGET_DIMENSION


@pytest.mark.usefixtures("svg_support")
async def test_svg_after_a_long_prolog_is_detected(tmp_path):
    path = tmp_path / "licensed.svg"
    path.write_bytes(b'<?xml version="1.0"?>\n<!-- ' + b"license text " * 200 + b"-->\n" + ICON_SVG.split(b"\n", 1)[1])
    (block,) = (await call_fetch([str(path)])).content
    assert block.type == "image"


@pytest.mark.usefixtures("svg_support")
async def test_svg_served_with_a_generic_content_type_is_detected(http_server):
    (block,) = (await call_fetch([http_server + "/icon-as-text"])).content
    assert block.type == "image"


async def test_html_with_inline_svg_is_not_treated_as_svg(http_server, tmp_path):
    page = tmp_path / "page.html"
    page.write_bytes(b"<!doctype html><html><body>" + ICON_SVG + b"</body></html>")
    result = await call_fetch([http_server + "/page-with-svg", str(page)])
    assert "not a recognized image (Content-Type: text/html)" in result.content[0].text
    assert "not a recognized image" in result.content[1].text


@pytest.mark.usefixtures("svg_support")
async def test_svg_cannot_read_other_local_files(tmp_path):
    (tmp_path / "secret.png").write_bytes(SMALL_PNG)
    path = tmp_path / "sneaky.svg"
    path.write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="64" height="48">'
        b'<rect width="64" height="48" fill="white"/>'
        b'<image xlink:href="' + (tmp_path / "secret.png").as_uri().encode() + b'" width="64" height="48"/></svg>'
    )
    (block,) = (await call_fetch([str(path)])).content
    assert decode(block).convert("RGB").getpixel((30, 20)) == (255, 255, 255)


async def test_svg_without_extra_explains_how_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_image, "find_spec", lambda name: None)
    path = tmp_path / "icon.svg"
    path.write_bytes(ICON_SVG)
    (block,) = (await call_fetch([str(path)])).content
    assert block.type == "text" and "mcp-image[svg]" in block.text
