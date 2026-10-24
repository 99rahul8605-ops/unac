
import os
import re
import uuid
import time
import asyncio
import threading
from pathlib import Path
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

app = FastAPI(title="Lecture Requests Download API")

ROOT = Path("/root/unac/api_downloads")
ROOT.mkdir(parents=True, exist_ok=True)

API_KEY = os.environ.get("LECTURE_API_KEY", "")
jobs = {}
jobs_lock = threading.Lock()
download_lock = threading.Lock()

CHUNK = 512 * 1024


class Resource(BaseModel):
    kind: str
    url: str


class DownloadRequest(BaseModel):
    lecture: str
    watch_url: str = ""
    files: list[Resource] = Field(default_factory=list)


def safe_name(value):
    value = re.sub(r"[^a-zA-Z0-9._ -]", "_", value)
    return value.strip(" ._")[:90] or "lecture"


def check_key(key):
    if not API_KEY:
        raise HTTPException(
            503,
            "Set LECTURE_API_KEY before starting API"
        )
    if key != API_KEY:
        raise HTTPException(401, "Invalid API key")


def change(job_id, **values):
    with jobs_lock:
        jobs[job_id].update(values)


def filename_for(kind, url):
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()

    valid = {
        ".mp4", ".m4v", ".webm", ".mp3",
        ".json", ".pdf", ".png", ".jpg",
        ".jpeg", ".webp", ".zip", ".txt"
    }

    if suffix not in valid:
        suffix = ".bin"

    return safe_name(kind) + suffix


def download_one(session, url, destination, job_id):
    parsed = urlparse(url)

    if parsed.scheme not in ("https", "http"):
        raise ValueError("Unsupported URL scheme")

    partial = destination.with_name(
        destination.name + ".part"
    )

    existing = partial.stat().st_size if partial.exists() else 0

    headers = {
        "User-Agent": "LectureDownloadAPI/1.0",
        "Accept": "*/*",
    }

    if existing:
        headers["Range"] = f"bytes={existing}-"

    with session.get(
        url,
        headers=headers,
        stream=True,
        timeout=(20, 90),
        allow_redirects=True
    ) as response:

        if response.status_code == 416 and partial.exists():
            partial.replace(destination)
            return destination

        response.raise_for_status()

        resumed = (
            existing > 0
            and response.status_code == 206
            and response.headers.get(
                "Content-Range", ""
            ).startswith(f"bytes {existing}-")
        )

        # Server ignored Range: restart from zero.
        if not resumed:
            existing = 0

        mode = "ab" if resumed else "wb"

        length = int(
            response.headers.get("Content-Length", "0") or "0"
        )

        total = existing + length if length else 0
        received = existing
        last_update = 0.0

        with open(partial, mode) as output:
            for chunk in response.iter_content(CHUNK):
                if not chunk:
                    continue

                output.write(chunk)
                received += len(chunk)

                now = time.monotonic()

                if now - last_update >= 1:
                    percent = (
                        round(received * 100 / total, 1)
                        if total else None
                    )

                    change(
                        job_id,
                        downloaded_bytes=received,
                        total_bytes=total,
                        percent=percent
                    )

                    last_update = now

        if total and received != total:
            raise RuntimeError(
                f"Incomplete download: {received}/{total}"
            )

        partial.replace(destination)

    return destination


def worker(job_id, payload):
    with download_lock:
        change(job_id, status="running")

        folder = ROOT / job_id
        folder.mkdir(parents=True, exist_ok=True)

        result = []
        session = requests.Session()
        session.trust_env = False

        try:
            for index, resource in enumerate(payload.files, 1):
                kind = safe_name(resource.kind)
                file_path = folder / filename_for(
                    f"{index}_{kind}",
                    resource.url
                )

                change(
                    job_id,
                    current_resource=kind,
                    percent=0,
                    downloaded_bytes=0,
                    total_bytes=0
                )

                try:
                    saved = download_one(
                        session,
                        resource.url,
                        file_path,
                        job_id
                    )

                    result.append({
                        "kind": kind,
                        "status": "downloaded",
                        "path": str(saved),
                        "bytes": saved.stat().st_size
                    })

                except Exception as error:
                    result.append({
                        "kind": kind,
                        "status": "failed",
                        "error": str(error)[:250]
                    })

            success = sum(
                x["status"] == "downloaded"
                for x in result
            )

            change(
                job_id,
                status=(
                    "completed" if success == len(result)
                    else "completed_with_errors"
                ),
                results=result,
                current_resource="",
                percent=100 if success == len(result) else None
            )

        except Exception as error:
            change(
                job_id,
                status="failed",
                error=str(error)[:300]
            )

        finally:
            session.close()


@app.get("/health")
def health():
    return {
        "ok": True,
        "active_download_limit": 1,
        "mode": "requests-streaming"
    }


@app.post("/download")
async def create_download(
    payload: DownloadRequest,
    x_api_key: str = Header(default="")
):
    check_key(x_api_key)

    if not payload.files:
        raise HTTPException(
            422,
            "No direct file URLs provided. "
            "A watch page URL must first be resolved "
            "into video, board, slides or PDF URLs."
        )

    if len(payload.files) > 30:
        raise HTTPException(
            422,
            "Maximum 30 resources per request"
        )

    for resource in payload.files:
        parsed = urlparse(resource.url)
        if parsed.scheme not in ("http", "https"):
            raise HTTPException(
                422,
                "Every file needs a direct HTTP(S) URL"
            )

    job_id = uuid.uuid4().hex[:12]

    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "lecture": payload.lecture,
            "watch_url": payload.watch_url,
            "status": "queued",
            "current_resource": "",
            "percent": 0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "results": []
        }

    asyncio.create_task(
        asyncio.to_thread(worker, job_id, payload)
    )

    return {
        "job_id": job_id,
        "status": "queued"
    }


@app.get("/status/{job_id}")
def status(
    job_id: str,
    x_api_key: str = Header(default="")
):
    check_key(x_api_key)

    with jobs_lock:
        if job_id not in jobs:
            raise HTTPException(404, "Job not found")
        return dict(jobs[job_id])


@app.get("/jobs")
def all_jobs(x_api_key: str = Header(default="")):
    check_key(x_api_key)

    with jobs_lock:
        return list(jobs.values())
