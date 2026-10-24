
import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import zipfile

from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from catalog import BASE, start_browser, load

# One active lecture transfer per bot process.
# Other asynchronous bot handlers may keep running.
DOWNLOAD_LOCK = asyncio.Semaphore(1)

MIN_FREE_RAM_MB = int(os.getenv("UNAC_MIN_FREE_RAM_MB", "650"))
MIN_FREE_DISK_MB = int(os.getenv("UNAC_MIN_FREE_DISK_MB", "768"))
DISK_RESERVE_MB = 256
MAX_ASSET_MB = 100
CHUNK_SIZE = 512 * 1024

VIDEO_PATTERN = re.compile(
    r"\.(mp4|webm)(?:\?|$)", re.I
)


def memory_available_mb():
    root = Path("/sys/fs/cgroup")

    try:
        maximum = (root / "memory.max").read_text().strip()

        if maximum != "max":
            current = int(
                (root / "memory.current").read_text().strip()
            )

            inactive = 0
            stat = root / "memory.stat"

            if stat.exists():
                for line in stat.read_text().splitlines():
                    if line.startswith("inactive_file "):
                        inactive = int(line.split()[1])
                        break

            available = int(maximum) - max(0, current - inactive)
            return max(0, available // (1024 * 1024))
    except (OSError, ValueError):
        pass

    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass

    return None


def check_resources(folder, browser=False):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    free_disk = shutil.disk_usage(folder).free // (1024 * 1024)

    if free_disk < MIN_FREE_DISK_MB:
        raise RuntimeError(
            f"Low disk space: {free_disk} MB free. "
            f"Need at least {MIN_FREE_DISK_MB} MB."
        )

    if browser:
        ram = memory_available_mb()

        if ram is not None and ram < MIN_FREE_RAM_MB:
            raise RuntimeError(
                f"Low available RAM: {ram} MB. "
                "Browser not started to avoid VPS OOM."
            )


def ensure_disk_reserve(folder):
    free = shutil.disk_usage(folder).free // (1024 * 1024)

    if free < DISK_RESERVE_MB:
        raise RuntimeError(
            "Low disk space; download paused safely. "
            f"Only {free} MB free."
        )


def is_video_url(url):
    return (
        "/api/public/v/" in url
        or bool(VIDEO_PATTERN.search(url))
    )


def count_board_events(value):
    count = 0
    stack = [(value, 0)]

    while stack:
        item, depth = stack.pop()

        if depth > 50:
            continue

        if isinstance(item, list):
            stack.extend((x, depth + 1) for x in item)

        elif isinstance(item, dict):
            if "plugin" in item and "p_time" in item:
                count += 1

            stack.extend(
                (x, depth + 1) for x in item.values()
            )

    return count


def walk_urls(value):
    found = set()
    stack = [(value, 0)]

    while stack:
        item, depth = stack.pop()

        if depth > 50:
            continue

        if isinstance(item, list):
            stack.extend((x, depth + 1) for x in item)

        elif isinstance(item, dict):
            url = item.get("u")

            if isinstance(url, str) and url.startswith("https://"):
                found.add(url)

            stack.extend(
                (x, depth + 1) for x in item.values()
            )

    return sorted(found)


def choose_board(folder, candidates):
    main = folder / "board.json"
    paths = list(candidates)

    if main.exists():
        paths.append(main)

    best_path = None
    best_count = 0
    best_urls = []

    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            count = count_board_events(data)

            if count > best_count:
                best_path = path
                best_count = count
                best_urls = walk_urls(data)

            del data

        except (OSError, ValueError, TypeError):
            continue

    if best_path is None:
        raise RuntimeError(
            "Board JSON not received or invalid."
        )

    if best_path != main:
        best_path.replace(main)

    for path in candidates:
        if path != main:
            path.unlink(missing_ok=True)

    return best_count, best_urls


def cookies_for(url, browser_cookies):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    result = {}

    for cookie in browser_cookies:
        domain = cookie.get("domain", "").lstrip(".").lower()
        path = cookie.get("path", "/")

        if not domain:
            continue

        if host != domain and not host.endswith("." + domain):
            continue

        if not parsed.path.startswith(path):
            continue

        if cookie.get("secure") and parsed.scheme != "https":
            continue

        result[cookie["name"]] = cookie["value"]

    return result


class Progress:
    def __init__(self, callback):
        self.callback = callback
        self.stage = None
        self.last_time = 0
        self.last_percent = -1

    async def send(self, stage, percent=0, force=False):
        percent = max(0, min(100, float(percent)))
        now = time.monotonic()

        changed = stage != self.stage

        if (
            not force
            and not changed
            and now - self.last_time < 4
            and abs(percent - self.last_percent) < 5
        ):
            return

        self.stage = stage
        self.last_time = now
        self.last_percent = percent

        print(
            f"[DOWNLOAD] {stage}: {percent:.1f}%",
            flush=True,
        )

        if self.callback:
            try:
                await asyncio.wait_for(
                    self.callback(stage, percent),
                    timeout=8,
                )
            except Exception as e:
                print(
                    "Progress callback:",
                    type(e).__name__,
                    flush=True,
                )


async def close_browser(pw, context):
    # Avoid indefinitely hanging while disposing
    # Playwright request contexts.
    try:
        await asyncio.wait_for(
            context.close(),
            timeout=12,
        )
    except Exception as e:
        print(
            "Browser close:",
            type(e).__name__,
            flush=True,
        )

    try:
        await asyncio.wait_for(
            pw.stop(),
            timeout=8,
        )
    except Exception:
        pass


async def capture_sources(
    profile,
    folder,
    lecture,
    progress,
):
    check_resources(folder, browser=True)

    pw, context = await start_browser(profile)

    candidates = []
    board_tasks = set()
    video_urls = []
    board_number = 0
    user_agent = None
    referer = BASE + "/"
    cookies = []

    async def save_board(response, path):
        try:
            raw = await response.body()

            if not raw:
                return

            if len(raw) > 80 * 1024 * 1024:
                raise RuntimeError(
                    "Board response too large"
                )

            await asyncio.to_thread(
                path.write_bytes,
                raw,
            )
            candidates.append(path)

        except Exception as e:
            print(
                "Board capture:",
                type(e).__name__,
                flush=True,
            )

    def on_request(request):
        if is_video_url(request.url):
            if request.url not in video_urls:
                video_urls.append(request.url)

    def on_response(response):
        nonlocal board_number

        if "/api/public/board/" not in response.url:
            return

        board_number += 1
        path = folder / (
            f".board_capture_{board_number}.json"
        )

        task = asyncio.create_task(
            save_board(response, path)
        )

        board_tasks.add(task)
        task.add_done_callback(board_tasks.discard)

    async def route_handler(route):
        request = route.request

        try:
            # Stop Chromium from transferring a second
            # copy of the large lecture video.
            if is_video_url(request.url):
                if request.url not in video_urls:
                    video_urls.append(request.url)

                await route.abort()
                return

            # Reduce browser memory and bandwidth.
            if request.resource_type in ("image", "font"):
                await route.abort()
                return

            await route.continue_()

        except Exception:
            pass

    try:
        context.on("request", on_request)
        context.on("response", on_response)

        await context.route(
            "**/*",
            route_handler,
        )

        page = (
            context.pages[0]
            if context.pages
            else await context.new_page()
        )

        await progress.send("Preparing", 0, force=True)

        await load(
            page,
            lecture["watch_url"],
        )

        await progress.send("Loading player", 0, force=True)

        try:
            await page.locator("video").first.evaluate(
                """v => {
                    v.preload = 'metadata';
                    v.load();
                }""",
                timeout=6000,
            )
        except Exception:
            pass

        # Wait for metadata only, not the entire video.
        for attempt in range(60):
            if video_urls and (
                candidates
                or (folder / "board.json").exists()
            ):
                break

            if attempt % 10 == 0:
                print(
                    "[DOWNLOAD] Waiting for media metadata:",
                    attempt // 2,
                    "seconds",
                    flush=True,
                )

            await asyncio.sleep(0.5)

        if board_tasks:
            await asyncio.wait(
                board_tasks,
                timeout=10,
            )

        if not video_urls:
            source = await page.evaluate(
                """() =>
                    document.querySelector('video')?.currentSrc
                    || document.querySelector('video')?.src
                    || null
                """
            )

            if (
                source
                and source.startswith(("https://", "http://"))
            ):
                video_urls.append(source)

        user_agent = await page.evaluate(
            "navigator.userAgent"
        )

        referer = page.url
        cookies = await context.cookies()

    finally:
        # Critical: browser closes BEFORE video download.
        await close_browser(pw, context)

    if not video_urls:
        raise RuntimeError(
            "Video source not found on lecture page."
        )

    if not candidates and not (folder / "board.json").exists():
        raise RuntimeError(
            "Board source not found on lecture page."
        )

    return (
        video_urls[-1],
        candidates,
        user_agent,
        referer,
        cookies,
    )


async def download_video(
    url,
    target,
    headers,
    progress,
    browser_cookies=None,
):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and target.stat().st_size > 0:
        await progress.send("Video", 100, force=True)
        return

    partial = Path(str(target) + ".part")
    meta = Path(str(target) + ".part.sha256")

    signature = hashlib.sha256(
        url.encode("utf-8")
    ).hexdigest()

    # Do not combine fragments from different signed URLs.
    if partial.exists():
        previous = (
            meta.read_text().strip()
            if meta.exists()
            else ""
        )

        if previous != signature:
            partial.unlink(missing_ok=True)

    meta.write_text(signature)

    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=30,
        sock_read=90,
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=aiohttp.TCPConnector(limit=2),
    ) as session:

        for attempt in range(4):
            start = (
                partial.stat().st_size
                if partial.exists()
                else 0
            )

            request_headers = dict(headers)

            if start:
                request_headers["Range"] = f"bytes={start}-"

            try:
                async with session.get(
                    url,
                    headers=request_headers,
                    cookies=cookies_for(
                        url,
                        browser_cookies or [],
                    ),
                ) as response:

                    if response.status == 416:
                        raise RuntimeError(
                            "Resume range rejected"
                        )

                    if response.status not in (200, 206):
                        raise RuntimeError(
                            f"Video HTTP {response.status}"
                        )

                    content_type = response.headers.get(
                        "Content-Type",
                        "",
                    ).lower()

                    if "text/html" in content_type:
                        raise RuntimeError(
                            "HTML returned instead of video"
                        )

                    resume = (
                        start > 0
                        and response.status == 206
                    )

                    content_range = response.headers.get(
                        "Content-Range",
                        "",
                    )

                    match = re.fullmatch(
                        r"bytes\s+(\d+)-(\d+)/(\d+|\*)",
                        content_range.strip(),
                    )

                    if resume and (
                        not match
                        or int(match.group(1)) != start
                    ):
                        partial.unlink(missing_ok=True)
                        raise RuntimeError(
                            "Invalid resume response"
                        )

                    if response.status == 206 and not resume:
                        raise RuntimeError(
                            "Unexpected partial response"
                        )

                    offset = start if resume else 0

                    length = response.headers.get(
                        "Content-Length"
                    )

                    if match and match.group(3) != "*":
                        expected = int(match.group(3))
                    elif length:
                        expected = offset + int(length)
                    else:
                        expected = None

                    received = offset
                    last_disk_check = offset
                    mode = "ab" if resume else "wb"

                    with partial.open(mode) as output:
                        async for chunk in response.content.iter_chunked(
                            CHUNK_SIZE
                        ):
                            if not chunk:
                                continue

                            output.write(chunk)
                            received += len(chunk)

                            if received - last_disk_check >= 16 * 1024 * 1024:
                                ensure_disk_reserve(target.parent)
                                last_disk_check = received

                            if expected:
                                await progress.send(
                                    "Video",
                                    100 * received / expected,
                                )
                            else:
                                await progress.send("Video", 0)

                actual = partial.stat().st_size

                if expected is not None and actual != expected:
                    raise RuntimeError(
                        f"Incomplete video: {actual}/{expected}"
                    )

                if actual == 0:
                    raise RuntimeError("Empty video")

                partial.replace(target)
                meta.unlink(missing_ok=True)

                await progress.send(
                    "Video",
                    100,
                    force=True,
                )
                return

            except asyncio.CancelledError:
                raise

            except Exception as e:
                print(
                    f"Video attempt {attempt + 1}:",
                    type(e).__name__,
                    str(e)[:100],
                    flush=True,
                )

                if isinstance(e, RuntimeError) and (
                    "Resume range rejected" in str(e)
                    or "Invalid resume response" in str(e)
                ):
                    partial.unlink(missing_ok=True)

                if attempt == 3:
                    raise

                await asyncio.sleep(3 * (attempt + 1))


