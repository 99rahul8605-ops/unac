
import asyncio
import time
from pathlib import Path

from telethon import Button

import txt_uploader as uploader

pending = {}
running = None
stop_requested = False


def active():
    return running is not None and not running.done()


async def handle_upload_event(event, client, owner, destination):
    global running, stop_requested

    text = (event.raw_text or "").strip()
    low = text.lower()

    if low == "/uploadstatus":
        info = uploader.progress_info
        await event.respond(
            "📤 TXT Uploader\n\n"
            f"Lecture: {info['lecture'][:100]}\n"
            f"Stage: {info['stage']}\n"
            f"Done: {info['done']}/{info['total']}\n"
            f"Progress: {info['percent']}%\n"
            f"Running: {active()}"
        )
        return True

    if low == "/uploadstop":
        if not active():
            await event.respond("No active upload.")
            return True

        stop_requested = True
        uploader.progress_info["stage"] = "Stopping"

        # Cancels the local Telegram uploader task.
        # The separate HTTP API may finish its current download.
        running.cancel()

        await event.respond(
            "🛑 Stop requested.\n"
            "Next lecture will not start.\n"
            "Existing upload receipts are preserved.\n\n"
            "Note: An already-running API download "
            "may continue on the API server."
        )
        return True

    if low == "/uploadcancel":
        if owner in pending:
            data = pending.pop(owner)
            path = data.get("path")
            if path:
                path.unlink(missing_ok=True)
            await event.respond("❌ Pending TXT cancelled.")
        else:
            await event.respond(
                "No pending TXT. Use /uploadstop "
                "for an active upload."
            )
        return True

    if low == "/uploadtxt":
        if active():
            await event.respond(
                "Uploader already running. "
                "Use /uploadstatus or /uploadstop."
            )
            return True

        previous = pending.pop(owner, None)
        if previous and previous.get("path"):
            previous["path"].unlink(missing_ok=True)

        pending[owner] = {"step": "file"}

        await event.respond(
            "📄 TXT file bhejo.\n\n"
            "File milne ke baad bot starting "
            "lecture number poochega.\n\n"
            "Cancel: /uploadcancel"
        )
        return True

    state = pending.get(owner)
    if state is None:
        return False

    if state["step"] == "file":
        if not event.message.document:
            return False

        filename = (
            getattr(event.file, "name", "") or ""
        ).lower()

        if not filename.endswith(".txt"):
            await event.respond("Sirf .txt file bhejo.")
            return True

        if (event.file.size or 0) > 2 * 1024 * 1024:
            await event.respond("TXT maximum 2 MB.")
            return True

        path = uploader.WORK / (
            f"input_{int(time.time())}_{event.id}.txt"
        )

        await event.download_media(file=str(path))

        try:
            lectures = uploader.parse_txt(path)
            if not lectures:
                raise ValueError("No valid watch links")
        except Exception as error:
            path.unlink(missing_ok=True)
            pending.pop(owner, None)
            await event.respond(
                f"Invalid TXT: {str(error)[:150]}"
            )
            return True

        pending[owner] = {
            "step": "start_number",
            "path": path,
            "lectures": lectures,
        }

        await event.respond(
            f"✅ TXT loaded: {len(lectures)} lectures.\n\n"
            "Kis lecture se downloading start karni hai?\n"
            f"1 se {len(lectures)} tak number bhejo.\n\n"
            "Example: 1, 15, 30\n"
            "Cancel: /uploadcancel"
        )
        return True

    if state["step"] == "start_number":
        if not text.isdigit():
            await event.respond(
                "Starting lecture number bhejo. "
                "Example: 1 or 15."
            )
            return True

        start = int(text)
        total = len(state["lectures"])

        if start < 1 or start > total:
            await event.respond(
                f"Number 1 se {total} ke beech hona chahiye."
            )
            return True

        state["start"] = start
        state["step"] = "confirm"

        lecture = state["lectures"][start - 1]

        await event.respond(
            "📤 Confirm TXT Upload\n\n"
            f"Total lectures: {total}\n"
            f"Start from: {start}\n"
            f"First: {lecture['name'][:110]}\n"
            f"Remaining: {total - start + 1}\n\n"
            "Already uploaded files will be skipped.",
            buttons=[[
                Button.inline(
                    "▶️ Start", b"upctl:start"
                ),
                Button.inline(
                    "❌ Cancel", b"upctl:cancel"
                ),
            ]]
        )
        return True

    if state["step"] == "confirm":
        await event.respond(
            "Neeche Start ya Cancel button dabao."
        )
        return True

    return False


async def handle_upload_callback(event, client, owner, destination):
    global running, stop_requested

    data = event.data.decode("utf-8", "ignore")

    if not data.startswith("upctl:"):
        return False

    if event.sender_id != owner:
        await event.answer("Not authorized", alert=True)
        return True

    if data == "upctl:cancel":
        item = pending.pop(owner, None)

        if item and item.get("path"):
            item["path"].unlink(missing_ok=True)

        await event.answer("Cancelled")
        await event.respond("❌ TXT upload cancelled.")
        return True

    if data == "upctl:start":
        state = pending.get(owner)

        if not state or state.get("step") != "confirm":
            await event.answer(
                "No pending upload", alert=True
            )
            return True

        if active():
            await event.answer(
                "Uploader already running", alert=True
            )
            return True

        selected = state["lectures"][
            state["start"] - 1:
        ]

        # Existing run_upload() accepts a TXT path.
        # Build a small TXT containing only the
        # requested lecture range.
        source = state["path"]

        lines = [
            "LECTURE MANAGER — SELECTED RANGE",
            f"START INDEX: {state['start']}",
            "",
        ]

        for lecture in selected:
            lines.extend([
                f"SUBJECT: {lecture['subject']}",
                f"CLASS: {lecture['class_number']}",
                f"LECTURE: {lecture['name']}",
                f"WATCH_URL: {lecture['watch_url']}",
                "",
            ])

        source.write_text(
            "\n".join(lines),
            encoding="utf-8"
        )

        pending.pop(owner, None)
        stop_requested = False

        async def runner():
            try:
                await uploader.run_upload(
                    client,
                    owner,
                    destination,
                    source,
                )
            except asyncio.CancelledError:
                await client.send_message(
                    owner,
                    "🛑 TXT uploader stopped.\n"
                    "Completed uploads are saved.\n"
                    "Resume using /uploadtxt."
                )
            except Exception as error:
                await client.send_message(
                    owner,
                    "❌ Upload task error:\n"
                    f"{type(error).__name__}: "
                    f"{str(error)[:170]}"
                )
            finally:
                source.unlink(missing_ok=True)
                uploader.progress_info["stage"] = "Idle"

        running = asyncio.create_task(runner())

        await event.answer("Started")
        await event.respond(
            f"▶️ Upload started from lecture "
            f"#{state['start']}.\n"
            "Check: /uploadstatus\n"
            "Stop: /uploadstop"
        )
        return True

    await event.answer("Invalid action")
    return True
