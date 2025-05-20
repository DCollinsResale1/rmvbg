import asyncio
import uuid
import io
import os
import aiofiles
import logging
import httpx
import urllib.parse

# --- CREATE DIRECTORIES AT THE VERY TOP ---
UPLOADS_DIR_STATIC = "/workspace/uploads"
PROCESSED_DIR_STATIC = "/workspace/processed"
BASE_DIR_STATIC = "/workspace/rmvbg"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    os.makedirs(UPLOADS_DIR_STATIC, exist_ok=True)
    os.makedirs(PROCESSED_DIR_STATIC, exist_ok=True)
    os.makedirs(BASE_DIR_STATIC, exist_ok=True)
    logger.info(f"Ensured uploads directory exists: {UPLOADS_DIR_STATIC}")
    logger.info(f"Ensured processed directory exists: {PROCESSED_DIR_STATIC}")
    logger.info(f"Ensured base directory exists: {BASE_DIR_STATIC}")
except OSError as e:
    logger.error(f"CRITICAL: Error creating essential directories: {e}", exc_info=True)
    # import sys; sys.exit(f"CRITICAL: Could not create essential directories: {e}")

from fastapi import FastAPI, Request, HTTPException, Form, UploadFile, File # Added Form, UploadFile, File
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from rembg import remove, new_session
from PIL import Image

app = FastAPI()

origins = [
    "null",  # Allow requests from file:/// origins (when you open HTML locally)
    "http://localhost", # If you ever serve your HTML from a local dev server
    "http://127.0.0.1"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # List of origins that are allowed to make cross-origin requests
    allow_credentials=True, # Allow cookies to be included in requests (not strictly needed for this app)
    allow_methods=["*"],    # Allow all methods (GET, POST, etc.)
    allow_headers=["*"],    # Allow all headers
)

# --- Configuration Constants ---
MAX_CONCURRENT_TASKS = 8
MAX_QUEUE_SIZE = 5000
ESTIMATED_TIME_PER_JOB = 13
TARGET_SIZE = 1024
HTTP_CLIENT_TIMEOUT = 30.0

ENABLE_LOGO_WATERMARK = True
LOGO_MAX_WIDTH = 150
LOGO_MARGIN = 20
LOGO_FILENAME = "logo.png"

# --- Directory and File Paths ---
BASE_DIR = BASE_DIR_STATIC
UPLOADS_DIR = UPLOADS_DIR_STATIC
PROCESSED_DIR = PROCESSED_DIR_STATIC
LOGO_PATH = os.path.join(BASE_DIR, LOGO_FILENAME) if ENABLE_LOGO_WATERMARK else ""

# --- Global State ---
prepared_logo_image = None
queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
results = {}
EXPECTED_API_KEY = "secretApiKey"

MIME_TO_EXT = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
    'image/bmp': '.bmp',
    'image/tiff': '.tiff'
}

# --- Pydantic Models ---
class SubmitJsonBody(BaseModel): # Renamed for clarity
    image: HttpUrl
    key: str
    model: str = "u2net"
    post_process: bool = False
    steps: int = 20
    samples: int = 1
    resolution: str = "1024x1024"

# --- Helper Functions ---
def get_proxy_url(request: Request):
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme}://{host}"

# --- API Endpoints ---

