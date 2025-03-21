#!/usr/bin/env python3

import os
import sys
import asyncio
import httpx
import logging
from io import BytesIO
from datetime import datetime
from PIL import Image as PILImage
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP, Image, Context
from typing import List, Dict, Any, Union, Optional

MAX_IMAGE_SIZE = 1024  # Maximum dimension size in pixels
TEMP_DIR = "./Temp"
DATA_DIR = "./data"

# Ensure directories exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Configure logging: first disable other loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

# Configure our logger
log_filename = os.path.join(DATA_DIR, datetime.now().strftime("%d-%m-%y.log"))
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Create handlers
file_handler = logging.FileHandler(log_filename)
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler(sys.stderr)
console_handler.setFormatter(formatter)

# Set up our logger
logger = logging.getLogger("image-mcp")
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(console_handler)
# Prevent double logging
logger.propagate = False

# Create a FastMCP server instance
mcp = FastMCP("image-service")

async def process_image_data(data: bytes, content_type: str, image_source: str, ctx: Context) -> Image | None:
    """Process image data and return an MCP Image object."""
    try:
        # If image is not large, try to log dimensions without processing
        if len(data) <= 1048576:
            try:
                with PILImage.open(BytesIO(data)) as img:
                    width, height = img.size
                    logger.debug(f"Original image dimensions from {image_source}: {width}x{height}")
            except Exception as e:
                logger.debug(f"Could not determine dimensions for {image_source}: {e}")
            return Image(data=data, format=content_type.split('/')[-1])

        # For large images, save to temp file and process
        temp_path = os.path.join(TEMP_DIR, f"temp_image_{hash(image_source)}." + content_type.split('/')[-1])
        with open(temp_path, "wb") as f:
            f.write(data)
        
        try:
            with PILImage.open(temp_path) as img:
                orig_width, orig_height = img.size
                logger.debug(f"Original image dimensions from {image_source}: {orig_width}x{orig_height}")
                
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                
                width, height = img.size
                new_img = img
                quality = 85
                
                while True:
                    img_byte_arr = BytesIO()
                    new_img.save(img_byte_arr, format='JPEG', quality=quality)
                    if len(img_byte_arr.getvalue()) <= 1048576:
                        try:
                            with PILImage.open(BytesIO(img_byte_arr.getvalue())) as processed_img:
                                new_width, new_height = processed_img.size
                                logger.debug(f"Processed image dimensions from {image_source}: {new_width}x{new_height} (quality={quality})")
                        except Exception as e:
                            logger.debug(f"Could not determine processed dimensions for {image_source}: {e}")
                        return Image(data=img_byte_arr.getvalue(), format='jpeg')
                    
                    if quality > 30:
                        quality -= 10
                    else:
                        width = int(width * 0.8)
                        height = int(height * 0.8)
                        if width < 200 or height < 200:
                            ctx.error("Unable to compress image to acceptable size while maintaining quality")
                            logger.error(f"Failed processing image from {image_source}: dimensions {width}x{height} too small")
                            return None
                        new_img = img.resize((width, height), PILImage.LANCZOS)
                        quality = 85
        except Exception as e:
            ctx.error(f"Image processing error: {str(e)}")
            logger.exception(f"Exception processing image from {image_source}")
            return None
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
                
    except Exception as e:
        ctx.error(f"Error processing image: {str(e)}")
        logger.exception(f"Unexpected error processing {image_source}")
        return None

