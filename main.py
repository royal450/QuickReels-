from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import os
import uuid
import time
import asyncio
from typing import Dict, Optional
from datetime import datetime
from collections import deque
import threading

# ============================================
# CONFIGURATION
# ============================================
DOWNLOAD_FOLDER = "/tmp/sark_downloads"
MAX_CONCURRENT = 3  # Render free tier limit
DOWNLOAD_EXPIRY = 60  # 60 seconds
RATE_LIMIT = 20  # Requests per minute per IP
QUEUE_SIZE = 50  # Max queue size

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# ============================================
# IN-MEMORY QUEUE (No Redis)
# ============================================
class MemoryQueue:
    def __init__(self):
        self.queue = deque()
        self.processing = {}
        self.tasks = {}
        self.task_counter = 0
    
    async def add_task(self, url: str, session_id: str) -> str:
        """Add task to queue"""
        self.task_counter += 1
        task_id = f"task_{self.task_counter}_{uuid.uuid4().hex[:6]}"
        
        task_data = {
            "task_id": task_id,
            "url": url,
            "session_id": session_id,
            "status": "pending",
            "progress": 0,
            "created_at": time.time(),
            "queue_position": len(self.queue) + 1
        }
        
        self.tasks[task_id] = task_data
        self.queue.append(task_id)
        
        return task_id
    
    async def get_next_task(self) -> Optional[str]:
        """Get next task from queue"""
        if self.queue and len(self.processing) < MAX_CONCURRENT:
            task_id = self.queue.popleft()
            self.processing[task_id] = self.tasks[task_id]
            self.tasks[task_id]["status"] = "processing"
            self.tasks[task_id]["started_at"] = time.time()
            return task_id
        return None
    
    async def complete_task(self, task_id: str, result: Dict):
        """Mark task as completed"""
        if task_id in self.tasks:
            self.tasks[task_id]["status"] = "completed"
            self.tasks[task_id]["result"] = result
            self.tasks[task_id]["completed_at"] = time.time()
        
        if task_id in self.processing:
            del self.processing[task_id]
    
    async def fail_task(self, task_id: str, error: str):
        """Mark task as failed"""
        if task_id in self.tasks:
            self.tasks[task_id]["status"] = "failed"
            self.tasks[task_id]["error"] = error
        
        if task_id in self.processing:
            del self.processing[task_id]
    
    async def get_task_status(self, task_id: str) -> Dict:
        """Get task status"""
        task = self.tasks.get(task_id)
        if not task:
            return {"error": "Task not found"}
        
        # Calculate queue position
        queue_pos = 0
        if task["status"] == "pending":
            try:
                queue_pos = list(self.queue).index(task_id) + 1
            except:
                queue_pos = 0
        
        return {
            "task_id": task_id,
            "status": task["status"],
            "progress": task.get("progress", 0),
            "queue_position": queue_pos,
            "result": task.get("result"),
            "error": task.get("error")
        }
    
    async def get_stats(self) -> Dict:
        """Get queue statistics"""
        return {
            "queue_length": len(self.queue),
            "processing": len(self.processing),
            "total_tasks": len(self.tasks),
            "max_concurrent": MAX_CONCURRENT
        }

# Global queue instance
queue = MemoryQueue()

# ============================================
# FASTAPI APP
# ============================================
app = FastAPI(title="SarkDownloader Free", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ============================================
# MODELS
# ============================================
class DownloadRequest(BaseModel):
    url: str
    session_id: Optional[str] = None

# ============================================
# RATE LIMITING (In-Memory)
# ============================================
rate_limit_store = {}

async def check_rate_limit(ip: str) -> bool:
    """Simple in-memory rate limiting"""
    now = time.time()
    minute_ago = now - 60
    
    if ip not in rate_limit_store:
        rate_limit_store[ip] = []
    
    # Clean old requests
    rate_limit_store[ip] = [t for t in rate_limit_store[ip] if t > minute_ago]
    
    if len(rate_limit_store[ip]) >= RATE_LIMIT:
        return False
    
    rate_limit_store[ip].append(now)
    return True

# ============================================
# VIDEO DOWNLOAD FUNCTION
# ============================================
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
}

def get_ydl_opts(output_path):
    return {
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'headers': HEADERS,
        'retries': 2,
        'fragment_retries': 2,
        'format': 'best[ext=mp4]/best',
        'http_headers': HEADERS,
        'sleep_interval': 0.5,
    }

async def download_video_async(url: str, task_id: str) -> Dict:
    """Download video and return file info"""
    unique_id = str(uuid.uuid4())[:6]
    timestamp = int(time.time())
    output_template = os.path.join(DOWNLOAD_FOLDER, f'v_{timestamp}_{unique_id}.%(ext)s')
    
    def sync_download():
        with yt_dlp.YoutubeDL(get_ydl_opts(output_template)) as ydl:
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
            
            return {
                "file_id": file_id,
                "filename": info.get('title', 'reel')[:50],
                "file_size_mb": round(file_size / (1024 * 1024), 2),
                "duration": info.get('duration', 0),
                "thumbnail": info.get('thumbnail', ''),
                "title": info.get('title', 'Video')[:100]
            }
    
    result = await asyncio.get_event_loop().run_in_executor(None, sync_download)
    
    # Auto-delete after 60 seconds
    async def auto_delete():
        await asyncio.sleep(DOWNLOAD_EXPIRY)
        file_path = os.path.join(DOWNLOAD_FOLDER, result["file_id"])
        if os.path.exists(file_path):
            os.remove(file_path)
    
    asyncio.create_task(auto_delete())
    
    return result

