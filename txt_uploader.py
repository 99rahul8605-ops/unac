
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

ROOT = Path("/root/unac")
STATE = ROOT / ".state"
WORK = STATE / "txt_uploads"
API_ROOT = ROOT / "api_downloads"
API_URL = "http://127.0.0.1:8091"

WORK.mkdir(parents=True, exist_ok=True)

# BATCH_FORUM_ROUTING_V1
ACTIVE_TOPIC_ID = None

active_task = None
progress_info = {
    "stage": "Idle",
    "lecture": "",
    "done": 0,
    "total": 0,
    "percent": 0,
    "speed_bps": 0,
    "current_bytes": 0,
    "total_bytes": 0,
    "eta_seconds": None,
    # SPEED_PATCH_V2
}

waiting_for_txt = set()


def parse_txt(path):
    rows = []
    subject = ""
    title = ""
    number = ""

    for raw in path.read_text(
        encoding="utf-8-sig"
    ).splitlines():

        line = raw.strip()

        if line.startswith("SUBJECT:"):
            subject = line.partition(":")[2].strip()

        elif line.startswith("CLASS:"):
            number = line.partition(":")[2].strip()

        elif line.startswith("LECTURE:"):
            title = line.partition(":")[2].strip()

        elif line.startswith("WATCH_URL:"):
            url = line.partition(":")[2].strip()
            parsed = urlsplit(url)

            if (
                parsed.scheme == "https"
                and parsed.hostname
                == "unacademy.hrvertex.workers.dev"
                and parsed.path == "/watch"
                and title
            ):
                rows.append({
                    "subject": subject,
                    "class_number": number,
                    "name": title,
                    "watch_url": url,
                })

    unique = {}

    for item in rows:
        unique[item["watch_url"]] = item

    if len(unique) > 1000:
        raise ValueError("Maximum 1000 lectures per TXT")

    # LECTURE_INDEX_FIX_V2
    _items = list(unique.values())
    for _index, _lecture in enumerate(_items, 1):
        _lecture.setdefault("txt_index", _index)
        _lecture.setdefault("txt_total", len(_items))
    return _items


def receipt_path(url):
    if ACTIVE_TOPIC_ID is None:
        raise RuntimeError("Batch topic not selected")

    digest = hashlib.sha256(
        f"{ACTIVE_TOPIC_ID}|{url}".encode("utf-8")
    ).hexdigest()

    return WORK / f"{digest}.json"


def load_receipt(url):
    path = receipt_path(url)

    if not path.exists():
        return {}

    return json.loads(path.read_text("utf-8"))


def save_receipt(url, receipt):
    path = receipt_path(url)
    temp = path.with_suffix(".tmp")

    temp.write_text(
        json.dumps(receipt, indent=2),
        encoding="utf-8",
    )

    temp.replace(path)


def api_key():
    path = STATE / "lecture_api.key"

    if not path.is_file():
        raise RuntimeError(
            "API key file missing: "
            ".state/lecture_api.key"
        )

    return path.read_text().strip()


def api_request(method, path, **kwargs):
    response = requests.request(
        method,
        API_URL + path,
        headers={"X-API-Key": api_key()},
        timeout=30,
        **kwargs,
    )

    response.raise_for_status()
    return response.json()


async def submit_api(lecture, resolved):
    files = []

    if resolved.get("video_url"):
        files.append({
            "kind": "video",
            "url": resolved["video_url"],
        })

    if resolved.get("board_json_url"):
        files.append({
            "kind": "board",
            "url": resolved["board_json_url"],
        })

    for index, url in enumerate(
        resolved.get("assets", []), 1
    ):
        if len(files) >= 30:
            break

        files.append({
            "kind": f"asset_{index}",
            "url": url,
        })

    if not files or files[0]["kind"] != "video":
        raise RuntimeError("Video source missing")

    payload = {
        "lecture": lecture["name"],
        "watch_url": lecture["watch_url"],
        "files": files,
    }

    result = await asyncio.to_thread(
        api_request,
        "POST",
        "/download",
        json=payload,
    )

    return result["job_id"]


