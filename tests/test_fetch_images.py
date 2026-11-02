import base64
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from PIL import Image

import mcp_image
from conftest import SMALL_PNG, Handler, call_fetch, decode, make_image, to_bytes

pytestmark = pytest.mark.anyio


def assert_within_limits(block):
    assert block.type == "image"
    assert block.mime_type in {"image/jpeg", "image/png", "image/gif", "image/webp"}
    assert len(base64.b64decode(block.data)) <= mcp_image.MAX_BYTES
    assert max(decode(block).size) <= mcp_image.MAX_DIMENSION


async def test_tool_is_listed_with_read_only_hint():
    async with Client(mcp_image.mcp) as client:
        (tool,) = (await client.list_tools()).tools
    assert tool.name == "fetch_images"
    assert tool.annotations.read_only_hint is True


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
    assert path.stat().st_size > mcp_image.MAX_BYTES
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


async def test_exif_orientation_is_applied_when_reencoding(tmp_path):
    img = make_image(size=(2000, 1000))
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
    monkeypatch.setattr(mcp_image, "MAX_BYTES", 2_000)
    path = tmp_path / "noise.jpg"
    path.write_bytes(to_bytes(make_image(size=(1200, 1200), noise=True), "JPEG"))
    (block,) = (await call_fetch([str(path)])).content
    assert block.type == "text" and "could not compress" in block.text


@pytest.mark.parametrize("route", ["/img.png", "/img-charset.png", "/redirect", "/octet", "/no-type"])
async def test_url_variants_return_the_image(http_server, route):
    (block,) = (await call_fetch([http_server + route])).content
    assert block.type == "image" and block.mime_type == "image/png"


async def test_url_sends_user_agent(http_server):
    Handler.seen_user_agents.clear()
    await call_fetch([http_server + "/img.png"])
    assert Handler.seen_user_agents[-1].startswith("mcp-image/")


@pytest.mark.parametrize(
    "route, message", [("/html", "not a recognized image (Content-Type: text/html)"), ("/missing", "HTTP 404 Not Found")]
)
async def test_url_failures_explain_why(http_server, route, message):
    (block,) = (await call_fetch([http_server + route])).content
    assert block.type == "text" and message in block.text


async def test_stdio_server_starts_without_writing_to_cwd(tmp_path):
    module = Path(mcp_image.__file__).resolve()
    work = tmp_path / "work"
    work.mkdir()
    params = StdioServerParameters(
        command=sys.executable, args=[str(module)], cwd=work, env={"MCP_IMAGE_LOG_DIR": str(tmp_path / "logs")}
    )
    async with Client(params) as client:
        result = await client.call_tool("fetch_images", {"image_sources": [str(module)]})
    assert result.content[0].type == "text"
    assert list(work.iterdir()) == []
    assert any((tmp_path / "logs").glob("*.log"))
