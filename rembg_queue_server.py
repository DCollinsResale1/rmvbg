from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse # Keep HTMLResponse for the basic root
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from rembg import remove, new_session
from PIL import Image # ImageOps might not be strictly needed if not used

import asyncio
import uuid
import io
import os
import aiofiles
import logging
from typing import List, Optional # Optional and List might not be needed if /upload-item is gone
import httpx
import urllib.parse

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

MAX_CONCURRENT_TASKS = 1
ESTIMATED_TIME_PER_JOB = 12
TARGET_SIZE = 1024
LOGO_MAX_WIDTH = 150
LOGO_MARGIN = 20

# Define directories
BASE_DIR = "/workspace/rmvbg" # This directory is now mainly for the logo
UPLOADS_DIR = "/workspace/uploads"
PROCESSED_DIR = "/workspace/processed"
LOGO_FILENAME = "logo.png"
LOGO_PATH = os.path.join(BASE_DIR, LOGO_FILENAME)

prepared_logo_image = None

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)

queue = asyncio.Queue()
results = {}

EXPECTED_API_KEY = "secretApiKey"

MIME_TO_EXT = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/bmp': '.bmp',
    'image/tiff': '.tiff',
}

class SubmitRequestBody(BaseModel):
    image: HttpUrl
    key: str
    model: str = "u2net"
    post_process: bool = False
    steps: int = 20
    samples: int = 1
    resolution: str = "1024x1024"


def get_proxy_url(request: Request):
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme}://{host}"

@app.post("/submit")
async def submit_image_for_processing(
    request: Request,
    body: SubmitRequestBody
):
    if body.key != EXPECTED_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not prepared_logo_image and os.path.exists(LOGO_PATH): # Check if logo should exist but isn't loaded
        logger.error("Logo image not loaded. Processing cannot continue with watermarking.")
        # If logo is critical, raise error. If optional, just log a warning.
        raise HTTPException(status_code=500, detail="Server configuration error: Logo not available.")
    elif not os.path.exists(LOGO_PATH):
        logger.warning(f"Logo file {LOGO_PATH} not found. Watermarking will be skipped.")
        # Allow processing without logo if it's not found.

    job_id = str(uuid.uuid4())
    
    try:
        async with httpx.AsyncClient() as client:
            img_response = await client.get(str(body.image))
            img_response.raise_for_status()
    except httpx.RequestError as e:
        logger.error(f"Error downloading image from {body.image}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to download image from URL: {e}")
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error downloading image {body.image}: {e.response.status_code} - {e.response.text}")
        raise HTTPException(status_code=e.response.status_code, detail=f"Error fetching image from URL: {e.response.reason_phrase}")

    image_content = await img_response.aread()
    content_type = img_response.headers.get("content-type", "").lower()

    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Invalid file type. URL does not point to an image.")

    extension = MIME_TO_EXT.get(content_type)
    if not extension:
        parsed_url_path = urllib.parse.urlparse(str(body.image)).path
        _, ext_from_url = os.path.splitext(parsed_url_path)
        if ext_from_url and ext_from_url.lower() in MIME_TO_EXT.values():
            extension = ext_from_url
        else:
            extension = ".png"
            logger.warning(f"Could not determine extension for {body.image}. Defaulting to '.png'.")
    
    original_filename = f"{job_id}_original{extension}"
    original_file_path = os.path.join(UPLOADS_DIR, original_filename)

    try:
        async with aiofiles.open(original_file_path, 'wb') as out_file:
            await out_file.write(image_content)
        logger.info(f"📝 Original image saved: {original_file_path} from URL {body.image}")
    except Exception as e:
        logger.error(f"Error saving downloaded file {original_filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save downloaded file: {e}")

    await queue.put((job_id, original_file_path, body.model, body.post_process))
    
    results[job_id] = {
        "status": "queued",
        "original_path": original_file_path,
        "processed_path": None,
        "error_message": None,
    }

    public_url_base = get_proxy_url(request)
    processed_image_placeholder_url = f"{public_url_base}/images/{job_id}.webp"
    eta_seconds = (queue.qsize()) * ESTIMATED_TIME_PER_JOB 

    return {
        "status": "processing",
        "image_links": [processed_image_placeholder_url],
        "etc": eta_seconds
    }

@app.get("/status/{job_id}")
async def check_status(request: Request, job_id: str):
    job_info = results.get(job_id)
    if not job_info:
        raise HTTPException(status_code=404, detail="Job not found")
    
    response_data = {"job_id": job_id, "status": job_info["status"]}
    if job_info["status"] == "done":
        public_url_base = get_proxy_url(request)
        processed_filename = f"{job_id}.webp" 
        response_data["processed_image_url"] = f"{public_url_base}/images/{processed_filename}"
    elif job_info["status"] == "error":
        response_data["error_message"] = job_info["error_message"]
    return JSONResponse(content=response_data)