async def wait_api(job_id):
    while True:
        result = await asyncio.to_thread(
            api_request,
            "GET",
            f"/status/{job_id}",
        )

        progress_info["stage"] = (
            "Downloading "
            + str(result.get("current_resource", ""))
        )

        progress_info["percent"] = (
            result.get("percent") or 0
        )
        progress_info["speed_bps"] = result.get("speed_bps") or 0
        progress_info["current_bytes"] = result.get("downloaded_bytes") or 0
        progress_info["total_bytes"] = result.get("total_bytes") or 0
        progress_info["eta_seconds"] = result.get("eta_seconds")

        if result["status"] in (
            "completed",
            "completed_with_errors",
            "failed",
        ):
            return result

        await asyncio.sleep(3)


def validated_file(filename):
    path = Path(filename).resolve()

    if not path.is_relative_to(API_ROOT.resolve()):
        raise RuntimeError("Invalid API file path")

    if not path.is_file():
        raise FileNotFoundError(path)

    return path


def prepare_file(path, kind):
    """
    Fix extension-only naming.
    Never modify actual video contents.
    """

    suffix = path.suffix.lower()

    if kind == "video" and suffix == ".bin":
        target = path.with_suffix(".webm")

    elif kind == "board" and suffix == ".bin":
        target = path.with_suffix(".json")

    else:
        return path

    if not target.exists():
        path.rename(target)

    return target


def recover_jpeg_part(job_id, index):
    """
    Existing API can incorrectly report an
    incomplete JPEG because of compressed length.

    Recover only JPEGs with SOI and EOI markers.
    """

    folder = API_ROOT / job_id
    name = f"{index}_asset_{index-2}.jpg.part"
    path = folder / name

    if not path.is_file():
        return None

    with path.open("rb") as f:
        start = f.read(3)
        f.seek(max(0, path.stat().st_size - 2))
        end = f.read(2)

    if start != b"\xff\xd8\xff" or end != b"\xff\xd9":
        return None

    target = path.with_suffix("")

    if not target.exists():
        path.rename(target)

    return target


async def upload_one(
    client,
    destination,
    lecture,
    url,
    kind,
    path,
):
    receipt = load_receipt(url)
    uploaded = receipt.get("uploaded", {})

    if kind in uploaded:
        return False

    progress_info["stage"] = f"Uploading {kind}"
    progress_info["percent"] = 0

    previous_time = time.monotonic()
    previous_bytes = 0
    speed = 0.0

    def on_progress(current, total):
        nonlocal previous_time, previous_bytes, speed

        now = time.monotonic()
        elapsed = now - previous_time

        if elapsed >= 0.75:
            instant = max(0, current - previous_bytes) / elapsed
            speed = instant if speed == 0 else (
                0.3 * instant + 0.7 * speed
            )
            previous_time = now
            previous_bytes = current

        progress_info["current_bytes"] = current
        progress_info["total_bytes"] = total
        progress_info["speed_bps"] = speed
        progress_info["eta_seconds"] = (
            round((total - current) / speed)
            if total and speed > 0 else None
        )
        if total:
            progress_info["percent"] = round(
                current * 100 / total, 1
            )

    # LECTURE_INDEX_FIX_V2
    _txt_index = lecture.get("txt_index", "?")
    _txt_total = lecture.get("txt_total", "?")
    caption = (
        f"{lecture['subject'][:100]}\n"
        f"Class {lecture['class_number']}: "
        f"{lecture['name'][:120]}\n"
        f"File: {kind}"
    )
    caption = f"🎬 Index: {_txt_index}/{_txt_total}\n" + caption

    # PYROGRAM_UPLOAD_ENGINE_V1
    from pyrogram_uploader import send_document

    message = await send_document(
        destination=destination,
        filename=str(path),
        caption=caption,
        progress_callback=on_progress,
        topic_id=ACTIVE_TOPIC_ID,
                        source_client=client,
    )

    uploaded[kind] = {
        "message_id": message.id,
        "size": path.stat().st_size,
    }

    receipt["uploaded"] = uploaded
    receipt["name"] = lecture["name"]

    save_receipt(url, receipt)

    # Files from this API job have been saved
    # to Telegram. Free the disk space.
    path.unlink(missing_ok=True)

    return True



