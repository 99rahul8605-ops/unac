import os
import re
import json
import time
import asyncio
import sqlite3
import logging
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events

from catalog import (
    discover_goals,
    discover_batches,
    discover_subjects,
    discover_classes,
)
from downloader import download_lecture
from storage import Storage


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

STATE = ROOT / ".state"
DOWNLOADS = ROOT / "downloads"

STATE.mkdir(exist_ok=True)
DOWNLOADS.mkdir(exist_ok=True)

logging.basicConfig(
    filename=STATE / "errors.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

REQUIRED = [
    "API_ID",
    "API_HASH",
    "BOT_TOKEN",
    "OWNER_ID",
    "STORAGE_GROUP_ID",
    "MONGODB_URI",
]

missing = [key for key in REQUIRED if not os.getenv(key)]

if missing:
    raise SystemExit(
        "Missing .env settings: " + ", ".join(missing)
    )

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

client = TelegramClient(
    str(STATE / "manager_session"),
    API_ID,
    API_HASH,
)

storage = Storage()

sql = sqlite3.connect(STATE / "jobs.sqlite3")
sql.execute("""
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    done INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    stage TEXT DEFAULT 'Queued'
)
""")

sql.execute(
    "UPDATE jobs SET status='queued' WHERE status='running'"
)
sql.commit()

menus = {}
paused = False
cancel_requested = False

status = {
    "stage": "Idle",
    "lecture": "",
    "subject": "",
    "percent": 0.0,
}

last_progress_at = 0
progress_message = None


def clean_error(error):
    return re.sub(
        r"([?&]k=)[^&\s]+",
        r"\1[hidden]",
        str(error),
    )[:650]


async def notify(text):
    try:
        await client.send_message(
            OWNER_ID,
            text[:4000],
        )
    except Exception:
        logging.exception("DM notification failed")


async def live(text, force=False):
    global last_progress_at, progress_message

    current = time.monotonic()

    if not force and current - last_progress_at < 8:
        return

    last_progress_at = current

    try:
        if progress_message:
            await client.edit_message(
                OWNER_ID,
                progress_message.id,
                text[:3900],
            )
        else:
            progress_message = await client.send_message(
                OWNER_ID,
                text[:3900],
            )
    except Exception:
        logging.exception("Live progress failed")


def numbered(items, title, page_number=0):
    start = page_number * 20
    end = min(start + 20, len(items))

    lines = [title, ""]

    for index in range(start, end):
        lines.append(
            f"{index + 1}. {items[index]['name'][:105]}"
        )

    lines.append("")
    lines.append(f"Total: {len(items)}")
    lines.append("Number bhejo. next / prev bhi use kar sakte ho.")

    return "\n".join(lines)[:3900]


async def process_job(job_id, payload):
    global cancel_requested

    goal = payload["goal"]
    pack = payload["pack"]
    subjects = payload["subjects"]

    done = 0
    errors = 0

    for subject_number, subject in enumerate(subjects, 1):
        if cancel_requested:
            break

        while paused and not cancel_requested:
            await asyncio.sleep(2)

        status["stage"] = "Discovering classes"
        status["subject"] = subject["name"]

        await live(
            f"🔎 Subject {subject_number}/{len(subjects)}\n"
            f"{subject['name']}\n"
            "Finding available classes...",
            force=True,
        )

        try:
            lessons, total = await discover_classes(
                subject,
                STATE / "browser_profile",
            )
        except Exception as error:
            errors += 1

            await notify(
                f"❌ CLASS DISCOVERY ERROR\n"
                f"Subject: {subject['name']}\n"
                f"{clean_error(error)}"
            )
            continue

        await notify(
            f"📚 {subject['name']}\n"
            f"Class entries: {total}\n"
            f"Playable links: {len(lessons)}"
        )

        for lecture_number, lesson in enumerate(lessons, 1):
            if cancel_requested:
                break

            while paused and not cancel_requested:
                await asyncio.sleep(2)

            lecture_key = (
                f"{goal['uid']}:"
                f"{pack['uid']}:"
                f"{subject['uid']}:"
                f"{lesson['class_number']}"
            )

            if storage.completed(lecture_key):
                done += 1
                continue

            status["lecture"] = lesson["title"]
            status["stage"] = "Downloading"

            async def download_progress(kind, percent):
                status["percent"] = percent
                await live(
                    f"📥 Downloading\n"
                    f"Subject {subject_number}/{len(subjects)}\n"
                    f"Lecture {lecture_number}/{len(lessons)}\n"
                    f"{lesson['title'][:90]}\n"
                    f"{kind}: {percent:.1f}%\n"
                    f"Errors: {errors}"
                )

            async def upload_progress(filename, percent):
                status["percent"] = percent
                await live(
                    f"📤 Uploading to Telegram\n"
                    f"Batch: {subject['name'][:70]}\n"
                    f"Class {lesson['class_number']}\n"
                    f"File: {filename}\n"
                    f"Progress: {percent:.1f}%\n"
                    f"Errors: {errors}"
                )

            try:
                await live(
                    f"📥 Starting lecture "
                    f"{lecture_number}/{len(lessons)}\n"
                    f"{lesson['title']}",
                    force=True,
                )

                folder, manifest = await download_lecture(
                    subject,
                    lesson,
                    lecture_key,
                    STATE / "browser_profile",
                    DOWNLOADS,
                    download_progress,
                )

                status["stage"] = "Uploading"

                await storage.upload_package(
                    client,
                    folder,
                    lecture_key,
                    subject,
                    lesson,
                    upload_progress,
                )

                storage.mark_complete(
                    lecture_key,
                    goal,
                    pack,
                    subject,
                    lesson,
                    manifest,
                )

                done += 1

                await live(
                    f"✅ Lecture uploaded\n"
                    f"{lesson['title']}\n"
                    f"Processed: {done}\n"
                    f"Errors: {errors}",
                    force=True,
                )

            except Exception as error:
                errors += 1
                logging.exception(
                    "Lecture failed: %s",
                    lecture_key,
                )

                await notify(
                    f"🚨 LECTURE ERROR\n"
                    f"Subject: {subject['name']}\n"
                    f"Class: {lesson['class_number']}\n"
                    f"Title: {lesson['title']}\n"
                    f"Stage: {status['stage']}\n"
                    f"Error: {clean_error(error)}\n\n"
                    f"Next lecture continue hoga."
                )

            sql.execute(
                """
                UPDATE jobs
                SET done=?, errors=?, stage=?
                WHERE id=?
                """,
                (done, errors, status["stage"], job_id),
            )
            sql.commit()

    return done, errors


async def worker():
    global cancel_requested

    while True:
        try:
            if paused:
                await asyncio.sleep(2)
                continue

            row = sql.execute("""
                SELECT id,payload
                FROM jobs
                WHERE status='queued'
                ORDER BY id
                LIMIT 1
            """).fetchone()

            if not row:
                await asyncio.sleep(3)
                continue

            job_id, payload_json = row
            cancel_requested = False

            sql.execute(
                """
                UPDATE jobs
                SET status='running', stage='Starting'
                WHERE id=?
                """,
                (job_id,),
            )
            sql.commit()

            try:
                done, errors = await process_job(
                    job_id,
                    json.loads(payload_json),
                )

                final_state = (
                    "cancelled"
                    if cancel_requested
                    else "completed_with_errors"
                    if errors
                    else "completed"
                )

                sql.execute(
                    """
                    UPDATE jobs
                    SET status=?,stage=?
                    WHERE id=?
                    """,
                    (final_state, final_state, job_id),
                )
                sql.commit()

                await notify(
                    f"🏁 JOB #{job_id} FINISHED\n"
                    f"Processed: {done}\n"
                    f"Errors: {errors}\n"
                    f"Status: {final_state}"
                )

            except Exception as error:
                logging.exception("Job failed")

                sql.execute(
                    """
                    UPDATE jobs
                    SET status='failed',stage='Failed'
                    WHERE id=?
                    """,
                    (job_id,),
                )
                sql.commit()

                await notify(
                    f"🚨 JOB #{job_id} FAILED\n"
                    f"{clean_error(error)}"
                )

            status["stage"] = "Idle"

        except asyncio.CancelledError:
            raise

        except Exception:
            logging.exception("Worker loop error")
            await asyncio.sleep(5)


@client.on(events.NewMessage(incoming=True))
async def handler(event):
    global paused, cancel_requested

    if not event.is_private:
        return

    if event.sender_id != OWNER_ID:
        return

    text = event.raw_text.strip()
    lower = text.lower()

    if lower == "/status":
        rows = sql.execute("""
            SELECT id,status,done,errors,stage
            FROM jobs
            ORDER BY id DESC
            LIMIT 5
        """).fetchall()

        lines = [
            f"Stage: {status['stage']}",
            f"Subject: {status['subject']}",
            f"Lecture: {status['lecture']}",
            f"Progress: {status['percent']:.1f}%",
            f"Paused: {paused}",
            "",
        ]

        for item in rows:
            lines.append(
                f"#{item[0]} {item[1]} "
                f"| Done={item[2]} Errors={item[3]} "
                f"| {item[4]}"
            )

        await event.reply("\n".join(lines)[:3900])
        return

    if lower == "/pause":
        paused = True
        await event.reply(
            "⏸ Queue paused after current operation."
        )
        return

    if lower == "/resume":
        paused = False
        await event.reply("▶️ Queue resumed.")
        return

    if lower == "/cancel":
        cancel_requested = True
        await event.reply(
            "🛑 Cancellation requested."
        )
        return

    if lower == "/retry":
        row = sql.execute("""
            SELECT id FROM jobs
            WHERE status IN ('failed','completed_with_errors')
            ORDER BY id DESC LIMIT 1
        """).fetchone()

        if row:
            sql.execute(
                """
                UPDATE jobs
                SET status='queued',stage='Retrying'
                WHERE id=?
                """,
                (row[0],),
            )
            sql.commit()

            await event.reply(
                f"♻️ Job #{row[0]} queued again."
            )
        else:
            await event.reply(
                "No failed job available."
            )
        return

    if lower in ("/start", "/search"):
        menus[event.sender_id] = {
            "step": "category",
            "page": 0,
        }

        await event.reply(
            "🤖 Lecture Manager\n\n"
            "Apni category likho:\n"
            "Example: CA / Chartered Accountant / UPSC\n\n"
            "Commands: /status /pause /resume /cancel /retry"
        )
        return

    state = menus.get(event.sender_id)

    if not state:
        await event.reply(
            "Start karne ke liye /start bhejo."
        )
        return

    step = state["step"]

    if lower in ("next", "prev") and step in (
        "goal", "batch", "subjects"
    ):
        field = {
            "goal": "goals",
            "batch": "batches",
            "subjects": "subjects",
        }[step]

        max_page = max(
            0,
            (len(state[field]) - 1) // 20,
        )

        state["page"] = max(
            0,
            min(
                max_page,
                state["page"] + (
                    1 if lower == "next" else -1
                ),
            ),
        )

        await event.reply(
            numbered(
                state[field],
                f"Select {step}:",
                state["page"],
            )
        )
        return

    if step == "category":
        await event.reply(
            f"🔎 Searching category: {text}"
        )

        try:
            goals = await discover_goals(
                text,
                STATE / "browser_profile",
            )
        except Exception as error:
            await event.reply(
                "Goal search failed:\n"
                + clean_error(error)
            )
            return

        if not goals:
            await event.reply(
                "No matching goals found. "
                "Try another keyword."
            )
            return

        state.update({
            "step": "goal",
            "goals": goals,
            "page": 0,
        })

        await event.reply(
            numbered(
                goals,
                "🎯 Goal number select karo:",
            )
        )
        return

    if step == "goal":
        if not text.isdigit() or not (
            1 <= int(text) <= len(state["goals"])
        ):
            await event.reply(
                "Valid goal index bhejo."
            )
            return

        goal = state["goals"][int(text) - 1]

        await event.reply(
            "🔎 Finding batches and packs..."
        )

        try:
            batches = await discover_batches(
                goal,
                STATE / "browser_profile",
            )
        except Exception as error:
            await event.reply(
                "Batch search error:\n"
                + clean_error(error)
            )
            return

        if not batches:
            await event.reply(
                "No batches found for this goal."
            )
            return

        state.update({
            "step": "batch",
            "goal": goal,
            "batches": batches,
            "page": 0,
        })

        await event.reply(
            numbered(
                batches,
                "📦 Batch/pack number select karo:",
            )
        )
        return

    if step == "batch":
        if not text.isdigit() or not (
            1 <= int(text) <= len(state["batches"])
        ):
            await event.reply(
                "Valid batch number bhejo."
            )
            return

        pack = state["batches"][int(text) - 1]

        await event.reply(
            "🔎 Finding subjects..."
        )

        try:
            subjects = await discover_subjects(
                pack,
                STATE / "browser_profile",
            )
        except Exception as error:
            await event.reply(
                "Subject discovery error:\n"
                + clean_error(error)
            )
            return

        if not subjects:
            await event.reply(
                "No subjects found."
            )
            return

        state.update({
            "step": "subjects",
            "pack": pack,
            "subjects": subjects,
            "page": 0,
        })

        await event.reply(
            numbered(
                subjects,
                "📚 Subjects select karo:\n"
                "all / 2 / 1,3,5",
            )
        )
        return

    if step == "subjects":
        if lower == "all":
            selected = state["subjects"]
        else:
            try:
                numbers = [
                    int(part.strip())
                    for part in text.split(",")
                ]

                if not numbers or any(
                    n < 1 or n > len(state["subjects"])
                    for n in numbers
                ):
                    raise ValueError()

                selected = [
                    state["subjects"][n - 1]
                    for n in dict.fromkeys(numbers)
                ]

            except Exception:
                await event.reply(
                    "Use all or numbers like 1,3,5."
                )
                return

        state.update({
            "step": "confirm",
            "selected": selected,
        })

        await event.reply(
            "📋 DOWNLOAD CONFIRMATION\n\n"
            f"Goal: {state['goal']['name']}\n"
            f"Pack: {state['pack']['name']}\n"
            f"Subjects: {len(selected)}\n\n"
            "YES = Download and Upload\n"
            "NO = Cancel"
        )
        return

    if step == "confirm":
        if lower != "yes":
            menus.pop(event.sender_id, None)
            await event.reply(
                "Cancelled. /start se dobara shuru karo."
            )
            return

        payload = {
            "goal": state["goal"],
            "pack": state["pack"],
            "subjects": state["selected"],
        }

        cursor = sql.execute(
            """
            INSERT INTO jobs(payload,status,stage)
            VALUES(?, 'queued', 'Queued')
            """,
            (json.dumps(payload),),
        )
        sql.commit()

        menus.pop(event.sender_id, None)

        await event.reply(
            f"✅ Job #{cursor.lastrowid} queued.\n"
            "Live progress aur errors isi DM mein aayenge.\n"
            "Check: /status"
        )


async def main():
    await client.start(
        bot_token=os.environ["BOT_TOKEN"]
    )

    await notify(
        "✅ Lecture Manager started.\n"
        "Send /start to begin."
    )

    task = asyncio.create_task(worker())

    try:
        await client.run_until_disconnected()
    finally:
        task.cancel()

        await asyncio.gather(
            task,
            return_exceptions=True,
        )

        sql.close()
        storage.close()


if __name__ == "__main__":
    asyncio.run(main())