async def download_asset(
    session,
    url,
    destination,
    headers,
    cookies,
):
    partial = Path(str(destination) + ".part")

    for attempt in range(3):
        try:
            partial.unlink(missing_ok=True)

            async with session.get(
                url,
                headers=headers,
                cookies=cookies_for(url, cookies),
            ) as response:

                if response.status != 200:
                    raise RuntimeError(
                        f"Asset HTTP {response.status}"
                    )

                length = response.content_length

                if length and length > MAX_ASSET_MB * 1024 * 1024:
                    raise RuntimeError(
                        "Asset exceeds safety size limit"
                    )

                received = 0
                first = True

                with partial.open("wb") as output:
                    async for chunk in response.content.iter_chunked(
                        CHUNK_SIZE
                    ):
                        if first:
                            first = False

                            if chunk[:200].lstrip().lower().startswith(
                                (b"<!doctype", b"<html")
                            ):
                                raise RuntimeError(
                                    "HTML returned instead of asset"
                                )

                        received += len(chunk)

                        if received > MAX_ASSET_MB * 1024 * 1024:
                            raise RuntimeError(
                                "Asset exceeds safety size limit"
                            )

                        if not chunk:
                            continue

                        output.write(chunk)

            if received == 0:
                raise RuntimeError("Empty asset")

            partial.replace(destination)
            return

        except asyncio.CancelledError:
            raise

        except Exception:
            partial.unlink(missing_ok=True)

            if attempt == 2:
                raise

            await asyncio.sleep(2)


