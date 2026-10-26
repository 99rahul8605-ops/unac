import asyncio
import hashlib
import io
import json
import mimetypes
import re
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

ROOT = Path("/root/unac")
RECEIPTS = ROOT / ".state/txt_uploads"
CACHE = ROOT / ".state/slide_zip_cache"
CACHE.mkdir(parents=True, exist_ok=True)

TOPIC_ID = 23
MAX_ZIP = 150 * 1024 * 1024
MAX_SLIDE = 20 * 1024 * 1024
MAX_FILES = 250

router = APIRouter()
locks = {}

SUPPORTED = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}


def stream():
    import foundation_stream
    return foundation_stream


def validate(lecture_id, request):
    if not re.fullmatch(r"[a-z0-9-]{1,100}", lecture_id):
        raise HTTPException(400, "Invalid lecture ID")

    exp = request.query_params.get("exp", "")
    sig = request.query_params.get("sig", "")

    if not stream().verify_signature(lecture_id, exp, sig):
        raise HTTPException(403, "Invalid playback signature")

    return exp, sig


def get_receipt(lecture_id):
    s = stream()
    s.refresh_index()

    if lecture_id not in s.video_index:
        raise HTTPException(404, "Video not uploaded")

    expected = s.video_index[lecture_id]
    source = s.find_source(expected.get("batch"))
    if not source:
        raise HTTPException(404, "Source TXT missing")
    name = ""
    subject = ""

    for raw in source[2].splitlines():
        line = raw.strip()
        upper = line.upper()

        if upper.startswith("SUBJECT:"):
            subject = line.partition(":")[2].strip()

        elif upper.startswith("LECTURE:"):
            name = line.partition(":")[2].strip()

        elif upper.startswith("WATCH_URL:"):
            url = line.partition(":")[2].strip()

            if s.make_id(url) != lecture_id:
                continue

            if name.casefold() != expected["name"].casefold():
                raise HTTPException(409, "Lecture name mismatch")

            if subject.casefold() != expected["subject"].casefold():
                raise HTTPException(409, "Subject mismatch")

            digest = hashlib.sha256(
                f"{expected.get('topic_id', TOPIC_ID)}|{url}".encode()
            ).hexdigest()

            file = RECEIPTS / f"{digest}.json"

            try:
                data = json.loads(file.read_text())
            except (OSError, ValueError):
                raise HTTPException(404, "Upload receipt missing")

            uploaded = data.get("uploaded") or {}
            video = uploaded.get("video") or {}

            if int(video.get("message_id") or 0) != int(
                expected["message_id"]
            ):
                raise HTTPException(
                    409, "Video receipt mismatch"
                )

            return uploaded

    raise HTTPException(404, "Lecture not found")


def get_message_id(info):
    if not isinstance(info, dict):
        return None
    try:
        value = int(info.get("message_id"))
        return value if value > 0 else None
    except (ValueError, TypeError):
        return None


async def telegram_message(message_id):
    s = stream()

    try:
        message = await s.TG.get_messages(
            s.CHAT_ID,
            ids=message_id
        )
    except Exception as exc:
        print("Slides Telegram error:", repr(exc), flush=True)
        raise HTTPException(502, "Telegram unavailable")

    if not message or not message.media:
        raise HTTPException(404, "Telegram file missing")

    return message


def detected_mime(data, filename):
    ext = Path(filename.lower()).suffix

    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    if data.startswith(b"%PDF-"):
        return "application/pdf"

    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"

    if len(data) >= 12 and (
        data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    ):
        return "image/webp"

    if ext in SUPPORTED:
        raise HTTPException(
            422,
            f"Invalid or unsupported {ext} slide bytes"
        )

    raise HTTPException(415, "Unsupported slide format")


def media_response(data, filename):
    mime = detected_mime(data, filename)

    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff"
        }
    )


def assets_from(uploaded):
    assets = []

    for key, value in uploaded.items():
        if not re.fullmatch(r"asset_[0-9]+", key):
            continue

        message_id = get_message_id(value)

        if message_id:
            assets.append((
                int(key.split("_")[1]),
                key,
                message_id
            ))

    return sorted(assets)


async def cached_zip(lecture_id, message_id):
    destination = CACHE / f"{lecture_id}-{message_id}.zip"

    if destination.exists() and destination.stat().st_size > 0:
        return destination

    lock_key = (lecture_id, message_id)

    if lock_key not in locks:
        locks[lock_key] = asyncio.Lock()

    async with locks[lock_key]:
        if destination.exists() and destination.stat().st_size > 0:
            return destination

        message = await telegram_message(message_id)
        size = int(message.file.size or 0)

        if size <= 0 or size > MAX_ZIP:
            raise HTTPException(413, "Slides ZIP too large")

        buffer = io.BytesIO()

        try:
            await stream().TG.download_media(
                message,
                file=buffer
            )
        except Exception as exc:
            print("ZIP download failed:", repr(exc), flush=True)
            raise HTTPException(502, "ZIP download failed")

        contents = buffer.getvalue()

        if len(contents) > MAX_ZIP:
            raise HTTPException(413, "ZIP size limit exceeded")

        if not zipfile.is_zipfile(io.BytesIO(contents)):
            raise HTTPException(422, "Slides ZIP is invalid")

        temporary = destination.with_suffix(".tmp")

        try:
            temporary.write_bytes(contents)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

        return destination