async def process_local_image(file_path: str, ctx: Context) -> Dict[str, Any]:
    """Processes a local image file and returns a dictionary with the result."""
    try:
        if not os.path.exists(file_path):
            error_msg = f"File not found: {file_path}"
            ctx.error(error_msg)
            logger.error(error_msg)
            return {"path": file_path, "error": error_msg}
        
        # Determine content type based on file extension
        _, ext = os.path.splitext(file_path)
        content_type = ext[1:].lower() if ext else "jpeg"  # Default to jpeg if no extension
        
        logger.debug(f"Processing local image from {file_path}")
        
        # For large files, read and process directly without loading entire file into memory
        file_size = os.path.getsize(file_path)
        if file_size > 1048576:
            logger.debug(f"Large local image detected: {file_path} ({file_size} bytes)")
            # Process the image directly using the same logic as for URL images
            return await process_large_local_image(file_path, content_type, ctx)
        
        # For smaller files, read the entire content
        with open(file_path, "rb") as f:
            file_data = f.read()
        
        logger.debug(f"Read local image from {file_path} with {len(file_data)} bytes")
        processed_image = await process_image_data(file_data, content_type, file_path, ctx)
        
        if processed_image is None:
            return {"path": file_path, "error": "Failed to process image"}
        
        return {"path": file_path, "image": processed_image}
        
    except Exception as e:
        error_msg = f"Error processing local image {file_path}: {str(e)}"
        ctx.error(error_msg)
        logger.exception(error_msg)
        return {"path": file_path, "error": error_msg}

async def process_large_local_image(file_path: str, content_type: str, ctx: Context) -> Dict[str, Any]:
    """Process a large local image file directly without loading it entirely into memory."""
    temp_path = None
    try:
        # Create a temporary file path for processing
        temp_path = os.path.join(TEMP_DIR, f"temp_local_{os.path.basename(file_path)}")
        
        # Open the original image with PIL directly
        with PILImage.open(file_path) as img:
            orig_width, orig_height = img.size
            logger.debug(f"Original large local image dimensions from {file_path}: {orig_width}x{orig_height}")
            
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            
            width, height = img.size
            new_img = img
            quality = 85
            
            while True:
                # Save the processed image to a temporary BytesIO
                img_byte_arr = BytesIO()
                new_img.save(img_byte_arr, format='JPEG', quality=quality)
                processed_data = img_byte_arr.getvalue()
                
                if len(processed_data) <= 1048576:
                    logger.debug(f"Successfully compressed large local image {file_path} to {len(processed_data)} bytes (quality={quality})")
                    return {"path": file_path, "image": Image(data=processed_data, format='jpeg')}
                
                if quality > 30:
                    quality -= 10
                    logger.debug(f"Reducing quality to {quality} for {file_path}")
                else:
                    width = int(width * 0.8)
                    height = int(height * 0.8)
                    if width < 200 or height < 200:
                        error_msg = f"Unable to compress large local image {file_path} to acceptable size while maintaining quality"
                        ctx.error(error_msg)
                        logger.error(error_msg)
                        return {"path": file_path, "error": error_msg}
                    
                    logger.debug(f"Resizing large local image {file_path} to {width}x{height}")
                    new_img = img.resize((width, height), PILImage.LANCZOS)
                    quality = 85
    
    except Exception as e:
        error_msg = f"Error processing large local image {file_path}: {str(e)}"
        ctx.error(error_msg)
        logger.exception(error_msg)
        return {"path": file_path, "error": error_msg}
    
    finally:
        # Clean up temporary file if it exists
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

async def fetch_single_image(url: str, client: httpx.AsyncClient, ctx: Context) -> Dict[str, Any]:
    """Fetches and processes a single image asynchronously."""
    try:
        parsed = urlparse(url)
        if not all([parsed.scheme in ['http', 'https'], parsed.netloc]):
            error_msg = f"Invalid URL: {url}"
            ctx.error(error_msg)
            return {"url": url, "error": error_msg}

        response = await client.get(url)
        response.raise_for_status()
        
        content_type = response.headers.get('content-type', '')
        if not content_type.startswith('image/'):
            error_msg = f"Not an image (got {content_type})"
            ctx.error(error_msg)
            return {"url": url, "error": error_msg}

        logger.debug(f"Fetched image from {url} with {len(response.content)} bytes")
        processed_image = await process_image_data(response.content, content_type.split('/')[-1], url, ctx)
        
        if processed_image is None:
            return {"url": url, "error": "Failed to process image"}
        
        return {"url": url, "image": processed_image}

    except httpx.HTTPError as e:
        error_msg = f"HTTP error: {str(e)}"
        ctx.error(error_msg)
        return {"url": url, "error": error_msg}
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        ctx.error(error_msg)
        return {"url": url, "error": error_msg}

