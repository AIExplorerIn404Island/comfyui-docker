import uuid
import asyncio
import re
import time
import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="Remote Executor", version="2.0.0")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = Path("/").resolve()
COMFY_OUTPUT_DIR = Path("/workspace/ComfyUI/output").resolve()
JOB_TTL_SECONDS = 3600  # auto-purge finished jobs after 1 hour

# ---------------------------------------------------------------------------
# In-memory job store
# ---------------------------------------------------------------------------
jobs: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class CommandRequest(BaseModel):
    command: str
    cwd: Optional[str] = None
    timeout: int = 1200  # 20 minutes
    env: Optional[dict[str, str]] = None  # extra env vars


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
BLOCKED_PATTERNS = [
    re.compile(r"^(sudo\s+)?rm\s+(-[a-z]*f[a-z]*\s+)?/\s*$", re.I),  # rm -rf /
    re.compile(r"^(sudo\s+)?ls\s+(-[a-z]*r[a-z]*)", re.I),            # ls -R
]


def is_blocked_command(command: str) -> Optional[str]:
    cmd = re.sub(r"\s+", " ", command.strip())
    for pattern in BLOCKED_PATTERNS:
        if pattern.search(cmd):
            return f"Blocked: command matches dangerous pattern ({pattern.pattern})"
    return None


def safe_path(path: str) -> Path:
    resolved = Path(path).resolve()
    return resolved


def purge_old_jobs():
    now = time.time()
    expired = [
        jid for jid, job in jobs.items()
        if job["status"] in ("finished", "error", "cancelled", "timeout")
        and now - job.get("finished_at", now) > JOB_TTL_SECONDS
    ]
    for jid in expired:
        del jobs[jid]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "uptime": time.time(), "jobs_count": len(jobs)}


@app.post("/exec")
async def exec_command(req: CommandRequest):
    purge_old_jobs()

    blocked = is_blocked_command(req.command)
    if blocked:
        raise HTTPException(status_code=400, detail=blocked)

    job_id = str(uuid.uuid4())
    output_lines: list[str] = []

    jobs[job_id] = {
        "status": "running",
        "command": req.command,
        "cwd": req.cwd,
        "created_at": time.time(),
        "output_lines": output_lines,
        "process": None,
    }

    async def run():
        proc = None
        try:
            env = os.environ.copy()
            if req.env:
                env.update(req.env)

            cwd = safe_path(req.cwd) if req.cwd else BASE_DIR
            proc = await asyncio.create_subprocess_shell(
                req.command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            jobs[job_id]["process"] = proc

            async def read_stream():
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    output_lines.append(line.decode(errors="replace"))

            try:
                await asyncio.wait_for(read_stream(), timeout=req.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                jobs[job_id].update({
                    "status": "timeout",
                    "stdout": "".join(output_lines),
                    "stderr": "",
                    "returncode": -1,
                    "finished_at": time.time(),
                })
                return

            await proc.wait()
            jobs[job_id].update({
                "status": "finished",
                "stdout": "".join(output_lines),
                "stderr": "",
                "returncode": proc.returncode,
                "finished_at": time.time(),
            })
        except Exception as e:
            jobs[job_id].update({
                "status": "error",
                "error": str(e),
                "finished_at": time.time(),
            })

    asyncio.create_task(run())
    return {"job_id": job_id}


@app.get("/exec/stream/{job_id}")
async def stream_output(job_id: str):
    """SSE stream of job output — connect while job is running to get live output."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        idx = 0
        while True:
            job = jobs.get(job_id)
            if not job:
                break
            lines = job.get("output_lines", [])
            while idx < len(lines):
                yield f"data: {lines[idx]}\n"
                idx += 1
            if job["status"] != "running":
                yield f"event: done\ndata: {job['status']}\n\n"
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/status/{job_id}")
async def status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    return {
        "job_id": job_id,
        "status": job["status"],
        "command": job.get("command", ""),
    }


@app.get("/result/{job_id}")
async def result(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    return {
        "job_id": job_id,
        "status": job["status"],
        "stdout": job.get("stdout", ""),
        "stderr": job.get("stderr", ""),
        "returncode": job.get("returncode"),
        "error": job.get("error"),
    }


@app.post("/cancel/{job_id}")
async def cancel_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[job_id]
    if job["status"] != "running":
        return {"message": f"Job is already {job['status']}"}
    proc = job.get("process")
    if proc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    job.update({"status": "cancelled", "finished_at": time.time()})
    return {"message": "Job cancelled"}


@app.get("/jobs")
async def list_jobs():
    purge_old_jobs()
    return {
        "jobs": [
            {
                "job_id": jid,
                "status": job["status"],
                "command": job.get("command", ""),
                "created_at": job.get("created_at"),
            }
            for jid, job in jobs.items()
        ]
    }


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

@app.get("/browse")
async def browse(path: str = Query(default="/workspace")):
    """List contents of a directory."""
    target = Path(path).resolve()
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    entries = []
    try:
        for item in sorted(target.iterdir()):
            try:
                stat = item.stat()
                entries.append({
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                    "size": stat.st_size if item.is_file() else None,
                })
            except PermissionError:
                entries.append({"name": item.name, "type": "unknown", "size": None})
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    return {"path": str(target), "entries": entries}


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    dest_dir: str = Form(default="/workspace"),
):
    """Upload a file to the specified directory."""
    dest = Path(dest_dir).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    file_path = dest / file.filename

    with open(file_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    return {
        "message": "File uploaded",
        "path": str(file_path),
        "size": file_path.stat().st_size,
    }


@app.get("/files")
async def list_generated_files():
    """List ComfyUI output files."""
    if not COMFY_OUTPUT_DIR.exists():
        return {"files": []}
    files = []
    for f in COMFY_OUTPUT_DIR.iterdir():
        if f.is_file():
            files.append({"name": f.name, "size": f.stat().st_size})
    return {"files": files}


@app.get("/files/{filename}")
async def get_generated_file(filename: str):
    """Download a ComfyUI output file."""
    file_path = (COMFY_OUTPUT_DIR / filename).resolve()
    if not str(file_path).startswith(str(COMFY_OUTPUT_DIR)):
        raise HTTPException(status_code=403, detail="Invalid file path")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type="application/octet-stream",
    )


@app.get("/disk")
async def disk_usage():
    """Get disk usage info for /workspace."""
    usage = shutil.disk_usage("/workspace")
    return {
        "total_gb": round(usage.total / (1024**3), 2),
        "used_gb": round(usage.used / (1024**3), 2),
        "free_gb": round(usage.free / (1024**3), 2),
    }