async def image_processing_worker(worker_id: int):
    logger.info(f"Worker {worker_id} started.")
    global prepared_logo_image

    while True:
        try:
            job_id, original_file_path, model_name, post_process_flag = await queue.get()
            logger.info(f"Worker {worker_id} picked up job {job_id}")
            results[job_id]["status"] = "processing"
            
            try:
                with open(original_file_path, 'rb') as i:
                    input_bytes = i.read()
                
                session = new_session(model_name)
                output_bytes = remove(
                    input_bytes,
                    session=session,
                    post_process_mask=post_process_flag
                )
                
                img_no_bg = Image.open(io.BytesIO(output_bytes)).convert("RGBA")

                original_width, original_height = img_no_bg.size
                ratio = min(TARGET_SIZE / original_width, TARGET_SIZE / original_height)
                new_width = int(original_width * ratio)
                new_height = int(original_height * ratio)
                
                img_resized = img_no_bg.resize((new_width, new_height), Image.Resampling.LANCZOS)
                square_canvas = Image.new("RGBA", (TARGET_SIZE, TARGET_SIZE), (0, 0, 0, 0))
                paste_x = (TARGET_SIZE - new_width) // 2
                paste_y = (TARGET_SIZE - new_height) // 2
                square_canvas.paste(img_resized, (paste_x, paste_y), img_resized)

                if prepared_logo_image: # Only try to paste if logo was loaded
                    logo_w, logo_h = prepared_logo_image.size
                    logo_pos_x = LOGO_MARGIN
                    logo_pos_y = TARGET_SIZE - logo_h - LOGO_MARGIN
                    square_canvas.paste(prepared_logo_image, (logo_pos_x, logo_pos_y), prepared_logo_image)
                # else:
                    # logger.info(f"Job {job_id}: Skipping watermark as logo is not available.")


                final_image = square_canvas
                processed_filename = f"{job_id}.webp"
                processed_file_path = os.path.join(PROCESSED_DIR, processed_filename)
                final_image.save(processed_file_path, 'WEBP', quality=90)

                results[job_id]["status"] = "done"
                results[job_id]["processed_path"] = processed_file_path
                logger.info(f"Worker {worker_id} finished job {job_id}. Processed image: {processed_file_path}")

            except Exception as e:
                logger.error(f"Worker {worker_id} error processing job {job_id}: {e}", exc_info=True)
                results[job_id]["status"] = "error"
                results[job_id]["error_message"] = str(e)
            
            queue.task_done()
        except asyncio.CancelledError:
            logger.info(f"Worker {worker_id} stopping.")
            break
        except Exception as e:
            logger.error(f"Critical error in worker {worker_id}: {e}", exc_info=True)
            if 'job_id' in locals() and job_id in results:
                 results[job_id]["status"] = "error"
                 results[job_id]["error_message"] = "Worker failed unexpectedly."
                 queue.task_done() 
            await asyncio.sleep(1)

@app.on_event("startup")
async def startup_event():
    global prepared_logo_image
    if os.path.exists(LOGO_PATH):
        try:
            logo = Image.open(LOGO_PATH).convert("RGBA")
            if logo.width > LOGO_MAX_WIDTH:
                l_ratio = LOGO_MAX_WIDTH / logo.width
                l_new_width = LOGO_MAX_WIDTH
                l_new_height = int(logo.height * l_ratio)
                logo = logo.resize((l_new_width, l_new_height), Image.Resampling.LANCZOS)
            prepared_logo_image = logo
            logger.info(f"Logo loaded and prepared from {LOGO_PATH}. Dimensions: {prepared_logo_image.size if prepared_logo_image else 'None'}")
        except Exception as e:
            logger.error(f"Failed to load or prepare logo from {LOGO_PATH}: {e}")
            prepared_logo_image = None # Ensure it's None on failure
    else:
        logger.warning(f"Logo file not found at {LOGO_PATH}. Watermarking will be skipped.")
        prepared_logo_image = None

    for i in range(MAX_CONCURRENT_TASKS):
        asyncio.create_task(image_processing_worker(worker_id=i+1))
    logger.info(f"{MAX_CONCURRENT_TASKS} worker(s) started.")

# Serve static files for processed and original images (needed for API to serve links)
app.mount("/images", StaticFiles(directory=PROCESSED_DIR), name="processed_images")
app.mount("/originals", StaticFiles(directory=UPLOADS_DIR), name="original_images")

# Basic root endpoint to indicate the server is running
@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html>
        <head>
            <title>Image Processing</title>
        </head>
        <body>
            <h1>Resale1 is in the House!</h1>
            <p>Use the /submit endpoint to process images.</p>
        </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
