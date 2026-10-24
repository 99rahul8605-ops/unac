import os
import json
import re
import asyncio
import sqlite3
import logging
import time
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button

from catalog import goals, batches, subjects, classes
from downloader import download_lecture
from storage import Storage

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

STATE = ROOT / ".state"
FILES = ROOT / "downloads"

STATE.mkdir(exist_ok=True)
FILES.mkdir(exist_ok=True)

required = [
    "API_ID", "API_HASH", "BOT_TOKEN",
    "OWNER_ID", "STORAGE_GROUP_ID", "MONGODB_URI",
]

missing = [x for x in required if not os.getenv(x)]

if missing:
    raise SystemExit(
        "Missing .env settings: " + ", ".join(missing)
    )

OWNER = int(os.environ["OWNER_ID"])
PROFILE = STATE / "browser_profile"

logging.basicConfig(
    filename=STATE / "errors.log",
    level=logging.INFO,
)

tg = TelegramClient(
    str(STATE / "manager_session"),
    int(os.environ["API_ID"]),
    os.environ["API_HASH"],
)

storage = Storage()

sql = sqlite3.connect(STATE / "jobs.sqlite3")
sql.execute("""
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    status TEXT DEFAULT 'queued',
    done INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0
)
""")
sql.execute(
    "UPDATE jobs SET status='queued' WHERE status='running'"
)
sql.commit()

menus = {}
paused = False
cancel_requested = False

activity = {
    "stage": "Idle",
    "lecture": "",
}

last_update = 0
progress_message = None


def clean_error(error):
    return re.sub(
        r"([?&]k=)[^&\s]+",
        r"\1[hidden]",
        str(error),
    )[:500]


