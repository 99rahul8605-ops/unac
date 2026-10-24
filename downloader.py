
import asyncio
import hashlib
import json
import re
import time
import zipfile

from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from catalog import (
    BASE,
    start_browser,
    stop_browser,
    load,
)


def walk_urls(value):
    found = set()

    def walk(item, depth=0):
        if depth > 50:
            return

        if isinstance(item, list):
            for value in item:
                walk(value, depth + 1)

        elif isinstance(item, dict):
            url = item.get("u")

            if (
                isinstance(url, str)
                and url.startswith("https://")
            ):
                found.add(url)

            for value in item.values():
                walk(value, depth + 1)

    walk(value)

    return sorted(found)


def count_board_events(value):
    count = 0

    def walk(item, depth=0):
        nonlocal count

        if depth > 50:
            return

        if isinstance(item, list):
            for value in item:
                walk(value, depth + 1)

        elif isinstance(item, dict):
            if "plugin" in item and "p_time" in item:
                count += 1

            for value in item.values():
                walk(value, depth + 1)

    walk(value)

    return count


async def safe_progress(callback, stage, percent):
    if callback is None:
        return

    try:
        await callback(
            stage,
            min(100, max(0, percent)),
        )
    except Exception as error:
        print(
            "Progress notification failed:",
            type(error).__name__,
        )


async def download_video(
    url,
    target,
    headers,
    progress,
):
    target = Path(target)
    partial = Path(str(target) + ".part")

    if target.exists() and target.stat().st_size > 0:
        await safe_progress(progress, "Video", 100)
        return

    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=60,
        sock_read=180,
    )

    last_update = 0.0
    last_percent = -1

    for attempt in range(4):
        start = (
            partial.stat().st_size
            if partial.exists()
            else 0
        )

        request_headers = dict(headers)

        if start:
            request_headers["Range"] = (
                f"bytes={start}-"
            )

        try:
            async with aiohttp.ClientSession(
                timeout=timeout
            ) as session:

                async with session.get(
                    url,
                    headers=request_headers,
                    allow_redirects=True,
                ) as response:

                    if response.status == 416:
                        if partial.exists():
                            partial.unlink()
                        raise RuntimeError(
                            "Invalid saved range; restarting"
                        )

                    if response.status not in (200, 206):
                        raise RuntimeError(
                            f"Video HTTP {response.status}"
                        )

                    append = (
                        start > 0
                        and response.status == 206
                    )

                    content_range = response.headers.get(
                        "Content-Range",
                        "",
                    )

                    match = re.search(
                        r"bytes\s+(\d+)-(\d+)/(\d+|\*)",
                        content_range,
                    )

                    if append:
                        if (
                            not match
                            or int(match.group(1)) != start
                        ):
                            if partial.exists():
                                partial.unlink()

                            raise RuntimeError(
                                "Invalid resume response"
                            )

                    offset = start if append else 0

                    length_header = response.headers.get(
                        "Content-Length"
                    )

                    content_length = (
                        int(length_header)
                        if length_header
                        else None
                    )

                    if (
                        match
                        and match.group(3) != "*"
                    ):
                        expected = int(match.group(3))
                    elif content_length is not None:
                        expected = offset + content_length
                    else:
                        expected = None

                    received = offset

                    mode = "ab" if append else "wb"

                    with partial.open(mode) as output:
                        async for chunk in response.content.iter_chunked(
                            1024 * 1024
                        ):
                            if not chunk:
                                continue

                            output.write(chunk)
                            received += len(chunk)

                            now = time.monotonic()

                            percent = (
                                received * 100 / expected
                                if expected
                                else 0
                            )

                            # Avoid thousands of Telegram
                            # progress messages for large videos.
                            if (
                                expected
                                and (
                                    now - last_update >= 3
                                    or percent - last_percent >= 5
                                )
                            ):
                                await safe_progress(
                                    progress,
                                    "Video",
                                    percent,
                                )

                                last_update = now
                                last_percent = percent

            size = partial.stat().st_size

            if not size:
                raise RuntimeError(
                    "Empty video download"
                )

            if expected is not None and size != expected:
                raise RuntimeError(
                    f"Incomplete video: {size}/{expected}"
                )

            partial.replace(target)

            await safe_progress(
                progress,
                "Video",
                100,
            )
            return

        except asyncio.CancelledError:
            raise

        except Exception:
            if attempt == 3:
                raise

            await asyncio.sleep(3)


