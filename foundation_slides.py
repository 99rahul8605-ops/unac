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
TOPIC_ID = 23

router = APIRouter()

VIDEO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}


def receipt_for(lecture_id):
    # Import existing stream index at request time.
    import foundation_stream as stream

    if not re.fullmatch(r"[a-z0-9-]{1,100}", lecture_id):
        raise HTTPException(400, "Invalid lecture ID")

    stream.refresh_index()

    if lecture_id not in stream.video_index:
        raise HTTPException(404, "Lecture not uploaded")

    record = stream.video_index[lecture_id]

    # Lecture ID is derived from the watch URL; get the
    # original receipt through the Foundation TXT.
    source = stream.find_source()

    if not source:
        raise HTTPException(404, "Foundation TXT missing")

    content = source[2]
    name = ""
    subject = ""

    for line in content.splitlines():
        line = line.strip()
        upper = line.upper()

        if upper.startswith("SUBJECT:"):
            subject = line.partition(":")[2].strip()

        elif upper.startswith("LECTURE:"):
            name = line.partition(":")[2].strip()

        elif upper.startswith("WATCH_URL:"):
            url = line.partition(":")[2].strip()

            if stream.make_id(url) != lecture_id:
                continue

            if (
                name.casefold() !=
                record["name"].casefold()
            ):
                raise HTTPException(
                    409, "Lecture name mismatch"
                )

            digest = hashlib.sha256(
                f"{TOPIC_ID}|{url}".encode()
            ).hexdigest()

            path = RECEIPTS / f"{digest}.json"

            if not path.exists():
                raise HTTPException(
                    404, "Upload receipt missing"
                )

            return json.loads(path.read_text())

    raise HTTPException(404, "Lecture not found")


def authorize(lecture_id, request):
    import foundation_stream as stream

    exp = request.query_params.get("exp", "")
    sig = request.query_params.get("sig", "")

    if not stream.verify_signature(lecture_id, exp, sig):
        raise HTTPException(
            403, "Invalid playback signature"
        )


def uploaded_files(lecture_id):
    receipt = receipt_for(lecture_id)
    uploaded = receipt.get("uploaded") or {}

    assets = []

    for key, info in uploaded.items():
        if not isinstance(info, dict):
            continue

        if not key.startswith("asset_"):
            continue

        try:
            message_id = int(info["message_id"])
        except (KeyError, TypeError, ValueError):
            continue

        assets.append({
            "key": key,
            "message_id": message_id,
            "number": int(key.split("_")[1])
        })

    assets.sort(key=lambda item: item["number"])

    zip_info = uploaded.get("slides_zip") or {}
    board_info = uploaded.get("board") or {}

    return {
        "assets": assets,
        "slides_zip": zip_info.get("message_id"),
        "board": board_info.get("message_id")
    }


@router.get("/slides/{lecture_id}/manifest")
async def slides_manifest(lecture_id: str, request: Request):
    authorize(lecture_id, request)

    data = uploaded_files(lecture_id)

    exp = request.query_params["exp"]
    sig = request.query_params["sig"]

    slides = [
        {
            "key": item["key"],
            "url": (
                f"/slides/{lecture_id}/asset/{item['key']}"
                f"?exp={exp}&sig={sig}"
            )
        }
        for item in data["assets"]
    ]

    return {
        "ok": True,
        "slides": slides,
        "has_zip": bool(data["slides_zip"]),
        "has_board": bool(data["board"]),
    }


@router.get("/slides/{lecture_id}/asset/{asset_key}")
async def slide_asset(
    lecture_id: str,
    asset_key: str,
    request: Request
):
    authorize(lecture_id, request)

    if not re.fullmatch(r"asset_[1-9][0-9]*", asset_key):
        raise HTTPException(400, "Invalid asset key")

    import foundation_stream as stream

    data = uploaded_files(lecture_id)

    matching = [
        item
        for item in data["assets"]
        if item["key"] == asset_key
    ]

    if not matching:
        raise HTTPException(404, "Slide not uploaded")

    message_id = matching[0]["message_id"]

    message = await stream.TG.get_messages(
        stream.CHAT_ID,
        ids=message_id
    )

    if not message or not message.media:
        raise HTTPException(404, "Slide message missing")

    # Only allow reasonably small individual assets.
    size = int(message.file.size or 0)

    if size <= 0 or size > 20 * 1024 * 1024:
        raise HTTPException(
            413, "Slide size not supported"
        )

    mime = message.file.mime_type or ""

    allowed = (
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "application/pdf",
    )

    if mime not in allowed:
        raise HTTPException(
            415, "Unsupported slide format"
        )

    buffer = io.BytesIO()

    await stream.TG.download_media(
        message,
        file=buffer
    )

    payload = buffer.getvalue()

    if mime == "image/jpeg" and not payload.startswith(b"\xff\xd8\xff"):
        raise HTTPException(422, "Invalid JPEG data")

    if mime == "image/png" and not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(422, "Invalid PNG data")

    if mime == "application/pdf" and not payload.startswith(b"%PDF-"):
        raise HTTPException(422, "Invalid PDF data")

    return Response(
        payload,
        media_type=mime,
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff"
        }
    )