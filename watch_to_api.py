
import os
import re
import json
import time
import asyncio
import argparse
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.async_api import async_playwright

ROOT = Path("/root/unac")
STATE = ROOT / ".state"
PROFILE = STATE / "verified_backup"
EXPORTS = ROOT / "exports"

API = "http://127.0.0.1:8091"
API_KEY = os.environ.get("LECTURE_API_KEY", "")

MEDIA_RE = re.compile(
    r"\.(mp4|webm|m4v)(?:[?#]|$)",
    re.I,
)

ASSET_RE = re.compile(
    r"\.(pdf|png|jpg|jpeg|webp)(?:[?#]|$)",
    re.I,
)


async def resolve_lecture(lecture):
    """
    Observe the authorized lecture player and collect
    available direct media/resource URLs.

    Does not bypass verification.
    Does not download the full video in Chromium.
    """

    watch_url = lecture.get("watch_url")

    if not watch_url:
        raise ValueError("Missing watch URL")

    video_urls = []
    board_urls = []
    assets = []
    pending = set()

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(PROFILE),
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        try:
            page = (
                context.pages[0]
                if context.pages
                else await context.new_page()
            )

            def remember(url):
                if not url.startswith(("https://", "http://")):
                    return

                if MEDIA_RE.search(url):
                    if url not in video_urls:
                        video_urls.append(url)

                elif "/api/public/board/" in url:
                    if url not in board_urls:
                        board_urls.append(url)

                elif ASSET_RE.search(url):
                    if url not in assets:
                        assets.append(url)

            def on_request(request):
                remember(request.url)

            def on_response(response):
                remember(response.url)

                content_type = response.headers.get(
                    "content-type", ""
                ).lower().split(";")[0].strip()

                if (
                    content_type.startswith("video/")
                    and response.status in (200, 206)
                    and response.url not in video_urls
                ):
                    video_urls.append(response.url)

            context.on("request", on_request)
            context.on("response", on_response)

            await page.goto(
                watch_url,
                wait_until="domcontentloaded",
                timeout=90000,
            )

            await page.wait_for_timeout(5000)

            last_asset_count = -1
            stable_seconds = 0

            for attempt in range(30):
                verify = page.get_by_text(
                    "Verify you're human",
                    exact=False,
                ).first

                if await verify.is_visible():
                    dialog = page.locator(
                        '[role="dialog"][aria-modal="true"]'
                    ).first

                    video = page.locator("video").first

                    if (
                        await dialog.is_visible()
                        and not await video.is_visible()
                    ):
                        raise RuntimeError(
                            "Website requires manual verification"
                        )

                if attempt == 2:
                    try:
                        await page.locator(
                            "video"
                        ).first.evaluate(
                            """v => {
                              v.preload = 'metadata';
                              v.pause();
                              v.load();
                            }""",
                            timeout=5000,
                        )
                    except Exception:
                        pass

                # SLIDES_SETTLE_WAIT_V1
                # Slide links arrive AFTER video/board. Keep waiting until
                # no new slide link appeared for 6 seconds.
                if video_urls and board_urls:
                    if len(assets) == last_asset_count:
                        stable_seconds += 1
                    else:
                        stable_seconds = 0
                        last_asset_count = len(assets)

                    if stable_seconds >= 6:
                        break

                await asyncio.sleep(1)

            # Last fallback: inspect HTML5 video element.
            try:
                src = await page.evaluate(
                    """() => {
                      const v = document.querySelector('video');
                      return v?.currentSrc || v?.src || '';
                    }"""
                )
                remember(src)
            except Exception:
                pass

            # Attachments directly linked on the page.
            try:
                links = await page.locator(
                    "a[href]"
                ).evaluate_all(
                    """elements => elements.map(e => e.href)"""
                )

                for url in links:
                    remember(url)
            except Exception:
                pass

        finally:
            await context.close()

    # Preserve explicit PDFs already found by catalog.
    for field in ("annotated_pdf", "plain_pdf"):
        url = lecture.get(field)

        if isinstance(url, str) and ASSET_RE.search(url):
            if url not in assets:
                assets.append(url)

    return {
        "video_url": video_urls[-1] if video_urls else None,
        "board_json_url": (
            board_urls[-1] if board_urls else None
        ),
        "assets": assets[:30],
    }


