
"""
Safe Lecture Manager launcher.

Run:
    python3 safe_bot.py

Uses existing bot.py and downloader.py.
Prevents low-RAM errors from skipping every lecture.
"""

import asyncio
import logging

import bot as manager
from downloader import check_resources


RECHECK_SECONDS = 30

original_download = manager.download_lecture


def is_resource_error(error):
    text = str(error).lower()

    return any(
        phrase in text
        for phrase in (
            "low available ram",
            "low disk space",
            "browser not started to avoid vps oom",
        )
    )


def update_waiting_status(error):
    manager.activity["stage"] = "Waiting for resources"
    manager.activity["detail"] = str(error)[:160]
    manager.activity["percent"] = 0


async def wait_for_resources():
    while True:
        if manager.cancel_requested:
            raise RuntimeError(
                "Job cancelled while waiting for resources"
            )

        if manager.paused:
            await asyncio.sleep(2)
            continue

        try:
            # Check the same limits as the downloader.
            # Do not launch Chromium yet.
            await asyncio.to_thread(
                check_resources,
                manager.FILES,
                True,
            )

            return

        except RuntimeError as error:
            if not is_resource_error(error):
                raise

            update_waiting_status(error)

            logging.warning(
                "Waiting for VPS resources: %s",
                str(error),
            )

            await asyncio.sleep(RECHECK_SECONDS)


async def safe_download_lecture(
    profile,
    root,
    subject,
    lecture,
    key,
    progress=None,
):
    warned = False

    while True:
        if manager.cancel_requested:
            raise RuntimeError(
                "Job cancelled by user"
            )

        while manager.paused:
            if manager.cancel_requested:
                raise RuntimeError(
                    "Job cancelled by user"
                )

            await asyncio.sleep(2)

        try:
            result = await original_download(
                profile,
                root,
                subject,
                lecture,
                key,
                progress,
            )

            return result

        except asyncio.CancelledError:
            raise

        except RuntimeError as error:
            if not is_resource_error(error):
                raise

            update_waiting_status(error)

            if not warned:
                warned = True

                await manager.notify(
                    "⚠️ Download waiting for VPS resources\n\n"
                    f"Lecture: {lecture['title'][:100]}\n"
                    f"Reason: {str(error)[:180]}\n\n"
                    "✅ Lecture is NOT marked failed.\n"
                    "✅ Next lecture will NOT start.\n"
                    "✅ Bot commands remain available.\n\n"
                    "Resources will be checked every "
                    f"{RECHECK_SECONDS} seconds.\n"
                    "Use /status to check progress."
                )

            await wait_for_resources()

            await manager.notify(
                "✅ VPS resource check passed.\n"
                "Retrying the same lecture."
            )

            manager.activity["stage"] = "Downloading"
            manager.activity["detail"] = "Retrying lecture"


# Replace the download function used by
# bot.process_job, without editing bot.py.
manager.download_lecture = safe_download_lecture


async def main():
    print(
        "Safe Lecture Manager started",
        flush=True,
    )

    print(
        "Concurrent lecture downloads: 1",
        flush=True,
    )

    print(
        f"Resource recheck: {RECHECK_SECONDS}s",
        flush=True,
    )

    await manager.main()


if __name__ == "__main__":
    asyncio.run(main())
