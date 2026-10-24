
import os
import re
import json
import time
import asyncio
import sqlite3
import logging

from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button

from catalog import goals, batches, subjects, classes
from downloader import download_lecture
from storage import Storage


# ==========================================
# CONFIGURATION
# ==========================================

ROOT = Path(__file__).resolve().parent

load_dotenv(ROOT / ".env")

STATE = ROOT / ".state"
FILES = ROOT / "downloads"

STATE.mkdir(parents=True, exist_ok=True)
FILES.mkdir(parents=True, exist_ok=True)

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

OWNER = int(os.environ["OWNER_ID"])

# IMPORTANT:
# Browsing and downloading must not use
# the same persistent Chromium profile.
#
# Browsing remains available while the
# download browser is running.

BROWSE_PROFILE = STATE / "browse_profile"
DOWNLOAD_PROFILE = STATE / "browser_profile"

# Jobs can queue independently.
# Only one download worker is started,
# avoiding simultaneous heavy downloads.

MAX_ACTIVE_DOWNLOADS = 1

LIVE_INTERVAL = 4
HEARTBEAT_INTERVAL = 12

logging.basicConfig(
    filename=STATE / "errors.log",
    level=logging.INFO,
    format=(
        "%(asctime)s %(levelname)s "
        "%(name)s %(message)s"
    ),
)

tg = TelegramClient(
    str(STATE / "manager_session"),
    int(os.environ["API_ID"]),
    os.environ["API_HASH"],
)

storage = Storage()


# ==========================================
# SQLITE JOB QUEUE
# ==========================================

sql = sqlite3.connect(
    STATE / "jobs.sqlite3",
    timeout=30,
)

sql.execute("PRAGMA busy_timeout=30000")

sql.execute("""
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    status TEXT DEFAULT 'queued',
    done INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0
)
""")

# An interrupted download job can be
# resumed through the queue on restart.
sql.execute("""
UPDATE jobs
SET status='queued'
WHERE status='running'
""")

sql.commit()


# ==========================================
# RUNTIME STATE
# ==========================================

menus = {}

paused = False
cancel_requested = False

catalog_lock = asyncio.Lock()
live_lock = asyncio.Lock()

progress_message = None
last_live_sent = 0.0

activity = {
    "job_id": None,
    "stage": "Idle",
    "detail": "",
    "subject": "",
    "lecture": "",
    "percent": 0.0,
    "done": 0,
    "errors": 0,
    "started": time.monotonic(),
    "last_progress": time.monotonic(),
}


# ==========================================
# HELPERS
# ==========================================

def clean_error(error):
    text = str(error)

    # Avoid exposing sensitive media query
    # parameters in Telegram errors.
    text = re.sub(
        r"([?&](?:k|token|signature|sig|key)=)[^&\s]+",
        r"\1[hidden]",
        text,
        flags=re.I,
    )

    return text[:500]


def elapsed_text(seconds):
    seconds = max(0, int(seconds))

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}m {seconds}s"

    if minutes:
        return f"{minutes}m {seconds}s"

    return f"{seconds}s"


def set_activity(
    stage=None,
    detail=None,
    percent=None,
    lecture=None,
    subject=None,
    job_id=None,
):
    now = time.monotonic()

    if stage is not None:
        activity["stage"] = stage

    if detail is not None:
        activity["detail"] = detail

    if percent is not None:
        activity["percent"] = max(
            0,
            min(100, float(percent)),
        )

    if lecture is not None:
        activity["lecture"] = lecture

    if subject is not None:
        activity["subject"] = subject

    if job_id is not None:
        activity["job_id"] = job_id

    activity["last_progress"] = now


def format_activity():
    now = time.monotonic()

    lines = [
        "📊 Lecture Manager — Live Progress",
        "",
        f"Job: #{activity['job_id'] or '-'}",
        f"Stage: {activity['stage']}",
    ]

    if activity["subject"]:
        lines.append(
            "Subject: "
            + activity["subject"][:100]
        )

    if activity["lecture"]:
        lines.append(
            "Lecture: "
            + activity["lecture"][:100]
        )

    if activity["detail"]:
        lines.append(
            "Operation: "
            + activity["detail"][:100]
        )

    if activity["stage"] in (
        "Downloading",
        "Uploading",
        "Packaging",
    ):
        lines.append(
            f"Progress: {activity['percent']:.1f}%"
        )

    lines.extend([
        f"Elapsed: {elapsed_text(now - activity['started'])}",
        (
            "Last progress update: "
            + elapsed_text(
                now - activity["last_progress"]
            )
            + " ago"
        ),
        (
            f"Done: {activity['done']} | "
            f"Errors: {activity['errors']}"
        ),
        f"Queue paused: {paused}",
    ])

    return "\n".join(lines)


