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

BATCHES = [
    ("Yoddha Regular Batch for CA Foundation Jan'27/May'27", 23),
    ("Yoddha Regular Batch for CA Foundation May'26 / Sep'26", 459),
]
BATCH_NAME, TOPIC_ID = BATCHES[0]

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


def find_source(batch_name=None):
    batch_name = batch_name or BATCH_NAME
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

        if batch.group(1).strip() != batch_name:
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


def _index_one(batch_name, topic_id):
    source = find_source(batch_name)
    if not source:
        return {}

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
                f"{topic_id}|{url}".encode()
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
                "topic_id": topic_id,
                "batch": batch_name,
                "name": current_name,
                "subject": current_subject,
                "class_number": current_number,
            }

    print(
        f"Source: {path.name} | {batch_name} | "
        f"Mapped videos: {len(result)}",
        flush=True
    )
    return result


# AUTO_BATCH_DISCOVERY
# New batches need no code change: any TXT with a BATCH: line is picked up,
# and its Telegram topic is found from the upload receipts.
_topic_cache = {}  # batch name -> (topic_id or None, receipts_seen, tried_at)


def _all_batch_names():
    paths = list((ROOT / "exports").rglob("*.txt"))
    paths += list(RECEIPTS.glob("input_*.txt"))
    names = set()

    for path in set(paths):
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue

        match = re.search(r"(?im)^BATCH:\s*(.+?)\s*$", content)
        if match:
            names.add(match.group(1).strip())

    return names


def _mapped_topic(batch_name):
    """Topic ID saved by the upload bot when it created the batch topic."""
    try:
        data = json.loads(
            (ROOT / ".state" / "batch_forum_topics.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return None

    def norm(value):
        return re.sub(r"\s+", " ", str(value)).strip().casefold()

    want = norm(batch_name)

    for key, value in data.items():
        if norm(key.partition(":")[2]) == want:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

    return None


def auto_topic(batch_name):
    known = {path.stem for path in RECEIPTS.glob("*.json")}
    hit = _topic_cache.get(batch_name)
    now = time.time()

    if hit:
        topic, seen, tried = hit
        if topic:
            return topic
        # Retry only when new receipts appeared, at most every 2 minutes.
        if seen == len(known) or now - tried < 120:
            return None

    topic = None
    source = find_source(batch_name)

    if source and known:
        urls = re.findall(
            r"(?im)^WATCH_URL:\s*(https://\S+)", source[2]
        )[:2]

        for candidate in range(1, 200001):
            if any(
                hashlib.sha256(f"{candidate}|{url}".encode()).hexdigest()
                in known
                for url in urls
            ):
                topic = candidate
                print(
                    f"Auto-detected topic {topic} for {batch_name}",
                    flush=True,
                )
                break

    _topic_cache[batch_name] = (topic, len(known), now)
    return topic


def refresh_index():
    global video_index, last_refresh

    topics = dict(BATCHES)
    merged = {}

    for batch_name in sorted(_all_batch_names() | set(topics)):
        topic_id = (
            topics.get(batch_name)
            or _mapped_topic(batch_name)
            or auto_topic(batch_name)
        )
        if topic_id:
            merged.update(_index_one(batch_name, topic_id))

    video_index = merged
    last_refresh = time.time()


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
        "batches": [b for b, _ in BATCHES],
        "topic_ids": [t for _, t in BATCHES],
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
