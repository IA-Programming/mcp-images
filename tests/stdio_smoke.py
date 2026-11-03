"""Smoke-test an MCP server command over stdio.

Usage: python tests/stdio_smoke.py SOURCE... -- COMMAND...
Initializes the server, lists its tools, calls fetch_images with SOURCE... and fails unless every
result is an image. Each response is awaited by id, so slow starts cannot race the input.
"""

import json
import subprocess
import sys


def main() -> None:
    split = sys.argv.index("--")
    sources, command = sys.argv[1:split], sys.argv[split + 1 :]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        proc.stdin.flush()

    def request(request_id: int, method: str, params: dict) -> dict:
        send({"id": request_id, "method": method, "params": params})
        for line in proc.stdout:
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    sys.exit(f"{method} failed: {message['error']}")
                return message["result"]
        sys.exit(f"server exited before answering {method}")

    info = request(1, "initialize", {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "stdio-smoke", "version": "1"},
    })
    send({"method": "notifications/initialized"})
    print("server:", info["serverInfo"])
    tools = [tool["name"] for tool in request(2, "tools/list", {})["tools"]]
    print("tools:", tools)
    result = request(3, "tools/call", {"name": "fetch_images", "arguments": {"image_sources": sources}})
    proc.stdin.close()
    proc.wait(timeout=30)

    for source, block in zip(sources, result["content"]):
        print(f"{source}: {block.get('mimeType') or block.get('text')}")
    if "fetch_images" not in tools or any(block["type"] != "image" for block in result["content"]):
        sys.exit("smoke test failed")


if __name__ == "__main__":
    main()