def submit_download(lecture, resolved):
    if not API_KEY:
        raise RuntimeError(
            "LECTURE_API_KEY is not set in this terminal"
        )

    files = []

    if resolved["video_url"]:
        files.append({
            "kind": "video",
            "url": resolved["video_url"],
        })

    if resolved["board_json_url"]:
        files.append({
            "kind": "board",
            "url": resolved["board_json_url"],
        })

    for number, url in enumerate(resolved["assets"], 1):
        files.append({
            "kind": f"asset_{number}",
            "url": url,
        })

    if not files:
        raise RuntimeError(
            "No direct resource URL found"
        )

    response = requests.post(
        API + "/download",
        headers={"X-API-Key": API_KEY},
        json={
            "lecture": lecture["name"],
            "watch_url": lecture["watch_url"],
            "files": files,
        },
        timeout=30,
    )

    response.raise_for_status()
    return response.json()["job_id"]


async def poll_download(api_job_id):
    previous = None

    while True:
        response = await asyncio.to_thread(
            requests.get,
            API + f"/status/{api_job_id}",
            headers={"X-API-Key": API_KEY},
            timeout=20,
        )

        response.raise_for_status()
        status = response.json()

        stage = status.get("current_resource", "")
        percent = status.get("percent")
        state = status.get("status", "")

        line = f"{state} | {stage} | {percent}%"

        if line != previous:
            print("  ", line, flush=True)
            previous = line

        if state in (
            "completed",
            "completed_with_errors",
            "failed",
        ):
            return status

        await asyncio.sleep(3)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=int, required=True)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--start", type=int, default=1)
    args = parser.parse_args()

    source = EXPORTS / f"job_{args.job}_lectures.json"

    if not source.exists():
        raise SystemExit(f"Export not found: {source}")

    if not API_KEY:
        raise SystemExit(
            "Set LECTURE_API_KEY to the SAME key "
            "used by the running API."
        )

    document = json.loads(source.read_text("utf-8"))

    lectures = []

    for subject in document.get("subjects", []):
        for lecture in subject.get("lectures", []):
            if lecture.get("watch_url"):
                lectures.append(lecture)

    print(f"Available lectures: {len(lectures)}")

    selected = lectures[
        max(0, args.start - 1):
        max(0, args.start - 1) + args.limit
    ]

    output = EXPORTS / (
        f"job_{args.job}_api_results.json"
    )

    results = (
        json.loads(output.read_text("utf-8"))
        if output.exists()
        else {}
    )

    for index, lecture in enumerate(
        selected,
        args.start,
    ):
        title = lecture.get("name", f"Lecture {index}")

        print(
            f"\n[{index}/{len(lectures)}] {title}",
            flush=True,
        )

        try:
            resolved = await resolve_lecture(lecture)

            lecture["video_url"] = resolved["video_url"]
            lecture["board_json_url"] = resolved[
                "board_json_url"
            ]
            lecture["slides"] = resolved["assets"]
            lecture["resolution_status"] = (
                "resolved"
                if resolved["video_url"]
                else "video_missing"
            )

            print(
                "  Video:",
                bool(resolved["video_url"]),
                "| Board:",
                bool(resolved["board_json_url"]),
                "| Assets:",
                len(resolved["assets"]),
                flush=True,
            )

            if not resolved["video_url"]:
                raise RuntimeError(
                    "Video URL could not be resolved"
                )

            api_job_id = await asyncio.to_thread(
                submit_download,
                lecture,
                resolved,
            )

            print("  API job:", api_job_id)

            final = await poll_download(api_job_id)

            results[str(index)] = {
                "lecture": title,
                "api_job_id": api_job_id,
                "status": final.get("status"),
                "resources": final.get("results", []),
            }

        except Exception as error:
            print(
                "  ERROR:",
                str(error)[:200],
                flush=True,
            )

            results[str(index)] = {
                "lecture": title,
                "status": "failed",
                "error": str(error)[:200],
            }

        finally:
            output.write_text(
                json.dumps(
                    results,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            source.write_text(
                json.dumps(
                    document,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

    print("\nResults saved:", output)


if __name__ == "__main__":
    asyncio.run(main())
