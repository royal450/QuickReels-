from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import os
import uuid
import time
import asyncio
import re

app = FastAPI(title="SarkDownloader API", version="1.0.0")

# CORS - Sabhi domains allow
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Config
DOWNLOAD_FOLDER = "/tmp/sark_downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# Headers to avoid blocking
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
}

class DownloadRequest(BaseModel):
    url: str

def detect_platform(url: str) -> str:
    if 'instagram.com' in url:
        return 'instagram'
    elif 'facebook.com' in url or 'fb.watch' in url:
        return 'facebook'
    return 'unknown'

@app.get("/")
async def root():
    return {
        "status": "active",
        "message": "SarkDownloader API is running",
        "version": "1.0.0",
        "endpoints": ["/", "/api/analyze", "/api/download", "/api/file/{file_id}"]
    }

@app.get("/ping")
async def ping():
    return {"pong": True, "timestamp": time.time()}

@app.post("/api/analyze")
async def analyze_video(request: DownloadRequest):
    url = request.url.strip()
    platform = detect_platform(url)
    
    if platform == 'unknown':
        raise HTTPException(status_code=400, detail="Only Instagram or Facebook links are supported")
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'headers': HEADERS,
        'extract_flat': False,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "success": True,
                "title": info.get('title', 'Video')[:100],
                "thumbnail": info.get('thumbnail', ''),
                "duration": info.get('duration', 0),
                "uploader": info.get('uploader', 'Unknown'),
                "platform": platform,
                "views": info.get('view_count', 0)
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/download")
async def download_video(request: DownloadRequest):
    url = request.url.strip()
    unique_id = str(uuid.uuid4())[:6]
    timestamp = int(time.time())
    output_template = os.path.join(DOWNLOAD_FOLDER, f'reel_{timestamp}_{unique_id}.%(ext)s')
    
    ydl_opts = {
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'headers': HEADERS,
        'format': 'best[ext=mp4]/best',
        'retries': 3,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            if not os.path.exists(filename):
                base = filename.rsplit('.', 1)[0]
                for ext in ['mp4', 'mkv', 'webm']:
                    candidate = f"{base}.{ext}"
                    if os.path.exists(candidate):
                        filename = candidate
                        break
            
            file_id = os.path.basename(filename)
            file_size = os.path.getsize(filename)
            
            # Auto delete after 60 seconds
            async def auto_delete():
                await asyncio.sleep(60)
                if os.path.exists(filename):
                    os.remove(filename)
            
            asyncio.create_task(auto_delete())
            
            return {
                "success": True,
                "file_id": file_id,
                "title": info.get('title', 'reel')[:50],
                "file_size_mb": round(file_size / (1024 * 1024), 2),
                "duration": info.get('duration', 0)
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/file/{file_id}")
async def get_file(file_id: str):
    file_path = os.path.join(DOWNLOAD_FOLDER, file_id)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found or expired")
    return FileResponse(
        file_path,
        media_type='video/mp4',
        filename=file_id,
        headers={"Cache-Control": "no-cache"}
    )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
