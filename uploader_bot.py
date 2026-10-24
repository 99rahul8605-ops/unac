import os
import time
import asyncio
import sqlite3
import logging
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()

API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
GROUP_ID = int(os.getenv("STORAGE_GROUP_ID") or 0)

if not all([API_ID, API_HASH, BOT_TOKEN, OWNER_ID, GROUP_ID]):
    raise SystemExit("ERROR: .env mein 5 settings fill karo.")

logging.basicConfig(
    filename="bot_errors.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

client = TelegramClient("lecture_storage_bot", API_ID, API_HASH)

db = sqlite3.connect("storage.db")
db.execute("""
CREATE TABLE IF NOT EXISTS topics (
    topic_name TEXT PRIMARY KEY,
    thread_id INTEGER NOT NULL
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS uploaded_files (
    topic_name TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (topic_name, path, size)
)
""")
db.commit()

ALLOWED = {
    ".webm", ".mp4", ".mkv", ".json",
    ".pdf", ".zip", ".jpg", ".jpeg",
    ".png", ".html"
}

job = {
    "running": False,
    "stage": "Idle",
    "file": "",
    "percent": 0,
    "done": 0,
    "total": 0,
    "errors": 0
}

lock = asyncio.Lock()


async def notify(message):
    try:
        await client.send_message(OWNER_ID, message)
    except Exception:
        logging.exception("DM notification failed")


async def get_topic(name):
    existing = db.execute(
        "SELECT thread_id FROM topics WHERE topic_name=?",
        (name,)
    ).fetchone()

    if existing:
        return existing[0]

    url = (
        f"https://api.telegram.org/bot"
        f"{BOT_TOKEN}/createForumTopic"
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json={
            "chat_id": GROUP_ID,
            "name": name[:128]
        }) as response:
            data = await response.json()

    if not data.get("ok"):
        raise RuntimeError(
            "Topic creation failed: " +
            str(data.get("description", data))
        )

    thread_id = data["result"]["message_thread_id"]

    db.execute(
        "INSERT INTO topics VALUES (?, ?)",
        (name, thread_id)
    )
    db.commit()

    return thread_id


def progress_text(name, percent, index, total, stage):
    return (
        f"📦 Lecture Storage Bot\n\n"
        f"Stage: {stage}\n"
        f"File: {name}\n"
        f"Progress: {percent:.1f}%\n"
        f"Files: {index}/{total}\n"
        f"Errors: {job['errors']}"
    )


async def do_upload(folder, topic_name):
    async with lock:
        job.update({
            "running": True,
            "stage": "Scanning",
            "file": "",
            "percent": 0,
            "done": 0,
            "errors": 0
        })

        try:
            folder = Path(folder).expanduser().resolve()

            if not folder.is_dir():
                raise RuntimeError(
                    f"Folder nahi mila: {folder}"
                )

            files = sorted(
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in ALLOWED
            )

            if not files:
                raise RuntimeError(
                    "Folder mein supported files nahi mili."
                )

            job["total"] = len(files)

            await notify(
                f"🚀 Upload job started\n"
                f"Topic: {topic_name}\n"
                f"Files: {len(files)}"
            )

            thread_id = await get_topic(topic_name)

            status = await client.send_message(
                OWNER_ID,
                "⏳ Starting upload..."
            )

            for index, path in enumerate(files, 1):
                size = path.stat().st_size

                old = db.execute("""
                    SELECT message_id FROM uploaded_files
                    WHERE topic_name=? AND path=? AND size=?
                """, (topic_name, str(path), size)).fetchone()

                if old:
                    job["done"] += 1
                    continue

                job["stage"] = "Uploading"
                job["file"] = path.name
                job["percent"] = 0

                last_update = [0.0]

                async def update_progress(sent, total):
                    percent = sent * 100 / total if total else 0
                    job["percent"] = percent

                    now = time.monotonic()

                    if (
                        now - last_update[0] < 5
                        and sent != total
                    ):
                        return

                    last_update[0] = now

                    try:
                        await client.edit_message(
                            OWNER_ID,
                            status.id,
                            progress_text(
                                path.name,
                                percent,
                                index,
                                len(files),
                                "Uploading to Telegram"
                            )
                        )
                    except Exception:
                        pass

                try:
                    # Forum topic ka root message target.
                    sent_message = await client.send_file(
                        GROUP_ID,
                        str(path),
                        caption=(
                            f"📚 {topic_name}\n"
                            f"📁 {path.name}"
                        )[:1024],
                        reply_to=thread_id,
                        force_document=True,
                        progress_callback=update_progress
                    )

                    db.execute("""
                        INSERT OR REPLACE INTO uploaded_files
                        (topic_name, path, size,
                         group_id, thread_id, message_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        topic_name, str(path), size,
                        GROUP_ID, thread_id,
                        sent_message.id
                    ))
                    db.commit()

                    job["done"] += 1

                    await client.edit_message(
                        OWNER_ID,
                        status.id,
                        progress_text(
                            path.name, 100,
                            index, len(files),
                            "File uploaded"
                        )
                    )

                except Exception as error:
                    job["errors"] += 1
                    logging.exception(
                        "Failed uploading %s", path
                    )

                    await notify(
                        f"❌ UPLOAD ERROR\n\n"
                        f"Topic: {topic_name}\n"
                        f"File: {path.name}\n"
                        f"Error: {str(error)[:700]}\n\n"
                        f"Remaining files continue kar raha hoon."
                    )

            job["stage"] = "Complete"

            await notify(
                f"✅ Upload job finished\n\n"
                f"Topic: {topic_name}\n"
                f"Uploaded: {job['done']}\n"
                f"Errors: {job['errors']}\n\n"
                f"File index saved: storage.db"
            )

        except Exception as error:
            job["stage"] = "Failed"
            job["errors"] += 1
            logging.exception("Job failed")
            await notify(
                f"🚨 JOB FAILED\n\n"
                f"{str(error)[:900]}"
            )

        finally:
            job["running"] = False


@client.on(events.NewMessage(incoming=True))
async def handler(event):
    if not event.is_private:
        return

    if event.sender_id != OWNER_ID:
        return

    text = event.raw_text.strip()

    if text == "/start":
        await event.reply(
            "🤖 Lecture Storage Bot\n\n"
            "Commands:\n"
            "/status - Live job status\n"
            "/topics - Created topics\n"
            "/files - Uploaded file count\n\n"
            "Test uploader:\n"
            "/upload /folder/path | Topic Name\n\n"
            "Example:\n"
            "/upload /mnt/r/unac | Test Lecture\n\n"
            "Category → Goal → Batch selection "
            "abhi next integration mein add hoga."
        )

    elif text == "/status":
        await event.reply(
            f"Running: {job['running']}\n"
            f"Stage: {job['stage']}\n"
            f"File: {job['file']}\n"
            f"Progress: {job['percent']:.1f}%\n"
            f"Completed: {job['done']}/{job['total']}\n"
            f"Errors: {job['errors']}"
        )

    elif text == "/topics":
        rows = db.execute(
            "SELECT topic_name, thread_id FROM topics"
        ).fetchall()

        message = "\n".join(
            f"• {name}: {thread_id}"
            for name, thread_id in rows
        ) or "Abhi koi topic nahi hai."

        await event.reply(message[:4000])

    elif text == "/files":
        count = db.execute(
            "SELECT COUNT(*) FROM uploaded_files"
        ).fetchone()[0]

        await event.reply(
            f"Indexed uploaded files: {count}"
        )

    elif text.startswith("/upload "):
        if job["running"]:
            await event.reply(
                "⚠️ Ek upload job already chal raha hai."
            )
            return

        payload = text[len("/upload "):].strip()

        if "|" not in payload:
            await event.reply(
                "Format:\n"
                "/upload /folder/path | Topic Name"
            )
            return

        folder, topic = [
            x.strip() for x in payload.split("|", 1)
        ]

        if not folder or not topic:
            await event.reply(
                "Folder aur topic name dono zaroori hain."
            )
            return

        job["running"] = True
        asyncio.create_task(
            do_upload(folder, topic)
        )

        await event.reply(
            "✅ Upload job accepted.\n"
            "Live progress isi DM mein milega."
        )


async def main():
    await client.start(bot_token=BOT_TOKEN)
    print("Telegram uploader bot running...")
    print("DM mein /start bhejo.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