# Endpoint 1: Submit with JSON body (image URL)
@app.post("/submit")
async def submit_json_image_for_processing( # Renamed for clarity
    request: Request,
    body: SubmitJsonBody
):
    if body.key != EXPECTED_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if ENABLE_LOGO_WATERMARK and os.path.exists(LOGO_PATH) and not prepared_logo_image:
        logger.error("Logo watermarking enabled, logo file exists, but not loaded. Check startup.")
    
    job_id = str(uuid.uuid4())
    public_url_base = get_proxy_url(request)
    
    try:
        # For JSON/URL submissions, queue the URL directly
        queue.put_nowait((job_id, str(body.image), body.model, body.post_process))
    except asyncio.QueueFull:
        logger.warning(f"Queue is full. Rejecting JSON request for image {body.image}.")
        raise HTTPException(status_code=503, detail=f"Server overloaded (queue full). Max: {MAX_QUEUE_SIZE}")
    
    status_check_url = f"{public_url_base}/status/{job_id}"
    results[job_id] = {
        "status": "queued", "input_image_url": str(body.image), "original_local_path": None,
        "processed_path": None, "error_message": None, "status_check_url": status_check_url
    }
    processed_image_placeholder_url = f"{public_url_base}/images/{job_id}.webp"
    eta_seconds = (queue.qsize()) * ESTIMATED_TIME_PER_JOB
    return {
        "status": "processing", "job_id": job_id, "image_links": [processed_image_placeholder_url],
        "eta": eta_seconds, "status_check_url": status_check_url
    }

# Endpoint 2: Submit with Form Data (file upload)
@app.post("/submit_form") # Kept distinct path
async def submit_form_image_for_processing( # Renamed for clarity
    request: Request,
    image_file: UploadFile = File(...),
    key: str = Form(...),
    model: str = Form("u2net"),
    post_process: bool = Form(False)
):
    if key != EXPECTED_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if ENABLE_LOGO_WATERMARK and os.path.exists(LOGO_PATH) and not prepared_logo_image:
        logger.error("Logo watermarking enabled, logo file exists, but not loaded. Check startup.")
    if not image_file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Invalid file type. Please upload an image.")

    job_id = str(uuid.uuid4())
    public_url_base = get_proxy_url(request)
    
    original_filename_from_upload = image_file.filename
    content_type_from_upload = image_file.content_type.lower()
    
    extension = MIME_TO_EXT.get(content_type_from_upload)
    if not extension:
        _, ext_from_filename = os.path.splitext(original_filename_from_upload)
        ext_from_filename_lower = ext_from_filename.lower()
        if ext_from_filename_lower in MIME_TO_EXT.values():
            extension = ext_from_filename_lower
        else:
            extension = ".png"
            logger.warning(f"Job {job_id} (form): Could not determine ext for {original_filename_from_upload} (Content-Type: {content_type_from_upload}). Defaulting to '{extension}'.")

    saved_original_filename = f"{job_id}_original{extension}"
    original_file_path = os.path.join(UPLOADS_DIR, saved_original_filename)

    try:
        async with aiofiles.open(original_file_path, 'wb') as out_file:
            file_content = await image_file.read()
            await out_file.write(file_content)
        logger.info(f"📝 (Form Upload) Original image saved: {original_file_path} for job {job_id}")
    except Exception as e:
        logger.error(f"Error saving uploaded file {saved_original_filename} for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {e}")
    finally:
        await image_file.close()

    file_uri_for_queue = f"file://{original_file_path}"
    try:
        queue.put_nowait((job_id, file_uri_for_queue, model, post_process))
    except asyncio.QueueFull:
        logger.warning(f"Queue is full. Rejecting form request for image {original_filename_from_upload} (job {job_id}).")
        if os.path.exists(original_file_path):
            try: os.remove(original_file_path)
            except OSError as e_clean: logger.error(f"Error cleaning {original_file_path} (queue full): {e_clean}")
        raise HTTPException(status_code=503, detail=f"Server overloaded (queue full). Max: {MAX_QUEUE_SIZE}")
    
    status_check_url = f"{public_url_base}/status/{job_id}"
    results[job_id] = {
        "status": "queued", "input_image_url": f"(form_upload: {original_filename_from_upload})",
        "original_local_path": original_file_path, "processed_path": None,
        "error_message": None, "status_check_url": status_check_url
    }
    processed_image_placeholder_url = f"{public_url_base}/images/{job_id}.webp"
    eta_seconds = (queue.qsize()) * ESTIMATED_TIME_PER_JOB
    return {
        "status": "processing", "job_id": job_id, "image_links": [processed_image_placeholder_url],
        "eta": eta_seconds, "status_check_url": status_check_url
    }


