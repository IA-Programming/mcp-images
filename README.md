# MCP Server - Tavily

This project implements an MCP server using Python. The server includes a tool called `fetch_image` that retrieves an image from a provided URL. The image is returned as a base64-encoded string along with its MIME type.

## Running the Server

To run the server, use the following command (adjust the path to your Python executable as needed):

```bash
D:\Tools\mcp-tavily\.venv\Scripts\python.exe -m mcp_server_tavily
```

Ensure you have installed the necessary dependencies (e.g., `requests`) in your virtual environment.

## MCP Server Configuration

Below is an example configuration snippet to register this MCP server:

```json
"mcpServers": {
  "image": {
    "command": "D:\\Tools\\mcp-image\\.venv\\Scripts\\python.exe",
    "args": ["D:\\Tools\\mcp-image\\mcp_image.py"]
  }
}
```

## Project Structure

- mcp_server_tavily.py: MCP server implementation with the `fetch_image` tool.
- .gitignore: Lists files and directories to ignore.
- README.md: This file.

## Notes

- This implementation uses a scalable MCP server instead of FastMCP to handle increased loads.
- Avoid creating a `requirements.txt` file as per project guidelines.
