import hashlib
import hmac
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from telethon import TelegramClient

ROOT = Path("/root/unac")
load_dotenv(ROOT / ".env")

BATCH_NAME = "Yoddha Regular Batch for CA Foundation Jan'27/May'27"
TOPIC_ID = 23

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["STORAGE_GROUP_ID"])
SECRET = os.environ.get("PLAYBACK_SECRET", "").encode()

if len(SECRET) < 32:
    raise RuntimeError("PLAYBACK_SECRET must have at least 32 characters")

RECEIPTS = ROOT / ".state" / "txt_uploads"
SESSION = ROOT / ".state" / "foundation_stream"
TG = TelegramClient(str(SESSION), API_ID, API_HASH)

# Each record: lecture_id -> Telegram video message ID
video_index = {}
last_refresh = 0


def make_id(value):
    raw = str(value)
    name = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
    return (name[:65].strip("-") or "item") + "-" + digest


def find_source():
    paths = list((ROOT / "exports").rglob("*.txt"))
    paths += list(RECEIPTS.glob("input_*.txt"))

    matches = []

    for path in set(paths):
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue

        batch = re.search(
            r"(?im)^BATCH:\s*(.+?)\s*$",
            content
        )
        if not batch:
            continue

        if batch.group(1).strip() != BATCH_NAME:
            continue

        count = len(re.findall(
            r"(?im)^WATCH_URL:\s*https?://",
            content
        ))
        if count:
            matches.append((count, path, content))

    if not matches:
        return None

    return max(matches, key=lambda item: item[0])


def refresh_index():
    global video_index, last_refresh

    source = find_source()
    if not source:
        video_index = {}
        last_refresh = time.time()
        return

    _, path, content = source
    result = {}

    current_name = ""
    current_subject = ""
    current_number = ""

    for raw in content.splitlines():
        line = raw.strip()
        upper = line.upper()

        if upper.startswith("SUBJECT:"):
            current_subject = line.partition(":")[2].strip()

        elif upper.startswith("CLASS:"):
            current_number = line.partition(":")[2].strip()

        elif upper.startswith("LECTURE:"):
            current_name = line.partition(":")[2].strip()

        elif upper.startswith("WATCH_URL:"):
            url = line.partition(":")[2].strip()

            if not (
                url.startswith("https://")
                and current_name
                and current_subject
            ):
                continue

            digest = hashlib.sha256(
                f"{TOPIC_ID}|{url}".encode()
            ).hexdigest()

            receipt_path = RECEIPTS / f"{digest}.json"

            try:
                receipt = json.loads(receipt_path.read_text())
            except (OSError, ValueError):
                continue

            receipt_name = str(receipt.get("name", "")).strip()

            # Never map conflicting lecture names.
            if receipt_name and (
                receipt_name.casefold() != current_name.casefold()
            ):
                continue

            uploaded = receipt.get("uploaded") or {}
            video = uploaded.get("video") or {}

            try:
                message_id = int(video.get("message_id"))
            except (TypeError, ValueError):
                continue

            if message_id <= 0:
                continue

            lecture_id = make_id(url)

            result[lecture_id] = {
                "message_id": message_id,
                "name": current_name,
                "subject": current_subject,
                "class_number": current_number,
            }

    video_index = result
    last_refresh = time.time()

    print(
        f"Foundation source: {path.name} | "
        f"Mapped videos: {len(video_index)}",
        flush=True
    )


def verify_signature(lecture_id, expiry, signature):
    if not re.fullmatch(r"[a-z0-9-]{1,100}", lecture_id):
        return False

    try:
        expiry_int = int(expiry)
    except (TypeError, ValueError):
        return False

    if expiry_int <= time.time():
        return False

    # Reject unusually long-lived URLs.
    if expiry_int > time.time() + 86400:
        return False

    payload = f"{lecture_id}.{expiry_int}".encode()
    expected = hmac.new(
        SECRET, payload, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, str(signature))


def parse_range(header, size):
    if not header:
        return 0, size - 1, False

    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match:
        raise HTTPException(
            416,
            "Unsupported byte range",
            headers={"Content-Range": f"bytes */{size}"}
        )

    start_raw, end_raw = match.groups()

    if not start_raw and not end_raw:
        raise HTTPException(416, "Invalid range")

    if not start_raw:
        length = int(end_raw)
        if length <= 0:
            raise HTTPException(416, "Invalid suffix range")
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else size - 1

    if start >= size or end < start:
        raise HTTPException(
            416,
            "Range not satisfiable",
            headers={"Content-Range": f"bytes */{size}"}
        )

    return start, min(end, size - 1), True


@asynccontextmanager
async def lifespan(app):
    refresh_index()

    print("Connecting separate playback Telegram session...", flush=True)
    await TG.start(bot_token=BOT_TOKEN)
    print("Playback Telegram session ready", flush=True)

    yield

    await TG.disconnect()


app = FastAPI(lifespan=lifespan)

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.bingolink.site",
        "https://unaclec.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Range",
        "X-Telegram-Init-Data",
    ],
    expose_headers=[
        "Content-Range",
        "Accept-Ranges",
        "Content-Length",
    ],
)


from foundation_slides import router as slides_router
app.include_router(slides_router)



@app.get("/health")
async def health():
    if time.time() - last_refresh > 30:
        refresh_index()

    return {
        "ok": True,
        "batch": BATCH_NAME,
        "topic_id": TOPIC_ID,
        "videos_mapped": len(video_index),
        "telegram_connected": TG.is_connected()
    }


@app.api_route("/video/{lecture_id}", methods=["GET", "HEAD"])
async def video(lecture_id: str, request: Request):
    expiry = request.query_params.get("exp", "")
    signature = request.query_params.get("sig", "")

    if not verify_signature(lecture_id, expiry, signature):
        raise HTTPException(403, "Invalid or expired playback link")

    if time.time() - last_refresh > 30:
        refresh_index()

    record = video_index.get(lecture_id)
    if not record:
        raise HTTPException(404, "Video not uploaded yet")

    try:
        message = await TG.get_messages(
            CHAT_ID,
            ids=record["message_id"]
        )
    except Exception as exc:
        print("Telegram message fetch error:", repr(exc), flush=True)
        raise HTTPException(502, "Telegram media unavailable")

    if not message or not message.media or not message.file:
        raise HTTPException(404, "Telegram video message missing")

    size = int(message.file.size or 0)
    if size <= 0:
        raise HTTPException(502, "Invalid Telegram video size")

    start, end, partial = parse_range(
        request.headers.get("range"),
        size
    )

    mime = message.file.mime_type or "video/webm"
    if not mime.startswith("video/"):
        mime = "video/webm"

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline"
    }

    if partial:
        headers["Content-Range"] = (
            f"bytes {start}-{end}/{size}"
        )

    if request.method == "HEAD":
        return JSONResponse(
            status_code=206 if partial else 200,
            content=None,
            headers=headers
        )

    async def chunks():
        remaining = end - start + 1

        try:
            async for part in TG.iter_download(
                message.media,
                offset=start,
                request_size=524288,
                chunk_size=524288,
                file_size=size
            ):
                if remaining <= 0:
                    break

                data = part[:remaining]
                if not data:
                    break

                yield data
                remaining -= len(data)

                if remaining <= 0:
                    break

        except Exception as exc:
            print("Telegram streaming error:", repr(exc), flush=True)
            raise

    return StreamingResponse(
        chunks(),
        status_code=206 if partial else 200,
        media_type=mime,
        headers=headers
    )

from session_security import install as install_session_security
install_session_security(app)