def is_url(path_or_url: str) -> bool:
    """Determine if the given string is a URL or a local file path."""
    parsed = urlparse(path_or_url)
    return bool(parsed.scheme and parsed.netloc)

async def process_images_async(image_sources: List[str], ctx: Context) -> List[Dict[str, Any]]:
    """Process multiple images (URLs or local files) concurrently."""
    if not image_sources:
        raise ValueError("No image sources provided")
    
    # Separate URLs from local file paths
    urls = [src for src in image_sources if is_url(src)]
    local_paths = [src for src in image_sources if not is_url(src)]
    
    results = []
    
    # Process URLs if any
    if urls:
        logger.debug(f"Processing {len(urls)} URLs")
        async with httpx.AsyncClient() as client:
            url_tasks = [fetch_single_image(url, client, ctx) for url in urls]
            url_results = await asyncio.gather(*url_tasks)
            results.extend(url_results)
    
    # Process local files if any
    if local_paths:
        logger.debug(f"Processing {len(local_paths)} local files")
        local_tasks = [process_local_image(path, ctx) for path in local_paths]
        local_results = await asyncio.gather(*local_tasks)
        results.extend(local_results)
    
    # Ensure results are in the same order as input sources
    ordered_results = []
    for src in image_sources:
        for result in results:
            if (src == result.get("url", None)) or (src == result.get("path", None)):
                ordered_results.append(result)
                break
    
    return ordered_results

@mcp.tool()
async def fetch_images(image_sources: List[str], ctx: Context) -> List[Image | None]:
    """
    Fetch and process images from URLs or local file paths, returning them in a format suitable for LLMs.
    
    This tool accepts a list of image sources which can be either:
    1. URLs pointing to web-hosted images (http:// or https://)
    2. Local file paths pointing to images stored on the local filesystem (e.g., "C:/images/photo1.jpg")
    
    For a single image, provide a one-element list. The function will process images in parallel
    when multiple sources are provided. Images that exceed the size limit (1MB) will be automatically 
    compressed while maintaining aspect ratio and reasonable quality.
    
    Args:
        image_sources: A list of image URLs or local file paths. For a single image, provide a one-element list.
        
    Returns:
        A list of Image objects or None values (if processing failed) in the same order as the input sources.
    """
    try:
        start_time = asyncio.get_event_loop().time()
        
        # Validate input
        if not image_sources:
            ctx.error("No image sources provided")
            logger.error("fetch_images called with empty source list")
            return []
        
        # Log the types of sources we're processing
        url_count = sum(1 for src in image_sources if is_url(src))
        local_count = len(image_sources) - url_count
        logger.debug(f"Processing {len(image_sources)} image sources: {url_count} URLs and {local_count} local files")
        
        # Process all images
        results = await process_images_async(image_sources, ctx)
        
        # Extract just the Image objects or None values
        image_results = []
        for result in results:
            if "image" in result:
                image_results.append(result["image"])
            else:
                image_results.append(None)
        
        elapsed = asyncio.get_event_loop().time() - start_time
        success_count = sum(1 for r in image_results if r is not None)
        
        logger.debug(
            f"Processed {len(image_sources)} images in {elapsed:.2f} seconds. "
            f"Success: {success_count}, Failed: {len(image_sources) - success_count}"
        )
        
        return image_results
    except Exception as e:
        logger.exception("Error in fetch_images")
        ctx.error(f"Failed to process images: {str(e)}")
        return [None] * len(image_sources)

if __name__ == "__main__":
    mcp.run(transport='stdio')