# ============================================
# BACKGROUND WORKER
# ============================================
async def background_worker(worker_id: int):
    """Background worker that processes tasks from queue"""
    print(f"✅ Worker {worker_id} started")
    
    while True:
        try:
            # Get next task from queue
            task_id = await queue.get_next_task()
            
            if task_id:
                task = queue.tasks.get(task_id)
                if task:
                    url = task["url"]
                    
                    try:
                        # Update progress
                        queue.tasks[task_id]["progress"] = 30
                        
                        # Download video
                        result = await download_video_async(url, task_id)
                        
                        # Mark as completed
                        await queue.complete_task(task_id, result)
                        print(f"✅ Worker {worker_id} completed: {task_id}")
                        
                    except Exception as e:
                        # Mark as failed
                        await queue.fail_task(task_id, str(e))
                        print(f"❌ Worker {worker_id} failed: {task_id} - {e}")
            
            await asyncio.sleep(0.5)
            
        except Exception as e:
            print(f"Worker {worker_id} error: {e}")
            await asyncio.sleep(1)

# ============================================
# API ENDPOINTS
# ============================================
@app.on_event("startup")
async def startup_event():
    """Start background workers on startup"""
    print("=" * 50)
    print("🚀 SarkDownloader Free Edition Started")
    print(f"📁 Download folder: {DOWNLOAD_FOLDER}")
    print(f"⚡ Max concurrent: {MAX_CONCURRENT}")
    print(f"⏱️ File expiry: {DOWNLOAD_EXPIRY} seconds")
    print("=" * 50)
    
    # Start 2 background workers
    for i in range(2):
        asyncio.create_task(background_worker(i + 1))

@app.get("/")
async def root():
    return {
        "status": "active",
        "service": "SarkDownloader Free",
        "version": "3.0.0",
        "timestamp": datetime.now().isoformat(),
        "stats": await queue.get_stats()
    }

@app.post("/api/download/start")
async def start_download(request: DownloadRequest, req: Request):
    """Start download - returns task ID for polling"""
    # Rate limiting
    client_ip = req.client.host
    if not await check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429, 
            detail=f"Rate limit exceeded. Max {RATE_LIMIT} requests per minute."
        )
    
    # Validate URL
    url = request.url.strip()
    if not ('instagram.com' in url or 'facebook.com' in url):
        raise HTTPException(status_code=400, detail="Only Instagram or Facebook links")
    
    # Check queue size
    stats = await queue.get_stats()
    if stats["queue_length"] >= QUEUE_SIZE:
        raise HTTPException(status_code=429, detail="Server busy. Please try again in a few seconds.")
    
    # Add to queue
    session_id = request.session_id or str(uuid.uuid4())
    task_id = await queue.add_task(url, session_id)
    
    return {
        "success": True,
        "task_id": task_id,
        "session_id": session_id,
        "queue_position": stats["queue_length"] + 1,
        "estimated_wait": (stats["queue_length"] + 1) * 8,  # ~8 seconds per video
        "status_url": f"/api/download/status/{task_id}"
    }

@app.get("/api/download/status/{task_id}")
async def get_status(task_id: str):
    """Get download status"""
    status = await queue.get_task_status(task_id)
    
    if "error" in status:
        raise HTTPException(status_code=404, detail="Task not found")
    
    return status

@app.get("/api/download/file/{file_id}")
async def get_file(file_id: str):
    """Download the actual file"""
    file_path = os.path.join(DOWNLOAD_FOLDER, file_id)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File expired or not found")
    
    return FileResponse(
        file_path,
        media_type='video/mp4',
        filename=file_id,
        headers={
            "Cache-Control": "no-cache",
            "Content-Disposition": f"attachment; filename={file_id}"
        }
    )

@app.get("/api/stats")
async def get_stats():
    """Get system statistics"""
    stats = await queue.get_stats()
    
    # Clean old rate limit entries
    now = time.time()
    for ip in list(rate_limit_store.keys()):
        rate_limit_store[ip] = [t for t in rate_limit_store[ip] if t > now - 60]
        if not rate_limit_store[ip]:
            del rate_limit_store[ip]
    
    return {
        **stats,
        "active_ips": len(rate_limit_store),
        "file_expiry_seconds": DOWNLOAD_EXPIRY,
        "rate_limit_per_minute": RATE_LIMIT
    }

@app.get("/api/health")
async def health():
    """Quick health check"""
    stats = await queue.get_stats()
    return {
        "status": "healthy",
        "queue_length": stats["queue_length"],
        "processing": stats["processing"]
    }
