import re
import json
import asyncio
import zipfile
import hashlib
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from catalog import BASE, open_browser, close_browser, navigate


def find_board_assets(data):
    urls = set()

    def walk(value, depth=0):
        if depth > 50:
            return

        if isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

        elif isinstance(value, dict):
            url = value.get("u")

            if isinstance(url, str) and url.startswith("https://"):
                urls.add(url)

            for item in value.values():
                walk(item, depth + 1)

    walk(data)
    return sorted(urls)


def board_event_count(data):
    count = 0

    def walk(value, depth=0):
        nonlocal count

        if depth > 50:
            return

        if isinstance(value, list):
            for item in value:
                walk(item, depth + 1)

        elif isinstance(value, dict):
            if "plugin" in value and "p_time" in value:
                count += 1

            for item in value.values():
                walk(item, depth + 1)

    walk(data)
    return count


async def fetch_video(url, target, headers, progress=None):
    if target.exists() and target.stat().st_size > 0:
        return

    part = Path(str(target) + ".part")

    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=60,
        sock_read=180,
    )

    for attempt in range(3):
        offset = part.stat().st_size if part.exists() else 0
        h = dict(headers)

        if offset:
            h["Range"] = f"bytes={offset}-"

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=h) as response:
                    if response.status not in (200, 206):
                        raise RuntimeError(
                            f"Video HTTP {response.status}"
                        )

                    append = (
                        offset > 0 and response.status == 206
                    )

                    start = offset if append else 0

                    content_range = response.headers.get(
                        "Content-Range", ""
                    )

                    match = re.search(r"/(\d+)$", content_range)

                    length = response.headers.get("Content-Length")

                    expected = (
                        int(match.group(1))
                        if match else
                        start + int(length)
                        if length else None
                    )

                    received = start

                    with open(part, "ab" if append else "wb") as output:
                        async for chunk in response.content.iter_chunked(262144):
                            output.write(chunk)
                            received += len(chunk)

                            if progress and expected:
                                await progress(
                                    "video",
                                    received * 100 / expected,
                                )

            if expected and part.stat().st_size != expected:
                raise RuntimeError("Incomplete video download")

            part.rename(target)
            return

        except Exception:
            if attempt == 2:
                raise

            await asyncio.sleep(3)


async def download_lecture(
    subject,
    lesson,
    lecture_key,
    profile,
    download_root,
    progress=None,
):
    folder_name = hashlib.sha256(
        lecture_key.encode()
    ).hexdigest()[:20]

    folder = Path(download_root) / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    pw, context = await open_browser(profile)

    try:
        page = context.pages[0] if context.pages else await context.new_page()

        video_url = None
        board = None
        pending = []

        def on_response(response):
            nonlocal video_url, board

            url = response.url

            if (
                "/api/public/v/" in url
                or re.search(r"\.(?:webm|mp4)(?:\?|$)", url, re.I)
            ):
                video_url = url

            if "/api/public/board/" in url:

                async def capture_board():
                    nonlocal board

                    try:
                        data = await response.json()

                        if isinstance(data, list):
                            if board is None or len(data) > len(board):
                                board = data

                    except Exception:
                        pass

                pending.append(
                    asyncio.create_task(capture_board())
                )

        context.on("response", on_response)

        await navigate(page, lesson["watch_url"])
        await page.wait_for_timeout(8000)

        try:
            await page.locator("video").first.evaluate(
                "v => { v.preload='auto'; v.load(); }"
            )
        except Exception:
            pass

        await page.wait_for_timeout(3500)

        if pending:
            await asyncio.gather(
                *pending,
                return_exceptions=True,
            )

        if not video_url:
            video_url = await page.evaluate("""
                () => document.querySelector("video")?.currentSrc
                   || document.querySelector("video")?.src
                   || null
            """)

        if not video_url or board is None:
            raise RuntimeError(
                "Video URL or board JSON could not be captured."
            )

        events = board_event_count(board)

        if not events:
            raise RuntimeError(
                "Board JSON contains no recognized events."
            )

        (folder / "board.json").write_text(
            json.dumps(board, ensure_ascii=False),
            encoding="utf-8",
        )

        suffix = (
            ".mp4"
            if urlparse(video_url).path.lower().endswith(".mp4")
            else ".webm"
        )

        cookies = await context.cookies(video_url)

        headers = {
            "User-Agent": await page.evaluate(
                "navigator.userAgent"
            ),
            "Referer": BASE + "/",
            "Accept-Encoding": "identity",
        }

        if cookies:
            headers["Cookie"] = "; ".join(
                f"{cookie['name']}={cookie['value']}"
                for cookie in cookies
            )

        await fetch_video(
            video_url,
            folder / ("video" + suffix),
            headers,
            progress,
        )

        assets = find_board_assets(board)

        for key in ("annotated_pdf", "plain_pdf"):
            url = lesson.get(key)

            if url and url not in assets:
                assets.append(url)

        asset_dir = folder / "assets"
        asset_dir.mkdir(exist_ok=True)

        saved = []
        failures = []

        for i, url in enumerate(assets, 1):
            ext = Path(urlparse(url).path).suffix.lower()

            if ext not in (
                ".jpg", ".jpeg", ".png", ".webp", ".pdf"
            ):
                ext = ".bin"

            name = f"asset_{i:03d}{ext}"
            destination = asset_dir / name

            try:
                response = await context.request.get(
                    url,
                    timeout=120000,
                )

                if not response.ok:
                    raise RuntimeError(
                        f"Asset HTTP {response.status}"
                    )

                data = await response.body()

                if not data:
                    raise RuntimeError("Empty asset")

                if data[:200].lstrip().lower().startswith(
                    (b"<!doctype", b"<html")
                ):
                    raise RuntimeError(
                        "Received HTML instead of asset"
                    )

                destination.write_bytes(data)

                saved.append({
                    "file": name,
                    "source_sha256": hashlib.sha256(
                        url.encode()
                    ).hexdigest(),
                })

            except Exception as error:
                failures.append({
                    "number": i,
                    "error": str(error)[:200],
                })

            if progress:
                await progress(
                    "assets",
                    i * 100 / max(len(assets), 1),
                )

        (folder / "asset_index.json").write_text(
            json.dumps(saved, indent=2),
            encoding="utf-8",
        )

        manifest = {
            "lecture_key": lecture_key,
            "batch_uid": subject["uid"],
            "class_number": lesson["class_number"],
            "title": lesson["title"],
            "board_events": events,
            "video": "video" + suffix,
            "assets_found": len(assets),
            "assets_saved": len(saved),
            "asset_failures": failures,
        }

        (folder / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )

        if failures:
            raise RuntimeError(
                f"{len(failures)} assets failed. "
                "Partial lecture kept locally."
            )

        with zipfile.ZipFile(
            folder / "slides.zip",
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:

            for file in sorted(asset_dir.iterdir()):
                if file.is_file():
                    archive.write(
                        file,
                        "slides/" + file.name,
                    )

            archive.write(
                folder / "asset_index.json",
                "asset_index.json",
            )

        return folder, manifest

    finally:
        await close_browser(pw, context)