async def notify(text):
    try:
        await tg.send_message(
            OWNER,
            text[:4000],
        )
    except Exception:
        logging.exception(
            "Owner notification failed"
        )


async def live(text=None, force=False):
    global progress_message, last_live_sent

    async with live_lock:
        now = time.monotonic()

        if (
            not force
            and now - last_live_sent < LIVE_INTERVAL
        ):
            return

        message = (
            text if text is not None
            else format_activity()
        )

        try:
            if progress_message is None:
                progress_message = await tg.send_message(
                    OWNER,
                    message[:3900],
                )
            else:
                await tg.edit_message(
                    OWNER,
                    progress_message.id,
                    message[:3900],
                )

            last_live_sent = time.monotonic()

        except Exception as error:
            # Editing identical text is not a
            # real failure.
            if "MessageNotModified" in type(error).__name__:
                last_live_sent = time.monotonic()
                return

            logging.exception(
                "Live progress update failed"
            )

            # If the progress message disappeared,
            # recreate it on the next attempt.
            if (
                "MessageIdInvalid" in type(error).__name__
                or "MessageDeleteForbidden" in type(error).__name__
            ):
                progress_message = None


async def heartbeat():
    while True:
        await asyncio.sleep(
            HEARTBEAT_INTERVAL
        )

        if activity["stage"] == "Idle":
            continue

        # Even when the website hasn't returned
        # any progress, the owner can see that the
        # worker is waiting and for how long.
        await live(
            format_activity(),
            force=True,
        )


