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
- Returns JPEG, PNG, GIF and WebP as-is when they are small enough; converts everything else (BMP, TIFF, ...) to PNG or JPEG
- Downscales to 1568px on the longer edge and keeps each image under ~750 KB (about 1 MB once base64-encoded, the limit Claude Desktop enforces)
- Keeps transparency (PNG) when possible, applies EXIF rotation
- Optional SVG support: small icons are rendered large enough to read, and an SVG cannot pull in other local files or URLs
- Processes multiple images in parallel; a failed source returns a message saying why, in its position in the list

## Prerequisites
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended)
- For SVG support only: the native Cairo library
  - Debian/Ubuntu: `sudo apt install libcairo2`
  - macOS: `brew install cairo` (on Apple Silicon, also set `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` for the server)
  - Windows: install the [GTK3 runtime](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer) and make sure its `bin` folder is on `PATH`

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

With Docker (SVG support included):

```bash
docker build -t mcp-image .
docker run -i --rm -v "$HOME/Pictures:/images:ro" mcp-image
```

Inside the container, local paths refer to the mounted folder, e.g. `/images/photo.jpg`.

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
| `MCP_IMAGE_MAX_BYTES` | `750000` | Maximum size of each returned image, before base64 encoding |
| `MCP_IMAGE_LOG_LEVEL` | `INFO` | Level of the logs written to stderr (`DEBUG` for details) |
| `MCP_IMAGE_LOG_DIR` | `data/` next to `mcp_image.py` | Folder for the daily debug log file |

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

Logs:
- stderr shows one line per processed source and a summary per call; most clients show this in their MCP server logs.
- A detailed debug log (formats, sizes, compression steps) is written daily to `MCP_IMAGE_LOG_DIR`.

## Contributing
Contributions are welcome! Please run `uv run pytest` before submitting a Pull Request.

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