def zip_entries(path):
    try:
        with zipfile.ZipFile(path) as archive:
            selected = []

            for info in archive.infolist():
                if info.is_dir():
                    continue

                ext = Path(info.filename.lower()).suffix
                if ext not in SUPPORTED:
                    continue

                if info.flag_bits & 0x1:
                    continue

                if info.file_size > MAX_SLIDE:
                    continue

                selected.append(info.filename)

                if len(selected) >= MAX_FILES:
                    break

            return selected

    except (OSError, zipfile.BadZipFile):
        raise HTTPException(422, "Invalid ZIP archive")


@router.get("/slides/{lecture_id}/manifest")
async def manifest(lecture_id: str, request: Request):
    exp, sig = validate(lecture_id, request)
    uploaded = get_receipt(lecture_id)

    slides = []
    query = f"exp={exp}&sig={sig}"

    assets = assets_from(uploaded)

    for _, key, _ in assets:
        slides.append({
            "key": key,
            "type": "asset",
            "name": key,
            "url": (
                f"/slides/{lecture_id}/asset/{key}?{query}"
            )
        })

    zip_id = get_message_id(uploaded.get("slides_zip"))
    zip_error = None

    if zip_id:
        try:
            path = await cached_zip(lecture_id, zip_id)
            entries = zip_entries(path)

            for index, filename in enumerate(entries):
                slides.append({
                    "key": f"zip_{index}",
                    "type": "zip",
                    "name": Path(filename).name,
                    "url": (
                        f"/slides/{lecture_id}/zip/{index}"
                        f"?{query}"
                    )
                })

        except HTTPException as exc:
            zip_error = str(exc.detail)
            print(
                f"Slides ZIP unavailable {lecture_id}: {zip_error}",
                flush=True
            )

    return {
        "ok": True,
        "slides": slides,
        "count": len(slides),
        "has_zip": bool(zip_id),
        "has_board": bool(
            get_message_id(uploaded.get("board"))
        ),
        "zip_error": zip_error
    }


@router.get("/slides/{lecture_id}/asset/{asset_key}")
async def asset(lecture_id: str, asset_key: str, request: Request):
    validate(lecture_id, request)
    uploaded = get_receipt(lecture_id)

    matches = [
        item for item in assets_from(uploaded)
        if item[1] == asset_key
    ]

    if not matches:
        raise HTTPException(404, "Slide not uploaded")

    message = await telegram_message(matches[0][2])

    size = int(message.file.size or 0)

    if size <= 0 or size > MAX_SLIDE:
        raise HTTPException(413, "Slide size unsupported")

    buffer = io.BytesIO()

    try:
        await stream().TG.download_media(
            message,
            file=buffer
        )
    except Exception:
        raise HTTPException(502, "Slide download failed")

    name = message.file.name or f"{asset_key}.jpg"

    return media_response(buffer.getvalue(), name)


@router.get("/slides/{lecture_id}/zip/{index}")
async def zip_slide(lecture_id: str, index: int, request: Request):
    validate(lecture_id, request)
    uploaded = get_receipt(lecture_id)

    zip_id = get_message_id(uploaded.get("slides_zip"))

    if not zip_id:
        raise HTTPException(404, "Slides ZIP not uploaded")

    if index < 0 or index >= MAX_FILES:
        raise HTTPException(404, "Invalid slide index")

    path = await cached_zip(lecture_id, zip_id)
    entries = zip_entries(path)

    if index >= len(entries):
        raise HTTPException(404, "Slide missing")

    try:
        with zipfile.ZipFile(path) as archive:
            name = entries[index]
            info = archive.getinfo(name)

            if info.file_size > MAX_SLIDE:
                raise HTTPException(413, "Slide too large")

            data = archive.read(info)

    except (OSError, RuntimeError, zipfile.BadZipFile):
        raise HTTPException(422, "Cannot read slide")

    return media_response(data, name)


# Signed JSON API for synchronized lecture whiteboard.
@router.get("/slides/{lecture_id}/board")
async def board_json(lecture_id: str, request: Request):
    validate(lecture_id, request)
    uploaded = get_receipt(lecture_id)
    message_id = get_message_id(uploaded.get("board"))

    if not message_id:
        raise HTTPException(404, "Board not uploaded")

    cache_dir = Path("/root/unac/.state/board_api_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / f"{lecture_id}-{message_id}.json"

    if destination.exists():
        raw = destination.read_bytes()
    else:
        message = await telegram_message(message_id)
        size = int(message.file.size or 0)

        if size <= 0 or size > 35 * 1024 * 1024:
            raise HTTPException(413, "Board file too large")

        buffer = io.BytesIO()

        try:
            await stream().TG.download_media(
                message, file=buffer
            )
        except Exception as exc:
            print("Board download failed:", repr(exc), flush=True)
            raise HTTPException(502, "Board download failed")

        raw = buffer.getvalue()

        if len(raw) > 35 * 1024 * 1024:
            raise HTTPException(413, "Board too large")

        try:
            parsed = json.loads(raw)
        except ValueError:
            raise HTTPException(422, "Invalid board JSON")

        if not isinstance(parsed, list):
            raise HTTPException(422, "Unexpected board format")

        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(raw)
        temporary.replace(destination)

    return Response(
        content=raw,
        media_type="application/json",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff"
        }
    )
