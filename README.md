# MCP Server - Image
A Model Context Protocol (MCP) server that lets an AI assistant look at images. Its `fetch_images` tool loads images from URLs or local file paths and returns them in a form LLM clients can display, downscaling and recompressing them when needed so they fit client limits.

## Support Us

If you find this project helpful and would like to support future projects, consider buying us a coffee! Your support helps us continue building innovative AI solutions.

<a href="https://www.buymeacoffee.com/blazzmocompany"><img src="https://img.buymeacoffee.com/button-api/?text=Buy me a coffee&emoji=&slug=blazzmocompany&button_colour=40DCA5&font_colour=ffffff&font_family=Cookie&outline_colour=000000&coffee_colour=FFDD00"></a>

Your contributions go a long way in fueling our passion for creating intelligent and user-friendly applications.

## Table of Contents
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Running the Server](#running-the-server)
- [Client Configuration](#client-configuration)
- [Available Tools](#available-tools)
- [Configuration](#configuration)
- [Testing and Debugging](#testing-and-debugging)
- [Contributing](#contributing)
- [License](#license)

## Features
- Fetch images from `http(s)` URLs (redirects followed) and local paths, including `~/...` and `file://` URIs
- Detects the real format from the file contents, so wrong extensions and `application/octet-stream` responses (e.g. Telegram's file API) still work
- Reads JPEG, PNG, GIF, WebP, BMP, TIFF, ICO, PPM, TGA and AVIF; returns JPEG, PNG, GIF and WebP as-is when they are small enough and converts the rest to PNG or JPEG. Other formats are refused (EPS, for instance, would run Ghostscript)
- Downscales to 1568px on the longer edge; all images in one call share a ~700 KB budget (about 930 KB once base64-encoded), under the ~1 MB tool-result limit Claude Desktop is reported to enforce
- Keeps transparency (PNG) when possible, applies EXIF rotation, and converts palette, 1-bit and 16-bit images without losing their tones
- Optional SVG support: small icons are rendered large enough to read, and an SVG cannot pull in other local files or URLs
- PDF rendering: query only the page count or render pages in a specified order; PyMuPDF is installed on first use through a multi-source download race
- Processes up to 20 sources per call in parallel; a failed source returns a message saying why, in its position in the list

## Prerequisites
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended)
- For SVG support only: the native Cairo library
  - Debian/Ubuntu: `sudo apt install libcairo2`
  - macOS: `brew install cairo` (on Apple Silicon, also set `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` for the server)
  - Windows: install the [GTK3 runtime](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer). Its default folder (`C:\Program Files\GTK3-Runtime Win64\bin`) is found automatically; for another location, or another app that bundles `libcairo-2.dll`, set `CAIROCFFI_DLL_DIRECTORIES` to that folder

## Installation
```bash
git clone https://github.com/IA-Programming/mcp-images.git
cd mcp-images
uv sync                # core
uv sync --extra svg    # or: with SVG support
```

## Running the Server
The server speaks MCP over stdio, so normally your MCP client starts it (see below). To run it by hand:

```bash
uv run mcp-image
```

Without cloning, straight from GitHub:

```bash
uvx --from git+https://github.com/IA-Programming/mcp-images mcp-image
```

With SVG support:

```bash
uvx --from "mcp-image[svg] @ git+https://github.com/IA-Programming/mcp-images" mcp-image
```

With Docker (SVG support included):

```bash
docker build -t mcp-image .
docker run -i --rm -v "$HOME/Pictures:/images:ro" mcp-image
```

Inside the container, local paths refer to the mounted folder, e.g. `/images/photo.jpg`.

The tool can read any file the server process can read and fetch any URL it can reach, including hosts on your local network. Running it in Docker with read-only mounts limits it to the folders you choose.

## Client Configuration
Replace `/path/to/mcp-images` with where you cloned the repository. Drop `"--extra", "svg"` if you did not install Cairo.

#### Claude Code
This repository ships a `.mcp.json`, so Claude Code sessions opened in it pick the server up automatically. To add it to another project:

```bash
claude mcp add image -- uv run --directory /path/to/mcp-images --extra svg mcp-image
```

#### Claude Desktop, Cursor, Windsurf
Add to `claude_desktop_config.json`, Cursor's *Settings → MCP*, or `~/.codeium/windsurf/mcp_config.json`:
```json
{
  "mcpServers": {
    "image": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcp-images", "--extra", "svg", "mcp-image"]
    }
  }
}
```

#### VS Code
Add to your user or workspace `mcp.json`:
```json
{
  "servers": {
    "image": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcp-images", "--extra", "svg", "mcp-image"]
    }
  }
}
```

## Available Tools
**fetch_images** (read-only): fetch images from URLs or local file paths.

Parameters:
- `image_sources` (required): list of URLs or file paths. For one image, pass a one-element list.

Returns one entry per source, in the same order: the image, or a text message explaining why that source failed (for example `Error processing /tmp/a.png: file not found: /tmp/a.png`).

Limits per call: 20 sources, 50 MB per file or download, 30 seconds per download, and 40 megapixels per decoded image (large JPEGs are decoded at a reduced scale, so camera photos are fine). SVGs are rendered in a separate process and must declare at most 40 megapixels, be at most 10 MB (after decompressing gzip), and render within 10 seconds; they can only embed PNG, JPEG, GIF, WebP or BMP images as `data:` URLs. On Linux the renderer is also limited to 1.5 GB of memory, and on Linux and macOS to 15 seconds of CPU time even if the server exits mid-render. Windows has neither limit: there the 10-second timeout only applies while the server is running, so a renderer can outlive a server that is killed mid-render.

**pdf_to_images**: render pages from a local PDF to images.

- `pdf_path` (required): local PDF path or `file://` URI.
- `pages` (optional): one-based page numbers; when omitted, only the total page count is returned and no pages are rendered.
- `dpi` (optional): rendering resolution, defaulting to 150 and clamped to 50-1200.
- Results preserve the requested page order, a failure on one page does not affect the others, and each batch is limited to 20 pages sharing the image-size budget.
- PyMuPDF is installed on first use when missing; it can also be installed in advance with the project's `pdf` extra.

### Usage Examples
- "Fetch these images: [list of URLs or file paths]"
- "Look at ~/Desktop/screenshot.png and tell me what's wrong with the layout"

```json
[
  "https://upload.wikimedia.org/wikipedia/commons/thumb/7/70/Chocolate_%28blue_background%29.jpg/330px-Chocolate_%28blue_background%29.jpg",
  "https://upload.wikimedia.org/wikipedia/commons/0/02/SVG_logo.svg",
  "C:\\Users\\username\\Pictures\\image1.jpg",
  "~/Pictures/image2.png"
]
```

## Configuration
Optional environment variables:

| Variable | Default | Description |
|---|---|---|
| `MCP_IMAGE_MAX_DIMENSION` | `1568` | Longest edge, in pixels, of returned images |
| `MCP_IMAGE_MAX_TOTAL_BYTES` | `700000` | Size budget shared by all images in one call, before base64 encoding |
| `MCP_IMAGE_LOG_LEVEL` | `INFO` | Level of the logs written to stderr (`DEBUG` for details) |
| `MCP_IMAGE_LOG_DIR` | unset | Folder for a detailed debug log file; no file is written when unset |
| `CAIROCFFI_DLL_DIRECTORIES` | GTK3 runtime folder, if installed | Windows only, read by cairocffi: folders (separated by `;`) containing `libcairo-2.dll` |

## Testing and Debugging
Run the test suite:

```bash
uv sync --all-extras
uv run pytest
```

Try the tool interactively with the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector uv run mcp-image
```

Or from the command line:

```bash
npx @modelcontextprotocol/inspector --cli uv run mcp-image --method tools/call --tool-name fetch_images --tool-arg 'image_sources=["/path/to/image.png"]'
```

Or with the bundled stdio smoke test, which exits non-zero unless every source comes back as an image:

```bash
uv run python tests/stdio_smoke.py /path/to/image.png -- uv run mcp-image
```

Logs:
- stderr shows one line per processed source and a summary per call; most clients show this in their MCP server logs.
- With `MCP_IMAGE_LOG_DIR` set, a detailed debug log (formats, sizes, compression steps) is also written there, in a file named after the date the server started.
- URLs are logged without credentials, query strings or Telegram bot tokens.

## Contributing
Contributions are welcome! Please run `uv run pytest` before submitting a Pull Request.

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
