
import asyncio
import re
from datetime import datetime
from pathlib import Path

from catalog import classes

ROOT = Path("/root/unac")
OUTPUT = ROOT / "exports"
OUTPUT.mkdir(parents=True, exist_ok=True)


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_name(value):
    return re.sub(
        r"[^a-zA-Z0-9_-]+", "_", clean(value)
    )[:55].strip("_") or "batch"


async def generate_txt(event, state, profile):
    goal = state["goal"]
    pack = state["pack"]
    subjects = list(state["chosen"])

    progress = await event.respond(
        f"📄 Generating TXT\n"
        f"Batch: {clean(pack.get('name'))}\n"
        f"Subjects: {len(subjects)}\n"
        "Starting discovery..."
    )

    lines = [
        "LECTURE MANAGER — BATCH TXT EXPORT",
        f"GOAL: {clean(goal.get('name'))}",
        f"BATCH: {clean(pack.get('name'))}",
        f"SUBJECTS SELECTED: {len(subjects)}",
        "",
    ]

    total_links = 0
    failures = []

    for index, subject in enumerate(subjects, 1):
        name = clean(subject.get("name")) or f"Subject {index}"

        await progress.edit(
            "📄 Generating TXT\n"
            f"Subject {index}/{len(subjects)}\n"
            f"{name[:100]}\n"
            f"Watch links found: {total_links}"
        )

        lines.extend([
            "=" * 55,
            f"SUBJECT: {name}",
            "",
        ])

        try:
            lectures, total = await classes(profile, subject)

            lines.append(f"TOTAL CLASSES: {total}")
            lines.append(f"WATCH LINKS: {len(lectures)}")
            lines.append("")

            for position, lecture in enumerate(lectures, 1):
                number = lecture.get("class_number", position)

                title = clean(
                    lecture.get("title")
                    or lecture.get("name")
                    or f"Lecture {number}"
                )

                url = (
                    lecture.get("watch_url")
                    or lecture.get("url")
                    or ""
                )

                lines.extend([
                    f"CLASS: {number}",
                    f"LECTURE: {title}",
                    f"WATCH_URL: {url or 'NOT_FOUND'}",
                    "",
                ])

                if url.startswith(("https://", "http://")):
                    total_links += 1

        except asyncio.CancelledError:
            raise

        except Exception as error:
            failures.append(name)
            lines.extend([
                f"ERROR: {type(error).__name__}: "
                f"{clean(error)[:170]}",
                "",
            ])

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    destination = OUTPUT / (
        f"{safe_name(pack.get('name'))}_{stamp}.txt"
    )

    destination.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    if total_links == 0:
        await progress.edit(
            "❌ TXT generation failed: "
            "koi playable watch link nahi mila.\n"
            "Discovery errors check karo."
        )
        return

    status = (
        "⚠️ Partial export"
        if failures
        else "✅ Export completed"
    )

    await progress.edit(
        f"{status}\n"
        f"Subjects: {len(subjects)}\n"
        f"Watch links: {total_links}\n"
        f"Failed subjects: {len(failures)}\n"
        "Sending TXT..."
    )

    await event.client.send_file(
        event.chat_id,
        str(destination),
        caption=(
            f"📄 {clean(pack.get('name'))[:120]}\n"
            f"Watch links: {total_links}\n"
            f"Failed subjects: {len(failures)}"
        ),
    )
