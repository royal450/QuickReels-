from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp
import re
import os
import uuid
import asyncio
import time
from typing import Optional
from datetime import datetime, timedelta

app = FastAPI()

# CORS - Allow all origins for your website
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
DOWNLOAD_FOLDER = "/tmp/sark_downloads"  # Render's temp storage
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Store last access time for auto-wake tracking
last_access = datetime.now()
request_count = 0

class DownloadRequest(BaseModel):
    url: str

def detect_platform(url: str) -> str:
    if re.search(r'instagram\.com/(reel|p)/', url):
        return "instagram"
    elif re.search(r'(facebook\.com|fb\.watch|facebook\.com/share)', url):
        return "facebook"
    return "unknown"

@app.get("/")
async def root():
    """Health check endpoint - keeps Render awake"""
    global last_access, request_count
    last_access = datetime.now()
    request_count += 1
    return {
        "status": "active",
        "message": "SarkDownloader API is running",
        "requests_served": request_count,
        "last_access": last_access.isoformat(),
        "server_time": datetime.now().isoformat()
    }

@app.get("/ping")
async def ping():
    """Simple ping endpoint for keep-alive"""
    return {"pong": True, "timestamp": time.time()}

@app.post("/api/analyze")
async def analyze_video(request: DownloadRequest):
    """Analyze URL and get video info without downloading"""
    global last_access
    last_access = datetime.now()
    
    url = request.url.strip()
    platform = detect_platform(url)
    
    if platform == "unknown":
        raise HTTPException(status_code=400, detail="Only Instagram or Facebook links are supported")
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "success": True,
                "platform": platform,
                "title": info.get('title', 'Video')[:100],
                "thumbnail": info.get('thumbnail', ''),
                "duration": info.get('duration', 0),
                "uploader": info.get('uploader', 'Unknown'),
                "views": info.get('view_count', 0)
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")

@app.post("/api/download")
async def download_video(request: DownloadRequest):
    """Download video and return file info"""
    global last_access
    last_access = datetime.now()
    
    url = request.url.strip()
    platform = detect_platform(url)
    
    if platform == "unknown":
        raise HTTPException(status_code=400, detail="Invalid URL")
    
    # Generate unique filename
    unique_id = str(uuid.uuid4())[:8]
    timestamp = int(time.time())
    
    ydl_opts = {
        'outtmpl': os.path.join(DOWNLOAD_FOLDER, f'reel_{timestamp}_{unique_id}.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'format': 'best[ext=mp4]/best',
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'retries': 3,
        'fragment_retries': 3,
    }
    
    # Platform specific optimizations
    if platform == "instagram":
        ydl_opts['format'] = 'best[ext=mp4]'
    elif platform == "facebook":
        ydl_opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            # Fix extension if needed
            if not os.path.exists(filename):
                base = filename.rsplit('.', 1)[0]
                for ext in ['mp4', 'mkv', 'webm', 'mov']:
                    candidate = f"{base}.{ext}"
                    if os.path.exists(candidate):
                        filename = candidate
                        break
            
            file_id = os.path.basename(filename)
            file_size = os.path.getsize(filename) if os.path.exists(filename) else 0
            
            # Auto-delete after 30 minutes
            async def auto_delete():
                await asyncio.sleep(1800)  # 30 minutes
                if os.path.exists(filename):
                    os.remove(filename)
            
            asyncio.create_task(auto_delete())
            
            return {
                "success": True,
                "file_id": file_id,
                "filename": info.get('title', 'reel')[:50],
                "platform": platform,
                "file_size": file_size,
                "message": "Video ready for download"
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

@app.get("/api/file/{file_id}")
async def get_file(file_id: str):
    """Serve the downloaded file"""
    file_path = os.path.join(DOWNLOAD_FOLDER, file_id)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    
    return FileResponse(
        file_path,
        media_type='video/mp4',
        filename=file_id,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{file_id}",
            "Cache-Control": "no-cache"
        }
    )

@app.get("/api/stats")
async def get_stats():
    """Get API statistics"""
    # Clean up old files
    count = 0
    current_time = time.time()
    for file in os.listdir(DOWNLOAD_FOLDER):
        file_path = os.path.join(DOWNLOAD_FOLDER, file)
        if os.path.getmtime(file_path) < (current_time - 3600):  # 1 hour
            os.remove(file_path)
            count += 1
    
    return {
        "total_requests": request_count,
        "last_access": last_access.isoformat(),
        "files_cleaned": count,
        "active_files": len(os.listdir(DOWNLOAD_FOLDER)),
        "status": "healthy"
    }

@app.on_event("startup")
async def startup_event():
    """Startup cleanup"""
    print("🚀 SarkDownloader API Started on Render")
    print(f"📁 Download folder: {DOWNLOAD_FOLDER}")
    print("✅ Ready to accept requests")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
