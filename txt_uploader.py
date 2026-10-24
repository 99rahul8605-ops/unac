
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

    if len(unique) > 500:
        raise ValueError("Maximum 500 lectures per TXT")

    return list(unique.values())


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

    caption = (
        f"{lecture['subject'][:100]}\n"
        f"Class {lecture['class_number']}: "
        f"{lecture['name'][:120]}\n"
        f"File: {kind}"
    )

    message = await client.send_file(
        destination,
        str(path),
        caption=caption,
        reply_to=ACTIVE_TOPIC_ID,
        force_document=True,
        progress_callback=on_progress,
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
    last = ""

    while True:
        info = progress_info.copy()

        mb = 1024 * 1024
        speed = info.get("speed_bps", 0) / mb
        current = info.get("current_bytes", 0) / mb
        total = info.get("total_bytes", 0) / mb
        eta = info.get("eta_seconds")
        eta_text = (
            f"{eta // 60}m {eta % 60}s"
            if eta is not None else "Calculating"
        )

        output = (
            "📤 TXT Uploader\n\n"
            f"Lecture: {info['lecture'][:110]}\n"
            f"Progress: {info['done']}/{info['total']}\n"
            f"Stage: {info['stage']}\n"
            f"Percentage: {info['percent']}%\\n"
            f"Speed: {speed:.2f} MB/s\\n"
            f"Transferred: {current:.1f}/{total:.1f} MB\\n"
            f"ETA: {eta_text}"
        )

        if output != last:
            try:
                await message.edit(output)
                last = output
            except Exception:
                pass

        await asyncio.sleep(8)


async def run_upload(
    client,
    owner,
    destination,
    txt_path,
):
    ticker = None
    done = 0
    failed = 0

    try:
        lectures = parse_txt(txt_path)

        # Batch topic selected before any download starts.
        global ACTIVE_TOPIC_ID
        from forum_topics import ensure_batch_topic

        ACTIVE_TOPIC_ID, batch_name = await ensure_batch_topic(
            client, destination, txt_path
        )

        await client.send_message(
            owner,
            "📁 Batch Forum Topic Ready\\n"
            f"Batch: {batch_name}\\n"
            f"Topic ID: {ACTIVE_TOPIC_ID}\\n"
            "All files will upload inside this topic."
        )

        if not lectures:
            await client.send_message(
                owner,
                "❌ TXT mein valid watch links nahi mile.",
            )
            return

        progress_info.update({
            "stage": "Starting",
            "lecture": "",
            "done": 0,
            "total": len(lectures),
            "percent": 0,
        })

        msg = await client.send_message(
            owner,
            f"✅ {len(lectures)} lectures loaded.\n"
            "Starting download + Telegram upload.",
        )

        ticker = asyncio.create_task(progress_ticker(msg))

        # Reuse exactly the resolver that successfully
        # detected the extension-less WebM media URL.
        from watch_to_api import resolve_lecture

        for lecture in lectures:
            url = lecture["watch_url"]

            progress_info["lecture"] = lecture["name"]

            receipt = load_receipt(url)

            if receipt.get("complete"):
                done += 1
                progress_info["done"] = done
                continue

            try:
                progress_info["stage"] = "Resolving player"
                progress_info["percent"] = 0

                resolved = await resolve_lecture(lecture)

                if not resolved.get("video_url"):
                    raise RuntimeError(
                        "Video URL not resolved"
                    )

                progress_info["stage"] = "API queued"

                api_job_id = await submit_api(
                    lecture, resolved
                )

                result = await wait_api(api_job_id)

                if result["status"] == "failed":
                    raise RuntimeError(
                        "API download failed"
                    )

                successful = set()
                upload_errors = []

                for item in result.get("results", []):
                    if item["status"] != "downloaded":
                        continue

                    kind = item["kind"]

                    path = prepare_file(
                        validated_file(item["path"]),
                        kind,
                    )

                    try:
                        await upload_one(
                            client,
                            destination,
                            lecture,
                            url,
                            kind,
                            path,
                        )
                        successful.add(kind)
                    except Exception as error:
                        upload_errors.append(
                            f"{kind}: {str(error)[:90]}"
                        )

                # Recover a JPEG affected by the
                # current Requests API size mismatch.
                for index, item in enumerate(
                    result.get("results", []), 1
                ):
                    if (
                        item["status"] == "failed"
                        and item["kind"].startswith("asset_")
                    ):
                        asset_path = recover_jpeg_part(
                            api_job_id, index
                        )

                        if asset_path:
                            try:
                                await upload_one(
                                    client,
                                    destination,
                                    lecture,
                                    url,
                                    item["kind"],
                                    asset_path,
                                )
                                successful.add(item["kind"])
                            except Exception as error:
                                upload_errors.append(
                                    str(error)[:100]
                                )

                expected = {
                    item["kind"]
                    for item in result.get("results", [])
                }

                receipt = load_receipt(url)
                uploaded_kinds = set(
                    receipt.get("uploaded", {})
                )

                if not upload_errors and expected <= uploaded_kinds:
                    receipt["complete"] = True
                    save_receipt(url, receipt)
                    done += 1
                    progress_info["done"] = done
                else:
                    failed += 1
                    await client.send_message(
                        owner,
                        f"⚠️ Partial lecture:\n"
                        f"{lecture['name'][:100]}\n"
                        f"Uploaded: {len(uploaded_kinds)}"
                        f"/{len(expected)}",
                    )

            except asyncio.CancelledError:
                raise

            except Exception as error:
                failed += 1
                await client.send_message(
                    owner,
                    f"❌ Lecture failed:\n"
                    f"{lecture['name'][:100]}\n"
                    f"{type(error).__name__}: "
                    f"{str(error)[:170]}",
                )

        await client.send_message(
            owner,
            "🏁 TXT upload run finished\n\n"
            f"Completed: {done}\n"
            f"Failed/partial: {failed}\n"
            f"Total: {len(lectures)}",
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