async def download_lecture(
    profile,
    root,
    subject,
    lecture,
    key,
    progress=None,
):
    folder = Path(root) / hashlib.sha256(
        key.encode("utf-8")
    ).hexdigest()[:20]

    folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    pw, context = await start_browser(profile)

    try:
        page = (
            context.pages[0]
            if context.pages
            else await context.new_page()
        )

        video_url = None
        board = None
        tasks = set()

        async def parse_board(response):
            nonlocal board

            try:
                data = await response.json()

                events = count_board_events(data)

                if events > 0:
                    if (
                        board is None
                        or events > count_board_events(board)
                    ):
                        board = data

            except Exception:
                pass

        def received(response):
            nonlocal video_url

            url = response.url

            if (
                "/api/public/v/" in url
                or re.search(
                    r"\.(mp4|webm)(\?|$)",
                    url,
                    re.I,
                )
            ):
                video_url = url

            if "/api/public/board/" in url:
                task = asyncio.create_task(
                    parse_board(response)
                )

                tasks.add(task)
                task.add_done_callback(tasks.discard)

        context.on("response", received)

        # The lecture URL comes from the website's
        # Watch class link discovered by catalog.py.
        await load(
            page,
            lecture["watch_url"],
        )

        await page.wait_for_timeout(3500)

        try:
            await page.locator("video").first.evaluate(
                """video => {
                    video.preload = 'auto';
                    video.load();
                }"""
            )
        except Exception:
            pass

        # Give video and board requests an opportunity
        # to finish instead of relying only on one delay.
        for _ in range(40):
            if video_url and board is not None:
                break

            await asyncio.sleep(0.5)

        if tasks:
            await asyncio.gather(
                *list(tasks),
                return_exceptions=True,
            )

        if not video_url:
            video_url = await page.evaluate(
                """() => {
                    const video = document.querySelector('video');

                    return video?.currentSrc
                        || video?.src
                        || null;
                }"""
            )

        if not video_url:
            raise RuntimeError(
                "Video URL unavailable on lecture page"
            )

        if board is None:
            raise RuntimeError(
                "Board JSON unavailable on lecture page"
            )

        event_count = count_board_events(board)

        if event_count == 0:
            raise RuntimeError(
                "Board JSON has no recognized events"
            )

        (folder / "board.json").write_text(
            json.dumps(
                board,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        extension = (
            ".mp4"
            if urlparse(video_url).path.lower().endswith(
                ".mp4"
            )
            else ".webm"
        )

        cookies = await context.cookies(
            video_url
        )

        headers = {
            "User-Agent": await page.evaluate(
                "navigator.userAgent"
            ),
            "Referer": page.url,
            "Accept-Encoding": "identity",
        }

        if cookies:
            headers["Cookie"] = "; ".join(
                cookie["name"]
                + "="
                + cookie["value"]
                for cookie in cookies
            )

        await download_video(
            video_url,
            folder / ("video" + extension),
            headers,
            progress,
        )

        urls = walk_urls(board)

        for field in (
            "annotated_pdf",
            "plain_pdf",
        ):
            url = lecture.get(field)

            if url and url not in urls:
                urls.append(url)

        assets_dir = folder / "assets"
        assets_dir.mkdir(exist_ok=True)

        saved = []
        errors = []

        total_assets = len(urls)

        for index, url in enumerate(urls, 1):
            asset_extension = Path(
                urlparse(url).path
            ).suffix.lower()

            if asset_extension not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".pdf",
            }:
                asset_extension = ".bin"

            filename = (
                f"asset_{index:03d}"
                + asset_extension
            )

            destination = assets_dir / filename

            try:
                # Reuse a successfully downloaded
                # asset after an interrupted job.
                if (
                    destination.exists()
                    and destination.stat().st_size > 0
                ):
                    content = None

                else:
                    response = await context.request.get(
                        url,
                        headers={
                            "Referer": page.url,
                        },
                        timeout=120000,
                    )

                    if not response.ok:
                        raise RuntimeError(
                            f"Asset HTTP {response.status}"
                        )

                    content = await response.body()

                    if not content:
                        raise RuntimeError(
                            "Empty asset"
                        )

                    if content[:200].lstrip().lower().startswith(
                        (
                            b"<!doctype",
                            b"<html",
                        )
                    ):
                        raise RuntimeError(
                            "HTML returned instead of asset"
                        )

                    destination.write_bytes(content)

                saved.append({
                    "filename": filename,
                    "url_sha256": hashlib.sha256(
                        url.encode("utf-8")
                    ).hexdigest(),
                })

            except asyncio.CancelledError:
                raise

            except Exception as error:
                errors.append({
                    "asset": index,
                    "error_type": type(error).__name__,
                })

            await safe_progress(
                progress,
                "Assets",
                index * 100 / max(total_assets, 1),
            )

        if errors:
            raise RuntimeError(
                f"{len(errors)} assets failed; "
                "partial files saved for retry"
            )

        (folder / "asset_index.json").write_text(
            json.dumps(
                saved,
                indent=2,
            ),
            encoding="utf-8",
        )

        manifest = {
            "lecture_key": key,
            "batch_uid": subject["uid"],
            "class_number": lecture["class_number"],
            "title": lecture["title"],
            "video": "video" + extension,
            "board_events": event_count,
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

        with zipfile.ZipFile(
            folder / "slides.zip",
            "w",
            zipfile.ZIP_DEFLATED,
        ) as archive:

            for file in sorted(
                assets_dir.iterdir()
            ):
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
        await stop_browser(
            pw,
            context,
        )