@app.get("/status/{job_id}")
async def check_status(request: Request, job_id: str):
    job_info = results.get(job_id)
    if not job_info:
        raise HTTPException(status_code=404, detail="Job not found")
    public_url_base = get_proxy_url(request)
    response_data = {
        "job_id": job_id, "status": job_info.get("status"),
        "input_image_url": job_info.get("input_image_url"), "status_check_url": job_info.get("status_check_url")
    }
    if job_info.get("original_local_path"):
        original_filename = os.path.basename(job_info["original_local_path"])
        response_data["downloaded_original_image_url"] = f"{public_url_base}/originals/{original_filename}"
    if job_info.get("status") == "done" and job_info.get("processed_path"):
        processed_filename = os.path.basename(job_info["processed_path"])
        response_data["processed_image_url"] = f"{public_url_base}/images/{processed_filename}"
    elif job_info.get("status") == "error":
        response_data["error_message"] = job_info.get("error_message")
    return JSONResponse(content=response_data)

# --- Background Worker ---
async def image_processing_worker(worker_id: int):
    logger.info(f"Worker {worker_id} started. Listening for jobs...")
    global prepared_logo_image

    while True:
        job_id, image_source_str, model_name, post_process_flag = await queue.get()
        logger.info(f"Worker {worker_id} picked up job {job_id} for source: {image_source_str}")
        if job_id not in results:
            logger.error(f"Worker {worker_id}: Job ID {job_id} from queue not found in results dict. Skipping.")
            queue.task_done()
            continue
        
        # This will hold the bytes of the image to be processed by rembg
        input_bytes_for_rembg: bytes = None
        # This will hold the path to the original file that rembg will process from (for logging/reference)
        # For http, it's a temporary download. For file://, it's the path from the form upload.
        path_of_source_for_rembg: str = None

        try:
            if image_source_str.startswith("file://"):
                results[job_id]["status"] = "processing_local_file"
                local_path_from_uri = image_source_str[len("file://"):]
                if not os.path.exists(local_path_from_uri):
                    raise FileNotFoundError(f"Local file for job {job_id} not found: {local_path_from_uri}")
                
                path_of_source_for_rembg = local_path_from_uri # This is already in UPLOADS_DIR
                async with aiofiles.open(path_of_source_for_rembg, 'rb') as f:
                    input_bytes_for_rembg = await f.read()
                logger.info(f"Worker {worker_id}: Reading local file {path_of_source_for_rembg} for job {job_id}")

            elif image_source_str.startswith(("http://", "https://")):
                results[job_id]["status"] = "downloading"
                async with httpx.AsyncClient(timeout=HTTP_CLIENT_TIMEOUT) as client:
                    img_response = await client.get(image_source_str)
                    img_response.raise_for_status()
                
                input_bytes_for_rembg = await img_response.aread()
                original_content_type_header = img_response.headers.get("content-type", "unknown")
                content_type = original_content_type_header.lower()
                logger.info(f"Job {job_id}: Received initial Content-Type='{original_content_type_header}' for URL {image_source_str}")

                if content_type == "application/octet-stream" or not content_type.startswith("image/"):
                    # ... (Content-Type inference logic) ...
                    file_ext_from_url = os.path.splitext(urllib.parse.urlparse(image_source_str).path)[1].lower()
                    potential_ct = None
                    if file_ext_from_url == ".webp": potential_ct = "image/webp"
                    elif file_ext_from_url == ".png": potential_ct = "image/png"
                    elif file_ext_from_url in [".jpg", ".jpeg"]: potential_ct = "image/jpeg"
                    elif file_ext_from_url == ".gif": potential_ct = "image/gif"
                    elif file_ext_from_url == ".bmp": potential_ct = "image/bmp"
                    elif file_ext_from_url in [".tif", ".tiff"]: potential_ct = "image/tiff"
                    if potential_ct: content_type = potential_ct
                
                if not content_type.startswith("image/"):
                    raise ValueError(f"Invalid final content type '{content_type}' from URL. Not an image.")
                
                extension = MIME_TO_EXT.get(content_type, ".bin") # Default to .bin if unknown
                # ... (logic to refine extension if content_type is generic) ...

                # Save the downloaded HTTP image to UPLOADS_DIR, this becomes the source for rembg
                temp_original_filename = f"{job_id}_original_downloaded{extension}"
                path_of_source_for_rembg = os.path.join(UPLOADS_DIR, temp_original_filename)
                results[job_id]["original_local_path"] = path_of_source_for_rembg

                async with aiofiles.open(path_of_source_for_rembg, 'wb') as out_file:
                    await out_file.write(input_bytes_for_rembg)
                logger.info(f"Worker {worker_id} saved downloaded original for job {job_id} to {path_of_source_for_rembg}")
            else:
                raise ValueError(f"Unsupported image source scheme for job {job_id}: {image_source_str}")

            # --- Common Processing Logic ---
            if input_bytes_for_rembg is None:
                raise ValueError(f"Image content for rembg is None for job {job_id}. This should not happen.")

            results[job_id]["status"] = "processing_rembg" # More specific status
            
            session = new_session(model_name)
            output_bytes = remove(input_bytes_for_rembg, session=session, post_process_mask=post_process_flag)
            
            results[job_id]["status"] = "processing_pil"
            img_no_bg = Image.open(io.BytesIO(output_bytes)).convert("RGBA")
            
            original_width, original_height = img_no_bg.size
            if original_width == 0 or original_height == 0:
                raise ValueError(f"Image dimensions zero after rembg for job {job_id}.")
            ratio = min(TARGET_SIZE / original_width, TARGET_SIZE / original_height)
            new_width, new_height = int(original_width * ratio), int(original_height * ratio)
            img_resized = img_no_bg.resize((new_width, new_height), Image.Resampling.LANCZOS)
            square_canvas = Image.new("RGBA", (TARGET_SIZE, TARGET_SIZE), (0, 0, 0, 0))
            paste_x, paste_y = (TARGET_SIZE - new_width) // 2, (TARGET_SIZE - new_height) // 2
            square_canvas.paste(img_resized, (paste_x, paste_y), img_resized)

            if ENABLE_LOGO_WATERMARK and prepared_logo_image:
                logo_w, logo_h = prepared_logo_image.size
                logo_pos_x, logo_pos_y = LOGO_MARGIN, TARGET_SIZE - logo_h - LOGO_MARGIN
                square_canvas.paste(prepared_logo_image, (logo_pos_x, logo_pos_y), prepared_logo_image)
            
            final_image = square_canvas
            processed_filename = f"{job_id}.webp"
            processed_file_path = os.path.join(PROCESSED_DIR, processed_filename)
            final_image.save(processed_file_path, 'WEBP', quality=90)

            results[job_id]["status"] = "done"
            results[job_id]["processed_path"] = processed_file_path
            logger.info(f"Worker {worker_id} finished job {job_id}. Processed: {processed_file_path}")

        except FileNotFoundError as e:
            logger.error(f"Worker {worker_id} FileNotFoundError for job {job_id}: {e}", exc_info=False) # Less verbose for FileNotFoundError
            results[job_id]["status"] = "error"; results[job_id]["error_message"] = f"File not found: {str(e)}"
        except httpx.HTTPStatusError as e:
            logger.error(f"Worker {worker_id} HTTP error for job {job_id}: {e.response.status_code} - {e.response.text}", exc_info=True)
            results[job_id]["status"] = "error"; results[job_id]["error_message"] = f"Download failed: HTTP {e.response.status_code} from {image_source_str}."
        except httpx.RequestError as e:
            logger.error(f"Worker {worker_id} Network error for job {job_id}: {e}", exc_info=True)
            results[job_id]["status"] = "error"; results[job_id]["error_message"] = f"Network error downloading from {image_source_str}: {type(e).__name__}."
        except (ValueError, IOError, OSError) as e:
            logger.error(f"Worker {worker_id} data/file error for job {job_id}: {e}", exc_info=True)
            results[job_id]["status"] = "error"; results[job_id]["error_message"] = f"Data or file error: {str(e)}"
        except Exception as e:
            logger.error(f"Worker {worker_id} critical error for job {job_id}: {e}", exc_info=True)
            results[job_id]["status"] = "error"; results[job_id]["error_message"] = f"Unexpected processing error: {str(e)}"
        finally:
            queue.task_done()