async def download_lecture(
    profile,
    root,
    subject,
    lecture,
    key,
    progress=None,
):
    async with DOWNLOAD_LOCK:
        folder = Path(root) / hashlib.sha256(
            key.encode("utf-8")
        ).hexdigest()[:20]

        folder.mkdir(parents=True, exist_ok=True)

        reporter = Progress(progress)

        await reporter.send(
            "Checking resources",
            0,
            force=True,
        )

        check_resources(folder, browser=True)

        (
            video_url,
            candidates,
            user_agent,
            referer,
            browser_cookies,
        ) = await capture_sources(
            profile,
            folder,
            lecture,
            reporter,
        )

        # Browser is already closed here.
        await reporter.send(
            "Board",
            0,
            force=True,
        )

        count, urls = await asyncio.to_thread(
            choose_board,
            folder,
            candidates,
        )

        await reporter.send(
            "Board",
            100,
            force=True,
        )

        for field in (
            "annotated_pdf",
            "plain_pdf",
        ):
            value = lecture.get(field)

            if value and value not in urls:
                urls.append(value)

        video_extension = (
            ".mp4"
            if urlparse(video_url).path.lower().endswith(".mp4")
            else ".webm"
        )

        headers = {
            "User-Agent": user_agent or "Mozilla/5.0",
            "Referer": referer,
            "Accept-Encoding": "identity",
        }

        await reporter.send(
            "Video",
            0,
            force=True,
        )

        await download_video(
            video_url,
            folder / ("video" + video_extension),
            headers,
            reporter,
            browser_cookies,
        )

        assets_dir = folder / "assets"
        assets_dir.mkdir(exist_ok=True)

        saved = []
        errors = []

        timeout = aiohttp.ClientTimeout(
            total=None,
            sock_connect=30,
            sock_read=90,
        )

        await reporter.send(
            "Assets",
            0,
            force=True,
        )

        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(limit=2),
        ) as session:

            for index, url in enumerate(urls, 1):
                suffix = Path(
                    urlparse(url).path
                ).suffix.lower()

                if suffix not in {
                    ".png", ".jpg", ".jpeg",
                    ".webp", ".pdf",
                }:
                    suffix = ".bin"

                filename = f"asset_{index:03d}{suffix}"
                destination = assets_dir / filename

                url_hash = hashlib.sha256(
                    url.encode("utf-8")
                ).hexdigest()

                try:
                    ensure_disk_reserve(folder)

                    await download_asset(
                        session,
                        url,
                        destination,
                        headers,
                        browser_cookies,
                    )

                    saved.append({
                        "filename": filename,
                        "url_sha256": url_hash,
                    })

                except asyncio.CancelledError:
                    raise

                except Exception as e:
                    errors.append({
                        "asset": index,
                        "error_type": type(e).__name__,
                    })

                await reporter.send(
                    "Assets",
                    index * 100 / max(len(urls), 1),
                )

        if errors:
            raise RuntimeError(
                f"{len(errors)} assets failed. "
                "Completed video and board kept for retry."
            )

        asset_index = folder / "asset_index.json"
        asset_index.write_text(
            json.dumps(saved, indent=2),
            encoding="utf-8",
        )

        manifest = {
            "lecture_key": key,
            "batch_uid": subject["uid"],
            "class_number": lecture["class_number"],
            "title": lecture["title"],
            "video": "video" + video_extension,
            "board_events": count,
            "assets_saved": len(saved),
        }

        (folder / "manifest.json").write_text(
            json.dumps(
                manifest,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        await reporter.send(
            "Packaging",
            0,
            force=True,
        )

        with zipfile.ZipFile(
            folder / "slides.zip",
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:

            for file in sorted(assets_dir.iterdir()):
                if file.is_file():
                    archive.write(
                        file,
                        "slides/" + file.name,
                    )

            archive.write(
                asset_index,
                "asset_index.json",
            )

        await reporter.send(
            "Packaging",
            100,
            force=True,
        )

        return folder, manifest