def recent_jobs(limit=5):
    return sql.execute(
        """
        SELECT id,status,done,errors
        FROM jobs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def update_job_counts(job_id, done, errors):
    sql.execute(
        """
        UPDATE jobs
        SET done=?, errors=?
        WHERE id=?
        """,
        (done, errors, job_id),
    )
    sql.commit()


# ==========================================
# INLINE MENUS
# ==========================================

def menu_text(items, label, page):
    per_page = 10

    start = page * per_page
    end = min(
        start + per_page,
        len(items),
    )

    pages = max(
        1,
        (len(items) + per_page - 1) // per_page,
    )

    lines = [
        label,
        "",
    ]

    for index in range(start, end):
        lines.append(
            f"{index + 1}. "
            f"{items[index]['name'][:130]}"
        )

    lines.extend([
        "",
        (
            f"Page {page + 1}/{pages} | "
            f"Total {len(items)}"
        ),
        "Neeche button se select karo.",
    ])

    return "\n".join(lines)


def menu_buttons(items, page):
    start = page * 10
    end = min(
        start + 10,
        len(items),
    )

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


async def show_menu(
    event,
    state,
    edit=False,
):
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
        items,
        titles[state["step"]],
        page,
    )

    buttons = menu_buttons(
        items,
        page,
    )

    if state["step"] == "subjects":
        buttons.append([
            Button.inline(
                "✅ Select ALL subjects",
                b"select:all",
            )
        ])

        text += (
            "\n\nYa 1,3,5 type kar sakte ho."
        )

    if edit:
        await event.edit(
            text,
            buttons=buttons,
        )
    else:
        await event.respond(
            text,
            buttons=buttons,
        )


async def confirm(event, state):
    chosen = state["chosen"]

    text = "\n".join([
        ("📄 Confirm TXT generation" if state.get("mode") == "txt" else "📋 Confirm download and upload"),
        "",
        "Goal: " + state["goal"]["name"],
        "Pack: " + state["pack"]["name"],
        f"Subjects: {len(chosen)}",
        "",
        "Authorized content only.",
    ])

    state["step"] = "confirm"

    await event.respond(
        text,
        buttons=[[
            Button.inline(
                "✅ YES",
                b"confirm:yes",
            ),
            Button.inline(
                "❌ NO",
                b"confirm:no",
            ),
        ]],
    )


# ==========================================
# COMMANDS AND SEARCH
# ==========================================

@tg.on(events.NewMessage(incoming=True))
async def message_handler(event):
    global paused, cancel_requested

    if (
        not event.is_private
        or event.sender_id != OWNER
    ):
        return

    # TXT_UPLOADER_HOOK_V1
    from upload_controls import handle_upload_event

    if await handle_upload_event(
        event, tg, OWNER, int(os.environ["STORAGE_GROUP_ID"])
    ):
        return

    message = event.raw_text.strip()
    low = message.lower()

    # TXT_EXPORT_COMMAND_V1
    if low.startswith("/export"):
        match = re.fullmatch(r"/export(?:@\w+)?\s+(\d+)", low)

        if not match:
            await event.respond("Usage: /export 2")
            return

        job_id = int(match.group(1))
        txt = ROOT / "exports" / f"job_{job_id}_lectures.txt"

        if not txt.is_file():
            await event.respond(
                f"Job #{job_id} ka TXT nahi mila. "
                "Pehle export generate karna hoga."
            )
            return

        if not re.search(
            r"^WATCH_URL: https?://",
            txt.read_text(encoding="utf-8"),
            re.M
        ):
            await event.respond(
                "TXT mein koi watch link nahi hai."
            )
            return

        await event.respond(
            f"Sending Job #{job_id} lecture TXT..."
        )
        await tg.send_file(
            event.chat_id,
            str(txt),
            caption=f"Lecture TXT — Job #{job_id}"
        )
        return


    # AUTO_TXT_FLOW_V1
    if low == "/txt":
        menus[OWNER] = {
            "step": "category",
            "page": 0,
            "mode": "txt",
        }
        await event.respond(
            "📄 TXT Generator\\n\\n"
            "Category/search keyword bhejo.\\n"
            "Example: CA / CA Intermediate"
        )
        return

    if low in ("/start", "/search"):
        menus[OWNER] = {
            "step": "category",
            "page": 0,
            "busy": False,
        }

        await event.respond(
            "🤖 Lecture Manager\n\n"
            "Category/search keyword bhejo.\n"
            "Example: CA / CA Foundation / UPSC\n\n"
            "Bot ko downloading ke waqt bhi "
            "use kar sakte ho.\n\n"
            "/status /queue /pause "
            "/resume /cancel /retry"
        )
        return

    if low == "/status":
        lines = [
            format_activity(),
            "",
            "Recent jobs:",
        ]

        lines.extend(
            (
                f"Job #{job_id}: {status} | "
                f"done={done} | errors={errors}"
            )
            for job_id, status, done, errors
            in recent_jobs()
        )

        await event.respond(
            "\n".join(lines)
        )
        return

    if low == "/queue":
        rows = recent_jobs(15)

        if not rows:
            await event.respond(
                "Queue empty."
            )
            return

        text = ["📋 Jobs", ""]

        for job_id, status, done, errors in rows:
            text.append(
                f"#{job_id} — {status} "
                f"| Done {done} | Errors {errors}"
            )

        await event.respond(
            "\n".join(text)
        )
        return

    if low == "/pause":
        paused = True

        await event.respond(
            "⏸ Queue paused.\n"
            "Current lecture can finish; "
            "next lecture won't start until /resume."
        )
        return

    if low == "/resume":
        paused = False

        await event.respond(
            "▶️ Queue resumed."
        )
        return

    if low == "/cancel":
        cancel_requested = True

        await event.respond(
            "🛑 Cancellation requested.\n"
            "Current operation may finish first. "
            "No next lecture will start."
        )
        return

    if low == "/retry":
        row = sql.execute(
            """
            SELECT id
            FROM jobs
            WHERE status IN (
                'failed',
                'completed_with_errors'
            )
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        if not row:
            await event.respond(
                "No failed job."
            )
            return

        sql.execute(
            """
            UPDATE jobs
            SET status='queued',
                done=0,
                errors=0
            WHERE id=?
            """,
            (row[0],),
        )

        sql.commit()

        await event.respond(
            f"♻️ Job #{row[0]} queued."
        )
        return

    state = menus.get(OWNER)

    if not state:
        await event.respond(
            "Send /start"
        )
        return

    if state["step"] == "category":
        if state.get("busy"):
            await event.respond(
                "Search already running."
            )
            return

        state["busy"] = True

        await event.respond(
            "🔎 Searching goals..."
        )

        try:
            # A separate Chromium profile prevents
            # conflict with the download browser.
            async with catalog_lock:
                items = await goals(
                    BROWSE_PROFILE,
                    message,
                )

        except Exception as error:
            await event.respond(
                "Goal discovery failed:\n"
                + clean_error(error)
            )
            return

        finally:
            state["busy"] = False

        # Ignore an old search if /start opened
        # a newer selection session.
        if menus.get(OWNER) is not state:
            return

        if not items:
            await event.respond(
                "No goals found. Try another term."
            )
            return

        state.update({
            "step": "goal",
            "goals": items,
            "page": 0,
        })

        await show_menu(
            event,
            state,
        )
        return

    if state["step"] == "subjects":
        try:
            indices = [
                int(x.strip())
                for x in message.split(",")
            ]

            if not indices or any(
                index < 1
                or index > len(state["subjects"])
                for index in indices
            ):
                raise ValueError()

            state["chosen"] = [
                state["subjects"][number - 1]
                for number in dict.fromkeys(indices)
            ]

        except ValueError:
            await event.respond(
                "Use numbers like 1,3,5 "
                "or Select ALL."
            )
            return

        await confirm(
            event,
            state,
        )
        return

    await event.respond(
        "Selection ke liye inline buttons use karo."
    )


# ==========================================
# INLINE BUTTON HANDLER
# ==========================================

@tg.on(events.CallbackQuery)
async def callback_handler(event):

    # UPLOAD_CONTROLS_V2
    from upload_controls import handle_upload_callback

    if await handle_upload_callback(
        event,
        tg,
        OWNER,
        int(os.environ["STORAGE_GROUP_ID"])
    ):
        return


    if event.sender_id != OWNER:
        await event.answer(
            "Not authorized",
            alert=True,
        )
        return

    state = menus.get(OWNER)

    if not state:
        await event.answer(
            "Session expired. Send /start",
            alert=True,
        )
        return

    data = event.data.decode(
        "utf-8",
        errors="ignore",
    )

    if data.startswith("page:"):
        if state["step"] not in (
            "goal",
            "pack",
            "subjects",
        ):
            await event.answer(
                "Invalid menu"
            )
            return

        field = {
            "goal": "goals",
            "pack": "packs",
            "subjects": "subjects",
        }[state["step"]]

        count = len(state[field])
        last = max(
            0,
            (count - 1) // 10,
        )

        change = (
            1 if data == "page:next"
            else -1
        )

        state["page"] = max(
            0,
            min(
                last,
                state["page"] + change,
            ),
        )

        await event.answer()

        await show_menu(
            event,
            state,
            edit=True,
        )
        return

    if data.startswith("pick:"):
        step = state["step"]

        if step not in (
            "goal",
            "pack",
            "subjects",
        ):
            await event.answer(
                "Invalid selection"
            )
            return

        if state.get("busy"):
            await event.answer(
                "Previous request still running."
            )
            return

        field = {
            "goal": "goals",
            "pack": "packs",
            "subjects": "subjects",
        }[step]

        try:
            index = int(
                data.split(":", 1)[1]
            )

            item = state[field][index]

        except (ValueError, IndexError):
            await event.answer(
                "Invalid selection"
            )
            return

        await event.answer()

        if step == "goal":
            state["busy"] = True
            state["goal"] = item

            await event.respond(
                "🔎 Finding packs..."
            )

            try:
                async with catalog_lock:
                    result = await batches(
                        BROWSE_PROFILE,
                        item,
                    )

            except Exception as error:
                await event.respond(
                    "Pack discovery failed:\n"
                    + clean_error(error)
                )
                return

            finally:
                state["busy"] = False

            if menus.get(OWNER) is not state:
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

            await show_menu(
                event,
                state,
            )
            return

        if step == "pack":
            state["busy"] = True
            state["pack"] = item

            await event.respond(
                "🔎 Finding subjects..."
            )

            try:
                async with catalog_lock:
                    result = await subjects(
                        BROWSE_PROFILE,
                        item,
                    )

            except Exception as error:
                await event.respond(
                    "Subject discovery failed:\n"
                    + clean_error(error)
                )
                return

            finally:
                state["busy"] = False

            if menus.get(OWNER) is not state:
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

            await show_menu(
                event,
                state,
            )
            return

        if step == "subjects":
            state["chosen"] = [item]

            await confirm(
                event,
                state,
            )
            return

    if data == "select:all":
        if state["step"] != "subjects":
            await event.answer(
                "Invalid step"
            )
            return

        await event.answer()

        state["chosen"] = state["subjects"]

        await confirm(
            event,
            state,
        )
        return

    if data == "confirm:no":
        menus.pop(OWNER, None)

        await event.answer(
            "Cancelled"
        )

        await event.respond(
            "Cancelled. Send /start again."
        )
        return

    if data == "confirm:yes":
        if state["step"] != "confirm":
            await event.answer(
                "Invalid step"
            )
            return

        # Immediately prevent the same YES button
        # from queuing duplicate jobs.
        state["step"] = "queued"

        if state.get("mode") == "txt":
            await event.answer("Generating TXT")
            menus.pop(OWNER, None)

            from txt_feature import generate_txt

            # Same profile as successful terminal TXT export.
            txt_profile = STATE / "verified_backup"

            try:
                await generate_txt(event, state, txt_profile)
            except Exception as error:
                logging.exception("TXT generation failed")
                await event.respond(
                    "TXT export error: "
                    + clean_error(error)
                )
            return

        payload = {
            "goal": state["goal"],
            "pack": state["pack"],
            "subjects": state["chosen"],
        }

        cursor = sql.execute(
            """
            INSERT INTO jobs(payload,status)
            VALUES(?, 'queued')
            """,
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                ),
            ),
        )

        sql.commit()

        menus.pop(OWNER, None)

        await event.answer(
            "Queued"
        )

        await event.respond(
            f"✅ Job #{cursor.lastrowid} queued.\n"
            "Download ke waqt bhi /start, "
            "/status aur /queue use kar sakte ho."
        )
        return

    await event.answer(
        "Unknown action"
    )


