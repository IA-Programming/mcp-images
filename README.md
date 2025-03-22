# MCP Server - Image
A Model Context Protocol (MCP) server that provides tools for fetching and processing images from URLs, local file paths, and numpy arrays. The server includes a tool called fetch_images that returns images as base64-encoded strings along with their MIME types.

## Support Us

If you find this project helpful and would like to support future projects, consider buying us a coffee! Your support helps us continue building innovative AI solutions.

<a href="https://www.buymeacoffee.com/blazzmocompany"><img src="https://img.buymeacoffee.com/button-api/?text=Buy me a coffee&emoji=&slug=blazzmocompany&button_colour=40DCA5&font_colour=ffffff&font_family=Cookie&outline_colour=000000&coffee_colour=FFDD00"></a>

Your contributions go a long way in fueling our passion for creating intelligent and user-friendly applications.

## Table of Contents
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Running the Server](#running-the-server)
  - [Direct Method](#1-direct-method)
  - [Configure for Windsurf/Cursor](#2-configure-for-windsurfcursor)
- [Available Tools](#available-tools)
  - [Usage Examples](#usage-examples)
- [Debugging](#debugging)
- [Contributing](#contributing)
- [License](#license)

## Features
- Fetch images from URLs (http/https)
- Load images from local file paths
- Specialized handling for large local images
- Automatic image compression for large images (>1MB)
- Parallel processing of multiple images
- Proper MIME type mapping for different file extensions
- Comprehensive error handling and logging
## Prerequisites
- Python 3.10+
- uv package manager (recommended)
## Installation
1. Clone this repository
2. Create and activate a virtual environment using uv:
```bash
uv venv
# On Windows:
.venv\Scripts\activate
# On Unix/MacOS:
source .venv/bin/activate
```
3. Install dependencies using uv:
```bash
uv pip install -r requirements.txt
```
## Running the Server
There are two ways to run the MCP server:

### 1. Direct Method
To start the MCP server directly:

```bash
uv run python mcp_image.py
```
### 2. Configure for Windsurf/Cursor
#### Windsurf
To add this MCP server to Windsurf:

1. Edit the configuration file at ~/.codeium/windsurf/mcp_config.json
2. Add the following configuration:
```json
{
  "mcpServers": {
    "image": {
      "command": "uv",
        "args": ["--directory", "/path/to/mcp-image", "run", "mcp_image.py"]
    }
  }
}
```
#### Cursor
To add this MCP server to Cursor:

1. Open Cursor and go to *Settings* (Navbar → Cursor Settings)
2. Navigate to *Features* → *MCP Servers*
3. Click on + Add New MCP Server
4. Enter the following configuration:
```json
{
  "mcpServers": {
    "image": {
      "command": "uv",
      "args": ["--directory", "/path/to/mcp-image", "run", "mcp_image.py"]
    }
  }
}
```

## Available Tools
The server provides the following tools:

[fetch_images](mcp_image.py#L318): Fetch and process images from URLs or local file paths
Parameters:
image_sources: List of URLs or file paths to images
Returns:
List of processed images with base64 encoding and MIME types

### Usage Examples
You can now use commands like:

- "Fetch these images: [list of URLs or file paths]"
- "Load and process this local image: [file_path]"

#### Examples
```
# URL-only test
[
  "https://upload.wikimedia.org/wikipedia/commons/thumb/7/70/Chocolate_%28blue_background%29.jpg/400px-Chocolate_%28blue_background%29.jpg",
  "https://imgs.search.brave.com/Sz7BdlhBoOmU4wZjnUkvgestdwmzOzrfc3GsiMr27Ik/rs:fit:860:0:0:0/g:ce/aHR0cHM6Ly9pbWdj/ZG4uc3RhYmxlZGlm/ZnVzaW9ud2ViLmNv/bS8yMDI0LzEwLzE4/LzJmOTY3NTViLTM0/YmQtNDczNi1iNDRh/LWJlMTVmNGM5MDBm/My5qcGc",
  "https://shigacare.fukushi.shiga.jp/mumeixxx/img/main.png"
]

# Mixed URL and local file test
[
  "https://upload.wikimedia.org/wikipedia/commons/thumb/7/70/Chocolate_%28blue_background%29.jpg/400px-Chocolate_%28blue_background%29.jpg",
  "C:\\Users\\username\\Pictures\\image1.jpg",
  "https://imgs.search.brave.com/Sz7BdlhBoOmU4wZjnUkvgestdwmzOzrfc3GsiMr27Ik/rs:fit:860:0:0:0/g:ce/aHR0cHM6Ly9pbWdj/ZG4uc3RhYmxlZGlm/ZnVzaW9ud2ViLmNv/bS8yMDI0LzEwLzE4/LzJmOTY3NTViLTM0/YmQtNDczNi1iNDRh/LWJlMTVmNGM5MDBm/My5qcGc",
  "C:\\Users\\username\\Pictures\\image2.jpg"
]
```

## Debugging
If you encounter any issues:

1. Check that all dependencies are installed correctly
2. Verify that the server is running and listening for connections
3. For local image loading issues, ensure the file paths are correct and accessible
4. For "Unsupported image type" errors, verify the content type handling
5. Look for any error messages in the server output
## Contributing
Contributions are welcome! Please feel free to submit a Pull Request.

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.