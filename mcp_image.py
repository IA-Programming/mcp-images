#!/usr/bin/env python3

import httpx
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP, Image, Context

# Create a FastMCP server instance
mcp = FastMCP("image-service")

@mcp.tool()
async def fetch_image(image_url: str, ctx: Context) -> Image | None:
    """Fetch an image from a URL and return it in a format suitable for LLMs."""
    # Validate URL
    try:
        parsed = urlparse(image_url)
        if not all([parsed.scheme in ['http', 'https'], parsed.netloc]):
            ctx.error(f"Invalid URL: {image_url}")
            return None
    except Exception as e:
        ctx.error(f"URL parsing error: {str(e)}")
        return None

    # Fetch the image
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(image_url)
            response.raise_for_status()
            
            content_type = response.headers.get('content-type', '')
            if not content_type.startswith('image/'):
                ctx.error(f"Not an image (got {content_type})")
                return None

            return Image(
                data=response.content,
                format=content_type.split('/')[-1]
            )

    except httpx.HTTPError as e:
        ctx.error(f"HTTP error: {str(e)}")
        return None
    except Exception as e:
        ctx.error(f"Error fetching image: {str(e)}")
        return None

if __name__ == "__main__":
    mcp.run(transport='stdio')