async def progress_ticker(message):
    # UPLOADER_REPAIR_V3
    last = ""

    while True:
        info = progress_info.copy()

        speed = float(info.get("speed_bps") or 0) / 1048576
        current = float(info.get("current_bytes") or 0) / 1048576
        total = float(info.get("total_bytes") or 0) / 1048576

        eta = info.get("eta_seconds")
        if eta is None:
            eta_text = "Calculating..."
        else:
            eta = max(0, int(eta))
            eta_text = f"{eta // 60}m {eta % 60}s"

        output = (
            "📤 TXT Uploader\n\n"
            f"Lecture: {info.get('lecture', '')[:110]}\n"
            f"Completed: {info.get('done', 0)}/"
            f"{info.get('total', 0)}\n"
            f"Stage: {info.get('stage', 'Idle')}\n"
            f"Progress: {info.get('percent', 0)}%\n"
            f"Speed: {speed:.2f} MB/s\n"
            f"Transferred: {current:.1f}/{total:.1f} MB\n"
            f"ETA: {eta_text}"
        )

        if output != last:
            try:
                await message.edit(output)
                last = output
            except Exception:
                pass

        await asyncio.sleep(5)




async def run_upload(client, owner, destination, txt_path):
    # SLIDES_SINGLE_ZIP_V1
    import zipfile
    from pyrogram.errors import FloodWait

    global ACTIVE_TOPIC_ID

    ticker = None
    completed = 0
    partial = 0
    failed = 0

    try:
        lectures = parse_txt(txt_path)

        if not lectures:
            await client.send_message(
                owner, "No valid watch links in TXT."
            )
            return

        from forum_topics import ensure_batch_topic

        ACTIVE_TOPIC_ID, batch_name = await ensure_batch_topic(
            client, destination, txt_path
        )

        await client.send_message(
            owner,
            f"📁 Batch Topic: {batch_name}\n"
            f"Topic ID: {ACTIVE_TOPIC_ID}\n"
            "🎬 Video + 📋 Board + 📦 One Slides ZIP"
        )

        progress_info.update({
            "stage": "Starting",
            "lecture": "",
            "done": 0,
            "total": len(lectures),
            "percent": 0,
            "speed_bps": 0,
            "current_bytes": 0,
            "total_bytes": 0,
            "eta_seconds": None,
        })

        message = await client.send_message(
            owner, f"📦 {len(lectures)} lectures loaded"
        )
        ticker = asyncio.create_task(progress_ticker(message))

        from watch_to_api import resolve_lecture

        for position, lecture in enumerate(lectures, 1):
            url = lecture["watch_url"]
            name = lecture["name"]

            progress_info.update({
                "lecture": f"{position}/{len(lectures)} — {name}",
                "stage": "Checking uploads",
                "percent": 0,
                "speed_bps": 0,
                "current_bytes": 0,
                "total_bytes": 0,
                "eta_seconds": None,
            })

            try:
                receipt = load_receipt(url)

                if (
                    receipt.get("complete")
                    and receipt.get("slides_zip_version") == 1
                ):
                    completed += 1
                    progress_info["done"] = completed
                    continue

                progress_info["stage"] = "Resolving video"
                resolved = await resolve_lecture(lecture)

                if not resolved.get("video_url"):
                    raise RuntimeError("Video URL not resolved")

                progress_info["stage"] = "Downloading resources"
                job_id = await submit_api(lecture, resolved)
                result = await wait_api(job_id)
                entries = result.get("results") or []

                if not entries:
                    raise RuntimeError("API returned no file results")

                problems = []
                slides = []
                asset_failures = []
                required = {"video"}

                if any(
                    x.get("kind") == "board" for x in entries
                ):
                    required.add("board")

                # Video and board stay separate.
                # Assets are collected, NOT uploaded individually.
                for entry in entries:
                    kind = entry.get("kind", "")
                    status = entry.get("status", "")
                    filename = entry.get("path")

                    if kind.startswith("asset_"):
                        if status == "downloaded" and filename:
                            try:
                                path = validated_file(filename)
                                if path.stat().st_size > 0:
                                    slides.append((kind, path))
                                else:
                                    asset_failures.append(
                                        f"{kind}: empty file"
                                    )
                            except Exception as error:
                                asset_failures.append(
                                    f"{kind}: {error}"
                                )
                        else:
                            asset_failures.append(
                                f"{kind}: {status} "
                                f"{str(entry.get('error') or '')[:70]}"
                            )
                        continue

                    if kind not in ("video", "board"):
                        continue

                    if status != "downloaded" or not filename:
                        problems.append(
                            f"{kind}: download {status}"
                        )
                        continue

                    try:
                        path = prepare_file(
                            validated_file(filename), kind
                        )

                        await upload_one(
                            client, destination,
                            lecture, url, kind, path
                        )
                    except asyncio.CancelledError:
                        raise
                    except FloodWait:
                        raise
                    except Exception as error:
                        problems.append(
                            f"{kind}: {type(error).__name__}: {error}"
                        )

                # VIDEO_BOARD_BEFORE_ZIP_V1
                # Never send slides until the main lecture
                # files are confirmed uploaded to Telegram.
                core_receipt = load_receipt(url)
                core_uploaded = set(
                    core_receipt.get("uploaded", {})
                )

                video_ready = "video" in core_uploaded
                board_needed = (
                    "board" in required
                    or bool(resolved.get("board_json_url"))
                )
                board_ready = (
                    not board_needed
                    or "board" in core_uploaded
                )

                if not video_ready or not board_ready:
                    details = "\\n".join(problems[:5])

                    await client.send_message(
                        owner,
                        f"❌ Main lecture files missing: "
                        f"{name[:100]}\\n"
                        f"🎬 Video: "
                        f"{'✅' if video_ready else '❌'}\\n"
                        f"📋 Board: "
                        f"{'✅' if board_ready else '❌'}\\n"
                        f"📦 ZIP: Not started\\n\\n"
                        f"Errors:\\n"
                        f"{details or 'API did not provide required files'}"
                    )

                    progress_info["stage"] = (
                        "Stopped: video/board missing"
                    )

                    # Stop batch rather than sending slides
                    # without the main lecture files.
                    break

                # Create exactly one archive per lecture.
                if slides:
                    folder = API_ROOT / str(job_id)
                    zip_path = folder / "Lecture_Slides.zip"

                    progress_info["stage"] = (
                        f"Packing {len(slides)} slides into ZIP"
                    )

                    def make_zip():
                        with zipfile.ZipFile(
                            zip_path,
                            "w",
                            compression=zipfile.ZIP_STORED,
                            allowZip64=True,
                        ) as archive:
                            for kind, source in slides:
                                archive.write(
                                    source,
                                    arcname=source.name,
                                )

                        with zipfile.ZipFile(zip_path) as archive:
                            bad = archive.testzip()
                            if bad is not None:
                                raise RuntimeError(
                                    f"ZIP integrity failed: {bad}"
                                )

                    try:
                        await asyncio.to_thread(make_zip)

                        await upload_one(
                            client, destination,
                            lecture, url,
                            "slides_zip", zip_path
                        )

                        # Assets can be cleaned only after
                        # their ZIP is safely uploaded.
                        for _, source in slides:
                            source.unlink(missing_ok=True)

                    except asyncio.CancelledError:
                        raise
                    except FloodWait:
                        raise
                    except Exception as error:
                        problems.append(
                            f"slides_zip: "
                            f"{type(error).__name__}: {error}"
                        )

                receipt = load_receipt(url)
                uploaded = set(receipt.get("uploaded", {}))

                video_ok = "video" in uploaded
                board_ok = (
                    "board" not in required
                    or "board" in uploaded
                )
                slides_ok = (
                    not slides
                    or "slides_zip" in uploaded
                )

                # Failed downloads are reported separately.
                fully_ok = (
                    video_ok and board_ok and slides_ok
                    and not asset_failures
                )

                receipt["complete"] = fully_ok
                receipt["slides_zip_version"] = 1
                receipt["video_complete"] = video_ok
                receipt["board_complete"] = board_ok
                receipt["asset_download_errors"] = (
                    asset_failures[:30]
                )
                save_receipt(url, receipt)

                if fully_ok:
                    completed += 1
                    progress_info["done"] = completed
                else:
                    if video_ok and board_ok:
                        partial += 1
                    else:
                        failed += 1

                    details = problems + asset_failures

                    await client.send_message(
                        owner,
                        f"⚠️ {name[:100]}\n"
                        f"🎬 Video: {'✅' if video_ok else '❌'}\n"
                        f"📋 Board: {'✅' if board_ok else '❌'}\n"
                        f"📦 Slides ZIP: "
                        f"{'✅' if 'slides_zip' in uploaded else '❌'}\n"
                        f"Downloaded assets: {len(slides)}\n"
                        f"Asset download errors: "
                        f"{len(asset_failures)}\n"
                        + "\n".join(details[:3])[:350]
                    )

            except asyncio.CancelledError:
                raise

            except FloodWait as error:
                seconds = int(error.value)
                await client.send_message(
                    owner,
                    f"⏳ Telegram FloodWait: {seconds}s\n"
                    "Uploader safely stopped. "
                    "Resume after the wait."
                )
                break

            except Exception as error:
                failed += 1
                await client.send_message(
                    owner,
                    f"❌ {name[:100]}\n"
                    f"{type(error).__name__}: "
                    f"{str(error)[:300]}"
                )

        await client.send_message(
            owner,
            "🏁 Upload finished\n"
            f"✅ Complete: {completed}\n"
            f"⚠️ Partial: {partial}\n"
            f"❌ Failed: {failed}"
        )

    finally:
        if ticker:
            ticker.cancel()

        progress_info["stage"] = "Idle"
        txt_path.unlink(missing_ok=True)




