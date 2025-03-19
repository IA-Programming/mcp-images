#!/usr/bin/env python3

import httpx
import os
from io import BytesIO
from PIL import Image as PILImage
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP, Image, Context

MAX_IMAGE_SIZE = 1024  # Maximum dimension size in pixels
TEMP_DIR = "./Temp"

# Create a FastMCP server instance
mcp = FastMCP("image-service")

@mcp.tool()
async def fetch_image(image_url: str, ctx: Context) -> Image | None:
    """
    Fetch an image from a URL, process it if needed, and return it in a format suitable for LLMs.
    If the image is too large, it will be resized to maintain aspect ratio with max dimension of 1024px.
    """
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

            # Check if image data exceeds maximum length
            if len(response.content) > 1048576:
                # Create temp directory if it doesn't exist
                os.makedirs(TEMP_DIR, exist_ok=True)
                
                # Save original image to temp file
                temp_path = os.path.join(TEMP_DIR, "temp_image." + content_type.split('/')[-1])
                with open(temp_path, "wb") as f:
                    f.write(response.content)
                
                try:
                    # Process image with PIL
                    with PILImage.open(temp_path) as img:
                        # Convert to RGB if necessary
                        if img.mode in ('RGBA', 'P'):
                            img = img.convert('RGB')
                        
                        # Start with original size
                        width, height = img.size
                        new_img = img
                        quality = 85
                        
                        # Iteratively try different quality settings and sizes
                        while True:
                            img_byte_arr = BytesIO()
                            new_img.save(img_byte_arr, format='JPEG', quality=quality)
                            if len(img_byte_arr.getvalue()) <= 1048576:
                                return Image(
                                    data=img_byte_arr.getvalue(),
                                    format='jpeg'
                                )
                            
                            if quality > 30:
                                # Try reducing quality first
                                quality -= 10
                            else:
                                # If quality is already low, reduce size
                                width = int(width * 0.8)
                                height = int(height * 0.8)
                                new_img = img.resize((width, height), PILImage.LANCZOS)
                                quality = 85  # Reset quality for the new size
                            
                            if width < 200 or height < 200:
                                ctx.error("Unable to compress image to acceptable size while maintaining quality")
                                return None
                
                except Exception as e:
                    ctx.error(f"Image processing error: {str(e)}")
                    return None
                finally:
                    # Clean up temp file
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
            
            # If image is within size limits, return as-is
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