# --- Application Startup Logic ---
@app.on_event("startup")
async def startup_event():
    global prepared_logo_image
    logger.info("Application startup event running...")
    if not os.path.isdir(UPLOADS_DIR): os.makedirs(UPLOADS_DIR, exist_ok=True)
    if not os.path.isdir(PROCESSED_DIR): os.makedirs(PROCESSED_DIR, exist_ok=True)

    if ENABLE_LOGO_WATERMARK:
        logger.info(f"Logo watermarking ENABLED. Attempting load from: {LOGO_PATH}")
        if os.path.exists(LOGO_PATH):
            try:
                logo = Image.open(LOGO_PATH).convert("RGBA")
                if logo.width > LOGO_MAX_WIDTH:
                    l_ratio = LOGO_MAX_WIDTH / logo.width
                    l_new_width, l_new_height = LOGO_MAX_WIDTH, int(logo.height * l_ratio)
                    logo = logo.resize((l_new_width, l_new_height), Image.Resampling.LANCZOS)
                prepared_logo_image = logo
                logger.info(f"Logo loaded. Dimensions: {prepared_logo_image.size if prepared_logo_image else 'None'}")
            except Exception as e:
                logger.error(f"Failed to load logo: {e}", exc_info=True); prepared_logo_image = None
        else:
            logger.warning(f"Logo file not found at {LOGO_PATH}."); prepared_logo_image = None
    else:
        logger.info("Logo watermarking DISABLED."); prepared_logo_image = None
    
    for i in range(MAX_CONCURRENT_TASKS):
        asyncio.create_task(image_processing_worker(worker_id=i+1))
    logger.info(f"{MAX_CONCURRENT_TASKS} workers started. Queue max size: {MAX_QUEUE_SIZE}.")

# --- Static File Serving ---
app.mount("/images", StaticFiles(directory=PROCESSED_DIR), name="processed_images")
app.mount("/originals", StaticFiles(directory=UPLOADS_DIR), name="original_images")

# --- Root Endpoint ---
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    logo_status = "Enabled" if ENABLE_LOGO_WATERMARK else "Disabled"
    if ENABLE_LOGO_WATERMARK and prepared_logo_image: logo_status += f" (Loaded, {prepared_logo_image.width}x{prepared_logo_image.height})"
    elif ENABLE_LOGO_WATERMARK and not prepared_logo_image: logo_status += " (Enabled but not loaded/found)"
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Image API</title><style>body{{font-family:sans-serif;margin:20px}}</style></head>
    <body><h1>Image Processing API Running</h1><p>Settings:<ul><li>Workers: {MAX_CONCURRENT_TASKS}</li><li>Queue: {MAX_QUEUE_SIZE}</li><li>Logo: {logo_status}</li></ul></p></body></html>"""

# --- Main Execution ---
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Uvicorn server for local development...")
    uvicorn.run(app, host="0.0.0.0", port=7000) # Changed port to 7000 as per your uvicorn command