# ==========================================
# JOB PROCESSING
# ==========================================

async def wait_if_paused():
    while paused and not cancel_requested:
        await asyncio.sleep(1)


async def process_job(
    job_id,
    payload,
):
    global cancel_requested

    done = 0
    errors = 0

    activity["done"] = 0
    activity["errors"] = 0

    for subject in payload["subjects"]:
        if cancel_requested:
            break

        await wait_if_paused()

        if cancel_requested:
            break

        set_activity(
            stage="Discovering classes",
            detail="Opening subject",
            percent=0,
            lecture="",
            subject=subject["name"],
            job_id=job_id,
        )

        await live(
            force=True
        )

        try:
            lectures, total = await classes(
                DOWNLOAD_PROFILE,
                subject,
            )

        except Exception as error:
            errors += 1

            activity["errors"] = errors

            update_job_counts(
                job_id,
                done,
                errors,
            )

            logging.exception(
                "Subject discovery failed"
            )

            await notify(
                "❌ Subject discovery failed:\n"
                + subject["name"]
                + "\n"
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

            await wait_if_paused()

            if cancel_requested:
                break

            key = (
                payload["goal"]["uid"]
                + ":"
                + payload["pack"]["uid"]
                + ":"
                + subject["uid"]
                + ":"
                + str(lecture["class_number"])
            )

            if storage.completed(key):
                done += 1

                activity["done"] = done

                update_job_counts(
                    job_id,
                    done,
                    errors,
                )
                continue

            set_activity(
                stage="Downloading",
                detail="Starting lecture",
                percent=0,
                lecture=lecture["title"],
                subject=subject["name"],
                job_id=job_id,
            )

            async def dl_progress(kind, percent):
                set_activity(
                    stage="Downloading",
                    detail=kind,
                    percent=percent,
                )

                await live()

            async def up_progress(filename, percent):
                set_activity(
                    stage="Uploading",
                    detail=filename,
                    percent=percent,
                )

                await live()

            await live(
                force=True
            )

            try:
                # The safe downloader streams to disk.
                # No second heavy download starts
                # until this lecture is finished.
                folder, manifest = await download_lecture(
                    DOWNLOAD_PROFILE,
                    FILES,
                    subject,
                    lecture,
                    key,
                    dl_progress,
                )

                set_activity(
                    stage="Uploading",
                    detail="Telegram",
                    percent=0,
                )

                await live(
                    force=True
                )

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

                activity["done"] = done

                set_activity(
                    stage="Lecture completed",
                    detail="Saved in MongoDB",
                    percent=100,
                )

                await live(
                    force=True
                )

            except asyncio.CancelledError:
                raise

            except Exception as error:
                errors += 1

                activity["errors"] = errors

                logging.exception(
                    "Lecture failure"
                )

                await notify(
                    "🚨 Lecture failed\n"
                    f"Subject: {subject['name']}\n"
                    f"Class: {lecture['class_number']}\n"
                    f"Error: {clean_error(error)}"
                )

            update_job_counts(
                job_id,
                done,
                errors,
            )

    return done, errors


# ==========================================
# BACKGROUND WORKER
# ==========================================

async def worker():
    global cancel_requested
    global progress_message

    while True:
        try:
            if paused:
                await asyncio.sleep(2)
                continue

            job = sql.execute(
                """
                SELECT id,payload
                FROM jobs
                WHERE status='queued'
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()

            if not job:
                await asyncio.sleep(2)
                continue

            job_id, raw = job

            cancel_requested = False
            progress_message = None

            activity.update({
                "job_id": job_id,
                "stage": "Starting job",
                "detail": "",
                "subject": "",
                "lecture": "",
                "percent": 0,
                "done": 0,
                "errors": 0,
                "started": time.monotonic(),
                "last_progress": time.monotonic(),
            })

            sql.execute(
                """
                UPDATE jobs
                SET status='running'
                WHERE id=?
                """,
                (job_id,),
            )

            sql.commit()

            await live(
                force=True
            )

            try:
                done, errors = await process_job(
                    job_id,
                    json.loads(raw),
                )

                if cancel_requested:
                    result = "cancelled"

                elif errors:
                    result = "completed_with_errors"

                else:
                    result = "completed"

                sql.execute(
                    """
                    UPDATE jobs
                    SET status=?
                    WHERE id=?
                    """,
                    (result, job_id),
                )

                sql.commit()

                await notify(
                    f"🏁 Job #{job_id}\n"
                    f"Done: {done}\n"
                    f"Errors: {errors}\n"
                    f"Status: {result}"
                )

            except asyncio.CancelledError:
                raise

            except Exception as error:
                logging.exception(
                    "Job failed"
                )

                sql.execute(
                    """
                    UPDATE jobs
                    SET status='failed'
                    WHERE id=?
                    """,
                    (job_id,),
                )

                sql.commit()

                await notify(
                    f"🚨 Job #{job_id} failed\n"
                    + clean_error(error)
                )

            finally:
                activity["stage"] = "Idle"
                activity["detail"] = ""
                activity["lecture"] = ""
                activity["percent"] = 0
                activity["job_id"] = None

                progress_message = None

        except asyncio.CancelledError:
            raise

        except Exception:
            logging.exception(
                "Background worker crashed"
            )

            await asyncio.sleep(5)


# ==========================================
# MAIN
# ==========================================

async def main():
    await tg.start(
        bot_token=os.environ["BOT_TOKEN"]
    )

    await notify(
        "✅ Lecture Manager started\n"
        "Live progress enabled.\n"
        "Browsing available during downloads.\n"
        "Active download limit: 1\n\n"
        "Send /start"
    )

    worker_task = asyncio.create_task(
        worker()
    )

    heartbeat_task = asyncio.create_task(
        heartbeat()
    )

    try:
        await tg.run_until_disconnected()

    finally:
        worker_task.cancel()
        heartbeat_task.cancel()

        await asyncio.gather(
            worker_task,
            heartbeat_task,
            return_exceptions=True,
        )

        sql.close()
        storage.close()


if __name__ == "__main__":
    asyncio.run(main())