def menu_text(items, label, page):
    per_page = 10
    start = page * per_page
    end = min(start + per_page, len(items))
    pages = max(1, (len(items) + per_page - 1) // per_page)

    lines = [
        label,
        "",
    ]

    for index in range(start, end):
        lines.append(
            f"{index + 1}. {items[index]['name'][:130]}"
        )

    lines.extend([
        "",
        f"Page {page + 1}/{pages} | Total {len(items)}",
        "Item select karne ke liye neeche button dabao.",
    ])

    return "\n".join(lines)


def menu_buttons(items, page):
    start = page * 10
    end = min(start + 10, len(items))

    rows = []

    for index in range(start, end):
        name = items[index]["name"][:29]
        rows.append([
            Button.inline(
                f"{index + 1}. {name}",
                f"pick:{index}".encode(),
            )
        ])

    navigation = []

    if page > 0:
        navigation.append(
            Button.inline(
                "⬅️ Previous",
                b"page:prev",
            )
        )

    if end < len(items):
        navigation.append(
            Button.inline(
                "Next ➡️",
                b"page:next",
            )
        )

    if navigation:
        rows.append(navigation)

    return rows


async def show_menu(event, state, edit=False):
    field = {
        "goal": "goals",
        "pack": "packs",
        "subjects": "subjects",
    }[state["step"]]

    titles = {
        "goal": "🎯 Select your goal",
        "pack": "📦 Select pack / batch",
        "subjects": "📚 Select subjects",
    }

    items = state[field]
    page = state.get("page", 0)

    text = menu_text(
        items, titles[state["step"]], page
    )
    buttons = menu_buttons(items, page)

    if state["step"] == "subjects":
        buttons.append([
            Button.inline(
                "✅ Select ALL subjects",
                b"select:all",
            )
        ])
        text += "\n\nYa 1,3,5 type kar sakte ho."

    if edit:
        await event.edit(text, buttons=buttons)
    else:
        await event.respond(text, buttons=buttons)


async def notify(text):
    try:
        await tg.send_message(OWNER, text[:4000])
    except Exception:
        logging.exception("Owner DM failed")


async def live(text, force=False):
    global last_update, progress_message

    current = time.monotonic()

    if not force and current - last_update < 8:
        return

    last_update = current

    try:
        if progress_message:
            await tg.edit_message(
                OWNER,
                progress_message.id,
                text[:3900],
            )
        else:
            progress_message = await tg.send_message(
                OWNER,
                text[:3900],
            )
    except Exception:
        logging.exception("Progress message failed")


async def confirm(event, state):
    chosen = state["chosen"]

    lines = [
        "📋 Confirm download and upload",
        "",
        "Goal: " + state["goal"]["name"],
        "Pack: " + state["pack"]["name"],
        f"Subjects: {len(chosen)}",
        "",
        "Authorized content only.",
    ]

    state["step"] = "confirm"

    await event.respond(
        "\n".join(lines),
        buttons=[[
            Button.inline("✅ YES", b"confirm:yes"),
            Button.inline("❌ NO", b"confirm:no"),
        ]],
    )


@tg.on(events.NewMessage(incoming=True))
async def message_handler(event):
    global paused, cancel_requested

    if not event.is_private or event.sender_id != OWNER:
        return

    message = event.raw_text.strip()
    low = message.lower()

    if low in ("/start", "/search"):
        menus[OWNER] = {"step": "category", "page": 0}

        await event.respond(
            "🤖 Lecture Manager\n\n"
            "Category/search keyword bhejo.\n"
            "Example: CA / CA Foundation / UPSC\n\n"
            "/status /pause /resume /cancel /retry"
        )
        return

    if low == "/status":
        jobs = sql.execute(
            "SELECT id,status,done,errors FROM jobs "
            "ORDER BY id DESC LIMIT 4"
        ).fetchall()

        text = [
            "Stage: " + activity["stage"],
            "Lecture: " + activity["lecture"],
            "Paused: " + str(paused),
        ]

        text += [
            f"Job #{i}: {s} | done={d} | errors={e}"
            for i, s, d, e in jobs
        ]

        await event.respond("\n".join(text))
        return

    if low == "/pause":
        paused = True
        await event.respond("⏸ Queue paused.")
        return

    if low == "/resume":
        paused = False
        await event.respond("▶️ Queue resumed.")
        return

    if low == "/cancel":
        cancel_requested = True
        await event.respond(
            "🛑 Cancellation requested."
        )
        return

    if low == "/retry":
        row = sql.execute(
            "SELECT id FROM jobs WHERE status IN "
            "('failed','completed_with_errors') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if not row:
            await event.respond("No failed job.")
        else:
            sql.execute(
                "UPDATE jobs SET status='queued' WHERE id=?",
                (row[0],),
            )
            sql.commit()
            await event.respond(
                f"♻️ Job #{row[0]} queued."
            )
        return

    state = menus.get(OWNER)

    if not state:
        await event.respond("Send /start")
        return

    if state["step"] == "category":
        await event.respond("🔎 Searching goals...")

        try:
            items = await goals(PROFILE, message)
        except Exception as error:
            await event.respond(
                "Goal discovery failed:\n"
                + clean_error(error)
            )
            return

        if not items:
            await event.respond(
                "No goals found. Try another search term."
            )
            return

        state.update({
            "step": "goal",
            "goals": items,
            "page": 0,
        })

        await show_menu(event, state)
        return

    if state["step"] == "subjects":
        try:
            indices = [
                int(x.strip()) for x in message.split(",")
            ]

            if not indices or any(
                x < 1 or x > len(state["subjects"])
                for x in indices
            ):
                raise ValueError()

            state["chosen"] = [
                state["subjects"][n - 1]
                for n in dict.fromkeys(indices)
            ]
        except ValueError:
            await event.respond(
                "Use subject numbers like 1,3,5 "
                "or Select ALL button."
            )
            return

        await confirm(event, state)
        return

    await event.respond(
        "Selection ke liye inline buttons use karo."
    )


@tg.on(events.CallbackQuery)
async def callback_handler(event):
    if event.sender_id != OWNER:
        await event.answer("Not authorized", alert=True)
        return

    state = menus.get(OWNER)

    if not state:
        await event.answer(
            "Session expired. Send /start",
            alert=True,
        )
        return

    data = event.data.decode("utf-8", errors="ignore")

    if data.startswith("page:"):
        if state["step"] not in (
            "goal", "pack", "subjects"
        ):
            await event.answer("Invalid menu")
            return

        field = {
            "goal": "goals",
            "pack": "packs",
            "subjects": "subjects",
        }[state["step"]]

        count = len(state[field])
        last = max(0, (count - 1) // 10)

        change = (
            1 if data == "page:next" else -1
        )

        state["page"] = max(
            0,
            min(last, state["page"] + change),
        )

        await event.answer()
        await show_menu(event, state, edit=True)
        return

    if data.startswith("pick:"):
        step = state["step"]

        if step not in ("goal", "pack", "subjects"):
            await event.answer("Invalid selection")
            return

        field = {
            "goal": "goals",
            "pack": "packs",
            "subjects": "subjects",
        }[step]

        try:
            index = int(data.split(":")[1])
            item = state[field][index]
        except (ValueError, IndexError):
            await event.answer("Invalid index")
            return

        await event.answer()

        if step == "goal":
            state["goal"] = item
            await event.respond("🔎 Finding packs...")

            try:
                result = await batches(PROFILE, item)
            except Exception as error:
                await event.respond(
                    "Pack discovery failed:\n"
                    + clean_error(error)
                )
                return

            if not result:
                await event.respond(
                    "No packs found for this goal."
                )
                return

            state.update({
                "packs": result,
                "step": "pack",
                "page": 0,
            })

            await show_menu(event, state)
            return

        if step == "pack":
            state["pack"] = item
            await event.respond("🔎 Finding subjects...")

            try:
                result = await subjects(PROFILE, item)
            except Exception as error:
                await event.respond(
                    "Subject discovery failed:\n"
                    + clean_error(error)
                )
                return

            if not result:
                await event.respond(
                    "No subjects in this pack."
                )
                return

            state.update({
                "subjects": result,
                "step": "subjects",
                "page": 0,
            })

            await show_menu(event, state)
            return

        if step == "subjects":
            state["chosen"] = [item]
            await confirm(event, state)
            return

    if data == "select:all":
        if state["step"] != "subjects":
            await event.answer("Invalid step")
            return

        await event.answer()
        state["chosen"] = state["subjects"]
        await confirm(event, state)
        return

    if data == "confirm:no":
        menus.pop(OWNER, None)
        await event.answer("Cancelled")
        await event.respond(
            "Cancelled. Send /start again."
        )
        return

    if data == "confirm:yes":
        if state["step"] != "confirm":
            await event.answer("Invalid step")
            return

        payload = {
            "goal": state["goal"],
            "pack": state["pack"],
            "subjects": state["chosen"],
        }

        cursor = sql.execute(
            "INSERT INTO jobs(payload,status) "
            "VALUES(?, 'queued')",
            (json.dumps(payload),),
        )
        sql.commit()

        menus.pop(OWNER, None)

        await event.answer("Queued")
        await event.respond(
            f"✅ Job #{cursor.lastrowid} queued.\n"
            "Use /status for progress."
        )
        return

    await event.answer("Unknown action")


async def process_job(job_id, payload):
    global cancel_requested

    done = 0
    errors = 0

    for subject in payload["subjects"]:
        if cancel_requested:
            break

        while paused and not cancel_requested:
            await asyncio.sleep(2)

        activity["stage"] = "Discovering classes"

        try:
            lectures, total = await classes(
                PROFILE, subject
            )
        except Exception as error:
            errors += 1
            await notify(
                "❌ Subject discovery failed:\n"
                + subject["name"] + "\n"
                + clean_error(error)
            )
            continue

        await notify(
            f"📚 {subject['name']}\n"
            f"Total classes: {total}\n"
            f"Playable links: {len(lectures)}"
        )

        for lecture in lectures:
            if cancel_requested:
                break

            while paused and not cancel_requested:
                await asyncio.sleep(2)

            key = (
                payload["goal"]["uid"] + ":"
                + payload["pack"]["uid"] + ":"
                + subject["uid"] + ":"
                + str(lecture["class_number"])
            )

            if storage.completed(key):
                done += 1
                continue

            activity["lecture"] = lecture["title"]

            async def dl_progress(kind, percent):
                await live(
                    f"📥 Downloading\n"
                    f"{lecture['title'][:80]}\n"
                    f"{kind}: {percent:.1f}%"
                )

            async def up_progress(filename, percent):
                await live(
                    f"📤 Uploading\n"
                    f"{lecture['title'][:80]}\n"
                    f"{filename}: {percent:.1f}%"
                )

            try:
                activity["stage"] = "Downloading"

                await live(
                    f"📥 Starting\n"
                    f"{lecture['title']}",
                    force=True,
                )

                folder, manifest = await download_lecture(
                    PROFILE,
                    FILES,
                    subject,
                    lecture,
                    key,
                    dl_progress,
                )

                activity["stage"] = "Uploading"

                await storage.upload(
                    tg,
                    folder,
                    key,
                    subject,
                    lecture,
                    up_progress,
                )

                storage.mark_complete(
                    key,
                    payload["goal"],
                    payload["pack"],
                    subject,
                    lecture,
                    manifest,
                )

                done += 1

                await live(
                    f"✅ Uploaded: {lecture['title']}\n"
                    f"Done: {done} | Errors: {errors}",
                    force=True,
                )

            except Exception as error:
                errors += 1
                logging.exception("Lecture failure")

                await notify(
                    f"🚨 Lecture failed\n"
                    f"Subject: {subject['name']}\n"
                    f"Class: {lecture['class_number']}\n"
                    f"Error: {clean_error(error)}"
                )

            sql.execute(
                "UPDATE jobs SET done=?,errors=? WHERE id=?",
                (done, errors, job_id),
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

            job = sql.execute(
                "SELECT id,payload FROM jobs "
                "WHERE status='queued' "
                "ORDER BY id LIMIT 1"
            ).fetchone()

            if not job:
                await asyncio.sleep(3)
                continue

            job_id, raw = job
            cancel_requested = False

            sql.execute(
                "UPDATE jobs SET status='running' "
                "WHERE id=?",
                (job_id,),
            )
            sql.commit()

            try:
                done, errors = await process_job(
                    job_id, json.loads(raw)
                )

                result = (
                    "cancelled" if cancel_requested
                    else "completed_with_errors" if errors
                    else "completed"
                )

                sql.execute(
                    "UPDATE jobs SET status=? WHERE id=?",
                    (result, job_id),
                )
                sql.commit()

                await notify(
                    f"🏁 Job #{job_id}\n"
                    f"Done: {done}\nErrors: {errors}\n"
                    f"Status: {result}"
                )

            except Exception as error:
                logging.exception("Job error")

                sql.execute(
                    "UPDATE jobs SET status='failed' "
                    "WHERE id=?",
                    (job_id,),
                )
                sql.commit()

                await notify(
                    f"🚨 Job #{job_id} failed\n"
                    + clean_error(error)
                )

            activity["stage"] = "Idle"

        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Worker crash")
            await asyncio.sleep(5)


async def main():
    await tg.start(bot_token=os.environ["BOT_TOKEN"])

    await notify(
        "✅ Lecture Manager started\n"
        "Send /start"
    )

    task = asyncio.create_task(worker())

    try:
        await tg.run_until_disconnected()
    finally:
        task.cancel()
        await asyncio.gather(
            task, return_exceptions=True
        )
        sql.close()
        storage.close()


if __name__ == "__main__":
    asyncio.run(main())