async def handle_upload_event(
    event,
    client,
    owner,
    destination,
):
    global active_task

    text = (event.raw_text or "").strip().lower()

    if text == "/uploadstatus":
        await event.respond(
            "📤 TXT Uploader\n"
            f"Stage: {progress_info['stage']}\n"
            f"Lecture: {progress_info['lecture'][:100]}\n"
            f"Done: {progress_info['done']}/"
            f"{progress_info['total']}\n"
            f"Percent: {progress_info['percent']}%"
        )
        return True

    if text == "/uploadtxt":
        if active_task and not active_task.done():
            await event.respond(
                "⏳ Uploader already running.\n"
                "Use /uploadstatus"
            )
            return True

        waiting_for_txt.add(owner)

        await event.respond(
            "📄 Ab TXT file Telegram par "
            "document ke roop mein bhejo.\n\n"
            "Bot automatic download + upload karega."
        )
        return True

    if owner not in waiting_for_txt:
        return False

    if not event.message.document:
        return False

    file_name = (
        getattr(event.file, "name", "") or ""
    ).lower()

    if not file_name.endswith(".txt"):
        await event.respond("Sirf .txt file bhejo.")
        return True

    if active_task and not active_task.done():
        await event.respond("Uploader already active.")
        return True

    if event.file.size > 2 * 1024 * 1024:
        await event.respond("TXT file maximum 2 MB.")
        return True

    waiting_for_txt.discard(owner)

    path = WORK / (
        f"input_{int(time.time())}_{event.id}.txt"
    )

    await event.download_media(file=str(path))

    active_task = asyncio.create_task(
        run_upload(
            client,
            owner,
            destination,
            path,
        )
    )

    await event.respond("✅ TXT accepted. Upload started.")
    return True
