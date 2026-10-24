import asyncio
import hashlib
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from catalog import (
    BASE, start_browser, stop_browser, load
)


def walk_urls(value):
    found = set()

    def walk(x, depth=0):
        if depth > 50:
            return

        if isinstance(x, list):
            for item in x:
                walk(item, depth + 1)

        elif isinstance(x, dict):
            url = x.get("u")

            if isinstance(url, str) and url.startswith("https://"):
                found.add(url)

            for item in x.values():
                walk(item, depth + 1)

    walk(value)
    return sorted(found)


def count_board_events(value):
    count = 0

    def walk(x, depth=0):
        nonlocal count

        if depth > 50:
            return

        if isinstance(x, list):
            for item in x:
                walk(item, depth + 1)

        elif isinstance(x, dict):
            if "plugin" in x and "p_time" in x:
                count += 1

            for item in x.values():
                walk(item, depth + 1)

    walk(value)
    return count


async def download_video(url, target, headers, progress):
    partial = Path(str(target) + ".part")

    if target.exists() and target.stat().st_size > 0:
        return

    timeout = aiohttp.ClientTimeout(
        total=None, sock_connect=60, sock_read=180
    )

    for attempt in range(3):
        start = partial.stat().st_size if partial.exists() else 0
        h = dict(headers)

        if start:
            h["Range"] = f"bytes={start}-"

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=h) as response:
                    if response.status not in (200, 206):
                        raise RuntimeError(
                            f"Video HTTP {response.status}"
                        )

                    append = start > 0 and response.status == 206
                    offset = start if append else 0

                    length = response.headers.get("Content-Length")
                    cr = response.headers.get("Content-Range", "")
                    m = re.search(r"/(\d+)$", cr)

                    expected = (
                        int(m.group(1)) if m
                        else offset + int(length) if length
                        else None
                    )

                    received = offset

                    with open(
                        partial, "ab" if append else "wb"
                    ) as output:
                        async for chunk in response.content.iter_chunked(262144):
                            output.write(chunk)
                            received += len(chunk)

                            if expected and progress:
                                await progress(
                                    "Video",
                                    received * 100 / expected,
                                )

            if expected and partial.stat().st_size != expected:
                raise RuntimeError("Incomplete video download")

            partial.replace(target)
            return

        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(3)


async def download_lecture(
    profile, root, subject, lecture, key, progress=None
):
    folder = Path(root) / hashlib.sha256(
        key.encode()
    ).hexdigest()[:20]

    folder.mkdir(parents=True, exist_ok=True)

    pw, context = await start_browser(profile)

    try:
        page = context.pages[0] if context.pages else await context.new_page()

        video_url = None
        board = None
        pending = []

        def received(response):
            nonlocal video_url, board

            url = response.url

            if (
                "/api/public/v/" in url
                or re.search(r"\.(mp4|webm)(\?|$)", url, re.I)
            ):
                video_url = url

            if "/api/public/board/" in url:
                async def get_board():
                    nonlocal board

                    try:
                        data = await response.json()

                        if isinstance(data, list):
                            if board is None or len(data) > len(board):
                                board = data
                    except Exception:
                        pass

                pending.append(asyncio.create_task(get_board()))

        context.on("response", received)

        await load(page, lecture["watch_url"])
        await page.wait_for_timeout(8500)

        try:
            await page.locator("video").first.evaluate(
                "v => { v.preload='auto'; v.load(); }"
            )
        except Exception:
            pass

        await page.wait_for_timeout(2500)

        if pending:
            await asyncio.gather(
                *pending, return_exceptions=True
            )

        if not video_url:
            video_url = await page.evaluate("""
            () => document.querySelector('video')?.currentSrc
               || document.querySelector('video')?.src
               || null
            """)

        if not video_url or board is None:
            raise RuntimeError(
                "Video or board unavailable on lecture page"
            )

        count = count_board_events(board)

        if count == 0:
            raise RuntimeError(
                "Board JSON has no recognized events"
            )

        (folder / "board.json").write_text(
            json.dumps(board, ensure_ascii=False),
            encoding="utf-8",
        )

        ext = (
            ".mp4" if urlparse(video_url).path.endswith(".mp4")
            else ".webm"
        )

        cookies = await context.cookies(video_url)

        headers = {
            "User-Agent": await page.evaluate("navigator.userAgent"),
            "Referer": BASE + "/",
            "Accept-Encoding": "identity",
        }

        if cookies:
            headers["Cookie"] = "; ".join(
                c["name"] + "=" + c["value"] for c in cookies
            )

        await download_video(
            video_url,
            folder / ("video" + ext),
            headers,
            progress,
        )

        urls = walk_urls(board)

        for field in ("annotated_pdf", "plain_pdf"):
            value = lecture.get(field)

            if value and value not in urls:
                urls.append(value)

        assets_dir = folder / "assets"
        assets_dir.mkdir(exist_ok=True)

        saved = []
        errors = []

        for index, url in enumerate(urls, 1):
            ext_asset = Path(urlparse(url).path).suffix.lower()

            if ext_asset not in (
                ".png", ".jpg", ".jpeg", ".webp", ".pdf"
            ):
                ext_asset = ".bin"

            name = f"asset_{index:03d}{ext_asset}"

            try:
                response = await context.request.get(
                    url, timeout=120000
                )

                if not response.ok:
                    raise RuntimeError(
                        f"HTTP {response.status}"
                    )

                content = await response.body()

                if not content:
                    raise RuntimeError("Empty asset")

                if content[:200].lstrip().lower().startswith(
                    (b"<!doctype", b"<html")
                ):
                    raise RuntimeError("HTML returned")

                (assets_dir / name).write_bytes(content)

                saved.append({
                    "filename": name,
                    "url_sha256": hashlib.sha256(
                        url.encode()
                    ).hexdigest(),
                })

            except Exception as error:
                errors.append({
                    "asset": index,
                    "error_type": type(error).__name__,
                })

            if progress:
                await progress(
                    "Assets",
                    index * 100 / max(len(urls), 1),
                )

        if errors:
            raise RuntimeError(
                f"{len(errors)} assets failed; "
                "partial files saved for retry"
            )

        (folder / "asset_index.json").write_text(
            json.dumps(saved, indent=2),
            encoding="utf-8",
        )

        manifest = {
            "lecture_key": key,
            "batch_uid": subject["uid"],
            "class_number": lecture["class_number"],
            "title": lecture["title"],
            "video": "video" + ext,
            "board_events": count,
            "assets_saved": len(saved),
        }

        (folder / "manifest.json").write_text(
            json.dumps(manifest, indent=2),
            encoding="utf-8",
        )

        with zipfile.ZipFile(
            folder / "slides.zip", "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:
            for file in assets_dir.iterdir():
                if file.is_file():
                    archive.write(
                        file, "slides/" + file.name
                    )

            archive.write(
                folder / "asset_index.json",
                "asset_index.json",
            )

        return folder, manifest

    finally:
        await stop_browser(pw, context)